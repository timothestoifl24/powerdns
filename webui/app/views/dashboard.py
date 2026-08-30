"""Landing page: server health, zone counts and recent activity."""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, render_template

from .. import audit
from ..pdns import PdnsError, client_from_config
from ..security import current_user, login_required

log = logging.getLogger(__name__)

bp = Blueprint("dashboard", __name__)

#: Statistics worth surfacing, with the label to show for each.
INTERESTING_STATS = (
    ("udp-queries", "UDP queries"),
    ("tcp-queries", "TCP queries"),
    ("servfail-answers", "SERVFAIL answers"),
    ("packetcache-hit", "Packet cache hits"),
    ("packetcache-miss", "Packet cache misses"),
    ("query-cache-hit", "Query cache hits"),
    ("uptime", "Uptime (seconds)"),
)


@bp.route("/")
@login_required
def index():
    user = current_user()
    assert user is not None

    client = client_from_config(current_app.config)
    error: str | None = None
    zones: list = []
    stats: dict[str, str] = {}
    server: dict = {}

    try:
        zones = client.list_zones()
        stats = client.statistics()
        server = client.server_info()
    except PdnsError as exc:
        # The panel stays usable and explains itself rather than showing a 500.
        error = str(exc)
        log.error("dashboard could not reach PowerDNS: %s", exc)

    visible = [zone for zone in zones if user.can_see_zone(zone.get("name", ""))]

    kinds: dict[str, int] = {}
    dnssec_count = 0
    for zone in visible:
        kinds[zone.get("kind", "Unknown")] = kinds.get(zone.get("kind", "Unknown"), 0) + 1
        if zone.get("dnssec"):
            dnssec_count += 1

    return render_template(
        "dashboard.html",
        zones=visible,
        zone_count=len(visible),
        total_zone_count=len(zones),
        kinds=kinds,
        dnssec_count=dnssec_count,
        stats=[(label, stats.get(key)) for key, label in INTERESTING_STATS if stats.get(key)],
        server=server,
        recent=audit.recent(limit=10) if user.is_admin else [],
        error=error,
    )
