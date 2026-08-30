"""Passwords, CSRF, redirect safety and login throttling."""

from __future__ import annotations

import re

import pytest
from werkzeug.security import check_password_hash

from app.security import (
    LoginThrottle,
    hash_password,
    is_safe_redirect_url,
    password_problems,
    verify_password,
)


class TestPasswords:
    def test_hash_round_trips(self):
        stored = hash_password("correct horse battery staple")
        assert check_password_hash(stored, "correct horse battery staple")
        assert not check_password_hash(stored, "wrong")

    def test_hash_is_salted(self):
        """Two users with the same password must not share a hash."""
        assert hash_password("same-password") != hash_password("same-password")

    def test_missing_user_still_runs_a_comparison(self):
        """Guards against username enumeration by response time."""
        assert verify_password(None, "anything") is False

    def test_user_without_password_hash_cannot_log_in(self):
        class ExternalUser:
            password_hash = None

        assert verify_password(ExternalUser(), "") is False

    @pytest.mark.parametrize(
        "password,expected_fragment",
        [
            ("short", "at least 12"),
            ("mypassword123456", "password"),
            (" leadingspace123 ", "space"),
        ],
    )
    def test_weak_passwords_rejected(self, password, expected_fragment):
        problems = password_problems(password, "someone")
        assert any(expected_fragment in problem.lower() for problem in problems)

    def test_password_equal_to_username_rejected(self):
        problems = password_problems("administrator", "administrator")
        assert any("same as the username" in problem for problem in problems)

    def test_reasonable_password_accepted(self):
        assert password_problems("Tr0ub4dor&3xample", "jdoe") == []


class TestRedirectSafety:
    @pytest.mark.parametrize(
        "target",
        ["https://evil.example.com/", "//evil.example.com", "/\\evil.example.com", "", None],
    )
    def test_external_targets_rejected(self, app, target):
        with app.test_request_context("/auth/login"):
            assert is_safe_redirect_url(target) is False

    @pytest.mark.parametrize("target", ["/zones/", "/zones/example.com.", "/?a=b"])
    def test_local_targets_accepted(self, app, target):
        with app.test_request_context("/auth/login"):
            assert is_safe_redirect_url(target) is True


class TestLoginThrottle:
    def test_locks_after_the_configured_number_of_failures(self):
        throttle = LoginThrottle(max_attempts=3, lockout_seconds=60)
        for _ in range(2):
            throttle.record_failure("bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") == 0
        throttle.record_failure("bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") > 0

    def test_lock_is_per_username_and_address(self):
        throttle = LoginThrottle(max_attempts=1, lockout_seconds=60)
        throttle.record_failure("bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") > 0
        assert throttle.is_locked("bob", "10.0.0.2") == 0
        assert throttle.is_locked("alice", "10.0.0.1") == 0

    def test_username_matching_is_case_insensitive(self):
        throttle = LoginThrottle(max_attempts=1, lockout_seconds=60)
        throttle.record_failure("Bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") > 0

    def test_reset_clears_the_lock(self):
        throttle = LoginThrottle(max_attempts=1, lockout_seconds=60)
        throttle.record_failure("bob", "10.0.0.1")
        throttle.reset("bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") == 0

    def test_lock_expires(self):
        throttle = LoginThrottle(max_attempts=1, lockout_seconds=0)
        throttle.record_failure("bob", "10.0.0.1")
        assert throttle.is_locked("bob", "10.0.0.1") == 0


class TestCsrf:
    def test_post_without_token_is_rejected(self, client):
        response = client.post("/auth/login", data={"username": "a", "password": "b"})
        assert response.status_code == 400

    def test_post_with_a_wrong_token_is_rejected(self, client):
        client.get("/auth/login")
        response = client.post(
            "/auth/login",
            data={"username": "a", "password": "b", "csrf_token": "not-the-token"},
        )
        assert response.status_code == 400

    def test_get_requests_need_no_token(self, client):
        assert client.get("/auth/login").status_code == 200

    def test_saml_acs_is_exempt(self, app):
        """The IdP posts cross-site, so it cannot carry our token."""
        assert "auth.saml_acs" in app.config["CSRF_EXEMPT"]


class TestSecurityHeaders:
    def test_headers_are_set(self, client):
        headers = client.get("/auth/login").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    def test_csp_allows_no_external_scripts(self, client):
        """Tabler is vendored, so nothing off-origin needs to be allowed."""
        csp = client.get("/auth/login").headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]


