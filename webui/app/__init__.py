"""Application factory."""

from __future__ import annotations

import logging
import sys

from flask import Flask, current_app, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import database
from .config import ConfigError, build_config
from .pdns import COMMON_RECORD_TYPES, ZONE_KINDS, client_from_config, relative_name
from .security import LoginThrottle, csrf_token, current_user, validate_csrf

__version__ = "1.0.0"


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        # gunicorn installs its own handlers; do not double up.
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


def create_app(overrides: dict | None = None) -> Flask:
    app = Flask(__name__)

    try:
        app.config.update(build_config())
    except ConfigError as exc:
        # A misconfigured container should say exactly what is wrong and stop,
        # not boot into a half-working state.
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if overrides:
        app.config.update(overrides)

    configure_logging(app.config["LOG_LEVEL"])
    log = logging.getLogger(__name__)

    # Only trust forwarding headers when the operator says how many proxies sit
    # in front; otherwise any client could spoof its address in the audit log.
    proxy_count = app.config["TRUSTED_PROXY_COUNT"]
    if proxy_count > 0:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app,
            x_for=proxy_count,
            x_proto=proxy_count,
            x_host=proxy_count,
            x_port=proxy_count,
        )
        log.info("trusting %d reverse prox(ies) for forwarded headers", proxy_count)

    database.init_app(app)

    app.extensions["pdnsadmin.throttle"] = LoginThrottle(
        max_attempts=app.config["LOGIN_MAX_ATTEMPTS"],
        lockout_seconds=app.config["LOGIN_LOCKOUT_SECONDS"],
    )

    from .auth import oauth as oauth_auth

    oauth_auth.init_app(app)

    # The IdP POSTs the SAML assertion cross-site, so it cannot carry our CSRF
    # token. The assertion's own signature is what authenticates that request.
    app.config["CSRF_EXEMPT"] = frozenset({"auth.saml_acs", "auth.saml_sls"})

    register_blueprints(app)
    register_hooks(app)
    register_error_handlers(app)
    register_template_helpers(app)

    log.info("PowerDNS admin panel %s ready", __version__)
    return app


def register_blueprints(app: Flask) -> None:
    from .views.admin import bp as admin_bp
    from .views.auth_views import bp as auth_bp
    from .views.dashboard import bp as dashboard_bp
    from .views.profile import bp as profile_bp
    from .views.zones import bp as zones_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(zones_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(profile_bp)


def register_hooks(app: Flask) -> None:
    @app.before_request
    def _csrf() -> None:
        validate_csrf()

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # Only meaningful over HTTPS, and actively harmful over plain HTTP
        # (it would pin a host the panel cannot serve). SESSION_COOKIE_SECURE
        # is the operator's statement that this deployment is HTTPS-only.
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # Everything is served from our own origin: Tabler is vendored into the
        # image, so no CDN needs to be allowed here.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'self'",
        )
        return response

    @app.route("/healthz")
    def healthz():
        """Liveness for the container healthcheck. Deliberately unauthenticated."""
        return {"status": "ok", "version": __version__}

    @app.route("/readyz")
    def readyz():
        """Readiness: reports whether the database and PowerDNS API answer."""
        from sqlalchemy import text

        checks = {"database": False, "powerdns": False}
        try:
            database.get_session().execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            current_app.logger.exception("readiness: database check failed")
        try:
            checks["powerdns"] = client_from_config(current_app.config).ping()
        except Exception:
            current_app.logger.exception("readiness: PowerDNS check failed")

        healthy = all(checks.values())
        return ({"status": "ok" if healthy else "degraded", **checks}, 200 if healthy else 503)


def register_error_handlers(app: Flask) -> None:
    def render_error(code: int, title: str, message: str):
        if request.accept_mimetypes.best == "application/json":
            return {"error": title, "message": message}, code
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(error):
        return render_error(
            400, "Bad request", getattr(error, "description", "The request was not valid.")
        )

    @app.errorhandler(401)
    def unauthorised(error):
        return render_error(401, "Sign-in required", "Please sign in to continue.")

    @app.errorhandler(403)
    def forbidden(error):
        return render_error(403, "Not allowed", "Your account does not have access to this page.")

    @app.errorhandler(404)
    def not_found(error):
        return render_error(404, "Not found", "That page does not exist.")

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover - exercised only on real faults
        app.logger.exception("unhandled error on %s", request.path)
        return render_error(
            500, "Something went wrong", "The error has been written to the server log."
        )


def register_template_helpers(app: Flask) -> None:
    from .pdns import PdnsError

    @app.context_processor
    def _globals() -> dict:
        return {
            "csrf_token": csrf_token,
            "current_user": current_user(),
            "site_name": app.config["SITE_NAME"],
            "app_version": __version__,
            "auth_config": app.config["AUTH"],
            "record_types": COMMON_RECORD_TYPES,
            "zone_kinds": ZONE_KINDS,
        }

    @app.template_filter("relname")
    def _relname(name: str, zone: str) -> str:
        return relative_name(name, zone)

    @app.template_filter("datetime")
    def _datetime(value) -> str:
        if value is None:
            return "—"
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")

    @app.template_filter("ttl")
    def _ttl(seconds) -> str:
        """Render a TTL the way an operator reads it: 3600 -> '1 hour'."""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return str(seconds)
        for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
            if seconds >= size and seconds % size == 0:
                count = seconds // size
                return f"{count} {unit}{'s' if count != 1 else ''}"
        return f"{seconds} second{'s' if seconds != 1 else ''}"

    app.jinja_env.globals["PdnsError"] = PdnsError
