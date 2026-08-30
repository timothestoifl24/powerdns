"""LDAP / Active Directory authentication.

Uses ldap3, which is pure Python -- no libldap, no OpenLDAP headers at build
time, and identical behaviour on every platform.

The flow is the standard two-bind dance:

1. Bind as the service account (or anonymously) and search for the user, so we
   learn their real DN and attributes. Directories rarely let you predict a DN.
2. Re-bind as that DN with the supplied password. A successful bind is the
   password check -- we never read or compare a password hash ourselves.
"""

from __future__ import annotations

import logging
import ssl

from ldap3 import ALL, AUTO_BIND_NO_TLS, SIMPLE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException

from ..config import AUTH_LDAP, LdapConfig
from .provisioning import IdentityClaim

log = logging.getLogger(__name__)


class LdapAuthError(Exception):
    """LDAP was unreachable or misconfigured -- distinct from a wrong password."""


def escape_filter_value(value: str) -> str:
    """Escape a value for use inside an LDAP filter (RFC 4515).

    Without this, a username of ``*)(uid=admin`` rewrites the filter and can
    authenticate as a different account. ldap3 ships an escaper, but doing it
    explicitly keeps the guarantee visible at the call site.
    """
    replacements = {
        "\\": r"\5c",
        "*": r"\2a",
        "(": r"\28",
        ")": r"\29",
        "\0": r"\00",
        "/": r"\2f",
    }
    return "".join(replacements.get(char, char) for char in value)


def _build_server(config: LdapConfig) -> Server:
    tls = None
    if config.uri.lower().startswith("ldaps://") or config.start_tls:
        tls = Tls(
            validate=ssl.CERT_REQUIRED if config.tls_verify else ssl.CERT_NONE,
            ca_certs_file=config.ca_cert_file or None,
            version=ssl.PROTOCOL_TLS_CLIENT if config.tls_verify else ssl.PROTOCOL_TLS,
        )
        if not config.tls_verify:
            log.warning(
                "LDAP_TLS_VERIFY is off: the directory's certificate is not being "
                "checked, so the connection is open to interception"
            )
    return Server(
        config.uri,
        get_info=ALL,
        tls=tls,
        connect_timeout=config.connect_timeout,
    )


def _service_connection(config: LdapConfig, server: Server) -> Connection:
    """Bind as the service account, or anonymously when none is configured."""
    try:
        if config.bind_dn:
            connection = Connection(
                server,
                user=config.bind_dn,
                password=config.bind_password,
                authentication=SIMPLE,
                auto_bind=AUTO_BIND_NO_TLS,
                raise_exceptions=False,
                receive_timeout=config.connect_timeout,
            )
        else:
            connection = Connection(server, auto_bind=AUTO_BIND_NO_TLS, raise_exceptions=False)
        if config.start_tls:
            connection.start_tls()
        if not connection.bound and not connection.bind():
            raise LdapAuthError(f"The LDAP service account could not bind: {connection.result}")
        return connection
    except LDAPException as exc:
        raise LdapAuthError(f"Cannot reach the LDAP server at {config.uri}: {exc}") from exc


def _groups_from_entry(config: LdapConfig, connection: Connection, entry) -> list[str]:
    """Group names for a user, from memberOf or from a group search."""
    groups: list[str] = []

    raw = entry.get(config.group_attribute)
    if raw:
        groups.extend(str(item) for item in (raw if isinstance(raw, list) else [raw]))

    # posixGroup-style directories do not populate memberOf; they store members
    # on the group. Search for the groups this DN belongs to instead.
    if config.group_search_base and config.group_filter:
        user_dn = escape_filter_value(str(entry.get("dn", "")))
        search_filter = config.group_filter.replace("{dn}", user_dn).replace(
            "{username}", escape_filter_value(str(entry.get("username", "")))
        )
        try:
            connection.search(
                search_base=config.group_search_base,
                search_filter=search_filter,
                attributes=["cn"],
            )
            for group in connection.entries:
                names = group.entry_attributes_as_dict.get("cn") or []
                groups.extend(str(name) for name in names)
        except LDAPException as exc:  # pragma: no cover - depends on directory
            log.warning("LDAP group search failed: %s", exc)

    return groups


