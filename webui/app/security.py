"""Session handling, CSRF, RBAC and login throttling."""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections.abc import Callable
from functools import wraps
from urllib.parse import urljoin, urlparse, urlunparse

from flask import (
    abort,
    current_app,
    flash,
    g,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .config import ROLE_ADMIN, ROLE_OPERATOR
from .database import get_session
from .models import User, utcnow

log = logging.getLogger(__name__)

SESSION_USER_ID = "uid"
SESSION_AUTH_TIME = "auth_time"
SESSION_CSRF = "csrf"

#: Methods that change state and therefore need a CSRF token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

PASSWORD_MIN_LENGTH = 12


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash with scrypt, Werkzeug's current default."""
    return generate_password_hash(password, method="scrypt")


#: A real hash of a value nobody can log in with, used to burn the same amount
#: of CPU when the account does not exist. Computed once at import.
_DUMMY_HASH = generate_password_hash("no-such-account-timing-equaliser", method="scrypt")


def verify_password(user: User | None, password: str) -> bool:
    """Constant-ish time password check.

    When the user does not exist we still run a hash comparison against a dummy
    value, so a missing account and a wrong password take comparable time and
    the login form does not leak which usernames are real.
    """
    stored = user.password_hash if user and user.password_hash else None
    if stored is None:
        check_password_hash(_DUMMY_HASH, password)
        return False
    return check_password_hash(stored, password)


def password_problems(password: str, username: str = "") -> list[str]:
    """Human-readable reasons a password is unacceptable; empty list if fine."""
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
    if username and password.lower().strip() == username.lower().strip():
        problems.append("The password must not be the same as the username.")
    if password.strip() != password:
        problems.append("The password must not start or end with a space.")
    lowered = password.lower()
    for weak in ("password", "changeme", "powerdns", "letmein", "12345678", "qwerty"):
        if weak in lowered:
            problems.append(f"The password must not contain {weak!r}.")
            break
    return problems


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def login_user(user: User) -> None:
    """Start an authenticated session for ``user``."""
    # A fresh session id on privilege change defeats session fixation: an
    # attacker who planted a cookie before login cannot ride it afterwards.
    session.clear()
    session[SESSION_USER_ID] = user.id
    session[SESSION_AUTH_TIME] = int(time.time())
    session[SESSION_CSRF] = secrets.token_urlsafe(32)
    session.permanent = True
    g.current_user = user

    db = get_session()
    user.last_login_at = utcnow()
    db.commit()


def logout_user() -> None:
    session.clear()
    g.pop("current_user", None)


def current_user() -> User | None:
    """The signed-in user, or ``None``. Cached for the current request."""
    if "current_user" in g:
        return g.current_user

    user_id = session.get(SESSION_USER_ID)
    user: User | None = None
    if user_id is not None:
        db = get_session()
        user = db.get(User, user_id)
        if user is not None and not user.is_active:
            # Deactivated mid-session: drop the session rather than serve them.
            log.info("session for deactivated user %s rejected", user.username)
            session.clear()
            user = None
        elif user is None:
            session.clear()

    g.current_user = user
    return user


def same_origin_path(target: str | None) -> str | None:
    """Reduce ``target`` to a path on this site, or ``None`` if it is not ours.

    Guards the ?next= parameter: without this an attacker can craft a login
    link that bounces the user to an external page after authenticating.

    The return value is *rebuilt* from the parsed components with the scheme
    and host left empty, rather than handed back as it arrived. That makes the
    result structurally incapable of carrying a scheme or a host: even if the
    checks below were subtly wrong, what comes out can only be a path on this
    site. Returning the caller's own string would leave the guarantee resting
    entirely on the checks being exhaustive.
    """
    if not target:
        return None
    # Protocol-relative ("//evil.test") and backslash forms are read
    # inconsistently across browsers, so they are refused before parsing.
    if target.startswith("//") or "\\" in target:
        return None

    reference = urlparse(request.host_url)
    candidate = urlparse(urljoin(request.host_url, target))
    if candidate.scheme not in ("http", "https"):
        return None
    if candidate.netloc != reference.netloc:
        return None

    path = candidate.path or "/"
    if not path.startswith("/"):
        return None
    return urlunparse(("", "", path, candidate.params, candidate.query, candidate.fragment))


def is_safe_redirect_url(target: str | None) -> bool:
    """Whether ``target`` points back at this site."""
    return same_origin_path(target) is not None


def redirect_target(default_endpoint: str = "dashboard.index") -> str:
    target = request.args.get("next") or request.form.get("next")
    return same_origin_path(target) or url_for(default_endpoint)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def csrf_token() -> str:
    """Token for the current session, minted on first use."""
    token = session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF] = token
    return token


