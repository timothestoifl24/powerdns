"""Administration of external identity providers.

Everything here is admin-only and audited. Secrets are write-only from the
browser's point of view: they are never rendered back into a form, and an
empty secret field on save means "leave the stored value alone" rather than
"clear it", so editing a provider's URL cannot silently wipe its password.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..auth.store import (
    KIND_LABELS,
    KINDS,
    ProviderConfigError,
    build,
    provider_problem,
)
from ..config import AUTH_LDAP, AUTH_OAUTH, AUTH_SAML, ROLES
from ..crypto import SecretDecryptionError, encrypt
from ..database import get_session
from ..models import AuthProviderConfig
from ..security import admin_required

log = logging.getLogger(__name__)

bp = Blueprint("authproviders", __name__, url_prefix="/admin/auth")

#: Which secret fields belong to which kind. Used to decide what to encrypt
#: and what to leave untouched when the field comes back empty.
SECRET_FIELDS = {
    AUTH_LDAP: ("bind_password",),
    AUTH_OAUTH: ("client_secret",),
    AUTH_SAML: ("idp_x509_cert", "sp_x509_cert", "sp_private_key"),
}

#: Settings accepted per kind. Anything not listed is ignored, so a crafted
#: form cannot inject unexpected keys into the stored JSON.
SETTING_FIELDS = {
    AUTH_LDAP: (
        "uri",
        "base_dn",
        "bind_dn",
        "user_filter",
        "username_attribute",
        "email_attribute",
        "display_name_attribute",
        "group_attribute",
        "group_search_base",
        "group_filter",
        "ca_cert_file",
    ),
    AUTH_OAUTH: (
        "discovery_url",
        "authorize_url",
        "token_url",
        "userinfo_url",
        "jwks_url",
        "client_id",
        "scopes",
        "username_claim",
        "email_claim",
        "name_claim",
        "groups_claim",
        "icon",
    ),
    AUTH_SAML: (
        "idp_metadata_url",
        "idp_entity_id",
        "idp_sso_url",
        "idp_slo_url",
        "sp_entity_id",
        "name_id_format",
        "attr_username",
        "attr_email",
        "attr_name",
        "attr_groups",
    ),
}

BOOLEAN_FIELDS = {
    AUTH_LDAP: ("start_tls", "tls_verify"),
    AUTH_OAUTH: (),
    AUTH_SAML: (
        "want_assertions_signed",
        "want_messages_signed",
        "want_name_id_encrypted",
        "strict",
    ),
}

#: Shared by every kind.
ROLE_FIELDS = ("admin_group", "operator_group", "user_group", "default_role")


def _slug(value: str) -> str:
    """Normalise a provider name to something safe in a URL and a filename."""
    cleaned = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in (value or "").strip().lower()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")


def _get_or_404(provider_id: int) -> AuthProviderConfig:
    row = get_session().get(AuthProviderConfig, provider_id)
    if row is None:
        abort(404)
    return row


@bp.route("/")
@admin_required
def index():
    from ..auth.store import all_providers

    rows = all_providers()
    problems = {row.id: provider_problem(row) for row in rows}
    env = current_app.config["AUTH"]
    return render_template(
        "admin/auth_providers.html",
        providers=rows,
        problems=problems,
        kind_labels=KIND_LABELS,
        env=env,
    )


@bp.route("/new/<kind>", methods=["GET", "POST"])
@admin_required
def create(kind: str):
    if kind not in KINDS:
        abort(404)
    row = AuthProviderConfig(kind=kind, name="", display_name="", enabled=True)
    if request.method == "POST":
        return _save(row, creating=True)
    return render_template(
        "admin/auth_provider_form.html",
        provider=row,
        kind=kind,
        kind_labels=KIND_LABELS,
        roles=ROLES,
        creating=True,
    )


@bp.route("/<int:provider_id>", methods=["GET", "POST"])
@admin_required
def edit(provider_id: int):
    row = _get_or_404(provider_id)
    if request.method == "POST":
        return _save(row, creating=False)
    return render_template(
        "admin/auth_provider_form.html",
        provider=row,
        kind=row.kind,
        kind_labels=KIND_LABELS,
        roles=ROLES,
        creating=False,
        problem=provider_problem(row),
    )


def _save(row: AuthProviderConfig, *, creating: bool):
    kind = row.kind
    session = get_session()

    name = _slug(request.form.get("name", ""))
    if not name:
        flash("A provider needs a name.", "danger")
        return _redisplay(row, kind, creating)
    row.name = name
    row.display_name = (request.form.get("display_name") or "").strip()
    row.enabled = request.form.get("enabled") == "on"

    settings = dict(row.settings)
    for field in SETTING_FIELDS[kind]:
        settings[field] = (request.form.get(field) or "").strip()
    for field in BOOLEAN_FIELDS[kind]:
        settings[field] = request.form.get(field) == "on"
    for field in ROLE_FIELDS:
        settings[field] = (request.form.get(field) or "").strip()
    if settings.get("default_role") not in (*ROLES, "none", ""):
        flash("Choose a valid default role.", "danger")
        return _redisplay(row, kind, creating)
    row.settings = settings

    # An empty secret field means "unchanged". That is what makes it safe to
    # edit a URL without having the password to hand.
    secrets = dict(row.secrets)
    secret_key = current_app.config["SECRET_KEY"]
    for field in SECRET_FIELDS[kind]:
        submitted = request.form.get(field)
        if submitted:
            secrets[field] = encrypt(submitted, secret_key)
        elif request.form.get(f"clear_{field}") == "on":
            secrets.pop(field, None)
    row.secrets = secrets

    # Refuse to store something that cannot work, with the reason, rather than
    # accepting it and failing at someone's next sign-in.
    try:
        build(row)
    except ProviderConfigError as exc:
        flash(str(exc), "danger")
        return _redisplay(row, kind, creating)
    except SecretDecryptionError as exc:
        flash(str(exc), "danger")
        return _redisplay(row, kind, creating)

    if creating:
        session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        flash(f"A provider named {name!r} already exists.", "danger")
        return _redisplay(row, kind, creating)

    audit.record(
        "auth_provider.create" if creating else "auth_provider.update",
        target=f"{kind}:{row.name}",
        detail=f"enabled={row.enabled}",
    )
    flash(f"Provider {row.name} has been saved.", "success")
    return redirect(url_for("authproviders.index"))


def _redisplay(row: AuthProviderConfig, kind: str, creating: bool):
    return (
        render_template(
            "admin/auth_provider_form.html",
            provider=row,
            kind=kind,
            kind_labels=KIND_LABELS,
            roles=ROLES,
            creating=creating,
        ),
        400,
    )


@bp.route("/<int:provider_id>/toggle", methods=["POST"])
@admin_required
def toggle(provider_id: int):
    row = _get_or_404(provider_id)
    row.enabled = not row.enabled
    get_session().commit()
    audit.record(
        "auth_provider.enable" if row.enabled else "auth_provider.disable",
        target=f"{row.kind}:{row.name}",
    )
    flash(
        f"Provider {row.name} has been {'enabled' if row.enabled else 'disabled'}.",
        "success",
    )
    return redirect(url_for("authproviders.index"))


@bp.route("/<int:provider_id>/delete", methods=["POST"])
@admin_required
def delete(provider_id: int):
    row = _get_or_404(provider_id)
    session = get_session()
    name, kind = row.name, row.kind
    session.delete(row)
    session.commit()
    audit.record("auth_provider.delete", target=f"{kind}:{name}")
    flash(f"Provider {name} has been deleted.", "success")
    return redirect(url_for("authproviders.index"))


@bp.route("/<int:provider_id>/test", methods=["POST"])
@admin_required
def test(provider_id: int):
    """Check the provider answers, without needing someone to attempt a login."""
    row = _get_or_404(provider_id)
    try:
        config = build(row)
    except (ProviderConfigError, SecretDecryptionError) as exc:
        flash(f"{row.name}: {exc}", "danger")
        return redirect(url_for("authproviders.index"))

    try:
        message = _probe(row.kind, config)
    except Exception as exc:  # noqa: BLE001 - the point is to report any failure
        log.info("provider test failed for %s: %s", row.name, exc)
        audit.record(
            "auth_provider.test",
            target=f"{row.kind}:{row.name}",
            success=False,
            detail=str(exc)[:400],
        )
        flash(f"{row.name}: {exc}", "danger")
        return redirect(url_for("authproviders.index"))

    audit.record("auth_provider.test", target=f"{row.kind}:{row.name}", detail=message[:400])
    flash(f"{row.name}: {message}", "success")
    return redirect(url_for("authproviders.index"))


#: Addresses the Test button must never reach. Cloud instance-metadata
#: services live on 169.254.169.254 and hand out credentials to anything that
#: asks, so link-local is the range that actually matters here. Loopback would
#: expose services bound only to the container itself.
#:
#: Private ranges are deliberately NOT blocked: an internal Keycloak or Active
#: Directory on 10.x or 192.168.x is the normal case for this application, and
#: refusing them would break the deployments most likely to use it.
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, incl. cloud metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("0.0.0.0/8"),  # "this host"
)


def _check_fetchable(url: str) -> str:
    """Validate a URL the panel is about to fetch on the server's behalf.

    The Test button turns an operator-supplied URL into a server-side request,
    which is a request-forgery primitive even though only administrators can
    reach it: the response and any error are reflected back to the browser.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError(
            f"Only http and https URLs can be tested, not {parsed.scheme or 'a relative URL'!r}."
        )
    host = parsed.hostname
    if not host:
        raise ProviderConfigError("That URL has no host.")

    try:
        resolved = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ProviderConfigError(f"{host} could not be resolved: {exc}") from exc

    for family, _type, _proto, _canon, sockaddr in resolved:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if address.version == network.version and address in network:
                raise ProviderConfigError(
                    f"{host} resolves to {address}, which the panel will not fetch "
                    "(link-local and loopback addresses are blocked)."
                )
    return url


