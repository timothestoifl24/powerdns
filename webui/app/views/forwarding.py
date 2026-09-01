"""Forward zones and global forwarders.

Forwarding is a Recursor feature -- the Authoritative Server has had no
``recursor=`` setting since 4.1 -- so everything here drives the recursor's HTTP
API. The recursor stores the zones itself, so the panel keeps no copy and there
is nothing that can drift out of step with what the resolver is doing.

Operator or admin only: a forward zone redirects a whole namespace, which is a
stronger power than editing records inside one zone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import audit
from ..pdns import PdnsError, canonical
from ..pdns import client_from_config as pdns_client_from_config
from ..recursor import (
    ROOT_ZONE,
    RecursorNotConfigured,
    client_from_config,
    is_configured,
    local_zone_target,
    parse_servers,
    sync_local_zones,
)
from ..security import operator_required

log = logging.getLogger(__name__)

bp = Blueprint("forwarding", __name__, url_prefix="/forwarding")


def recursor_required(view: Callable) -> Callable:
    """Operator access, and a recursor to talk to.

    Deliberately not a blueprint-wide ``before_request``: that runs before the
    view's own decorators, so the "no recursor configured" page would have been
    served to anyone who asked, signed in or not. Wrapping the view keeps
    authentication first and the explanation second.
    """

    @wraps(view)
    @operator_required
    def wrapper(*args, **kwargs):
        if not is_configured(current_app.config):
            return render_template("forwarding/unconfigured.html")
        return view(*args, **kwargs)

    return wrapper


def _sync_local_zones(recursor) -> str:
    """Reconcile the local-zone forward rules, reporting what changed.

    Runs whenever this page is opened rather than only when a zone is created:
    the authoritative zone list can change through the PowerDNS API directly,
    and a rule that is merely stale is invisible until someone queries the name
    and gets the wrong answer.

    A failure here is reported but never blocks the page -- the operator may
    well be opening it to fix the very thing that is broken.
    """
    config = current_app.config
    try:
        target = local_zone_target(config)
        names = [zone.get("name", "") for zone in pdns_client_from_config(config).list_zones()]
    except (PdnsError, RecursorNotConfigured, ValueError) as exc:
        log.warning("could not work out the local zones to forward: %s", exc)
        return ""

    try:
        result = sync_local_zones(names, recursor, target)
    except PdnsError as exc:
        log.warning("could not sync local zones to the recursor: %s", exc)
        flash(f"The local zone forwarding could not be brought up to date: {exc}", "warning")
        return ""

    if result.changed:
        audit.record("forwarding.sync", target="local zones", detail=result.summary())
    return result.summary() if result.changed else ""


@bp.route("/")
@recursor_required
def index():
    config = current_app.config
    recursor = client_from_config(config)
    reachable, zones, error = True, [], None
    try:
        zones = recursor.forward_zones()
    except PdnsError as exc:
        reachable, error = False, str(exc)
        log.error("could not list forward zones: %s", exc)

    synced = _sync_local_zones(recursor) if reachable else ""
    if reachable and synced:
        try:
            zones = recursor.forward_zones()
        except PdnsError:  # pragma: no cover - already reported above
            pass

    local_target = ""
    try:
        local_target = local_zone_target(config)
    except (RecursorNotConfigured, ValueError):
        pass

    return render_template(
        "forwarding/index.html",
        zones=[zone for zone in zones if not zone.is_global],
        global_forwarders=next((zone for zone in zones if zone.is_global), None),
        reachable=reachable,
        error=error,
        synced=synced,
        local_target=local_target,
        recursor_url=config["RECURSOR_API_URL"],
    )


@bp.route("/new", methods=["GET", "POST"])
@recursor_required
def create():
    if request.method == "POST":
        return _save(original_name="")
    return render_template("forwarding/form.html", zone=None, form={})


@bp.route("/<path:name>/edit", methods=["GET", "POST"])
@recursor_required
def edit(name: str):
    recursor = client_from_config(current_app.config)
    if request.method == "POST":
        return _save(original_name=name)
    try:
        zone = recursor.get_forward_zone(name)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("forwarding.index"))
    if zone is None:
        flash(f"There is no forward zone for {name}.", "warning")
        return redirect(url_for("forwarding.index"))
    return render_template("forwarding/form.html", zone=zone, form={})


def _save(*, original_name: str):
    """Create or replace one forward zone from the submitted form."""
    recursor = client_from_config(current_app.config)
    name = (request.form.get("name") or "").strip()
    servers_text = request.form.get("servers") or ""
    recurse = request.form.get("recursion_desired") == "on"

    problems: list[str] = []
    if not name:
        problems.append("Enter the zone to forward.")
    servers: list[str] = []
    try:
        servers = parse_servers(servers_text)
    except ValueError as exc:
        problems.append(str(exc))

    if problems:
        for problem in problems:
            flash(problem, "danger")
        return (
            render_template(
                "forwarding/form.html",
                zone=None,
                form={"name": name, "servers": servers_text, "recursion_desired": recurse},
                editing=bool(original_name),
            ),
            400,
        )

    canonical_name = canonical(name)
    try:
        # A rename is a different zone, so the old one has to go or both stay
        # in effect and the more specific one silently wins.
        if original_name and canonical(original_name) != canonical_name:
            recursor.delete_forward_zone(original_name)
        recursor.save_forward_zone(canonical_name, servers, recursion_desired=recurse)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("forwarding.index"))

    audit.record(
        "forwarding.save",
        target=canonical_name,
        detail=f"servers={', '.join(servers)} recursion_desired={recurse}",
    )
    label = "Global forwarders" if canonical_name == ROOT_ZONE else canonical_name
    flash(f"{label} saved.", "success")
    return redirect(url_for("forwarding.index"))


@bp.route("/global", methods=["POST"])
@recursor_required
def save_global():
    """Global forwarders: a forward zone for the root, so anything with no more
    specific rule and no local zone goes upstream."""
    recursor = client_from_config(current_app.config)
    servers_text = request.form.get("servers") or ""

    if not servers_text.strip():
        try:
            recursor.delete_forward_zone(ROOT_ZONE)
        except PdnsError as exc:
            if not exc.is_not_found:
                flash(str(exc), "danger")
                return redirect(url_for("forwarding.index"))
        audit.record("forwarding.save", target=ROOT_ZONE, detail="cleared")
        flash(
            "Global forwarders cleared. The resolver now answers from the root "
            "servers itself for anything with no forward zone.",
            "success",
        )
        return redirect(url_for("forwarding.index"))

    try:
        servers = parse_servers(servers_text)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("forwarding.index"))

    try:
        # Recursion desired: upstream resolvers expect to be asked to recurse,
        # and without the bit set most of them refuse the query outright.
        recursor.save_forward_zone(ROOT_ZONE, servers, recursion_desired=True)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("forwarding.index"))

    audit.record("forwarding.save", target=ROOT_ZONE, detail=f"servers={', '.join(servers)}")
    flash("Global forwarders saved.", "success")
    return redirect(url_for("forwarding.index"))


@bp.route("/<path:name>/delete", methods=["POST"])
@recursor_required
def delete(name: str):
    recursor = client_from_config(current_app.config)
    try:
        recursor.delete_forward_zone(name)
    except PdnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("forwarding.index"))
    audit.record("forwarding.delete", target=canonical(name))
    flash(f"Forwarding for {canonical(name)} has been removed.", "success")
    return redirect(url_for("forwarding.index"))


@bp.route("/sync", methods=["POST"])
@recursor_required
def sync():
    """Re-apply the local-zone forward rules on demand."""
    recursor = client_from_config(current_app.config)
    summary = _sync_local_zones(recursor)
    flash(
        f"Local zone forwarding updated: {summary}."
        if summary
        else "Local zone forwarding was already up to date.",
        "success",
    )
    return redirect(url_for("forwarding.index"))
