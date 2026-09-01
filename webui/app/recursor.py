"""Client for the PowerDNS Recursor HTTP API, and the forwarding model.

The Authoritative Server cannot forward: ``recursor=`` was removed in
PowerDNS Authoritative 4.1, and the gpgsql backend only ever serves zones it
holds. Forward zones and global forwarders are therefore a Recursor feature,
and this module is how the panel drives one.

The Recursor is the source of truth, exactly as the Authoritative Server is for
zone data. Nothing about forwarding is mirrored into the panel's own database,
so there is no second copy to drift: what the API reports is what the resolver
is doing. The Recursor persists the zones itself, writing one config fragment
per zone into its ``api-config-dir`` and reading them back through
``include-dir`` at start.

API reference: https://doc.powerdns.com/recursor/http-api/zone.html
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .pdns import ApiClient, PdnsError, canonical

log = logging.getLogger(__name__)

#: The root zone. A Forwarded zone for "." is how PowerDNS expresses "send
#: everything I am not otherwise responsible for to these servers", which is
#: what an operator means by global forwarders.
ROOT_ZONE = "."

#: What the Recursor calls a zone it forwards. It also reports "Native" zones,
#: which for a Recursor are the RFC 1918 reverse zones it serves locally by
#: default -- around twenty of them, and none of them forwarding.
KIND_FORWARDED = "Forwarded"


class RecursorNotConfigured(RuntimeError):
    """The panel has no Recursor to talk to."""


@dataclass(frozen=True)
class ForwardZone:
    """One zone the Recursor forwards, as the API reports it."""

    name: str
    servers: tuple[str, ...]
    recursion_desired: bool
    zone_id: str = ""

    @property
    def is_global(self) -> bool:
        return self.name == ROOT_ZONE

    @property
    def display_name(self) -> str:
        return "Global forwarders" if self.is_global else self.name

    @property
    def servers_text(self) -> str:
        return ", ".join(self.servers)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> ForwardZone:
        return cls(
            name=payload.get("name") or "",
            servers=tuple(payload.get("servers") or ()),
            recursion_desired=bool(payload.get("recursion_desired")),
            zone_id=payload.get("id") or "",
        )


def zone_id_for(name: str) -> str:
    """The Recursor's URL identifier for a zone name.

    Mirrors ``apiNameToId`` in the PowerDNS source: everything outside
    ``[A-Za-z0-9.-]`` becomes ``=XX``, a trailing dot is added, and the root
    zone becomes ``=2E`` because a lone dot does not survive every URL path
    parser between here and there.
    """
    identifier = "".join(
        char if (char.isascii() and (char.isalnum() or char in ".-")) else f"={ord(char):02X}"
        for char in canonical(name)
    )
    if not identifier.endswith("."):
        identifier += "."
    return "=2E" if identifier == "." else identifier


def normalise_server(value: str, default_port: int = 53) -> str:
    """Validate one forwarder address and render it as PowerDNS stores it.

    Forward targets must be addresses: the Recursor parses them at
    configuration time and has no resolver available to turn a name into one.
    Accepting "dns.example.com" here would produce a forward zone that silently
    never answers, so a name is rejected with a message that says why.
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("Enter a forwarder address.")

    address, port = text, default_port
    if text.startswith("["):  # [2001:db8::1]:5353
        closing = text.find("]")
        if closing == -1:
            raise ValueError(f"{text!r} is missing a closing bracket.")
        address = text[1:closing]
        remainder = text[closing + 1 :]
        if remainder.startswith(":"):
            port = _port(remainder[1:], text)
        elif remainder:
            raise ValueError(f"{text!r} is not a valid address.")
    elif text.count(":") == 1:  # 10.0.0.1:5353
        address, _, raw_port = text.partition(":")
        port = _port(raw_port, text)

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError(
            f"{text!r} is not an IP address. Forwarders have to be addresses, "
            "not host names: the resolver reads them from its configuration "
            "before it can resolve anything."
        ) from None

    if parsed.version == 6:
        return f"[{parsed.compressed}]:{port}"
    return f"{parsed.compressed}:{port}"


def _port(raw: str, original: str) -> int:
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{original!r} has a port that is not a number.") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{original!r} has a port outside 1-65535.")
    return port


def parse_servers(text: str, default_port: int = 53) -> list[str]:
    """Split an operator's comma or newline separated list into addresses."""
    parts = [part.strip() for chunk in (text or "").splitlines() for part in chunk.split(",")]
    servers = [normalise_server(part, default_port) for part in parts if part.strip()]
    if not servers:
        raise ValueError("Give at least one forwarder address.")
    # Deduplicate but keep the operator's order: it is the query order.
    return list(dict.fromkeys(servers))


