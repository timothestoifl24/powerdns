"""Identity providers configured through the web UI."""

from __future__ import annotations

import pytest

from app.config import AUTH_LDAP, AUTH_OAUTH, AUTH_SAML
from app.crypto import decrypt, is_encrypted
from app.database import get_session
from app.models import AuthProviderConfig

OIDC_FORM = {
    "name": "keycloak",
    "display_name": "Keycloak",
    "enabled": "on",
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "pdns-admin",
    "client_secret": "s3cr3t-value",
    "default_role": "user",
}


def _provider(app, name: str) -> AuthProviderConfig:
    with app.app_context():
        return get_session().query(AuthProviderConfig).filter(AuthProviderConfig.name == name).one()


def _create(client, token, kind: str, form: dict):
    path = f"/admin/auth/new/{kind}"
    data = dict(form)
    data["csrf_token"] = token(path)
    return client.post(path, data=data, follow_redirects=False)


class TestAccessControl:
    @pytest.mark.parametrize("username", ["operator", "viewer"])
    def test_non_admins_cannot_reach_the_provider_screens(self, client, users, login, username):
        login(username)
        assert client.get("/admin/auth/").status_code == 403

    def test_anonymous_is_redirected_to_login(self, client):
        response = client.get("/admin/auth/", follow_redirects=False)
        assert response.status_code == 302
        assert "login" in response.headers["Location"]

    def test_admin_sees_the_page(self, client, users, login):
        login("admin")
        assert client.get("/admin/auth/").status_code == 200


class TestCreate:
    def test_an_oidc_provider_can_be_created(self, app, client, users, login, token):
        login("admin")
        response = _create(client, token, AUTH_OAUTH, OIDC_FORM)
        assert response.status_code == 302

        row = _provider(app, "keycloak")
        assert row.kind == AUTH_OAUTH
        assert row.enabled
        assert row.settings["client_id"] == "pdns-admin"

    def test_the_client_secret_is_encrypted_at_rest(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)

        row = _provider(app, "keycloak")
        stored = row.secrets["client_secret"]
        assert stored != "s3cr3t-value", "the secret is stored verbatim"
        assert is_encrypted(stored)
        assert "s3cr3t-value" not in row.secrets_json
        with app.app_context():
            assert decrypt(stored, app.config["SECRET_KEY"]) == "s3cr3t-value"

    def test_the_name_is_normalised_into_a_slug(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, {**OIDC_FORM, "name": "  My IdP!! "})
        assert _provider(app, "my-idp").display_name == "Keycloak"

    def test_an_ldap_provider_can_be_created(self, app, client, users, login, token):
        login("admin")
        response = _create(
            client,
            token,
            AUTH_LDAP,
            {
                "name": "corp",
                "enabled": "on",
                "uri": "ldaps://dc.example.com",
                "base_dn": "dc=example,dc=com",
                "bind_dn": "cn=svc,dc=example,dc=com",
                "bind_password": "bind-pw",
                "username_attribute": "sAMAccountName",
                "admin_group": "DNS-Admins",
                "default_role": "none",
            },
        )
        assert response.status_code == 302
        row = _provider(app, "corp")
        assert row.settings["username_attribute"] == "sAMAccountName"
        assert is_encrypted(row.secrets["bind_password"])

    def test_a_saml_provider_can_be_created(self, app, client, users, login, token):
        login("admin")
        response = _create(
            client,
            token,
            AUTH_SAML,
            {
                "name": "okta",
                "enabled": "on",
                "idp_metadata_url": "https://idp.example.com/metadata",
                "default_role": "user",
            },
        )
        assert response.status_code == 302
        assert _provider(app, "okta").kind == AUTH_SAML

    def test_an_unknown_kind_is_404(self, client, users, login):
        login("admin")
        assert client.get("/admin/auth/new/kerberos").status_code == 404


