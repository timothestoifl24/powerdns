"""Zone and record management.

Every change goes through the PowerDNS API. Access is checked per zone: an
operator or admin sees all of them, everyone else only the zones granted to
them on the user administration page.
"""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import audit
from ..dnsutil import validate_rrset, validate_ttl
from ..pdns import (
    PdnsError,
    absolute_name,
    canonical,
    client_from_config,
    relative_name,
)
from ..recursor import RecursorNotConfigured, local_zone_target
from ..recursor import client_from_config as recursor_client_from_config
from ..recursor import is_configured as recursor_is_configured
from ..security import (
    current_user,
    flash_errors,
    login_required,
    operator_required,
    require_zone_access,
)

log = logging.getLogger(__name__)

bp = Blueprint("zones", __name__, url_prefix="/zones")

#: Record sets the UI does not let you edit by hand. PowerDNS maintains these.
MANAGED_TYPES = frozenset({"RRSIG", "NSEC", "NSEC3", "NSEC3PARAM", "DNSKEY", "CDS", "CDNSKEY"})


def _client():
    return client_from_config(current_app.config)


def _forward_locally(zone_name: str, *, remove: bool = False) -> None:
    """Keep the recursor's rule for one local zone in step.

    The recursor is the front door, so a zone the authoritative server has just
    started (or stopped) hosting is answered from the public internet until it
    learns about the change. Doing it here means a new zone resolves at once
    rather than at the next visit to the Forwarding page.

    Best effort on purpose: forwarding not being configured, or the recursor
    being down, must not fail a zone operation that already succeeded.
    """
    config = current_app.config
    if not recursor_is_configured(config):
        return
    try:
        client = recursor_client_from_config(config)
        target = local_zone_target(config)
        if remove:
            existing = client.get_forward_zone(zone_name)
            # Only ours to remove. A rule pointing elsewhere is an operator's
            # deliberate configuration that happens to share the name.
            if existing is None or tuple(existing.servers) != (target,):
                return
            client.delete_forward_zone(zone_name)
        else:
            client.save_forward_zone(zone_name, [target], recursion_desired=False)
    except (PdnsError, RecursorNotConfigured, ValueError) as exc:
        log.warning("could not update recursor forwarding for %s: %s", zone_name, exc)
        flash(
            f"The zone was saved, but the resolver could not be told about it: {exc} "
            "Open Forwarding to retry.",
            "warning",
        )


def _handle_pdns_error(exc: PdnsError, action: str):
    """Turn an API failure into a flash message and a sensible redirect."""
    log.error("PowerDNS API error during %s: %s", action, exc)
    if exc.is_not_found:
        abort(404)
    flash(str(exc), "danger")
    return None


@bp.route("/")
@login_required
def index():
    user = current_user()
    assert user is not None

    query = (request.args.get("q") or "").strip().lower()
    try:
        zones = _client().list_zones()
    except PdnsError as exc:
        log.error("could not list zones: %s", exc)
        flash(str(exc), "danger")
        zones = []

    visible = [zone for zone in zones if user.can_see_zone(zone.get("name", ""))]
    if query:
        visible = [zone for zone in visible if query in zone.get("name", "").lower()]

    return render_template("zones/index.html", zones=visible, query=query)


@bp.route("/new", methods=["GET", "POST"])
@operator_required
def create():
    user = current_user()
    defaults = current_app.config["DEFAULT_NAMESERVERS"]

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        kind = (request.form.get("kind") or "Native").strip().title()
        nameservers = [
            line.strip()
            for line in (request.form.get("nameservers") or "").replace(",", "\n").splitlines()
            if line.strip()
        ]
        masters = [
            line.strip()
            for line in (request.form.get("masters") or "").replace(",", "\n").splitlines()
            if line.strip()
        ]
        dnssec = request.form.get("dnssec") == "on"

        problems: list[str] = []
        if not name:
            problems.append("Enter a zone name.")
        if kind not in ("Native", "Master", "Slave"):
            problems.append("Choose a valid zone kind.")
        if kind == "Slave" and not masters:
            problems.append("A slave zone needs at least one master address.")
        if kind != "Slave" and not nameservers:
            problems.append("Add at least one nameserver, or the zone will not resolve.")

        if problems:
            flash_errors(problems)
            return (
                render_template(
                    "zones/new.html",
                    default_nameservers=defaults,
                    form=request.form,
                ),
                400,
            )

        try:
            zone = _client().create_zone(
                name=name,
                kind=kind,
                nameservers=nameservers,
                masters=masters,
                soa_edit_api=current_app.config["DEFAULT_SOA_EDIT_API"],
                dnssec=dnssec,
            )
        except PdnsError as exc:
            log.error("could not create zone %s: %s", name, exc)
            flash(str(exc), "danger")
            audit.record("zone.create", target=name, detail=str(exc), actor=user, success=False)
            return (
                render_template("zones/new.html", default_nameservers=defaults, form=request.form),
                400,
            )

        audit.record(
            "zone.create",
            target=zone.get("name", name),
            detail=f"kind={kind} dnssec={dnssec}",
            actor=user,
        )
        _forward_locally(zone.get("name") or canonical(name))
        flash(f"Zone {zone.get('name', name)} has been created.", "success")
        return redirect(url_for("zones.detail", zone_id=zone.get("id") or canonical(name)))

    return render_template("zones/new.html", default_nameservers=defaults, form={})


