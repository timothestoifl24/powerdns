"""Several LDAP servers: one directory, more than one host to reach it at."""

from __future__ import annotations

import pytest
from ldap3 import FIRST, Server, ServerPool
from ldap3.core.exceptions import LDAPSocketOpenError

from app.auth import ldap_auth
from app.auth.ldap_auth import (
    POOL_EXHAUST_SECONDS,
    LdapAuthError,
    _build_server,
    _needs_start_tls,
)
from app.config import LdapConfig, split_uris

TWO_SERVERS = "ldaps://dc1.example.com\nldaps://dc2.example.com"


def config(uri: str = TWO_SERVERS, **overrides) -> LdapConfig:
    return LdapConfig(enabled=True, uri=uri, base_dn="dc=example,dc=com", **overrides)


class TestSplitting:
    @pytest.mark.parametrize(
        "raw",
        [
            "ldaps://dc1.example.com,ldaps://dc2.example.com",
            "ldaps://dc1.example.com, ldaps://dc2.example.com",
            "ldaps://dc1.example.com\nldaps://dc2.example.com",
            "ldaps://dc1.example.com ldaps://dc2.example.com",
            "  ldaps://dc1.example.com ;\n ldaps://dc2.example.com  ",
        ],
    )
    def test_every_separator_people_reach_for(self, raw):
        assert split_uris(raw) == ["ldaps://dc1.example.com", "ldaps://dc2.example.com"]

    def test_order_is_preserved(self):
        assert config().uris == ("ldaps://dc1.example.com", "ldaps://dc2.example.com")
        assert config().primary_uri == "ldaps://dc1.example.com"

    def test_one_server_still_looks_like_one(self):
        assert config("ldap://dc.example.com").uris == ("ldap://dc.example.com",)

    def test_nothing_configured(self):
        assert config("").uris == ()
        assert config("").primary_uri == ""


class TestServerConstruction:
    def test_a_single_uri_makes_a_plain_server(self):
        server = _build_server(config("ldaps://dc.example.com"))
        assert isinstance(server, Server)
        assert server.name == "ldaps://dc.example.com:636"

    def test_several_uris_make_a_failover_pool(self):
        pool = _build_server(config())
        assert isinstance(pool, ServerPool)
        # FIRST, not round robin: the order is a preference, and the second
        # server is where you go when the first is unreachable.
        assert pool.strategy == FIRST
        assert [server.name for server in pool.servers] == [
            "ldaps://dc1.example.com:636",
            "ldaps://dc2.example.com:636",
        ]

    def test_each_server_is_tried_once_before_giving_up(self):
        """Otherwise a dead first server retries forever instead of failing over."""
        pool = _build_server(config())
        assert pool.active == 2
        assert pool.exhaust == POOL_EXHAUST_SECONDS

    def test_tls_is_decided_per_server(self):
        pool = _build_server(config("ldap://dc1.example.com\nldaps://dc2.example.com"))
        plain, secure = pool.servers
        assert plain.tls is None
        assert secure.tls is not None

    def test_start_tls_applies_to_the_plain_server_only(self):
        settings = config("ldap://dc1.example.com\nldaps://dc2.example.com", start_tls=True)
        plain, secure = _build_server(settings).servers
        assert _needs_start_tls(settings, plain) is True
        # Asking a server already inside TLS to start TLS fails, so it is not asked.
        assert _needs_start_tls(settings, secure) is False

    def test_start_tls_off_means_never(self):
        settings = config("ldap://dc.example.com")
        assert _needs_start_tls(settings, _build_server(settings)) is False


class TestFailure:
    def test_an_unreachable_directory_names_every_server(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise LDAPSocketOpenError("connection refused")

        monkeypatch.setattr(ldap_auth, "Connection", refuse)
        with pytest.raises(LdapAuthError) as caught:
            ldap_auth.authenticate(config(), "jdoe", "hunter2hunter2")
        message = str(caught.value)
        assert "ldaps://dc1.example.com" in message
        assert "ldaps://dc2.example.com" in message


class TestProviderForm:
    """The same list, configured through the web UI rather than the environment."""

    def test_a_pasted_list_is_stored_one_per_line(self, app, client, users, login, token):
        from app.auth.store import to_ldap_config
        from app.database import get_session
        from app.models import AuthProviderConfig

        login("admin")
        response = client.post(
            "/admin/auth/new/ldap",
            data={
                "csrf_token": token("/admin/auth/new/ldap"),
                "name": "corp",
                "display_name": "Corp AD",
                "enabled": "on",
                "uri": "ldaps://dc1.example.com, ldaps://dc2.example.com",
                "base_dn": "dc=example,dc=com",
                "username_attribute": "sAMAccountName",
                "default_role": "user",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        with app.app_context():
            row = get_session().query(AuthProviderConfig).filter_by(name="corp").one()
            assert row.setting("uri") == "ldaps://dc1.example.com\nldaps://dc2.example.com"
            assert to_ldap_config(row).uris == (
                "ldaps://dc1.example.com",
                "ldaps://dc2.example.com",
            )


class TestSettingsPage:
    def test_every_server_is_listed(self, make_app):
        """An operator checking the settings page should see the whole list."""
        from conftest import csrf_from

        from app.config import AUTH_LOCAL, ROLE_ADMIN
        from app.database import get_session
        from app.models import User
        from app.security import hash_password

        app = make_app(
            LDAP_ENABLED="true",
            LDAP_URI="ldaps://dc1.example.com,ldaps://dc2.example.com",
            LDAP_BASE_DN="DC=example,DC=com",
        )
        with app.app_context():
            session = get_session()
            session.add(
                User(
                    username="admin",
                    auth_source=AUTH_LOCAL,
                    password_hash=hash_password("admin-password-123"),
                    role=ROLE_ADMIN,
                )
            )
            session.commit()

        client = app.test_client()
        client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": csrf_from(client.get("/auth/login").data),
            },
        )
        page = client.get("/admin/settings").data
        assert b">ldaps://dc1.example.com<" in page
        assert b">ldaps://dc2.example.com" in page
        # The second is marked as the fallback rather than looking like a peer.
        assert b"failover" in page
