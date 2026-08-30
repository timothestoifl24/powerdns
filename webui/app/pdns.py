"""Client for the PowerDNS Authoritative Server HTTP API.

All zone and record changes go through here rather than through SQL. PowerDNS
owns serial bumping, NSEC3/DNSSEC bookkeeping and record validation; writing to
the tables behind its back means silently serving stale or unsigned data.

API reference: https://doc.powerdns.com/authoritative/http-api/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

#: Record types offered in the UI. PowerDNS accepts more; this is the set worth
#: putting in a dropdown, ordered by how often they are used.
COMMON_RECORD_TYPES = (
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "NS",
    "SRV",
    "PTR",
    "CAA",
    "SOA",
    "SPF",
    "SSHFP",
    "TLSA",
    "DS",
    "NAPTR",
    "ALIAS",
    "LOC",
    "HTTPS",
    "SVCB",
)

#: Zone kinds PowerDNS supports for the gpgsql backend.
ZONE_KINDS = ("Native", "Master", "Slave")


class PdnsError(RuntimeError):
    """An error reported by, or while talking to, the PowerDNS API."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_not_found(self) -> bool:
        return self.status_code == 404


@dataclass(frozen=True)
class RRSet:
    """One resource record set: all records sharing a name, type and TTL."""

    name: str
    type: str
    ttl: int
    records: tuple[dict[str, Any], ...]
    comments: tuple[dict[str, Any], ...] = ()

    @property
    def contents(self) -> list[str]:
        return [record.get("content", "") for record in self.records]

    @property
    def is_disabled(self) -> bool:
        return all(record.get("disabled", False) for record in self.records) and bool(self.records)

    @property
    def comment_text(self) -> str:
        return " ".join(
            comment.get("content", "") for comment in self.comments if comment.get("content")
        )

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "RRSet":
        return cls(
            name=payload.get("name", ""),
            type=payload.get("type", ""),
            ttl=int(payload.get("ttl", 0) or 0),
            records=tuple(payload.get("records") or ()),
            comments=tuple(payload.get("comments") or ()),
        )


def canonical(name: str) -> str:
    """Normalise a DNS name to lower case with exactly one trailing dot."""
    name = (name or "").strip().lower()
    if not name:
        return ""
    if name == ".":
        return "."
    return name.rstrip(".") + "."


def relative_name(name: str, zone: str) -> str:
    """Render ``name`` the way a zone file would: relative to ``zone``, or ``@``."""
    name_c, zone_c = canonical(name), canonical(zone)
    if name_c == zone_c:
        return "@"
    if name_c.endswith("." + zone_c):
        return name_c[: -(len(zone_c) + 1)]
    return name_c.rstrip(".")


def absolute_name(name: str, zone: str) -> str:
    """Inverse of :func:`relative_name`: expand ``@``/relative input to an FQDN."""
    name = (name or "").strip().lower()
    zone_c = canonical(zone)
    if name in ("", "@"):
        return zone_c
    if name.endswith("."):
        return canonical(name)
    return canonical(f"{name}.{zone_c.rstrip('.')}")