@bp.route("/<path:zone_id>")
@login_required
def detail(zone_id: str):
    zone_name = canonical(zone_id)
    require_zone_access(zone_name)

    try:
        zone = _client().get_zone(zone_id)
    except PdnsError as exc:
        result = _handle_pdns_error(exc, f"loading zone {zone_id}")
        if result is None:
            return redirect(url_for("zones.index"))
        return result

    rrsets = sorted(
        (rrset for rrset in zone.get("rrsets", [])),
        # SOA and NS first, then alphabetically by name -- the order an
        # operator reads a zone file in.
        key=lambda rrset: (
            0 if rrset.get("type") == "SOA" else 1 if rrset.get("type") == "NS" else 2,
            rrset.get("name", ""),
            rrset.get("type", ""),
        ),
    )
    editable = [rrset for rrset in rrsets if rrset.get("type") not in MANAGED_TYPES]
    managed = [rrset for rrset in rrsets if rrset.get("type") in MANAGED_TYPES]

    return render_template(
        "zones/detail.html",
        zone=zone,
        zone_name=zone.get("name", zone_name),
        rrsets=editable,
        managed_rrsets=managed,
        default_ttl=current_app.config["DEFAULT_TTL"],
    )


@bp.route("/<path:zone_id>/records", methods=["POST"])
@login_required
def save_record(zone_id: str):
    """Create or replace one record set."""
    zone_name = canonical(zone_id)
    user = require_zone_access(zone_name)

    rtype = (request.form.get("type") or "").upper().strip()
    raw_name = request.form.get("name") or "@"
    name = absolute_name(raw_name, zone_name)
    contents = [
        line.strip() for line in (request.form.get("content") or "").splitlines() if line.strip()
    ]
    disabled = request.form.get("disabled") == "on"
    comment = (request.form.get("comment") or "").strip()[:512]
    # The name/type pair being replaced, when the operator renamed a record.
    original_name = request.form.get("original_name") or ""
    original_type = (request.form.get("original_type") or "").upper().strip()

    if rtype in MANAGED_TYPES:
        flash(f"{rtype} records are maintained by PowerDNS and cannot be edited here.", "warning")
        return redirect(url_for("zones.detail", zone_id=zone_id))

    ttl, ttl_error = validate_ttl(request.form.get("ttl") or current_app.config["DEFAULT_TTL"])
    problems = [ttl_error] if ttl_error else []

    client = _client()

    # Existing types at this name, so CNAME conflicts are caught before the API
    # rejects them with a less helpful message.
    existing_types: set[str] = set()
    try:
        for rrset in client.zone_rrsets(zone_id):
            if canonical(rrset.name) == name and rrset.type != rtype:
                existing_types.add(rrset.type)
    except PdnsError as exc:
        log.warning("could not pre-check zone %s: %s", zone_id, exc)

    problems.extend(validate_rrset(name, rtype, contents, zone_name, existing_types))

    if problems:
        flash_errors(problems)
        return redirect(url_for("zones.detail", zone_id=zone_id))

    try:
        # A rename is a delete of the old set plus a write of the new one;
        # PowerDNS has no rename operation.
        if original_name and original_type:
            original_absolute = absolute_name(original_name, zone_name)
            if (original_absolute, original_type) != (name, rtype):
                client.delete_rrset(zone_id, original_absolute, original_type)

        client.replace_rrset(
            zone_id,
            name=name,
            rtype=rtype,
            ttl=ttl,
            contents=contents,
            disabled=disabled,
            comment=comment,
            account=user.username,
        )
    except PdnsError as exc:
        log.error("could not save %s %s in %s: %s", rtype, name, zone_id, exc)
        flash(str(exc), "danger")
        audit.record(
            "record.save",
            target=f"{name} {rtype}",
            detail=str(exc),
            actor=user,
            success=False,
        )
        return redirect(url_for("zones.detail", zone_id=zone_id))

    audit.record(
        "record.save",
        target=f"{name} {rtype}",
        detail=f"ttl={ttl} records={len(contents)}" + (" disabled" if disabled else ""),
        actor=user,
    )
    flash(f"{relative_name(name, zone_name)} {rtype} has been saved.", "success")
    return redirect(url_for("zones.detail", zone_id=zone_id))


