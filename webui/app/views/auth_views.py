"""Sign-in and sign-out, for all four authentication sources."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .. import audit
from ..auth import ProvisioningError, resolve_identity
from ..auth import local as local_auth
from ..auth import oauth as oauth_auth
from ..auth import saml as saml_auth
from ..auth.ldap_auth import LdapAuthError
from ..auth.ldap_auth import authenticate as ldap_authenticate
from ..auth.store import effective_auth_config
from ..security import (
    current_user,
    get_throttle,
    login_user,
    logout_user,
    redirect_target,
)

log = logging.getLogger(__name__)

bp = Blueprint("auth", __name__, url_prefix="/auth")

#: Shown for every failed local/LDAP attempt. Saying "no such user" would let
#: anyone enumerate accounts through the login form.
BAD_CREDENTIALS = "Incorrect username or password."


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(redirect_target())

    auth_config = effective_auth_config()
    if not auth_config.any_enabled:
        return render_template(
            "error.html",
            code=500,
            title="No sign-in method is configured",
            message=(
                "Every authentication backend is disabled. Set LOCAL_AUTH_ENABLED=true "
                "or configure LDAP, OAuth or SAML."
            ),
        ), 500

    next_url = request.args.get("next", "")

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remote_addr = request.remote_addr or "unknown"
        throttle = get_throttle()

        if not username or not password:
            flash("Enter both a username and a password.", "danger")
            return render_template("login.html", next_url=next_url, username=username), 400

        locked_for = throttle.is_locked(username, remote_addr)
        if locked_for:
            minutes = max(1, round(locked_for / 60))
            flash(
                f"Too many failed attempts. Try again in about {minutes} minute"
                f"{'s' if minutes != 1 else ''}.",
                "danger",
            )
            audit.record("login.throttled", target=username, success=False)
            return render_template("login.html", next_url=next_url, username=username), 429

        user = None
        if auth_config.local_enabled:
            user = local_auth.authenticate(username, password)

        if user is None and auth_config.ldap.enabled:
            try:
                claim = ldap_authenticate(auth_config.ldap, username, password)
            except LdapAuthError as exc:
                # A directory outage is not a credential problem; say so rather
                # than telling the user their password is wrong.
                log.error("LDAP authentication error: %s", exc)
                flash(
                    "The directory server could not be reached. Contact an administrator.",
                    "danger",
                )
                audit.record("login.ldap_error", target=username, detail=str(exc), success=False)
                return render_template("login.html", next_url=next_url, username=username), 502
            if claim is not None:
                try:
                    user = resolve_identity(claim, auth_config.ldap.roles)
                except ProvisioningError as exc:
                    flash(str(exc), "danger")
                    audit.record("login.denied", target=username, detail=str(exc), success=False)
                    return render_template("login.html", next_url=next_url, username=username), 403

        if user is None:
            throttle.record_failure(username, remote_addr)
            log.info("failed login for %r from %s", username, remote_addr)
            audit.record("login.failed", target=username, success=False)
            flash(BAD_CREDENTIALS, "danger")
            return render_template("login.html", next_url=next_url, username=username), 401

        throttle.reset(username, remote_addr)
        login_user(user)
        audit.record("login.success", target=user.username, actor=user)
        log.info("user %s signed in via %s", user.username, user.auth_source)
        return redirect(redirect_target())

    return render_template("login.html", next_url=next_url, username="")


@bp.route("/logout", methods=["POST"])
def logout():
    user = current_user()
    if user is not None:
        audit.record("logout", target=user.username, actor=user)
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# OAuth 2.0 / OpenID Connect
# ---------------------------------------------------------------------------


def _provider_or_404(name: str):
    provider = effective_auth_config().provider(name)
    if provider is None:
        abort(404)
    return provider


@bp.route("/oauth/<provider>")
def oauth_login(provider: str):
    configured = _provider_or_404(provider)
    # Survives the round trip to the provider and back to the callback.
    session["oauth_next"] = request.args.get("next", "")
    try:
        return oauth_auth.start_login(configured)
    except oauth_auth.OAuthError as exc:
        log.error("could not start OAuth login with %s: %s", provider, exc)
        flash(str(exc), "danger")
        return redirect(url_for("auth.login"))


@bp.route("/oauth/<provider>/callback")
def oauth_callback(provider: str):
    configured = _provider_or_404(provider)

    # The provider reports user-facing refusals here rather than by failing.
    error = request.args.get("error")
    if error:
        description = request.args.get("error_description") or error
        log.info("OAuth provider %s returned an error: %s", provider, description)
        flash(f"{configured.display_name} refused the sign-in: {description}", "danger")
        audit.record("login.oauth_error", target=provider, detail=description, success=False)
        return redirect(url_for("auth.login"))

    try:
        claim = oauth_auth.complete_login(configured)
        user = resolve_identity(claim, configured.roles)
    except oauth_auth.OAuthError as exc:
        flash(str(exc), "danger")
        audit.record("login.oauth_error", target=provider, detail=str(exc), success=False)
        return redirect(url_for("auth.login"))
    except ProvisioningError as exc:
        flash(str(exc), "danger")
        audit.record("login.denied", target=provider, detail=str(exc), success=False)
        return redirect(url_for("auth.login"))

    login_user(user)
    audit.record("login.success", target=user.username, detail=f"oauth:{provider}", actor=user)
    log.info("user %s signed in via OAuth provider %s", user.username, provider)

    next_url = session.pop("oauth_next", "")
    return redirect(redirect_target() if not next_url else _safe_next(next_url))


def _safe_next(candidate: str) -> str:
    """Where to send the browser after an external sign-in.

    The OAuth "next" value and the SAML RelayState both come back through the
    identity provider, so neither is trustworthy. same_origin_path returns a
    rebuilt site-relative path or nothing at all, so an absolute URL cannot
    survive this call.
    """
    from ..security import same_origin_path

    return same_origin_path(candidate) or url_for("dashboard.index")


# ---------------------------------------------------------------------------
# SAML 2.0
# ---------------------------------------------------------------------------


def _saml_config():
    config = effective_auth_config().saml
    if not config.enabled:
        abort(404)
    return config


@bp.route("/saml/login")
def saml_login():
    config = _saml_config()
    try:
        return redirect(saml_auth.start_login(config, request, request.args.get("next") or None))
    except saml_auth.SamlError as exc:
        log.error("could not start SAML login: %s", exc)
        flash(str(exc), "danger")
        return redirect(url_for("auth.login"))


@bp.route("/saml/acs", methods=["POST"])
def saml_acs():
    """Assertion consumer service -- where the IdP POSTs the signed assertion.

    CSRF-exempt (see create_app): this is a cross-site POST by design, and the
    assertion's XML signature is what authenticates it.
    """
    config = _saml_config()
    try:
        claim = saml_auth.complete_login(config, request)
        user = resolve_identity(claim, config.roles)
    except saml_auth.SamlError as exc:
        log.warning("SAML sign-in failed: %s", exc)
        flash(str(exc), "danger")
        audit.record("login.saml_error", target="saml", detail=str(exc), success=False)
        return redirect(url_for("auth.login"))
    except ProvisioningError as exc:
        flash(str(exc), "danger")
        audit.record("login.denied", target="saml", detail=str(exc), success=False)
        return redirect(url_for("auth.login"))

    login_user(user)
    audit.record("login.success", target=user.username, detail="saml", actor=user)
    log.info("user %s signed in via SAML", user.username)

    relay_state = request.form.get("RelayState") or ""
    return redirect(_safe_next(relay_state) if relay_state else url_for("dashboard.index"))


@bp.route("/saml/sls", methods=["GET", "POST"])
def saml_sls():
    """Single logout service."""
    _saml_config()
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/saml/metadata")
def saml_metadata():
    """SP metadata, for handing to the identity provider."""
    config = _saml_config()
    try:
        xml = saml_auth.metadata_xml(config)
    except saml_auth.SamlError as exc:
        # /auth/saml/metadata is unauthenticated by design -- the IdP fetches
        # it. The exception text can name internal hosts, certificate paths and
        # configuration, so it goes to the log and the caller gets nothing.
        log.error("could not build SP metadata: %s", exc)
        return Response(
            "SAML metadata is unavailable. See the server log for the reason.",
            status=500,
            mimetype="text/plain",
        )
    return Response(xml, mimetype="text/xml")
