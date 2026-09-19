"""Zone and record management.

Every change goes through the PowerDNS API. Access is checked per zone: an
operator or admin sees all of them, everyone else only the zones granted to
them on the user administration page.

Reverse DNS is handled alongside the forward side rather than as a separate
chore: a zone can be created together with the reverse zones for its networks,
and an A/AAAA record can own a PTR that follows it for the rest of its life.
The arithmetic and the bookkeeping for that live in :mod:`app.reverse`.
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

from .. import audit, reverse
from ..dnsutil import (
    SOA_DEFAULTS,
    Soa,
    email_to_rname,
    parse_soa,
    validate_content,
    validate_email,
    validate_name,
    validate_rrset,
    validate_ttl,
)
from ..pdns import (
    ZONE_KINDS,
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


def _create_reverse_zone(name: str, *, forward: str, user, **zone_options) -> None:
    """Create one reverse zone beside the forward zone just created.

    The forward zone already exists by the time this runs, so a failure here
    is reported and nothing is rolled back: an operator who has to retry wants
    the zone that did work to still be there. The reverse zone inherits the
    forward zone's kind, nameservers and DNSSEC setting, because a reverse zone
    served differently from the zone it belongs to is almost always a mistake.
    """
    try:
        zone = _client().create_zone(name=name, **zone_options)
    except PdnsError as exc:
        log.error("could not create reverse zone %s: %s", name, exc)
        audit.record("zone.create_reverse", target=name, detail=str(exc), actor=user, success=False)
        flash(f"The reverse zone {name.rstrip('.')} could not be created: {exc}", "warning")
        return

    created = zone.get("name", canonical(name))
    audit.record("zone.create_reverse", target=created, detail=f"for {forward}", actor=user)
    _forward_locally(created)
    flash(f"Reverse zone {created} has been created.", "success")


def _sync_reverse(client, user, *, zone, name, rtype, ttl, contents, enabled, previous=None):
    """Bring the PTR side of a forward record up to date, and say what changed.

    ``previous`` is the (name, type) pair a rename replaced: its PTRs are
    retired first, otherwise a record renamed from ``old`` to ``new`` would
    leave ``old``'s PTR behind pointing at a name that no longer exists.

    Never raises. The forward record has already been written by the time this
    runs, and a reverse zone being unreachable is not a reason to report the
    save as failed -- it is a reason to say what did not happen.
    """
    try:
        if previous is not None and previous != (canonical(name), rtype):
            stale = reverse.sync_record(
                client,
                user,
                zone=zone,
                name=previous[0],
                rtype=previous[1],
                ttl=ttl,
                addresses=[],
                enabled=False,
            )
            for message, category in stale.messages():
                flash(message, category)

        result = reverse.sync_record(
            client,
            user,
            zone=zone,
            name=name,
            rtype=rtype,
            ttl=ttl,
            addresses=contents,
            enabled=enabled,
        )
    except PdnsError as exc:
        log.error("reverse sync failed for %s %s: %s", name, rtype, exc)
        flash(f"The reverse record could not be updated: {exc}", "warning")
        return

    for message, category in result.messages():
        flash(message, category)
    if result.changed:
        audit.record(
            "record.reverse_sync",
            target=f"{canonical(name)} {rtype}",
            detail=(
                f"written={len(result.written)} removed={len(result.removed)}"
                + (" stolen=" + ",".join(result.stolen) if result.stolen else "")
            ),
            actor=user,
        )


def _unlink_edited_ptr(name: str, contents: list[str]) -> None:
    """Hand a linked PTR back when an operator edits it in the reverse zone.

    The panel only owns a PTR for as long as it answers with the forward name
    it was created from. Once someone points it somewhere else by hand, that
    is their record: the link goes, both records stay.
    """
    link = reverse.link_for_ptr(name)
    if link is None or [canonical(content) for content in contents] == [link.forward_name]:
        return
    reverse.forget_ptr(name)
    flash(
        f"{name.rstrip('.')} is no longer linked to {link.forward_name.rstrip('.')}: "
        "it now answers with something else, so the panel will leave it alone.",
        "info",
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
        with_reverse = request.form.get("create_reverse") == "on"

        problems: list[str] = []
        if not name:
            problems.append("Enter a zone name.")
        if kind not in ("Native", "Master", "Slave"):
            problems.append("Choose a valid zone kind.")
        if kind == "Slave" and not masters:
            problems.append("A slave zone needs at least one master address.")
        if kind != "Slave" and not nameservers:
            problems.append("Add at least one nameserver, or the zone will not resolve.")

        # Worked out before anything is created, so a mistyped network is a
        # form error rather than a forward zone with no reverse beside it.
        reverse_zones: list[str] = []
        if with_reverse:
            reverse_zones, reverse_problems = reverse.reverse_zones_for_networks(
                request.form.get("reverse_networks") or ""
            )
            problems.extend(reverse_problems)
            if not reverse_zones and not reverse_problems:
                problems.append(
                    "Enter the network the reverse zone is for, for example 192.0.2.0/24."
                )

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

        for reverse_zone in reverse_zones:
            _create_reverse_zone(
                reverse_zone,
                forward=zone.get("name", canonical(name)),
                user=user,
                kind=kind,
                nameservers=nameservers,
                masters=masters,
                soa_edit_api=current_app.config["DEFAULT_SOA_EDIT_API"],
                dnssec=dnssec,
            )

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

    # Which record sets on this page take part in a link, so the page can show
    # it and the editor can open with the box already ticked. Both directions
    # are looked up: a zone can hold forward records, PTRs, or both.
    links = reverse.links_for_zone(zone.get("name", zone_name))
    linked_forward = {(link.forward_name, link.forward_type) for link in links}
    linked_ptrs = {
        link.ptr_name: link.forward_name for link in links if link.reverse_zone == zone_name
    }
    # The zone-level pairing, which decides whether the record editor offers a
    # reverse record by default and which zone it names.
    reverse_zones = reverse.linked_reverse_zones(zone_name)

    return render_template(
        "zones/detail.html",
        zone=zone,
        zone_name=zone.get("name", zone_name),
        rrsets=editable,
        managed_rrsets=managed,
        default_ttl=current_app.config["DEFAULT_TTL"],
        linked_forward=linked_forward,
        linked_ptrs=linked_ptrs,
        address_types=sorted(reverse.ADDRESS_TYPES),
        reverse_zones=reverse_zones,
        reverse_sources=reverse.linked_forward_zones(zone_name),
    )


@bp.route("/<path:zone_id>/settings", methods=["GET", "POST"])
@login_required
def settings(zone_id: str):
    """The zone's own configuration, as opposed to the records in it.

    Everything here is stored by PowerDNS as a record or a zone property --
    nameservers are the apex NS set, the administrator's address is the SOA's
    RNAME field -- so this page is a friendlier way of editing things that are
    otherwise hand-written into a record's content. The exception is the list
    of reverse zones, which is the panel's own pairing.
    """
    zone_name = canonical(zone_id)
    user = require_zone_access(zone_name)
    client = _client()

    try:
        zone = client.get_zone(zone_id)
    except PdnsError as exc:
        result = _handle_pdns_error(exc, f"loading zone {zone_id}")
        if result is None:
            return redirect(url_for("zones.index"))
        return result

    zone_name = zone.get("name", zone_name)
    is_slave = (zone.get("kind") or "").lower() == "slave"

    if request.method == "POST":
        return _save_settings(client, user, zone, zone_id, is_slave=is_slave)

    return render_template(
        "zones/settings.html",
        zone=zone,
        zone_name=zone_name,
        is_slave=is_slave,
        form=_settings_form(zone, zone_name),
        reverse_zones=_selectable_reverse_zones(client, user, zone_name),
        linked_reverse=reverse.linked_reverse_zones(zone_name),
        linked_forward=reverse.linked_forward_zones(zone_name),
        is_reverse=reverse.is_reverse_zone(zone_name),
        default_ttl=current_app.config["DEFAULT_TTL"],
    )


def _apex(zone: dict, zone_name: str, rtype: str) -> dict | None:
    """The apex record set of one type, as the API returned it."""
    for rrset in zone.get("rrsets", []):
        if canonical(rrset.get("name", "")) == canonical(zone_name) and rrset.get("type") == rtype:
            return rrset
    return None


def _settings_form(zone: dict, zone_name: str) -> dict:
    """The current settings, in the shape the form renders."""
    soa_rrset = _apex(zone, zone_name, "SOA")
    records = (soa_rrset or {}).get("records") or []
    soa = parse_soa(records[0].get("content", "")) if records else None
    ns_rrset = _apex(zone, zone_name, "NS")

    form = {
        "kind": zone.get("kind", "Native"),
        "masters": "\n".join(zone.get("masters") or []),
        "nameservers": "\n".join(
            record.get("content", "") for record in (ns_rrset or {}).get("records") or []
        ),
        "ns_ttl": (ns_rrset or {}).get("ttl", ""),
        "soa_ttl": (soa_rrset or {}).get("ttl", ""),
    }
    if soa is not None:
        form.update(
            {
                "mname": soa.mname,
                "email": soa.email,
                "refresh": soa.refresh,
                "retry": soa.retry,
                "expire": soa.expire,
                "minimum": soa.minimum,
            }
        )
    else:
        form.update(SOA_DEFAULTS)
    return form


def _selectable_reverse_zones(client, user, zone_name: str) -> list[str]:
    """Reverse zones this user could pair the zone with.

    A zone cannot be its own reverse zone, and a zone the user cannot see is
    not offered -- picking it would only produce a PTR they are not allowed to
    write.
    """
    try:
        zones = client.list_zones()
    except PdnsError as exc:
        log.warning("could not list zones for the settings page: %s", exc)
        return []
    return [
        name
        for name in (entry.get("name", "") for entry in zones)
        if reverse.is_reverse_zone(name)
        and canonical(name) != canonical(zone_name)
        and user.can_see_zone(name)
    ]


def _save_settings(client, user, zone: dict, zone_id: str, *, is_slave: bool):
    zone_name = zone.get("name", canonical(zone_id))
    form = request.form
    problems: list[str] = []

    # The kind and its masters decide how the zone is served rather than what
    # it answers, so they stay with the operators who can create and delete
    # zones. Everything below is editable by anyone who can edit the records,
    # because the record editor already reaches the same NS and SOA sets.
    if user.is_operator:
        kind = (form.get("kind") or zone.get("kind") or "Native").strip().title()
        masters = _lines(form.get("masters"))
    else:
        kind = (zone.get("kind") or "Native").strip().title()
        masters = list(zone.get("masters") or [])

    if kind not in ZONE_KINDS:
        problems.append("Choose a valid zone kind.")
    if kind == "Slave" and not masters:
        problems.append("A slave zone needs at least one master address.")

    # A slave's NS and SOA come from its master, so the form does not offer
    # them and a posted value is ignored rather than written over the transfer.
    editing_content = kind != "Slave"
    nameservers = _lines(form.get("nameservers")) if editing_content else []
    ns_ttl = soa_ttl = current_app.config["DEFAULT_TTL"]
    soa: Soa | None = None

    if editing_content:
        if not nameservers:
            problems.append("Add at least one nameserver, or the zone will not resolve.")
        for nameserver in nameservers:
            problem = validate_content("NS", canonical(nameserver))
            if problem:
                problems.append(problem)

        ns_ttl, error = validate_ttl(form.get("ns_ttl") or current_app.config["DEFAULT_TTL"])
        if error:
            problems.append(f"Nameserver TTL: {error}")
        soa_ttl, error = validate_ttl(form.get("soa_ttl") or current_app.config["DEFAULT_TTL"])
        if error:
            problems.append(f"SOA TTL: {error}")

        email_error = validate_email(form.get("email") or "")
        if email_error:
            problems.append(email_error)
        mname = canonical(form.get("mname") or "")
        if not mname:
            problems.append("Enter the primary nameserver for the SOA record.")
        else:
            problem = validate_name(mname)
            if problem:
                problems.append(problem)

        timers: dict[str, int] = {}
        for field, label in (
            ("refresh", "Refresh"),
            ("retry", "Retry"),
            ("expire", "Expire"),
            ("minimum", "Negative TTL"),
        ):
            value, error = validate_ttl(form.get(field) or SOA_DEFAULTS[field])
            if error:
                problems.append(f"{label}: {error}")
            timers[field] = value

        if not problems:
            existing = _apex(zone, zone_name, "SOA")
            records = (existing or {}).get("records") or []
            current = parse_soa(records[0].get("content", "")) if records else None
            soa = Soa(
                mname=mname,
                rname=email_to_rname(form.get("email") or ""),
                # PowerDNS bumps the serial itself on every change, so the
                # existing value is carried across rather than edited here.
                serial=current.serial if current else 1,
                **timers,
            )

    # Reverse zones: existing ones ticked on the form, plus any the operator
    # asked to create from a network.
    wanted_reverse = [canonical(name) for name in form.getlist("reverse_zones")]
    selectable = {canonical(name) for name in _selectable_reverse_zones(client, user, zone_name)}
    unknown = [name for name in wanted_reverse if name not in selectable]
    if unknown:
        problems.append(
            f"{', '.join(name.rstrip('.') for name in unknown)} is not a reverse zone "
            "you can use here."
        )

    new_reverse: list[str] = []
    networks = (form.get("reverse_networks") or "").strip()
    if networks:
        if not user.is_operator:
            problems.append("Only an operator can create a new reverse zone.")
        else:
            new_reverse, network_problems = reverse.reverse_zones_for_networks(networks)
            problems.extend(network_problems)

    if problems:
        flash_errors(problems)
        return (
            render_template(
                "zones/settings.html",
                zone=zone,
                zone_name=zone_name,
                is_slave=is_slave,
                form=form,
                reverse_zones=sorted(selectable),
                linked_reverse=wanted_reverse,
                linked_forward=reverse.linked_forward_zones(zone_name),
                is_reverse=reverse.is_reverse_zone(zone_name),
                default_ttl=current_app.config["DEFAULT_TTL"],
            ),
            400,
        )

    changes: list[str] = []
    try:
        if kind != zone.get("kind") or (kind == "Slave" and masters != (zone.get("masters") or [])):
            update: dict = {"kind": kind}
            if kind == "Slave":
                update["masters"] = masters
            client.update_zone(zone_id, update)
            changes.append(f"kind={kind}")

        if editing_content:
            client.replace_rrset(
                zone_id,
                name=zone_name,
                rtype="NS",
                ttl=ns_ttl,
                contents=[canonical(nameserver) for nameserver in nameservers],
                account=user.username,
            )
            changes.append(f"nameservers={len(nameservers)}")
            if soa is not None:
                client.replace_rrset(
                    zone_id,
                    name=zone_name,
                    rtype="SOA",
                    ttl=soa_ttl,
                    contents=[soa.to_content()],
                    account=user.username,
                )
                changes.append("soa")
    except PdnsError as exc:
        log.error("could not save settings for %s: %s", zone_id, exc)
        flash(str(exc), "danger")
        audit.record("zone.settings", target=zone_name, detail=str(exc), actor=user, success=False)
        return redirect(url_for("zones.settings", zone_id=zone_id))

    for name in new_reverse:
        if name in selectable:
            continue  # Already there; ticking it is enough.
        _create_reverse_zone(
            name,
            forward=zone_name,
            user=user,
            kind=kind,
            nameservers=nameservers,
            masters=masters,
            soa_edit_api=current_app.config["DEFAULT_SOA_EDIT_API"],
            dnssec=bool(zone.get("dnssec")),
        )

    added, removed = reverse.set_linked_reverse_zones(zone_name, [*wanted_reverse, *new_reverse])
    if added:
        changes.append("linked=" + ",".join(name.rstrip(".") for name in added))
    if removed:
        changes.append("unlinked=" + ",".join(name.rstrip(".") for name in removed))

    audit.record("zone.settings", target=zone_name, detail=" ".join(changes), actor=user)
    flash("The zone settings have been saved.", "success")
    return redirect(url_for("zones.settings", zone_id=zone_id))


def _lines(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").replace(",", "\n").splitlines() if line.strip()]


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
    # Whether this address record should own a PTR. Unticking it on a record
    # that has one is how the link is broken, so the value matters even when
    # it is off.
    sync_ptr = request.form.get("sync_ptr") == "on"
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

    # The (name, type) a rename leaves behind, so its PTRs can be retired
    # after the write succeeds.
    previous_pair: tuple[str, str] | None = None

    try:
        # A rename is a delete of the old set plus a write of the new one;
        # PowerDNS has no rename operation.
        if original_name and original_type:
            original_absolute = absolute_name(original_name, zone_name)
            if (original_absolute, original_type) != (name, rtype):
                client.delete_rrset(zone_id, original_absolute, original_type)
                previous_pair = (original_absolute, original_type)

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

    if rtype in reverse.ADDRESS_TYPES or previous_pair is not None:
        _sync_reverse(
            client,
            user,
            zone=zone_name,
            name=name,
            rtype=rtype,
            ttl=ttl,
            contents=contents,
            # A disabled record answers nothing, so a PTR pointing at it would
            # be a dangling answer; it is retired until the record comes back.
            enabled=sync_ptr and not disabled,
            previous=previous_pair,
        )
    if rtype == "PTR":
        _unlink_edited_ptr(name, contents)
    if previous_pair is not None and previous_pair[1] == "PTR":
        reverse.forget_ptr(previous_pair[0])

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

    client = _client()
    try:
        client.delete_rrset(zone_id, name, rtype)
    except PdnsError as exc:
        log.error("could not delete %s %s from %s: %s", rtype, name, zone_id, exc)
        flash(str(exc), "danger")
        audit.record(
            "record.delete", target=f"{name} {rtype}", detail=str(exc), actor=user, success=False
        )
        return redirect(url_for("zones.detail", zone_id=zone_id))

    audit.record("record.delete", target=f"{name} {rtype}", actor=user)
    flash(f"{relative_name(name, zone_name)} {rtype} has been deleted.", "success")

    if rtype in reverse.ADDRESS_TYPES:
        # The forward record is gone, so any PTR the panel created for it now
        # answers with a name that resolves to nothing.
        _sync_reverse(
            client,
            user,
            zone=zone_name,
            name=name,
            rtype=rtype,
            ttl=current_app.config["DEFAULT_TTL"],
            contents=[],
            enabled=False,
        )
    elif rtype == "PTR":
        reverse.forget_ptr(name)

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
    # Both sides of any link through this zone are gone with it; keeping the
    # rows would only let the panel write to records that no longer exist.
    reverse.forget_zone(zone_name)
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
