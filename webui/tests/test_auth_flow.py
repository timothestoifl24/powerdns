"""Sign-in, sign-out, session handling and identity provisioning."""

from __future__ import annotations

import pytest

from app.auth.ldap_auth import escape_filter_value
from app.auth.provisioning import (
    IdentityClaim,
    ProvisioningError,
    normalise_username,
    resolve_identity,
)
from app.config import AUTH_LDAP, AUTH_LOCAL, AUTH_OAUTH, ROLE_ADMIN, ROLE_USER, GroupRoleMap
from app.database import get_session
from app.models import User


class TestLocalLogin:
    def test_correct_credentials_sign_in(self, client, users, login):
        response = login("admin")
        assert response.status_code == 302
        assert client.get("/").status_code == 200

    def test_wrong_password_is_refused(self, client, users, login):
        response = login("admin", "wrong-password")
        assert response.status_code == 401
        assert b"Incorrect username or password" in response.data

    def test_unknown_user_gets_the_same_message(self, client, users, login):
        """The response must not reveal whether the account exists."""
        unknown = login("no-such-user", "whatever")
        wrong = login("admin", "wrong-password")
        assert unknown.status_code == wrong.status_code == 401
        assert b"Incorrect username or password" in unknown.data

    def test_deactivated_user_cannot_sign_in(self, client, users, login):
        assert login("disabled", "disabled-password-123").status_code == 401

    def test_login_is_throttled(self, client, users, login, app):
        limit = app.config["LOGIN_MAX_ATTEMPTS"]
        for _ in range(limit):
            login("admin", "wrong-password")
        response = login("admin", "wrong-password")
        assert response.status_code == 429
        assert b"Too many failed attempts" in response.data

    def test_successful_login_clears_the_throttle(self, client, users, login):
        login("admin", "wrong-password")
        assert login("admin").status_code == 302

    def test_logout_ends_the_session(self, client, users, login, token):
        login("admin")
        assert client.get("/").status_code == 200
        client.post("/auth/logout", data={"csrf_token": token("/")})
        assert client.get("/").status_code == 302

    def test_session_is_rejected_once_the_user_is_deactivated(self, client, users, login, app):
        login("viewer")
        assert client.get("/").status_code == 200
        with app.app_context():
            session = get_session()
            user = session.get(User, users["viewer"])
            user.is_active = False
            session.commit()
        assert client.get("/").status_code == 302

    def test_next_parameter_stays_on_site(self, client, users, login):
        client.get("/auth/login")
        page = client.get("/auth/login?next=/zones/")
        from conftest import csrf_from

        response = client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": csrf_from(page.data),
                "next": "https://evil.example.com/",
            },
        )
        assert response.status_code == 302
        assert "evil.example.com" not in response.headers["Location"]


class TestLoginPage:
    def test_shows_the_local_form_by_default(self, client):
        assert b"Sign in to your account" in client.get("/auth/login").data

    def test_shows_configured_oauth_providers(self, make_app):
        app = make_app(
            OAUTH_PROVIDERS="keycloak",
            OAUTH_KEYCLOAK_CLIENT_ID="id",
            OAUTH_KEYCLOAK_CLIENT_SECRET="secret",
            OAUTH_KEYCLOAK_DISCOVERY_URL="https://sso.test/.well-known/openid-configuration",
            OAUTH_KEYCLOAK_DISPLAY_NAME="Company SSO",
        )
        page = app.test_client().get("/auth/login")
        assert b"Company SSO" in page.data
        assert b"/auth/oauth/keycloak" in page.data

    def test_shows_saml_button(self, make_app):
        app = make_app(SAML_ENABLED="true", SAML_IDP_METADATA_URL="https://sso.test/descriptor")
        assert b"Single sign-on (SAML)" in app.test_client().get("/auth/login").data

    def test_unknown_oauth_provider_is_404(self, client):
        assert client.get("/auth/oauth/nope").status_code == 404

    def test_saml_routes_absent_when_disabled(self, client):
        assert client.get("/auth/saml/metadata").status_code == 404


class TestUsernameNormalisation:
    def test_lower_cased_and_trimmed(self):
        assert normalise_username("  JDoe ") == "jdoe"

    def test_email_addresses_are_kept_whole(self):
        assert normalise_username("J.Doe@Example.COM") == "j.doe@example.com"

    @pytest.mark.parametrize("bad", ["", "   ", "has space", "semi;colon", "back\\slash"])
    def test_invalid_names_rejected(self, bad):
        with pytest.raises(ProvisioningError):
            normalise_username(bad)