@bp.route("/<path:zone_id>/records/delete", methods=["POST"])
@login_required
def delete_record(zone_id: str):
    zone_name = canonical(zone_id)
    user = require_zone_access(zone_name)

    rtype = (request.form.get("type") or "").upper().strip()
    name = absolute_name(request.form.get("name") or "", zone_name)

    if rtype == "SOA":
        flash("The SOA record cannot be deleted; edit it instead.", "warning")
        return redirect(url_for("zones.detail", zone_id=zone_id))
    if rtype in MANAGED_TYPES:
        flash(f"{rtype} records are maintained by PowerDNS.", "warning")
        return redirect(url_for("zones.detail", zone_id=zone_id))

    try:
        _client().delete_rrset(zone_id, name, rtype)
    except PdnsError as exc:
        log.error("could not delete %s %s from %s: %s", rtype, name, zone_id, exc)
        flash(str(exc), "danger")
        audit.record(
            "record.delete", target=f"{name} {rtype}", detail=str(exc), actor=user, success=False
        )
        return redirect(url_for("zones.detail", zone_id=zone_id))

    audit.record("record.delete", target=f"{name} {rtype}", actor=user)
    flash(f"{relative_name(name, zone_name)} {rtype} has been deleted.", "success")
    return redirect(url_for("zones.detail", zone_id=zone_id))


@bp.route("/<path:zone_id>/delete", methods=["POST"])
@operator_required
def delete(zone_id: str):
    user = current_user()
    zone_name = canonical(zone_id)

    # Deleting a zone removes every record in it, so require the operator to
    # type the name rather than trusting a single click.
    confirmation = canonical(request.form.get("confirm") or "")
    if confirmation != zone_name:
        flash("Type the zone name exactly to confirm deletion.", "danger")
        return redirect(url_for("zones.detail", zone_id=zone_id))

    try:
        _client().delete_zone(zone_id)
    except PdnsError as exc:
        log.error("could not delete zone %s: %s", zone_id, exc)
        flash(str(exc), "danger")
        audit.record("zone.delete", target=zone_name, detail=str(exc), actor=user, success=False)
        return redirect(url_for("zones.detail", zone_id=zone_id))

    audit.record("zone.delete", target=zone_name, actor=user)
    _forward_locally(zone_name, remove=True)
    flash(f"Zone {zone_name} and all of its records have been deleted.", "success")
    return redirect(url_for("zones.index"))


@bp.route("/<path:zone_id>/notify", methods=["POST"])
@operator_required
def notify(zone_id: str):
    user = current_user()
    try:
        _client().notify_zone(zone_id)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("zones.detail", zone_id=zone_id))
    audit.record("zone.notify", target=canonical(zone_id), actor=user)
    flash("Notifications have been sent to the secondaries.", "success")
    return redirect(url_for("zones.detail", zone_id=zone_id))


@bp.route("/<path:zone_id>/retrieve", methods=["POST"])
@operator_required
def retrieve(zone_id: str):
    user = current_user()
    try:
        _client().retrieve_zone(zone_id)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("zones.detail", zone_id=zone_id))
    audit.record("zone.retrieve", target=canonical(zone_id), actor=user)
    flash("A transfer from the master has been requested.", "success")
    return redirect(url_for("zones.detail", zone_id=zone_id))


@bp.route("/<path:zone_id>/export")
@login_required
def export(zone_id: str):
    zone_name = canonical(zone_id)
    require_zone_access(zone_name)
    try:
        content = _client().export_zone(zone_id)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("zones.detail", zone_id=zone_id))

    filename = zone_name.rstrip(".") or "zone"
    return Response(
        content,
        mimetype="text/dns",
        headers={"Content-Disposition": f'attachment; filename="{filename}.zone"'},
    )


@bp.route("/<path:zone_id>/dnssec", methods=["GET", "POST"])
@operator_required
def dnssec(zone_id: str):
    user = current_user()
    zone_name = canonical(zone_id)
    client = _client()

    if request.method == "POST":
        action = request.form.get("action") or ""
        try:
            if action == "enable":
                client.set_zone_dnssec(zone_id, True)
                if not client.cryptokeys(zone_id):
                    # A signed zone with no key serves broken answers; create a
                    # combined signing key so enabling it is a single step.
                    client.create_cryptokey(zone_id, keytype="csk", active=True)
                audit.record("zone.dnssec_enable", target=zone_name, actor=user)
                flash("DNSSEC has been enabled and a signing key created.", "success")
            elif action == "disable":
                for key in client.cryptokeys(zone_id):
                    client.delete_cryptokey(zone_id, key["id"])
                client.set_zone_dnssec(zone_id, False)
                audit.record("zone.dnssec_disable", target=zone_name, actor=user)
                flash("DNSSEC has been disabled and the signing keys removed.", "warning")
            else:
                flash("Unknown action.", "danger")
        except PdnsError as exc:
            log.error("DNSSEC change failed for %s: %s", zone_id, exc)
            flash(str(exc), "danger")
            audit.record(
                f"zone.dnssec_{action}",
                target=zone_name,
                detail=str(exc),
                actor=user,
                success=False,
            )
        return redirect(url_for("zones.dnssec", zone_id=zone_id))

    try:
        zone = client.get_zone(zone_id)
        keys = client.cryptokeys(zone_id)
    except PdnsError as exc:
        result = _handle_pdns_error(exc, f"loading DNSSEC state for {zone_id}")
        if result is None:
            return redirect(url_for("zones.index"))
        return result

    return render_template("zones/dnssec.html", zone=zone, zone_name=zone_name, keys=keys)
