"""Shared fixtures.

The app runs against an in-memory SQLite database and the fake PowerDNS from
``fake_pdns``. The fake is installed by replacing ``requests.Session`` inside
``app.pdns``, so every test exercises the real PdnsClient code path.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_pdns import FakePowerDNS  # noqa: E402

API_KEY = "testtesttesttest"

#: The environment every test starts from. Individual tests add to this via the
#: `env` fixture to switch authentication backends on.
BASE_ENV = {
    "DATABASE_URL": "sqlite://",
    "SECRET_KEY": "x" * 48,
    "PDNS_API_KEY": API_KEY,
    "PDNS_API_URL": "http://pdns.test:8081",
    "SESSION_COOKIE_SECURE": "false",
    "LOG_LEVEL": "CRITICAL",
    "BOOTSTRAP_ADMIN_PASSWORD": "",
    "DEFAULT_NAMESERVERS": "ns1.example.com,ns2.example.com",
}

#: Cleared before each test so a leftover value cannot leak between them.
MANAGED_PREFIXES = ("LDAP_", "OAUTH_", "SAML_", "PDNS_", "DB_", "LOCAL_AUTH", "BOOTSTRAP_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(MANAGED_PREFIXES) or name in (
            "DATABASE_URL",
            "SECRET_KEY",
            "SESSION_COOKIE_SECURE",
            "BASE_URL",
            "DEFAULT_NAMESERVERS",
            "LOG_LEVEL",
        ):
            monkeypatch.delenv(name, raising=False)
    for name, value in BASE_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def pdns(monkeypatch) -> FakePowerDNS:
    """Install the fake PowerDNS transport.

    Only the session handed to PdnsClient is swapped, never requests.Session
    itself -- Authlib subclasses that class, and replacing it globally breaks
    OAuth at import time.
    """
    fake = FakePowerDNS(api_key=API_KEY)

    import app as app_package
    from app.pdns import PdnsClient
    from app.views import admin as admin_views
    from app.views import dashboard as dashboard_views
    from app.views import zones as zones_views

    def factory(config):
        return PdnsClient(
            base_url=config["PDNS_API_URL"],
            api_key=config["PDNS_API_KEY"],
            server_id=config["PDNS_SERVER_ID"],
            timeout=config["PDNS_API_TIMEOUT"],
            session=fake,
        )

    # The factory is imported by name into each namespace that uses it, so each
    # one needs patching -- including the app package, where /readyz lives.
    for module in (app_package, zones_views, dashboard_views, admin_views):
        monkeypatch.setattr(module, "client_from_config", factory)

    fake.client_factory = factory
    return fake


@pytest.fixture
def make_app(pdns):
    """Build an app. Pass env overrides to switch on an auth backend."""

    def factory(**env):
        for key, value in env.items():
            os.environ[key] = str(value)
        from app import create_app
        from app.database import create_all

        application = create_app()
        application.config["TESTING"] = True
        create_all(application)
        return application

    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def users(app):
    """Create one user per role, plus a deactivated one."""
    from app.config import AUTH_LOCAL, ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER
    from app.database import get_session
    from app.models import User
    from app.security import hash_password

    created = {}
    with app.app_context():
        session = get_session()
        for username, role in (
            ("admin", ROLE_ADMIN),
            ("operator", ROLE_OPERATOR),
            ("viewer", ROLE_USER),
        ):
            user = User(
                username=username,
                auth_source=AUTH_LOCAL,
                password_hash=hash_password(f"{username}-password-123"),
                role=role,
                is_active=True,
            )
            session.add(user)
        session.add(
            User(
                username="disabled",
                auth_source=AUTH_LOCAL,
                password_hash=hash_password("disabled-password-123"),
                role=ROLE_USER,
                is_active=False,
            )
        )
        session.commit()
        for user in session.query(User).all():
            created[user.username] = user.id
    return created


def csrf_from(html: bytes) -> str:
    """Pull a CSRF token out of a rendered page."""
    match = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the response"
    return match.group(1).decode()


@pytest.fixture
def login(client):
    """Sign in as a user, returning the client."""

    def do_login(username: str, password: str | None = None):
        page = client.get("/auth/login")
        token = csrf_from(page.data)
        response = client.post(
            "/auth/login",
            data={
                "username": username,
                "password": password or f"{username}-password-123",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        return response

    return do_login


@pytest.fixture
def token(client):
    """A CSRF token valid for the current session."""

    def get_token(path: str = "/auth/login") -> str:
        return csrf_from(client.get(path).data)

    return get_token