class TestProvisioning:
    def test_creates_a_user_on_first_sign_in(self, app):
        claim = IdentityClaim(
            username="newperson",
            auth_source=AUTH_OAUTH,
            provider="keycloak",
            external_id="sub-123",
            email="new@example.com",
            display_name="New Person",
            groups=["dns-admins"],
        )
        with app.test_request_context():
            user = resolve_identity(claim, GroupRoleMap(admin_groups=("dns-admins",)))
            assert user.username == "newperson"
            assert user.role == ROLE_ADMIN
            assert user.auth_source == AUTH_OAUTH

    def test_matches_a_returning_user_by_subject_not_username(self, app):
        """A rename at the IdP must not create a second account."""
        first = IdentityClaim("olduser", AUTH_OAUTH, "keycloak", "sub-123", groups=["staff"])
        renamed = IdentityClaim("newuser", AUTH_OAUTH, "keycloak", "sub-123", groups=["staff"])
        roles = GroupRoleMap(default_role=ROLE_USER)
        with app.test_request_context():
            created = resolve_identity(first, roles)
            updated = resolve_identity(renamed, roles)
            assert created.id == updated.id
            assert updated.username == "newuser"
            assert get_session().query(User).count() == 1

    def test_role_follows_group_membership_on_each_sign_in(self, app):
        roles = GroupRoleMap(admin_groups=("dns-admins",), default_role=ROLE_USER)
        with app.test_request_context():
            promoted = resolve_identity(
                IdentityClaim("someone", AUTH_OAUTH, "kc", "sub-1", groups=["dns-admins"]), roles
            )
            assert promoted.role == ROLE_ADMIN
            demoted = resolve_identity(
                IdentityClaim("someone", AUTH_OAUTH, "kc", "sub-1", groups=["staff"]), roles
            )
            assert demoted.role == ROLE_USER

    def test_user_in_no_mapped_group_is_refused(self, app):
        roles = GroupRoleMap(admin_groups=("dns-admins",), default_role=None)
        with (
            app.test_request_context(),
            pytest.raises(ProvisioningError, match="not a member of any group"),
        ):
            resolve_identity(
                IdentityClaim("outsider", AUTH_OAUTH, "kc", "sub-9", groups=["other"]), roles
            )

    def test_sso_cannot_take_over_an_existing_local_account(self, app, users):
        """Otherwise anyone able to create a matching name at the IdP becomes admin."""
        claim = IdentityClaim("admin", AUTH_OAUTH, "keycloak", "sub-evil", groups=["staff"])
        with (
            app.test_request_context(),
            pytest.raises(ProvisioningError, match="local account named"),
        ):
            resolve_identity(claim, GroupRoleMap(default_role=ROLE_USER))

    def test_deactivated_external_user_is_refused(self, app):
        claim = IdentityClaim("someone", AUTH_LDAP, "ldap", "uid=someone", groups=["staff"])
        roles = GroupRoleMap(default_role=ROLE_USER)
        with app.test_request_context():
            user = resolve_identity(claim, roles)
            user.is_active = False
            get_session().commit()
            with pytest.raises(ProvisioningError, match="deactivated"):
                resolve_identity(claim, roles)

    def test_local_accounts_are_untouched_by_provisioning(self, app, users):
        with app.app_context():
            user = get_session().get(User, users["admin"])
            assert user.auth_source == AUTH_LOCAL
            assert user.password_hash is not None


class TestLdapFilterEscaping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("*)(uid=admin", r"\2a\29\28uid=admin"),
            ("normal", "normal"),
            ("a\\b", r"a\5cb"),
            ("(paren)", r"\28paren\29"),
        ],
    )
    def test_injection_characters_are_escaped(self, raw, expected):
        assert escape_filter_value(raw) == expected

    def test_escaped_value_cannot_break_out_of_the_filter(self):
        from app.config import LdapConfig

        template = LdapConfig().user_filter
        built = template.format(
            username=escape_filter_value("*)(uid=admin"), username_attribute="uid"
        )
        # The injected parentheses must not appear as filter syntax.
        assert built == r"(&(objectClass=person)(uid=\2a\29\28uid=admin))"
        assert built.count("(") == 3
