"""Configuration parsing and the group-to-role mapping."""

from __future__ import annotations

import pytest

from app.config import (
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_USER,
    AuthConfig,
    ConfigError,
    GroupRoleMap,
    LdapConfig,
    OAuthProvider,
    SamlConfig,
    build_config,
    env_bool,
    env_secret,
)


class TestSecrets:
    def test_file_variant_wins_and_strips_newline(self, tmp_path, monkeypatch):
        secret = tmp_path / "password"
        secret.write_text("s3cret-from-file\n")
        monkeypatch.setenv("THING", "from-environment")
        monkeypatch.setenv("THING_FILE", str(secret))
        assert env_secret("THING") == "s3cret-from-file"

    def test_unreadable_file_is_an_error(self, monkeypatch):
        monkeypatch.setenv("THING_FILE", "/nonexistent/path")
        with pytest.raises(ConfigError, match="could not be read"):
            env_secret("THING")

    def test_falls_back_to_plain_variable(self, monkeypatch):
        monkeypatch.setenv("THING", "plain")
        assert env_secret("THING") == "plain"

    def test_bad_boolean_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FLAG", "maybe")
        with pytest.raises(ConfigError, match="not a boolean"):
            env_bool("FLAG")


class TestSecretKey:
    def test_missing_secret_key_is_fatal(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            build_config()

    def test_short_secret_key_is_rejected(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "tooshort")
        with pytest.raises(ConfigError, match="at least 32"):
            build_config()

    def test_ephemeral_key_allowed_when_opted_in(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("ALLOW_EPHEMERAL_SECRET_KEY", "true")
        assert len(build_config()["SECRET_KEY"]) >= 32


class TestGroupRoleMap:
    def test_matches_bare_group_name(self):
        mapping = GroupRoleMap(admin_groups=("DNS-Admins",))
        assert mapping.resolve(["DNS-Admins"]) == ROLE_ADMIN

    def test_matches_the_cn_of_a_full_dn(self):
        """LDAP memberOf hands back DNs, so a bare CN in config must still match."""
        mapping = GroupRoleMap(admin_groups=("DNS-Admins",))
        assert mapping.resolve(["CN=DNS-Admins,OU=Groups,DC=example,DC=com"]) == ROLE_ADMIN

    def test_matches_a_full_dn_in_config(self):
        mapping = GroupRoleMap(operator_groups=("cn=ops,ou=groups,dc=example,dc=com",))
        assert mapping.resolve(["CN=Ops,OU=Groups,DC=example,DC=com"]) == ROLE_OPERATOR

    def test_comparison_is_case_insensitive(self):
        mapping = GroupRoleMap(admin_groups=("dns-admins",))
        assert mapping.resolve(["DNS-ADMINS"]) == ROLE_ADMIN

    def test_highest_role_wins(self):
        mapping = GroupRoleMap(admin_groups=("a",), operator_groups=("o",), user_groups=("u",))
        assert mapping.resolve(["u", "o", "a"]) == ROLE_ADMIN
        assert mapping.resolve(["u", "o"]) == ROLE_OPERATOR

    def test_no_match_falls_back_to_default(self):
        mapping = GroupRoleMap(admin_groups=("a",), default_role=ROLE_USER)
        assert mapping.resolve(["something-else"]) == ROLE_USER

    def test_no_match_denies_when_default_is_none(self):
        mapping = GroupRoleMap(admin_groups=("a",), default_role=None)
        assert mapping.resolve(["something-else"]) is None

    def test_no_groups_configured_gives_everyone_the_default(self):
        mapping = GroupRoleMap(default_role=ROLE_USER)
        assert mapping.resolve([]) == ROLE_USER

    def test_empty_and_blank_groups_are_ignored(self):
        mapping = GroupRoleMap(admin_groups=("a",), default_role=None)
        assert mapping.resolve(["", "   ", None]) is None


class TestLdapConfig:
    def test_disabled_by_default(self):
        assert LdapConfig.from_env().enabled is False

    def test_enabled_requires_uri(self, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        with pytest.raises(ConfigError, match="LDAP_URI"):
            LdapConfig.from_env()

    def test_enabled_requires_base_dn(self, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("LDAP_URI", "ldaps://dc.example.com")
        with pytest.raises(ConfigError, match="LDAP_BASE_DN"):
            LdapConfig.from_env()

    def test_reads_role_mapping(self, monkeypatch):
        monkeypatch.setenv("LDAP_ENABLED", "true")
        monkeypatch.setenv("LDAP_URI", "ldaps://dc.example.com")
        monkeypatch.setenv("LDAP_BASE_DN", "DC=example,DC=com")
        monkeypatch.setenv("LDAP_ADMIN_GROUP", "DNS-Admins, Infra")
        monkeypatch.setenv("LDAP_DEFAULT_ROLE", "none")
        config = LdapConfig.from_env()
        assert config.roles.admin_groups == ("DNS-Admins", "Infra")
        assert config.roles.default_role is None


class TestOAuthProvider:
    def _minimal(self, monkeypatch, **extra):
        monkeypatch.setenv("OAUTH_KC_CLIENT_ID", "id")
        monkeypatch.setenv("OAUTH_KC_CLIENT_SECRET", "secret")
        for key, value in extra.items():
            monkeypatch.setenv(f"OAUTH_KC_{key}", value)

    def test_oidc_provider(self, monkeypatch):
        self._minimal(monkeypatch, DISCOVERY_URL="https://sso/.well-known/openid-configuration")
        provider = OAuthProvider.from_env("kc")
        assert provider.is_oidc
        assert provider.scopes == "openid email profile"
        assert provider.username_claim == "preferred_username"

    def test_plain_oauth_provider(self, monkeypatch):
        self._minimal(
            monkeypatch,
            AUTHORIZE_URL="https://gh/login/oauth/authorize",
            TOKEN_URL="https://gh/login/oauth/access_token",
            USERINFO_URL="https://api.gh/user",
        )
        provider = OAuthProvider.from_env("kc")
        assert not provider.is_oidc
        # A non-OIDC provider has no preferred_username claim.
        assert provider.username_claim == "login"

    def test_missing_credentials_is_an_error(self, monkeypatch):
        monkeypatch.setenv("OAUTH_KC_DISCOVERY_URL", "https://sso/.well-known/x")
        with pytest.raises(ConfigError, match="CLIENT_ID"):
            OAuthProvider.from_env("kc")

    def test_needs_discovery_or_explicit_endpoints(self, monkeypatch):
        self._minimal(monkeypatch)
        with pytest.raises(ConfigError, match="DISCOVERY_URL"):
            OAuthProvider.from_env("kc")

    def test_explicit_endpoints_must_be_complete(self, monkeypatch):
        self._minimal(monkeypatch, AUTHORIZE_URL="https://gh/authorize")
        with pytest.raises(ConfigError, match="TOKEN_URL"):
            OAuthProvider.from_env("kc")

    def test_duplicate_providers_rejected(self, monkeypatch):
        monkeypatch.setenv("OAUTH_PROVIDERS", "kc,kc")
        self._minimal(monkeypatch, DISCOVERY_URL="https://sso/.well-known/x")
        with pytest.raises(ConfigError, match="more than once"):
            AuthConfig.from_env()


class TestSamlConfig:
    def test_disabled_by_default(self):
        assert SamlConfig.from_env().enabled is False

    def test_needs_an_idp(self, monkeypatch):
        monkeypatch.setenv("SAML_ENABLED", "true")
        with pytest.raises(ConfigError, match="SAML_IDP_METADATA_URL"):
            SamlConfig.from_env()

    def test_manual_idp_needs_a_certificate(self, monkeypatch):
        """Without the IdP certificate, assertions cannot be validated."""
        monkeypatch.setenv("SAML_ENABLED", "true")
        monkeypatch.setenv("SAML_IDP_SSO_URL", "https://sso/saml")
        with pytest.raises(ConfigError, match="X509_CERT"):
            SamlConfig.from_env()

    def test_metadata_url_is_enough(self, monkeypatch):
        monkeypatch.setenv("SAML_ENABLED", "true")
        monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://sso/descriptor")
        assert SamlConfig.from_env().enabled is True

    def test_strict_and_signed_assertions_default_on(self, monkeypatch):
        monkeypatch.setenv("SAML_ENABLED", "true")
        monkeypatch.setenv("SAML_IDP_METADATA_URL", "https://sso/descriptor")
        config = SamlConfig.from_env()
        assert config.strict is True
        assert config.want_assertions_signed is True
