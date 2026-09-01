"""Configuration, read from the environment.

Every setting that holds a secret also accepts a ``<NAME>_FILE`` variant
pointing at a file, so Docker/Swarm secrets and Kubernetes secret mounts work
without putting values in the environment. The file form wins when both
are set.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER)

#: Human-readable descriptions, used on the user administration screens.
ROLE_DESCRIPTIONS = {
    ROLE_ADMIN: "Full access, including user administration and settings",
    ROLE_OPERATOR: "Create, edit and delete every zone; no user administration",
    ROLE_USER: "Read and edit only the zones explicitly granted to them",
}

AUTH_LOCAL = "local"
AUTH_LDAP = "ldap"
AUTH_OAUTH = "oauth"
AUTH_SAML = "saml"


class ConfigError(RuntimeError):
    """Raised when the environment is missing or contradicts something required."""


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def env_secret(name: str, default: str = "") -> str:
    """Read ``name``, preferring the contents of ``name_FILE`` when present."""
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                # Strip the trailing newline `echo secret > file` leaves behind.
                return handle.read().rstrip("\r\n")
        except OSError as exc:
            raise ConfigError(f"{name}_FILE={path} could not be read: {exc}") from exc
    return env_str(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use true/false)")


def env_int(name: str, default: int) -> int:
    raw = env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def env_name(name: str, default: str) -> str:
    """An identifier-like setting where blank means "use the default".

    Attribute names, claim names and search filters have no meaningful empty
    value: an empty group attribute is not "look at no attribute", it is a
    misconfiguration that silently strips every user of their groups. Unset and
    set-to-blank therefore mean the same thing here.
    """
    return env_str(name, default) or default


def env_list(name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = env_str(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _role_or_none(name: str, default: str) -> str | None:
    """A role name, or ``None`` when the value is ``none``/empty.

    ``none`` means "do not create an account for this person", which is how you
    restrict access to users in a specific directory group.
    """
    raw = env_str(name, default).lower()
    if raw in ("", "none", "deny"):
        return None
    if raw not in ROLES:
        raise ConfigError(f"{name}={raw!r} must be one of {', '.join(ROLES)}, or none")
    return raw


@dataclass(frozen=True)
class GroupRoleMap:
    """Maps directory/IdP group names onto panel roles."""

    admin_groups: tuple[str, ...] = ()
    operator_groups: tuple[str, ...] = ()
    user_groups: tuple[str, ...] = ()
    default_role: str | None = ROLE_USER

    @staticmethod
    def _keys(group: str) -> set[str]:
        """Names a group may be configured under.

        LDAP hands back ``memberOf`` as full DNs, so
        ``CN=DNS-Admins,OU=Groups,DC=example,DC=com`` has to match a config
        value of either the whole DN or just ``DNS-Admins``. Comparison is
        case-insensitive: directories, Active Directory above all, are
        inconsistent about the case of group names.
        """
        # IdPs do send nulls inside a groups array; treat anything that is not
        # a non-empty string as absent rather than crashing the sign-in.
        if not isinstance(group, str):
            return set()
        group = group.strip()
        if not group:
            return set()
        keys = {group.lower()}
        first_rdn = group.split(",", 1)[0]
        if "=" in first_rdn:
            keys.add(first_rdn.split("=", 1)[1].strip().lower())
        return keys

    def resolve(self, groups: list[str]) -> str | None:
        """Highest role whose group list intersects ``groups``."""
        present: set[str] = set()
        for group in groups or ():
            present |= self._keys(group)

        def matches(configured: tuple[str, ...]) -> bool:
            return any(name.strip().lower() in present for name in configured if name.strip())

        if matches(self.admin_groups):
            return ROLE_ADMIN
        if matches(self.operator_groups):
            return ROLE_OPERATOR
        if matches(self.user_groups):
            return ROLE_USER
        # No group matched. If any group mapping was configured at all, this
        # person is not entitled to access; otherwise fall back to the default.
        if self.admin_groups or self.operator_groups or self.user_groups:
            return self.default_role if self.default_role else None
        return self.default_role

    @classmethod
    def from_env(cls, prefix: str, default_role: str = ROLE_USER) -> GroupRoleMap:
        return cls(
            admin_groups=tuple(env_list(f"{prefix}_ADMIN_GROUP")),
            operator_groups=tuple(env_list(f"{prefix}_OPERATOR_GROUP")),
            user_groups=tuple(env_list(f"{prefix}_USER_GROUP")),
            default_role=_role_or_none(f"{prefix}_DEFAULT_ROLE", default_role),
        )


@dataclass(frozen=True)
class LdapConfig:
    enabled: bool = False
    uri: str = ""
    start_tls: bool = False
    tls_verify: bool = True
    ca_cert_file: str = ""
    bind_dn: str = ""
    bind_password: str = ""
    base_dn: str = ""
    user_filter: str = "(&(objectClass=person)({username_attribute}={username}))"
    username_attribute: str = "uid"
    email_attribute: str = "mail"
    display_name_attribute: str = "cn"
    group_attribute: str = "memberOf"
    group_search_base: str = ""
    group_filter: str = ""
    connect_timeout: int = 5
    roles: GroupRoleMap = field(default_factory=GroupRoleMap)

    @classmethod
    def from_env(cls) -> LdapConfig:
        enabled = env_bool("LDAP_ENABLED", False)
        if not enabled:
            return cls()
        uri = env_str("LDAP_URI")
        base_dn = env_str("LDAP_BASE_DN")
        if not uri:
            raise ConfigError("LDAP_ENABLED is set but LDAP_URI is empty")
        if not base_dn:
            raise ConfigError("LDAP_ENABLED is set but LDAP_BASE_DN is empty")
        return cls(
            enabled=True,
            uri=uri,
            start_tls=env_bool("LDAP_START_TLS", False),
            tls_verify=env_bool("LDAP_TLS_VERIFY", True),
            ca_cert_file=env_str("LDAP_CA_CERT_FILE"),
            bind_dn=env_str("LDAP_BIND_DN"),
            bind_password=env_secret("LDAP_BIND_PASSWORD"),
            base_dn=base_dn,
            user_filter=env_name(
                "LDAP_USER_FILTER",
                "(&(objectClass=person)({username_attribute}={username}))",
            ),
            username_attribute=env_name("LDAP_USERNAME_ATTRIBUTE", "uid"),
            email_attribute=env_name("LDAP_EMAIL_ATTRIBUTE", "mail"),
            display_name_attribute=env_name("LDAP_DISPLAY_NAME_ATTRIBUTE", "cn"),
            group_attribute=env_name("LDAP_GROUP_ATTRIBUTE", "memberOf"),
            group_search_base=env_str("LDAP_GROUP_SEARCH_BASE"),
            group_filter=env_str("LDAP_GROUP_FILTER"),
            connect_timeout=env_int("LDAP_CONNECT_TIMEOUT", 5),
            roles=GroupRoleMap.from_env("LDAP"),
        )


@dataclass(frozen=True)
class OAuthProvider:
    """One OAuth 2.0 / OpenID Connect provider.

    Set ``discovery_url`` for a standards-compliant OIDC provider (Keycloak,
    Authentik, Entra ID, Google, Okta, ...). Providers that only speak plain
    OAuth 2.0 -- GitHub is the common one -- get explicit endpoint URLs
    instead.
    """

    name: str
    display_name: str
    client_id: str
    client_secret: str
    discovery_url: str = ""
    authorize_url: str = ""
    token_url: str = ""
    userinfo_url: str = ""
    jwks_url: str = ""
    scopes: str = "openid email profile"
    username_claim: str = "preferred_username"
    email_claim: str = "email"
    name_claim: str = "name"
    groups_claim: str = "groups"
    icon: str = "ti-key"
    roles: GroupRoleMap = field(default_factory=GroupRoleMap)

    @property
    def is_oidc(self) -> bool:
        return bool(self.discovery_url)

    @classmethod
    def from_env(cls, name: str) -> OAuthProvider:
        prefix = f"OAUTH_{name.upper().replace('-', '_')}"
        client_id = env_str(f"{prefix}_CLIENT_ID")
        client_secret = env_secret(f"{prefix}_CLIENT_SECRET")
        discovery_url = env_str(f"{prefix}_DISCOVERY_URL")
        authorize_url = env_str(f"{prefix}_AUTHORIZE_URL")

        if not client_id or not client_secret:
            raise ConfigError(
                f"OAuth provider {name!r} needs {prefix}_CLIENT_ID and {prefix}_CLIENT_SECRET"
            )
        if not discovery_url and not authorize_url:
            raise ConfigError(
                f"OAuth provider {name!r} needs either {prefix}_DISCOVERY_URL (OpenID "
                f"Connect) or {prefix}_AUTHORIZE_URL/_TOKEN_URL/_USERINFO_URL (plain OAuth 2.0)"
            )
        if authorize_url and not discovery_url:
            for required in ("TOKEN_URL", "USERINFO_URL"):
                if not env_str(f"{prefix}_{required}"):
                    raise ConfigError(
                        f"OAuth provider {name!r} sets {prefix}_AUTHORIZE_URL so "
                        f"{prefix}_{required} is required too"
                    )

        default_scopes = "openid email profile" if discovery_url else "read:user user:email"
        return cls(
            name=name.lower(),
            display_name=env_str(f"{prefix}_DISPLAY_NAME", name.title()),
            client_id=client_id,
            client_secret=client_secret,
            discovery_url=discovery_url,
            authorize_url=authorize_url,
            token_url=env_str(f"{prefix}_TOKEN_URL"),
            userinfo_url=env_str(f"{prefix}_USERINFO_URL"),
            jwks_url=env_str(f"{prefix}_JWKS_URL"),
            scopes=env_str(f"{prefix}_SCOPES", default_scopes),
            username_claim=env_str(
                f"{prefix}_USERNAME_CLAIM", "preferred_username" if discovery_url else "login"
            ),
            email_claim=env_str(f"{prefix}_EMAIL_CLAIM", "email"),
            name_claim=env_str(f"{prefix}_NAME_CLAIM", "name"),
            groups_claim=env_str(f"{prefix}_GROUPS_CLAIM", "groups"),
            icon=env_str(f"{prefix}_ICON", "ti-key"),
            roles=GroupRoleMap.from_env(prefix),
        )


@dataclass(frozen=True)
class SamlConfig:
    enabled: bool = False
    sp_entity_id: str = ""
    sp_x509_cert: str = ""
    sp_private_key: str = ""
    idp_entity_id: str = ""
    idp_sso_url: str = ""
    idp_slo_url: str = ""
    idp_x509_cert: str = ""
    idp_metadata_url: str = ""
    name_id_format: str = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
    attr_username: str = "username"
    attr_email: str = "email"
    attr_name: str = "displayName"
    attr_groups: str = "groups"
    want_assertions_signed: bool = True
    want_messages_signed: bool = False
    want_name_id_encrypted: bool = False
    strict: bool = True
    roles: GroupRoleMap = field(default_factory=GroupRoleMap)

    @classmethod
    def from_env(cls) -> SamlConfig:
        if not env_bool("SAML_ENABLED", False):
            return cls()
        metadata_url = env_str("SAML_IDP_METADATA_URL")
        sso_url = env_str("SAML_IDP_SSO_URL")
        if not metadata_url and not sso_url:
            raise ConfigError(
                "SAML_ENABLED is set but neither SAML_IDP_METADATA_URL nor "
                "SAML_IDP_SSO_URL is configured"
            )
        if sso_url and not env_str("SAML_IDP_X509_CERT") and not metadata_url:
            raise ConfigError(
                "SAML_IDP_SSO_URL is set so SAML_IDP_X509_CERT is required to "
                "validate the IdP's signature"
            )
        return cls(
            enabled=True,
            sp_entity_id=env_str("SAML_SP_ENTITY_ID"),
            sp_x509_cert=env_secret("SAML_SP_X509_CERT"),
            sp_private_key=env_secret("SAML_SP_PRIVATE_KEY"),
            idp_entity_id=env_str("SAML_IDP_ENTITY_ID"),
            idp_sso_url=sso_url,
            idp_slo_url=env_str("SAML_IDP_SLO_URL"),
            idp_x509_cert=env_secret("SAML_IDP_X509_CERT"),
            idp_metadata_url=metadata_url,
            name_id_format=env_str(
                "SAML_NAME_ID_FORMAT",
                "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            ),
            attr_username=env_str("SAML_ATTR_USERNAME", "username"),
            attr_email=env_str("SAML_ATTR_EMAIL", "email"),
            attr_name=env_str("SAML_ATTR_NAME", "displayName"),
            attr_groups=env_str("SAML_ATTR_GROUPS", "groups"),
            want_assertions_signed=env_bool("SAML_WANT_ASSERTIONS_SIGNED", True),
            want_messages_signed=env_bool("SAML_WANT_MESSAGES_SIGNED", False),
            want_name_id_encrypted=env_bool("SAML_WANT_NAME_ID_ENCRYPTED", False),
            strict=env_bool("SAML_STRICT", True),
            roles=GroupRoleMap.from_env("SAML"),
        )


@dataclass(frozen=True)
class AuthConfig:
    local_enabled: bool = True
    ldap: LdapConfig = field(default_factory=LdapConfig)
    oauth_providers: tuple[OAuthProvider, ...] = ()
    saml: SamlConfig = field(default_factory=SamlConfig)

    def provider(self, name: str) -> OAuthProvider | None:
        for candidate in self.oauth_providers:
            if candidate.name == name.lower():
                return candidate
        return None

    @property
    def any_enabled(self) -> bool:
        return bool(
            self.local_enabled or self.ldap.enabled or self.oauth_providers or self.saml.enabled
        )

    @classmethod
    def from_env(cls) -> AuthConfig:
        providers = tuple(OAuthProvider.from_env(name) for name in env_list("OAUTH_PROVIDERS"))
        seen: set[str] = set()
        for candidate in providers:
            if candidate.name in seen:
                raise ConfigError(f"OAUTH_PROVIDERS lists {candidate.name!r} more than once")
            seen.add(candidate.name)
        return cls(
            local_enabled=env_bool("LOCAL_AUTH_ENABLED", True),
            ldap=LdapConfig.from_env(),
            oauth_providers=providers,
            saml=SamlConfig.from_env(),
        )


def database_url() -> str:
    """Build the SQLAlchemy URL, from ``DATABASE_URL`` or the discrete parts."""
    explicit = env_str("DATABASE_URL")
    if explicit:
        return explicit

    from urllib.parse import quote_plus

    user = env_str("DB_USER", "pdnsadmin")
    password = env_secret("DB_PASSWORD")
    host = env_str("DB_HOST", "db")
    port = env_int("DB_PORT", 5432)
    name = env_str("DB_NAME", "pdns")
    if not password:
        raise ConfigError("DB_PASSWORD (or DB_PASSWORD_FILE, or DATABASE_URL) must be set")
    return (
        f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(name)}"
    )


def build_config() -> dict:
    """Assemble the Flask config mapping from the environment."""
    secret_key = env_secret("SECRET_KEY")
    if not secret_key:
        if env_bool("ALLOW_EPHEMERAL_SECRET_KEY", False):
            # Development only: every restart invalidates all sessions, and
            # multiple gunicorn workers would each get a different key.
            secret_key = secrets.token_urlsafe(48)
        else:
            raise ConfigError(
                "SECRET_KEY (or SECRET_KEY_FILE) must be set. Generate one with: "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
    if len(secret_key) < 32 and not env_bool("ALLOW_EPHEMERAL_SECRET_KEY", False):
        raise ConfigError("SECRET_KEY must be at least 32 characters")

    session_minutes = env_int("SESSION_LIFETIME_MINUTES", 480)

    return {
        "SECRET_KEY": secret_key,
        "SQLALCHEMY_DATABASE_URI": database_url(),
        "DB_SCHEMA": env_str("DB_SCHEMA", "pdnsadmin"),
        "BASE_URL": env_str("BASE_URL").rstrip("/"),
        "SITE_NAME": env_str("SITE_NAME", "PowerDNS Admin"),
        # PowerDNS HTTP API
        "PDNS_API_URL": env_str("PDNS_API_URL", "http://pdns:8081").rstrip("/"),
        "PDNS_API_KEY": env_secret("PDNS_API_KEY"),
        "PDNS_SERVER_ID": env_str("PDNS_SERVER_ID", "localhost"),
        "PDNS_API_TIMEOUT": env_int("PDNS_API_TIMEOUT", 10),
        # PowerDNS Recursor HTTP API -- forward zones and global forwarders.
        # Forwarding is off when either of the first two is empty, and the
        # Forwarding page then explains what to set rather than erroring.
        "RECURSOR_API_URL": env_str("RECURSOR_API_URL").rstrip("/"),
        "RECURSOR_API_KEY": env_secret("RECURSOR_API_KEY"),
        "RECURSOR_SERVER_ID": env_str("RECURSOR_SERVER_ID", "localhost"),
        "RECURSOR_API_TIMEOUT": env_int("RECURSOR_API_TIMEOUT", 10),
        # Where the recursor should send queries for the zones this stack is
        # authoritative for. Forward targets are addresses, never names, so
        # this is an IP even though the service is reachable as "pdns".
        "PDNS_DNS_ADDRESS": env_str("PDNS_DNS_ADDRESS"),
        "PDNS_DNS_PORT": env_int("PDNS_DNS_PORT", 53),
        # Zone defaults offered on the "new zone" form
        "DEFAULT_NAMESERVERS": env_list("DEFAULT_NAMESERVERS"),
        "DEFAULT_SOA_EDIT_API": env_str("DEFAULT_SOA_EDIT_API", "DEFAULT"),
        "DEFAULT_TTL": env_int("DEFAULT_TTL", 3600),
        # Session / cookie hardening
        "SESSION_COOKIE_NAME": env_str("SESSION_COOKIE_NAME", "pdnsadmin_session"),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": env_str("SESSION_COOKIE_SAMESITE", "Lax"),
        "SESSION_COOKIE_SECURE": env_bool("SESSION_COOKIE_SECURE", True),
        "PERMANENT_SESSION_LIFETIME": session_minutes * 60,
        "SESSION_LIFETIME_MINUTES": session_minutes,
        "TRUSTED_PROXY_COUNT": env_int("TRUSTED_PROXY_COUNT", 0),
        # Brute-force protection on the local login form
        "LOGIN_MAX_ATTEMPTS": env_int("LOGIN_MAX_ATTEMPTS", 10),
        "LOGIN_LOCKOUT_SECONDS": env_int("LOGIN_LOCKOUT_SECONDS", 300),
        # First-run bootstrap of the initial administrator
        "BOOTSTRAP_ADMIN_USERNAME": env_str("BOOTSTRAP_ADMIN_USERNAME", "admin"),
        "BOOTSTRAP_ADMIN_PASSWORD": env_secret("BOOTSTRAP_ADMIN_PASSWORD"),
        "BOOTSTRAP_ADMIN_EMAIL": env_str("BOOTSTRAP_ADMIN_EMAIL", ""),
        "LOG_LEVEL": env_str("LOG_LEVEL", "INFO").upper(),
        "AUTH": AuthConfig.from_env(),
    }
