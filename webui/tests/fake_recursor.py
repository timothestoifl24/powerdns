"""A small in-memory PowerDNS Recursor, standing in for the real HTTP API.

Like fake_pdns, this fakes the *transport*, so RecursorClient's URL building,
zone-id encoding, error handling and POST-then-PUT fallback are all exercised.

The behaviours reproduced here are the ones observed against a real recursor
5.x/4.9 and that the client has to cope with:

* the zone list also contains the ~20 RFC 1918 reverse zones the recursor
  serves itself, as ``Native``;
* POST for a name it already knows is refused with "Zone already exists",
  including for those built-in zones;
* the zone id keeps the case the zone was created with, and DELETE matches on
  that id exactly -- a lower-cased id reads back fine and then fails to delete.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import unquote, urlparse

from fake_pdns import FakeResponse, canonical

#: The recursor generates these at start; they are Native, not Forwarded.
BUILT_IN_REVERSE_ZONES = (
    "10.in-addr.arpa.",
    "127.in-addr.arpa.",
    "168.192.in-addr.arpa.",
)


def zone_id(name: str) -> str:
    """PowerDNS's apiNameToId, preserving case exactly as the real one does."""
    identifier = "".join(
        char if (char.isascii() and (char.isalnum() or char in ".-")) else f"={ord(char):02X}"
        for char in name
    )
    if not identifier.endswith("."):
        identifier += "."
    return "=2E" if identifier == "." else identifier


class FakeRecursor:
    """Implements just enough of the recursor API for the panel's needs."""

    def __init__(self, api_key: str = "recursor-test-key-1234"):
        self.api_key = api_key
        #: id -> zone document, mirroring how the recursor keys its own files.
        self.zones: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.unreachable = False
        for name in BUILT_IN_REVERSE_ZONES:
            self.zones[zone_id(name)] = {
                "id": zone_id(name),
                "name": name,
                "kind": "Native",
                "servers": [],
                "recursion_desired": False,
                "records": [],
            }

    # -- test helpers -----------------------------------------------------

    def add_forward_zone(self, name: str, servers: list[str], recursion_desired: bool = False):
        name = name if name == "." else canonical(name)
        document = {
            "id": zone_id(name),
            "name": name,
            "kind": "Forwarded",
            "servers": list(servers),
            "recursion_desired": recursion_desired,
            "records": [],
        }
        self.zones[document["id"]] = document
        return document

    @property
    def forwarded(self) -> dict[str, list[str]]:
        """name -> servers, for the zones actually being forwarded."""
        return {
            zone["name"]: list(zone["servers"])
            for zone in self.zones.values()
            if zone["kind"] == "Forwarded"
        }

    def _find_by_name(self, name: str) -> dict[str, Any] | None:
        wanted = name if name == "." else canonical(name)
        for zone in self.zones.values():
            zone_name = zone["name"]
            compare = zone_name if zone_name == "." else canonical(zone_name)
            if compare == wanted:
                return zone
        return None

    # -- transport --------------------------------------------------------

    def request(self, method: str, url: str, headers=None, timeout=None, **kwargs):
        if self.unreachable:
            import requests

            raise requests.ConnectionError("connection refused")

        path = urlparse(url).path
        self.requests.append((method.upper(), path))

        if (headers or {}).get("X-API-Key") != self.api_key:
            return FakeResponse(401, {"error": "Unauthorized"})

        prefix = "/api/v1/servers/localhost"
        if path == prefix:
            return FakeResponse(
                200, {"type": "Server", "id": "localhost", "daemon_type": "recursor"}
            )
        if path == f"{prefix}/zones":
            if method.upper() == "GET":
                return FakeResponse(200, list(self.zones.values()))
            if method.upper() == "POST":
                return self._create(kwargs.get("json") or {})
        if path.startswith(f"{prefix}/zones/"):
            identifier = unquote(path[len(f"{prefix}/zones/") :])
            return self._zone_detail(method.upper(), identifier, kwargs.get("json") or {})

        return FakeResponse(404, {"error": f"no route for {path}"})

    def _create(self, body: dict[str, Any]) -> FakeResponse:
        name = body.get("name") or ""
        if not name:
            return FakeResponse(422, {"error": "Field 'name' is required"})
        if self._find_by_name(name) is not None:
            return FakeResponse(409, {"error": "Zone already exists"})
        if body.get("kind") != "Forwarded":
            return FakeResponse(422, {"error": "invalid kind"})
        if not body.get("servers"):
            return FakeResponse(422, {"error": "Need at least one upstream server when forwarding"})
        document = {
            "id": zone_id(name),
            "name": name,
            "kind": "Forwarded",
            "servers": list(body["servers"]),
            "recursion_desired": bool(body.get("recursion_desired")),
            "records": [],
        }
        self.zones[document["id"]] = document
        return FakeResponse(201, document)

    def _zone_detail(self, method: str, identifier: str, body: dict[str, Any]) -> FakeResponse:
        if method == "GET":
            # Lookups go through a case-insensitive DNSName comparison.
            for zone in self.zones.values():
                if zone["id"].lower() == identifier.lower():
                    return FakeResponse(200, zone)
            return FakeResponse(404, {"error": "Could not find domain"})

        # PUT and DELETE act on the stored file, so the id must match exactly.
        if identifier not in self.zones:
            return FakeResponse(422, {"error": "Deleting domain failed"})
        if method == "DELETE":
            del self.zones[identifier]
            return FakeResponse(204)
        if method == "PUT":
            del self.zones[identifier]
            created = self._create(body)
            if created.status_code >= 400:
                return created
            return FakeResponse(204)
        return FakeResponse(405, {"error": f"method {method} not allowed"})

    # requests.Session compatibility -------------------------------------

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FakeRecursor forwarding={json.dumps(self.forwarded, sort_keys=True)}>"