def _probe(kind: str, config) -> str:
    """Reach the provider and report what came back."""
    if kind == AUTH_LDAP:
        from ..auth.ldap_auth import test_connection

        return test_connection(config)
    if kind == AUTH_OAUTH:
        import requests

        url = _check_fetchable(config.discovery_url or config.authorize_url)
        # Redirects are not followed: a permitted host could otherwise redirect
        # to a blocked one, and re-validating every hop is more machinery than
        # a test button warrants. A provider that redirects its discovery
        # document is unusual, and the message below says what to do about it.
        response = requests.get(url, timeout=10, allow_redirects=False)
        if response.is_redirect:
            raise ProviderConfigError(
                f"that URL redirects to {response.headers.get('Location', 'elsewhere')} -- "
                "configure the final URL instead"
            )
        response.raise_for_status()
        if config.discovery_url:
            payload = response.json()
            issuer = payload.get("issuer", "?")
            return f"discovery document read, issuer {issuer}"
        return f"authorize endpoint reachable (HTTP {response.status_code})"
    if kind == AUTH_SAML:
        if not config.idp_metadata_url:
            return "configuration is valid (no metadata URL to fetch)"
        import requests

        url = _check_fetchable(config.idp_metadata_url)
        response = requests.get(url, timeout=10, allow_redirects=False)
        if response.is_redirect:
            raise ProviderConfigError(
                f"that URL redirects to {response.headers.get('Location', 'elsewhere')} -- "
                "configure the final URL instead"
            )
        response.raise_for_status()
        if "EntityDescriptor" not in response.text:
            raise ProviderConfigError("that URL did not return SAML metadata")
        return "IdP metadata fetched and looks like SAML"
    raise ProviderConfigError(f"Unknown provider kind {kind!r}.")