class RecursorClient(ApiClient):
    """Forward-zone management on a PowerDNS Recursor."""

    service = "the PowerDNS Recursor"
    key_setting = "RECURSOR_API_KEY"

    def list_zones(self) -> list[dict[str, Any]]:
        return self._request("GET", f"/servers/{self.server_id}/zones") or []

    def forward_zones(self) -> list[ForwardZone]:
        """Every forwarded zone, global forwarders first, then by name.

        The Recursor's own RFC 1918 reverse zones come back in the same list as
        ``Native``; they are not forwarding anywhere and would be twenty rows
        of noise on the page, so they are filtered out here.
        """
        zones = [
            ForwardZone.from_api(zone)
            for zone in self.list_zones()
            if zone.get("kind") == KIND_FORWARDED
        ]
        return sorted(zones, key=lambda zone: (not zone.is_global, zone.name))

    def get_forward_zone(self, name: str) -> ForwardZone | None:
        wanted = canonical(name)
        for zone in self.forward_zones():
            if canonical(zone.name) == wanted:
                return zone
        return None

    def resolve_zone_id(self, name: str) -> str:
        """The identifier the Recursor itself uses for ``name``.

        DNS names are case-insensitive and the Recursor's lookups honour that,
        but deleting a zone does not go through a lookup: it unlinks a file
        named after the zone *as it was created*. An id we computed from a
        lower-cased name therefore reads a zone back happily and then fails to
        delete it with a 422. Asking the Recursor which id it stored is the
        only reliable answer; the computed form is the fallback for a zone it
        has never heard of.
        """
        wanted = canonical(name)
        for zone in self.list_zones():
            if canonical(zone.get("name", "")) == wanted and zone.get("id"):
                return str(zone["id"])
        return zone_id_for(name)

    def save_forward_zone(
        self, name: str, servers: list[str], recursion_desired: bool = False
    ) -> None:
        """Create the zone, or replace it if the Recursor already has one.

        POST is rejected with "Zone already exists" for a name the Recursor
        knows -- including the RFC 1918 reverse zones it generates itself, which
        an operator may legitimately want to point at an internal server. PUT
        replaces, so falling back to it makes saving idempotent and lets those
        built-in zones be overridden.
        """
        if not servers:
            raise ValueError("A forward zone needs at least one forwarder.")
        body = {
            "name": canonical(name),
            "kind": KIND_FORWARDED,
            "servers": list(servers),
            "recursion_desired": bool(recursion_desired),
        }
        try:
            self._request("POST", f"/servers/{self.server_id}/zones", json=body)
        except PdnsError as exc:
            if "already exists" not in str(exc).lower():
                raise
            self._request("PUT", self._zone_path(self.resolve_zone_id(name)), json=body)

    def delete_forward_zone(self, name: str) -> None:
        self._request("DELETE", self._zone_path(self.resolve_zone_id(name)))

    def _zone_path(self, zone_id: str) -> str:
        return f"/servers/{self.server_id}/zones/{quote(zone_id, safe='')}"


def client_from_config(config) -> RecursorClient:
    """Build a client, or refuse clearly when forwarding is not configured."""
    url = (config.get("RECURSOR_API_URL") or "").strip()
    key = config.get("RECURSOR_API_KEY") or ""
    if not url or not key:
        raise RecursorNotConfigured(
            "Forwarding needs a PowerDNS Recursor. Set RECURSOR_API_URL and "
            "RECURSOR_API_KEY (the compose file does both for the bundled "
            "recursor service)."
        )
    return RecursorClient(
        base_url=url,
        api_key=key,
        server_id=config.get("RECURSOR_SERVER_ID", "localhost"),
        timeout=config.get("RECURSOR_API_TIMEOUT", 10),
    )


def is_configured(config) -> bool:
    return bool((config.get("RECURSOR_API_URL") or "").strip() and config.get("RECURSOR_API_KEY"))


# ---------------------------------------------------------------------------
# Keeping the authoritative zones answerable through the recursor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"started forwarding {', '.join(self.added)}")
        if self.removed:
            parts.append(f"stopped forwarding {', '.join(self.removed)}")
        return "; ".join(parts) or "no changes"


def local_zone_target(config) -> str:
    """Where the recursor should send queries for our own zones."""
    address = (config.get("PDNS_DNS_ADDRESS") or "").strip()
    if not address:
        raise RecursorNotConfigured(
            "PDNS_DNS_ADDRESS is not set, so the recursor cannot be told where "
            "the authoritative server is."
        )
    return normalise_server(address, int(config.get("PDNS_DNS_PORT", 53)))


def sync_local_zones(
    authoritative_names: list[str], recursor: RecursorClient, target: str
) -> SyncResult:
    """Forward every authoritative zone to the authoritative server.

    The recursor answers on the published port, so without this a query for a
    zone this stack hosts would be resolved from the public internet instead of
    from our own data -- the wrong answer, or none.

    Only rules pointing at ``target`` are considered ours. A forward zone an
    operator created by hand, for a zone that happens to share a name, points
    somewhere else and is left exactly as it is: this must never quietly
    redirect a deliberate configuration.
    """
    wanted = {canonical(name) for name in authoritative_names if name and name.strip()}
    current = recursor.forward_zones()
    ours = {
        zone.name: zone
        for zone in current
        if not zone.is_global and tuple(zone.servers) == (target,)
    }
    # A name someone else's rule already owns is not ours to create.
    taken = {zone.name for zone in current} - set(ours)

    added, removed = [], []
    for name in sorted(wanted - set(ours)):
        if name in taken:
            log.info(
                "not forwarding %s to the authoritative server: a forward zone "
                "for it already points somewhere else",
                name,
            )
            continue
        recursor.save_forward_zone(name, [target], recursion_desired=False)
        added.append(name)
    for name in sorted(set(ours) - wanted):
        recursor.delete_forward_zone(name)
        removed.append(name)

    return SyncResult(added=tuple(added), removed=tuple(removed))