class PdnsClient:
    """Thin, synchronous wrapper around the PowerDNS API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        server_id: str = "localhost",
        timeout: int = 10,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.server_id = server_id
        self.timeout = timeout
        self._session = session or requests.Session()

    # -- plumbing ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = self._url(path)
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        try:
            response = self._session.request(
                method, url, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.Timeout as exc:
            raise PdnsError(f"PowerDNS API timed out after {self.timeout}s") from exc
        except requests.RequestException as exc:
            raise PdnsError(f"Cannot reach the PowerDNS API at {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise PdnsError(
                "PowerDNS rejected the API key. Check that PDNS_API_KEY matches on "
                "both the pdns and webui containers.",
                401,
            )
        if response.status_code >= 400:
            raise PdnsError(self._error_message(response), response.status_code)

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise PdnsError("PowerDNS returned a response that is not JSON") from exc

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        """PowerDNS puts a human-readable reason in an "error" field."""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("errors")
            if message:
                if isinstance(message, list):
                    message = "; ".join(str(item) for item in message)
                return str(message)
        text = (response.text or "").strip()
        if text:
            return f"HTTP {response.status_code}: {text[:400]}"
        return f"HTTP {response.status_code} from the PowerDNS API"

    # -- server -----------------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        return self._request("GET", f"/servers/{self.server_id}") or {}

    def statistics(self) -> dict[str, str]:
        """Flatten the statistics list into a name -> value mapping."""
        payload = self._request("GET", f"/servers/{self.server_id}/statistics") or []
        stats: dict[str, str] = {}
        for entry in payload:
            if isinstance(entry, dict) and entry.get("type") == "StatisticItem":
                stats[str(entry.get("name"))] = str(entry.get("value"))
        return stats

    def ping(self) -> bool:
        """Whether the API answers and accepts our key."""
        try:
            self.server_info()
            return True
        except PdnsError:
            return False

    # -- zones ------------------------------------------------------------

    def list_zones(self) -> list[dict[str, Any]]:
        # dnssec=false keeps PowerDNS from reading key material for every zone,
        # which is a measurable cost once you have a few hundred of them.
        payload = self._request("GET", f"/servers/{self.server_id}/zones") or []
        return sorted(payload, key=lambda zone: zone.get("name", ""))

    def get_zone(self, zone_id: str) -> dict[str, Any]:
        return self._request("GET", self._zone_path(zone_id)) or {}

    def zone_rrsets(self, zone_id: str) -> list[RRSet]:
        zone = self.get_zone(zone_id)
        return [RRSet.from_api(item) for item in zone.get("rrsets", [])]

    def create_zone(
        self,
        name: str,
        kind: str = "Native",
        nameservers: list[str] | None = None,
        masters: list[str] | None = None,
        soa_edit_api: str = "DEFAULT",
        dnssec: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": canonical(name),
            "kind": kind,
            "soa_edit_api": soa_edit_api,
            "dnssec": dnssec,
        }
        if kind.lower() == "slave":
            # A slave zone has no local content; PowerDNS refuses nameservers here.
            body["masters"] = masters or []
            body["nameservers"] = []
        else:
            body["nameservers"] = [canonical(ns) for ns in (nameservers or [])]
        return self._request("POST", f"/servers/{self.server_id}/zones", json=body) or {}

    def update_zone(self, zone_id: str, changes: dict[str, Any]) -> None:
        self._request("PUT", self._zone_path(zone_id), json=changes)

    def delete_zone(self, zone_id: str) -> None:
        self._request("DELETE", self._zone_path(zone_id))

    def notify_zone(self, zone_id: str) -> None:
        self._request("PUT", f"{self._zone_path(zone_id)}/notify")

    def retrieve_zone(self, zone_id: str) -> None:
        """Ask PowerDNS to AXFR a slave zone from its master right now."""
        self._request("PUT", f"{self._zone_path(zone_id)}/axfr-retrieve")

    def export_zone(self, zone_id: str) -> str:
        """The zone as a BIND-format zone file."""
        url = self._url(f"{self._zone_path(zone_id)}/export")
        try:
            response = self._session.get(
                url, headers={"X-API-Key": self.api_key}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise PdnsError(f"Cannot reach the PowerDNS API: {exc}") from exc
        if response.status_code >= 400:
            raise PdnsError(self._error_message(response), response.status_code)
        return response.text

    # -- record sets ------------------------------------------------------

    def replace_rrset(
        self,
        zone_id: str,
        name: str,
        rtype: str,
        ttl: int,
        contents: list[str],
        disabled: bool = False,
        comment: str = "",
        account: str = "",
    ) -> None:
        """Create or overwrite one record set.

        PowerDNS PATCH semantics are per-rrset replace: whatever is sent becomes
        the complete set for that (name, type) pair.
        """
        rrset: dict[str, Any] = {
            "name": canonical(name),
            "type": rtype.upper(),
            "ttl": int(ttl),
            "changetype": "REPLACE",
            "records": [
                {"content": content, "disabled": disabled}
                for content in contents
                if content.strip()
            ],
        }
        if comment:
            rrset["comments"] = [
                {"content": comment, "account": account, "modified_at": 0}
            ]
        else:
            # An empty list clears any existing comment; omitting the key would
            # leave a stale comment attached to the new content.
            rrset["comments"] = []
        self._patch_rrsets(zone_id, [rrset])

    def delete_rrset(self, zone_id: str, name: str, rtype: str) -> None:
        self._patch_rrsets(
            zone_id,
            [{"name": canonical(name), "type": rtype.upper(), "changetype": "DELETE"}],
        )

    def _patch_rrsets(self, zone_id: str, rrsets: list[dict[str, Any]]) -> None:
        self._request("PATCH", self._zone_path(zone_id), json={"rrsets": rrsets})

    # -- DNSSEC -----------------------------------------------------------

    def cryptokeys(self, zone_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"{self._zone_path(zone_id)}/cryptokeys") or []

    def create_cryptokey(self, zone_id: str, keytype: str = "csk", active: bool = True) -> dict:
        return (
            self._request(
                "POST",
                f"{self._zone_path(zone_id)}/cryptokeys",
                json={"keytype": keytype, "active": active},
            )
            or {}
        )

    def delete_cryptokey(self, zone_id: str, key_id: int) -> None:
        self._request("DELETE", f"{self._zone_path(zone_id)}/cryptokeys/{int(key_id)}")

    def set_zone_dnssec(self, zone_id: str, enabled: bool) -> None:
        self.update_zone(zone_id, {"dnssec": bool(enabled)})

    # -- search -----------------------------------------------------------

    def search(self, query: str, max_results: int = 100) -> list[dict[str, Any]]:
        """Search zones, records and comments. ``*`` is the wildcard."""
        if not query.strip():
            return []
        return (
            self._request(
                "GET",
                f"/servers/{self.server_id}/search-data",
                params={"q": query, "max": max_results},
            )
            or []
        )

    def _zone_path(self, zone_id: str) -> str:
        return f"/servers/{self.server_id}/zones/{quote(zone_id, safe='')}"


def client_from_config(config) -> PdnsClient:
    return PdnsClient(
        base_url=config["PDNS_API_URL"],
        api_key=config["PDNS_API_KEY"],
        server_id=config["PDNS_SERVER_ID"],
        timeout=config["PDNS_API_TIMEOUT"],
    )