class TestSchemaNameValidation:
    """DB_SCHEMA is the one config value that reaches SQL as text, not as a
    bound parameter, so it is validated rather than trusted."""

    @pytest.mark.parametrize("schema", ["pdnsadmin", "PdnsAdmin", "_private", "s3", "a" * 63])
    def test_valid_identifiers_accepted(self, schema):
        from app.database import validate_schema_name

        assert validate_schema_name(schema) == schema

    @pytest.mark.parametrize(
        "schema",
        [
            "",
            "3leading",
            "has space",
            "has-dash",
            'quote"inject',
            'x"; DROP SCHEMA public CASCADE; --',
            "public,evil",
            "a" * 64,
        ],
    )
    def test_injection_shaped_values_rejected(self, schema):
        from app.database import validate_schema_name

        with pytest.raises(ValueError, match="not a valid PostgreSQL identifier"):
            validate_schema_name(schema)

    def test_engine_creation_rejects_a_bad_schema(self):
        from app.database import make_engine

        with pytest.raises(ValueError):
            make_engine("postgresql+psycopg://u:p@h/db", 'evil"; --')


class TestTransportHeaders:
    def test_hsts_absent_over_plain_http(self, client):
        """Pinning HTTPS on an HTTP-only deployment would lock users out."""
        assert "Strict-Transport-Security" not in client.get("/auth/login").headers

    def test_hsts_present_when_deployment_is_https(self, make_app):
        app = make_app(SESSION_COOKIE_SECURE="true")
        response = app.test_client().get("/auth/login")
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]


class TestEveryRouteIsGuarded:
    """A route added without an auth decorator should fail CI, not ship.

    Decorator introspection cannot do this -- functools.wraps copies the inner
    function's name onto the wrapper -- so these walk the URL map and check
    what the app actually does with an anonymous caller.
    """

    #: Endpoints that must stay reachable without a session: the login form
    #: itself, the OAuth/SAML handshake (the caller is not signed in yet, by
    #: definition), static assets, and the container healthchecks.
    PUBLIC_ENDPOINTS = {
        "auth.login",
        "auth.logout",
        "auth.oauth_login",
        "auth.oauth_callback",
        "auth.saml_login",
        "auth.saml_acs",
        "auth.saml_metadata",
        "auth.saml_sls",
        "static",
        "healthz",
        "readyz",
    }

    SAMPLE_ARGS = {
        "user_id": "1",
        "provider_id": "1",
        "provider": "keycloak",
        "kind": "oauth",
        "zone_id": "example.com.",
        "filename": "css/app.css",
    }

    def _urls(self, app):
        for rule in sorted(app.url_map.iter_rules(), key=str):
            if rule.endpoint in self.PUBLIC_ENDPOINTS:
                continue
            url = str(rule)
            for key, value in self.SAMPLE_ARGS.items():
                url = (
                    url.replace(f"<int:{key}>", value)
                    .replace(f"<path:{key}>", value)
                    .replace(f"<{key}>", value)
                )
            # A placeholder left in the URL would not match its rule, and the
            # 404 that follows looks exactly like "unguarded" in the assertions
            # below. Fail as the harness gap it is, naming the missing argument.
            assert "<" not in url, f"{url} has an unsubstituted argument; add it to SAMPLE_ARGS"
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                yield method, url

    @staticmethod
    def _anonymous_csrf_token(client):
        """An anonymous caller can get a valid token -- the login form needs one."""
        page = client.get("/auth/login").get_data(as_text=True)
        match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
        assert match, "login form should carry a CSRF token"
        return match.group(1)

    @staticmethod
    def _redirects_to_login(response):
        if response.status_code in (401, 403):
            return True
        return response.status_code in (301, 302, 303, 307, 308) and (
            "login" in response.headers.get("Location", "").lower()
        )

    def test_authentication_guards_every_route(self, app, client):
        """No route is reachable without a session.

        Unsafe methods are sent *with* a valid CSRF token, so the CSRF layer
        cannot mask a missing authentication decorator: whatever refuses the
        request here is the authentication check.
        """
        token = self._anonymous_csrf_token(client)
        reachable = []
        for method, url in self._urls(app):
            kwargs = {"follow_redirects": False}
            if method not in ("GET", "HEAD", "OPTIONS"):
                kwargs["data"] = {"csrf_token": token}
            response = client.open(url, method=method, **kwargs)
            if not self._redirects_to_login(response):
                reachable.append((method, url, response.status_code))
        assert not reachable, f"routes reachable without authentication: {reachable}"

    def test_unsafe_methods_also_require_a_csrf_token(self, app, client):
        """The second layer: a state-changing request with no token is rejected."""
        accepted = []
        for method, url in self._urls(app):
            if method in ("GET", "HEAD", "OPTIONS"):
                continue
            response = client.open(url, method=method, follow_redirects=False)
            if response.status_code != 400:
                accepted.append((method, url, response.status_code))
        assert not accepted, f"state-changing routes without CSRF enforcement: {accepted}"
