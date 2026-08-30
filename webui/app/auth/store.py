"""Database-backed identity providers, merged with the ones from the environment.

The authentication backends themselves are unchanged: they still consume the
frozen dataclasses in app.config. All this module does is widen where those
dataclasses come from, so a provider added through the web UI and a provider
declared in the environment are indistinguishable by the time anyone signs in.

Precedence is deliberate and one-way: **the environment always wins**. A
deployment that manages configuration as code keeps doing so, and a row in the
database can never quietly override it. Colliding rows are reported to the
administrator instead of being applied.
"""

from __future__ import annotations

import logging

from flask import current_app, g
from sqlalchemy import select

from ..config import (
    AUTH_LDAP,
    AUTH_OAUTH,
    AUTH_SAML,
    AuthConfig,
    GroupRoleMap,
    LdapConfig,
    OAuthProvider,
    SamlConfig,
)
from ..crypto import SecretDecryptionError, decrypt
from ..database import get_session
from ..models import AuthProviderConfig

log = logging.getLogger(__name__)

_REQUEST_CACHE_KEY = "effective_auth_config"

KINDS = (AUTH_LDAP, AUTH_OAUTH, AUTH_SAML)

KIND_LABELS = {
    AUTH_LDAP: "LDAP / Active Directory",
    AUTH_OAUTH: "OAuth 2.0 / OpenID Connect",
    AUTH_SAML: "SAML 2.0",
}


class ProviderConfigError(ValueError):
    """A stored provider is not usable as configured."""


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _roles(row: AuthProviderConfig) -> GroupRoleMap:
    default = (row.setting("default_role") or "").lower()
    return GroupRoleMap(
        admin_groups=_split(row.setting("admin_group")),
        operator_groups=_split(row.setting("operator_group")),
        user_groups=_split(row.setting("user_group")),
        default_role=default if default and default != "none" else None,
    )


def _secret(row: AuthProviderConfig, key: str) -> str:
    """Decrypt one stored secret.

    A decryption failure is raised rather than swallowed: continuing with an
    empty password would turn a key-rotation mistake into an anonymous LDAP
    bind, which can silently succeed and admit the wrong people.
    """
    return decrypt(row.secrets.get(key, ""), current_app.config["SECRET_KEY"])


def to_ldap_config(row: AuthProviderConfig) -> LdapConfig:
    uri = row.setting("uri")
    base_dn = row.setting("base_dn")
    if not uri or not base_dn:
        raise ProviderConfigError("An LDAP provider needs a server URI and a base DN.")
    return LdapConfig(
        enabled=True,
        uri=uri,
        start_tls=bool(row.settings.get("start_tls")),
        tls_verify=bool(row.settings.get("tls_verify", True)),
        ca_cert_file=row.setting("ca_cert_file"),
        bind_dn=row.setting("bind_dn"),
        bind_password=_secret(row, "bind_password"),
        base_dn=base_dn,
        user_filter=row.setting(
            "user_filter", "(&(objectClass=person)({username_attribute}={username}))"
        ),
        username_attribute=row.setting("username_attribute", "uid"),
        email_attribute=row.setting("email_attribute", "mail"),
        display_name_attribute=row.setting("display_name_attribute", "cn"),
        group_attribute=row.setting("group_attribute", "memberOf"),
        group_search_base=row.setting("group_search_base"),
        group_filter=row.setting("group_filter"),
        connect_timeout=int(row.settings.get("connect_timeout") or 5),
        roles=_roles(row),
    )


def to_oauth_provider(row: AuthProviderConfig) -> OAuthProvider:
    discovery_url = row.setting("discovery_url")
    authorize_url = row.setting("authorize_url")
    client_id = row.setting("client_id")
    if not client_id:
        raise ProviderConfigError("An OAuth provider needs a client ID.")
    if not discovery_url and not authorize_url:
        raise ProviderConfigError(
            "Give either a discovery URL (OpenID Connect) or the authorize, token "
            "and userinfo URLs (plain OAuth 2.0)."
        )
    if authorize_url and not discovery_url:
        for field, label in (("token_url", "token URL"), ("userinfo_url", "userinfo URL")):
            if not row.setting(field):
                raise ProviderConfigError(f"A plain OAuth 2.0 provider also needs a {label}.")
    default_scopes = "openid email profile" if discovery_url else "read:user user:email"
    return OAuthProvider(
        name=row.name,
        display_name=row.display_name or row.name.title(),
        client_id=client_id,
        client_secret=_secret(row, "client_secret"),
        discovery_url=discovery_url,
        authorize_url=authorize_url,
        token_url=row.setting("token_url"),
        userinfo_url=row.setting("userinfo_url"),
        jwks_url=row.setting("jwks_url"),
        scopes=row.setting("scopes", default_scopes),
        username_claim=row.setting(
            "username_claim", "preferred_username" if discovery_url else "login"
        ),
        email_claim=row.setting("email_claim", "email"),
        name_claim=row.setting("name_claim", "name"),
        groups_claim=row.setting("groups_claim", "groups"),
        icon=row.setting("icon", "ti-key"),
        roles=_roles(row),
    )


