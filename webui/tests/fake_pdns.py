"""A small in-memory PowerDNS, standing in for the real HTTP API.

This fakes the *transport*, not our client, so PdnsClient's URL building, error
handling and JSON parsing are all exercised by the tests that use it.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

SERVER_VERSION = "4.9.7"


def canonical(name: str) -> str:
    name = (name or "").strip().lower()
    return name.rstrip(".") + "." if name else ""


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = (
            text if text is not None else (json.dumps(payload) if payload is not None else "")
        )
        self.content = self.text.encode()

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakePowerDNS:
    """Implements just enough of the API for the panel's needs."""

    def __init__(self, api_key: str = "testtesttesttest"):
        self.api_key = api_key
        self.zones: dict[str, dict[str, Any]] = {}
        self.cryptokeys: dict[str, list[dict[str, Any]]] = {}
        self.notified: list[str] = []
        self.retrieved: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self._next_key_id = 1

    # -- test helpers -----------------------------------------------------

    def add_zone(self, name: str, kind: str = "Native", dnssec: bool = False, rrsets=None):
        name = canonical(name)
        self.zones[name] = {
            "id": name,
            "name": name,
            "kind": kind,
            "dnssec": dnssec,
            "serial": 2024010101,
            "masters": [],
            "rrsets": list(rrsets or []),
        }
        return self.zones[name]

    def rrset(self, zone: str, name: str, rtype: str) -> dict | None:
        zone_data = self.zones.get(canonical(zone))
        if not zone_data:
            return None
        for rrset in zone_data["rrsets"]:
            if rrset["name"] == canonical(name) and rrset["type"] == rtype.upper():
                return rrset
        return None

    # -- transport --------------------------------------------------------

    def request(self, method: str, url: str, headers=None, timeout=None, **kwargs):
        headers = headers or {}
        if headers.get("X-API-Key") != self.api_key:
            return FakeResponse(401, {"error": "Unauthorized"})

        path = url.split("/api/v1", 1)[-1]
        self.requests.append((method.upper(), path))
        return self._route(method.upper(), path, kwargs)

    # requests.Session also exposes these directly.
    def get(self, url, headers=None, timeout=None, **kwargs):
        return self.request("GET", url, headers=headers, timeout=timeout, **kwargs)

    def _route(self, method: str, path: str, kwargs: dict) -> FakeResponse:
        body = kwargs.get("json") or {}

        if path == "/servers/localhost":
            return FakeResponse(
                200, {"id": "localhost", "version": SERVER_VERSION, "type": "Server"}
            )

        if path == "/servers/localhost/statistics":
            return FakeResponse(
                200,
                [
                    {"type": "StatisticItem", "name": "udp-queries", "value": "42"},
                    {"type": "StatisticItem", "name": "uptime", "value": "3600"},
                ],
            )

        if path == "/servers/localhost/zones" and method == "GET":
            return FakeResponse(
                200,
                [
                    {key: zone[key] for key in ("id", "name", "kind", "dnssec", "serial")}
                    for zone in self.zones.values()
                ],
            )

        if path == "/servers/localhost/zones" and method == "POST":
            name = canonical(body.get("name", ""))
            if not name:
                return FakeResponse(422, {"error": "Zone name is required"})
            if name in self.zones:
                return FakeResponse(409, {"error": f"Domain '{name}' already exists"})
            rrsets = [
                {
                    "name": name,
                    "type": "SOA",
                    "ttl": 3600,
                    "records": [
                        {
                            "content": (
                                "a.misconfigured.dns.server.invalid. "
                                f"hostmaster.{name} 1 10800 3600 604800 3600"
                            ),
                            "disabled": False,
                        }
                    ],
                    "comments": [],
                }
            ]
            nameservers = body.get("nameservers") or []
            if nameservers:
                rrsets.append(
                    {
                        "name": name,
                        "type": "NS",
                        "ttl": 3600,
                        "records": [
                            {"content": canonical(ns), "disabled": False} for ns in nameservers
                        ],
                        "comments": [],
                    }
                )
            zone = self.add_zone(
                name,
                kind=body.get("kind", "Native"),
                dnssec=body.get("dnssec", False),
                rrsets=rrsets,
            )
            zone["masters"] = body.get("masters") or []
            return FakeResponse(201, zone)

        zone_match = re.match(r"^/servers/localhost/zones/([^/]+)(/.*)?$", path)
        if zone_match:
            zone_id = canonical(unquote(zone_match.group(1)))
            suffix = zone_match.group(2) or ""
            zone = self.zones.get(zone_id)
            if zone is None:
                return FakeResponse(404, {"error": "Not Found"})
            return self._zone_route(method, zone, suffix, body)

        if path.startswith("/servers/localhost/search-data"):
            return FakeResponse(200, [])

        return FakeResponse(404, {"error": f"no route for {method} {path}"})

    def _zone_route(self, method: str, zone: dict, suffix: str, body: dict) -> FakeResponse:
        zone_id = zone["id"]

        if suffix == "":
            if method == "GET":
                return FakeResponse(200, zone)
            if method == "DELETE":
                self.zones.pop(zone_id, None)
                return FakeResponse(204)
            if method == "PUT":
                zone.update({key: value for key, value in body.items() if key != "rrsets"})
                return FakeResponse(204)
            if method == "PATCH":
                for change in body.get("rrsets", []):
                    self._apply_rrset(zone, change)
                zone["serial"] += 1
                return FakeResponse(204)

        if suffix == "/notify" and method == "PUT":
            self.notified.append(zone_id)
            return FakeResponse(200, {"result": "Notification queued"})

        if suffix == "/axfr-retrieve" and method == "PUT":
            self.retrieved.append(zone_id)
            return FakeResponse(200, {"result": "Added retrieval request"})

        if suffix == "/export" and method == "GET":
            lines = []
            for rrset in zone["rrsets"]:
                for record in rrset["records"]:
                    lines.append(
                        f"{rrset['name']}\t{rrset['ttl']}\tIN\t{rrset['type']}\t{record['content']}"
                    )
            return FakeResponse(200, None, text="\n".join(lines) + "\n")

        if suffix == "/cryptokeys":
            keys = self.cryptokeys.setdefault(zone_id, [])
            if method == "GET":
                return FakeResponse(200, keys)
            if method == "POST":
                key = {
                    "id": self._next_key_id,
                    "keytype": body.get("keytype", "csk"),
                    "active": body.get("active", True),
                    "algorithm": "ECDSAP256SHA256",
                    "bits": 256,
                    "ds": [f"{self._next_key_id} 13 2 " + "ab" * 32],
                }
                self._next_key_id += 1
                keys.append(key)
                return FakeResponse(201, key)

        key_match = re.match(r"^/cryptokeys/(\d+)$", suffix)
        if key_match and method == "DELETE":
            key_id = int(key_match.group(1))
            keys = self.cryptokeys.setdefault(zone_id, [])
            self.cryptokeys[zone_id] = [key for key in keys if key["id"] != key_id]
            return FakeResponse(204)

        return FakeResponse(404, {"error": f"no route for {method} {suffix}"})

    @staticmethod
    def _apply_rrset(zone: dict, change: dict) -> None:
        name = canonical(change.get("name", ""))
        rtype = (change.get("type") or "").upper()
        zone["rrsets"] = [
            rrset
            for rrset in zone["rrsets"]
            if not (rrset["name"] == name and rrset["type"] == rtype)
        ]
        if (change.get("changetype") or "").upper() == "DELETE":
            return
        zone["rrsets"].append(
            {
                "name": name,
                "type": rtype,
                "ttl": change.get("ttl", 3600),
                "records": change.get("records", []),
                "comments": change.get("comments", []),
            }
        )