def validate_csrf() -> None:
    """Abort with 400 unless the request carries the session's CSRF token.

    Registered as a before_request hook, so it covers every state-changing
    endpoint by default rather than by remembering to decorate each one.
    """
    if request.method not in UNSAFE_METHODS:
        return
    if getattr(request.url_rule, "endpoint", None) in current_app.config.get("CSRF_EXEMPT", ()):
        return

    expected = session.get(SESSION_CSRF)
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        log.warning(
            "CSRF validation failed for %s %s from %s",
            request.method,
            request.path,
            request.remote_addr,
        )
        abort(400, description="Your session expired or the form was stale. Please try again.")


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.accept_mimetypes.best == "application/json":
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def role_required(*roles: str) -> Callable:
    """Require one of ``roles``. Admin always passes."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            user = current_user()
            assert user is not None  # login_required guarantees this
            if user.role not in roles and not user.is_admin:
                log.warning(
                    "user %s (role %s) denied access to %s",
                    user.username,
                    user.role,
                    request.path,
                )
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view: Callable) -> Callable:
    return role_required(ROLE_ADMIN)(view)


def operator_required(view: Callable) -> Callable:
    """Operator or admin: write access to every zone."""
    return role_required(ROLE_ADMIN, ROLE_OPERATOR)(view)


def require_zone_access(zone_name: str) -> User:
    """Return the current user, or abort if they may not touch ``zone_name``."""
    user = current_user()
    if user is None:
        abort(401)
    if not user.can_see_zone(zone_name):
        log.warning("user %s denied access to zone %s", user.username, zone_name)
        abort(403)
    return user


# ---------------------------------------------------------------------------
# Login throttling
# ---------------------------------------------------------------------------


class LoginThrottle:
    """In-memory failed-login counter.

    Deliberately simple: state is per-process, so with several gunicorn workers
    the effective allowance is roughly ``max_attempts * workers``. That is fine
    for slowing down password guessing, which is what this is for. A shared
    store would be needed to make it an exact limit.
    """

    def __init__(self, max_attempts: int = 10, lockout_seconds: int = 300):
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _key(username: str, remote_addr: str) -> str:
        return f"{username.lower()}|{remote_addr}"

    def _prune(self, now: float) -> None:
        if len(self._failures) < 1024:
            return
        stale = [
            key for key, (_, last) in self._failures.items() if now - last > self.lockout_seconds
        ]
        for key in stale:
            self._failures.pop(key, None)

    def is_locked(self, username: str, remote_addr: str) -> int:
        """Seconds remaining in the lockout, or 0 if not locked."""
        entry = self._failures.get(self._key(username, remote_addr))
        if not entry:
            return 0
        count, last_failure = entry
        if count < self.max_attempts:
            return 0
        remaining = int(self.lockout_seconds - (time.time() - last_failure))
        if remaining <= 0:
            self._failures.pop(self._key(username, remote_addr), None)
            return 0
        return remaining

    def record_failure(self, username: str, remote_addr: str) -> None:
        now = time.time()
        self._prune(now)
        key = self._key(username, remote_addr)
        count, last_failure = self._failures.get(key, (0, now))
        # Counting restarts once the window has elapsed.
        if now - last_failure > self.lockout_seconds:
            count = 0
        self._failures[key] = (count + 1, now)

    def reset(self, username: str, remote_addr: str) -> None:
        self._failures.pop(self._key(username, remote_addr), None)


def get_throttle() -> LoginThrottle:
    return current_app.extensions["pdnsadmin.throttle"]


def flash_errors(errors: list[str]) -> None:
    for error in errors:
        flash(error, "danger")