def to_saml_config(row: AuthProviderConfig) -> SamlConfig:
    metadata_url = row.setting("idp_metadata_url")
    sso_url = row.setting("idp_sso_url")
    if not metadata_url and not sso_url:
        raise ProviderConfigError("A SAML provider needs either an IdP metadata URL or an SSO URL.")
    if sso_url and not metadata_url and not _secret(row, "idp_x509_cert"):
        raise ProviderConfigError(
            "Without a metadata URL, the IdP certificate is required to validate "
            "the assertion signature."
        )
    return SamlConfig(
        enabled=True,
        sp_entity_id=row.setting("sp_entity_id"),
        sp_x509_cert=_secret(row, "sp_x509_cert"),
        sp_private_key=_secret(row, "sp_private_key"),
        idp_entity_id=row.setting("idp_entity_id"),
        idp_sso_url=sso_url,
        idp_slo_url=row.setting("idp_slo_url"),
        idp_x509_cert=_secret(row, "idp_x509_cert"),
        idp_metadata_url=metadata_url,
        name_id_format=row.setting(
            "name_id_format", "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
        ),
        attr_username=row.setting("attr_username", "username"),
        attr_email=row.setting("attr_email", "email"),
        attr_name=row.setting("attr_name", "displayName"),
        attr_groups=row.setting("attr_groups", "groups"),
        want_assertions_signed=bool(row.settings.get("want_assertions_signed", True)),
        want_messages_signed=bool(row.settings.get("want_messages_signed")),
        want_name_id_encrypted=bool(row.settings.get("want_name_id_encrypted")),
        strict=bool(row.settings.get("strict", True)),
        roles=_roles(row),
    )


def all_providers() -> list[AuthProviderConfig]:
    """Every stored provider, enabled or not, for the administration screens."""
    session = get_session()
    return list(
        session.scalars(
            select(AuthProviderConfig).order_by(AuthProviderConfig.kind, AuthProviderConfig.name)
        )
    )


def get_provider(provider_id: int) -> AuthProviderConfig | None:
    return get_session().get(AuthProviderConfig, provider_id)


def provider_problem(row: AuthProviderConfig) -> str:
    """Why this provider is not in effect, or an empty string when it is fine.

    Used by the admin UI so a misconfigured or shadowed provider is visible
    there rather than only at someone's failed sign-in.
    """
    env = current_app.config["AUTH"]
    if row.kind == AUTH_LDAP and env.ldap.enabled:
        return "Shadowed: LDAP is configured in the environment, which takes precedence."
    if row.kind == AUTH_SAML and env.saml.enabled:
        return "Shadowed: SAML is configured in the environment, which takes precedence."
    if row.kind == AUTH_OAUTH and env.provider(row.name) is not None:
        return (
            f"Shadowed: an OAuth provider named {row.name!r} is configured in the "
            "environment, which takes precedence."
        )
    try:
        build(row)
    except SecretDecryptionError as exc:
        return str(exc)
    except ProviderConfigError as exc:
        return str(exc)
    return ""


def build(row: AuthProviderConfig):
    """Convert one row into its frozen config dataclass."""
    if row.kind == AUTH_LDAP:
        return to_ldap_config(row)
    if row.kind == AUTH_OAUTH:
        return to_oauth_provider(row)
    if row.kind == AUTH_SAML:
        return to_saml_config(row)
    raise ProviderConfigError(f"Unknown provider kind {row.kind!r}.")


def effective_auth_config() -> AuthConfig:
    """The environment's configuration, widened by the enabled database providers.

    Cached for the duration of the request: a sign-in touches this several
    times and the providers cannot change underneath a single request.
    """
    cached = g.get(_REQUEST_CACHE_KEY)
    if cached is not None:
        return cached

    env = current_app.config["AUTH"]
    ldap = env.ldap
    saml = env.saml
    oauth = list(env.oauth_providers)
    taken = {provider.name for provider in oauth}

    try:
        rows = [row for row in all_providers() if row.enabled]
    except Exception as exc:  # pragma: no cover - database unavailable
        # Sign-in should still work from environment configuration alone.
        log.warning("could not load database auth providers: %s", exc)
        rows = []

    for row in rows:
        try:
            if row.kind == AUTH_LDAP:
                if not ldap.enabled:
                    ldap = to_ldap_config(row)
            elif row.kind == AUTH_SAML:
                if not saml.enabled:
                    saml = to_saml_config(row)
            elif row.kind == AUTH_OAUTH and row.name not in taken:
                oauth.append(to_oauth_provider(row))
                taken.add(row.name)
        except (ProviderConfigError, SecretDecryptionError) as exc:
            # One broken provider must not take the login page down with it.
            log.warning("ignoring auth provider %s: %s", row.name, exc)

    config = AuthConfig(
        local_enabled=env.local_enabled,
        ldap=ldap,
        oauth_providers=tuple(oauth),
        saml=saml,
    )
    setattr(g, _REQUEST_CACHE_KEY, config)
    return config