class TestValidation:
    """A provider that cannot work is refused at save time, not at someone's sign-in."""

    def test_oauth_without_endpoints_or_discovery_is_refused(self, client, users, login, token):
        login("admin")
        response = _create(
            client,
            token,
            AUTH_OAUTH,
            {"name": "broken", "client_id": "x", "client_secret": "y", "default_role": "user"},
        )
        assert response.status_code == 400
        assert b"discovery URL" in response.data

    def test_plain_oauth_missing_a_token_url_is_refused(self, client, users, login, token):
        login("admin")
        response = _create(
            client,
            token,
            AUTH_OAUTH,
            {
                "name": "github",
                "client_id": "x",
                "client_secret": "y",
                "authorize_url": "https://github.com/login/oauth/authorize",
                "userinfo_url": "https://api.github.com/user",
                "default_role": "user",
            },
        )
        assert response.status_code == 400
        assert b"token URL" in response.data

    def test_ldap_without_a_base_dn_is_refused(self, client, users, login, token):
        login("admin")
        response = _create(
            client, token, AUTH_LDAP, {"name": "bad", "uri": "ldap://x", "default_role": "user"}
        )
        assert response.status_code == 400
        assert b"base DN" in response.data

    def test_saml_without_metadata_or_certificate_is_refused(self, client, users, login, token):
        login("admin")
        response = _create(
            client,
            token,
            AUTH_SAML,
            {
                "name": "bad",
                "idp_sso_url": "https://idp.example.com/sso",
                "default_role": "user",
            },
        )
        assert response.status_code == 400
        assert b"certificate is required" in response.data

    def test_a_duplicate_name_is_refused(self, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        response = _create(client, token, AUTH_OAUTH, OIDC_FORM)
        assert response.status_code == 400
        assert b"already exists" in response.data


class TestEditing:
    def test_an_empty_secret_field_keeps_the_stored_secret(self, app, client, users, login, token):
        """Editing a URL must not silently wipe the password."""
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        provider_id = _provider(app, "keycloak").id
        before = _provider(app, "keycloak").secrets["client_secret"]

        path = f"/admin/auth/{provider_id}"
        response = client.post(
            path,
            data={
                **OIDC_FORM,
                "client_secret": "",
                "scopes": "openid email",
                "csrf_token": token(path),
            },
            follow_redirects=False,
        )
        assert response.status_code == 302

        after = _provider(app, "keycloak")
        assert after.secrets["client_secret"] == before
        assert after.settings["scopes"] == "openid email"

    def test_the_clear_checkbox_removes_a_secret(self, app, client, users, login, token):
        login("admin")
        _create(
            client,
            token,
            AUTH_LDAP,
            {
                "name": "corp",
                "uri": "ldaps://dc.example.com",
                "base_dn": "dc=example,dc=com",
                "bind_password": "bind-pw",
                "default_role": "user",
            },
        )
        provider_id = _provider(app, "corp").id
        path = f"/admin/auth/{provider_id}"
        client.post(
            path,
            data={
                "name": "corp",
                "uri": "ldaps://dc.example.com",
                "base_dn": "dc=example,dc=com",
                "bind_password": "",
                "clear_bind_password": "on",
                "default_role": "user",
                "csrf_token": token(path),
            },
        )
        assert "bind_password" not in _provider(app, "corp").secrets

    def test_toggle_disables_and_re_enables(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        provider_id = _provider(app, "keycloak").id
        path = f"/admin/auth/{provider_id}/toggle"
        client.post(path, data={"csrf_token": token("/admin/auth/")})
        assert _provider(app, "keycloak").enabled is False

    def test_delete_removes_the_provider(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        provider_id = _provider(app, "keycloak").id
        client.post(
            f"/admin/auth/{provider_id}/delete",
            data={"csrf_token": token("/admin/auth/")},
        )
        with app.app_context():
            assert get_session().query(AuthProviderConfig).count() == 0

    def test_edit_page_renders_and_never_echoes_the_secret(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        provider_id = _provider(app, "keycloak").id
        response = client.get(f"/admin/auth/{provider_id}")
        assert response.status_code == 200
        assert b"s3cr3t-value" not in response.data
        assert b"unchanged" in response.data


class TestEffectiveConfiguration:
    def test_a_database_provider_reaches_the_login_page(self, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        client.post("/auth/logout", data={"csrf_token": token("/admin/auth/")})

        page = client.get("/auth/login")
        assert b"Keycloak" in page.data
        assert b"/auth/oauth/keycloak" in page.data

    def test_a_disabled_provider_does_not(self, app, client, users, login, token):
        login("admin")
        _create(client, token, AUTH_OAUTH, OIDC_FORM)
        provider_id = _provider(app, "keycloak").id
        client.post(f"/admin/auth/{provider_id}/toggle", data={"csrf_token": token("/admin/auth/")})
        client.post("/auth/logout", data={"csrf_token": token("/admin/auth/")})
        assert b"/auth/oauth/keycloak" not in client.get("/auth/login").data

    def test_the_environment_wins_over_a_colliding_database_provider(self, make_app, token):
        """A deployment managing configuration as code cannot be overridden from the UI."""
        app = make_app(
            OAUTH_PROVIDERS="keycloak",
            OAUTH_KEYCLOAK_CLIENT_ID="from-env",
            OAUTH_KEYCLOAK_CLIENT_SECRET="env-secret",
            OAUTH_KEYCLOAK_DISCOVERY_URL="https://env.example.com/.well-known/openid-configuration",
        )
        client = app.test_client()

        from app.config import AUTH_LOCAL, ROLE_ADMIN
        from app.models import User
        from app.security import hash_password

        with app.app_context():
            session = get_session()
            session.add(
                User(
                    username="admin",
                    auth_source=AUTH_LOCAL,
                    password_hash=hash_password("admin-password-123"),
                    role=ROLE_ADMIN,
                    is_active=True,
                )
            )
            session.commit()

        page = client.get("/auth/login")
        import re

        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        client.post(
            "/auth/login",
            data={"username": "admin", "password": "admin-password-123", "csrf_token": csrf},
        )

        listing = client.get("/admin/auth/")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', listing.data).group(1).decode()
        client.post(
            "/admin/auth/new/oauth",
            data={**OIDC_FORM, "client_id": "from-db", "csrf_token": csrf},
        )

        with app.app_context():
            from app.auth.store import effective_auth_config

            with app.test_request_context():
                config = effective_auth_config()
                provider = config.provider("keycloak")
                assert provider.client_id == "from-env", "the database overrode the environment"
                assert len([p for p in config.oauth_providers if p.name == "keycloak"]) == 1

        assert b"Shadowed" in client.get("/admin/auth/").data

    def test_a_broken_provider_does_not_break_the_login_page(self, app, client, users, login):
        """One unusable provider must not take sign-in down with it."""
        with app.app_context():
            session = get_session()
            session.add(
                AuthProviderConfig(
                    kind=AUTH_OAUTH,
                    name="broken",
                    display_name="Broken",
                    enabled=True,
                    settings_json='{"client_id": "x"}',
                    secrets_json="{}",
                )
            )
            session.commit()

        page = client.get("/auth/login")
        assert page.status_code == 200
        assert b"/auth/oauth/broken" not in page.data


class TestProbeUrlGuard:
    """The Test button turns an operator-supplied URL into a server-side
    request, so it is a request-forgery primitive even behind admin-only
    access: the response and any error come back to the browser."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # cloud instance metadata
            "https://169.254.169.254/",
            "http://127.0.0.1:8081/api/v1/servers",  # the PowerDNS API itself
            "http://localhost/",
            "http://[::1]/",
        ],
    )
    def test_link_local_and_loopback_are_refused(self, app, url):
        from app.auth.store import ProviderConfigError
        from app.views.authproviders import _check_fetchable

        with app.app_context(), pytest.raises(ProviderConfigError, match="blocked"):
            _check_fetchable(url)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/"])
    def test_only_http_schemes_are_allowed(self, app, url):
        from app.auth.store import ProviderConfigError
        from app.views.authproviders import _check_fetchable

        with app.app_context(), pytest.raises(ProviderConfigError, match="http"):
            _check_fetchable(url)

    def test_a_private_address_is_allowed(self, app, monkeypatch):
        """An internal Keycloak or AD on 10.x is the normal case here, so
        private ranges must keep working."""
        import socket

        from app.views.authproviders import _check_fetchable

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, None, None, "", ("10.1.2.3", 0))],
        )
        with app.app_context():
            assert _check_fetchable("https://idp.internal/metadata")

    def test_an_unresolvable_host_is_reported(self, app):
        from app.auth.store import ProviderConfigError
        from app.views.authproviders import _check_fetchable

        with app.app_context(), pytest.raises(ProviderConfigError, match="could not be resolved"):
            _check_fetchable("https://no-such-host.invalid/metadata")
