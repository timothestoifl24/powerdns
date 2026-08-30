"""Passwords, CSRF, redirect safety and login throttling."""

from __future__ import annotations

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