def authenticate(config: LdapConfig, username: str, password: str) -> IdentityClaim | None:
    """Verify ``username``/``password``.

    Returns the claim on success, ``None`` when the credentials are wrong, and
    raises :class:`LdapAuthError` when the directory itself is the problem --
    the caller shows those very differently.
    """
    if not config.enabled:
        return None
    if not password:
        # An empty password makes an LDAP simple bind succeed as an anonymous
        # bind on many servers, which would authenticate anyone.
        return None

    server = _build_server(config)
    connection = _service_connection(config, server)

    try:
        search_filter = config.user_filter.format(
            username=escape_filter_value(username),
            username_attribute=config.username_attribute,
        )
        attributes = [
            attribute
            for attribute in {
                config.username_attribute,
                config.email_attribute,
                config.display_name_attribute,
                config.group_attribute,
            }
            if attribute
        ]
        connection.search(
            search_base=config.base_dn,
            search_filter=search_filter,
            attributes=attributes,
        )
        if not connection.entries:
            log.info("LDAP: no entry for %r under %s", username, config.base_dn)
            return None
        if len(connection.entries) > 1:
            log.warning(
                "LDAP: %d entries matched %r; refusing to guess which one",
                len(connection.entries),
                username,
            )
            return None

        entry = connection.entries[0]
        attrs = entry.entry_attributes_as_dict
        user_dn = entry.entry_dn

        def first(name: str, default: str = "") -> str:
            values = attrs.get(name) or []
            return str(values[0]) if values else default

        # The actual password check.
        user_connection = Connection(
            server,
            user=user_dn,
            password=password,
            authentication=SIMPLE,
            raise_exceptions=False,
            receive_timeout=config.connect_timeout,
        )
        if config.start_tls:
            try:
                user_connection.open()
                user_connection.start_tls()
            except LDAPException as exc:
                raise LdapAuthError(f"LDAP StartTLS failed: {exc}") from exc
        if not user_connection.bind():
            log.info("LDAP: bind as %s failed (wrong password)", user_dn)
            return None
        user_connection.unbind()

        groups = _groups_from_entry(
            config,
            connection,
            {**attrs, "dn": user_dn, "username": username},
        )
        return IdentityClaim(
            username=first(config.username_attribute, username),
            auth_source=AUTH_LDAP,
            provider="ldap",
            external_id=user_dn,
            email=first(config.email_attribute),
            display_name=first(config.display_name_attribute),
            groups=groups,
        )
    except LDAPException as exc:
        raise LdapAuthError(f"LDAP error while authenticating {username!r}: {exc}") from exc
    finally:
        try:
            connection.unbind()
        except LDAPException:  # pragma: no cover - best effort cleanup
            pass


def test_connection(config: LdapConfig) -> str:
    """Bind to the directory and report what happened.

    Used by the administration screen so a new provider can be checked without
    asking someone to attempt a sign-in and interpret the failure. This proves
    the server is reachable, TLS negotiates and the service account can bind --
    the three things that go wrong most often.
    """
    server = _build_server(config)
    connection = _service_connection(config, server)
    if connection is None:
        raise LdapAuthError(
            "Could not bind to the directory. Check the URI, the bind DN and its password."
        )
    try:
        found = connection.search(
            search_base=config.base_dn,
            search_filter="(objectClass=*)",
            search_scope="BASE",
            attributes=[],
        )
        if not found:
            raise LdapAuthError(
                f"Bound successfully, but the base DN {config.base_dn!r} could not be read."
            )
        who = config.bind_dn or "anonymously"
        return f"bound as {who}, base DN {config.base_dn} is readable"
    finally:
        try:
            connection.unbind()
        except Exception:  # pragma: no cover - best effort cleanup
            pass
