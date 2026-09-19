"""Reverse DNS: the zone names, the PTR names, and the link between the two.

Two features live here.

*Reverse zones.* A forward zone is rarely useful on its own -- the networks
behind it need ``in-addr.arpa``/``ip6.arpa`` zones as well, and working out
that ``192.0.2.0/24`` is ``2.0.192.in-addr.arpa`` by hand is a reliable source
of typos. :func:`reverse_zones_for_network` does that arithmetic.

*Linked PTR records.* An ``A`` or ``AAAA`` record and its ``PTR`` are two
records in two zones that have to agree, and in practice they drift: the
forward record is renamed or repointed and the PTR is forgotten. A link records
that the panel owns a particular PTR on behalf of a particular forward record,
so every later change to that record -- new address, rename, delete -- can be
carried across to the reverse zone.

The link itself is panel metadata, not zone data: the records on both sides of
it live in PowerDNS and are written through the API like every other change.
Deleting the rows would leave the DNS exactly as it is, and only stop the
panel from keeping the pair in step.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select

from .database import get_session
from .models import ReverseLink
from .pdns import PdnsError, canonical

log = logging.getLogger(__name__)

#: Record types that have a reverse counterpart.
ADDRESS_TYPES = frozenset({"A", "AAAA"})

#: The suffixes that make a zone a reverse zone.
REVERSE_SUFFIXES = ("in-addr.arpa.", "ip6.arpa.")

#: How many reverse zones one network may expand into. A /22 is four
#: ``in-addr.arpa`` zones, which is reasonable; a /12 is 16 and a /8 would be
#: 65536. The cap is here so a mistyped prefix asks a question instead of
#: creating a few thousand zones.
MAX_ZONES_PER_NETWORK = 16

#: The most specific reverse zone worth creating, per address family. Reverse
#: delegation stops at the /24 for IPv4 -- anything longer is RFC 2317
#: territory, which is an arrangement with whoever delegates the /24 rather
#: than something to infer from a prefix -- and at the /64 for IPv6, which is
#: where addresses live.
_SMALLEST_ZONE_PREFIX = {4: 24, 6: 64}

#: Bits per label in each family: one octet of an IPv4 address, one nibble of
#: an IPv6 address.
_LABEL_BITS = {4: 8, 6: 4}


class ReverseError(ValueError):
    """A network or address the reverse helpers cannot work with."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def is_reverse_zone(name: str) -> bool:
    return canonical(name).endswith(REVERSE_SUFFIXES)


def ptr_name(address: str) -> str:
    """The PTR name for one address, e.g. ``1.2.0.192.in-addr.arpa.``.

    Raises :class:`ReverseError` if ``address`` is not an IP address, which is
    the normal case for the contents of a record type that is not A or AAAA.
    """
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError as exc:
        raise ReverseError(f"{address!r} is not an IP address") from exc
    return canonical(ip.reverse_pointer)


def reverse_zones_for_network(network: str) -> list[str]:
    """The reverse zone name(s) covering ``network``.

    Reverse delegation happens on label boundaries -- whole octets for IPv4,
    whole nibbles for IPv6 -- and a prefix rarely lands on one:

    - ``192.0.2.0/24`` is exactly ``2.0.192.in-addr.arpa``.
    - ``203.0.113.0/23`` spans two of those boundaries, so it is two zones.
    - ``198.51.100.64/26`` is *part of* one, so the zone to create is the /24
      it sits in, ``100.51.198.in-addr.arpa``. The rest of the /24 lives there
      too. (RFC 2317 is how a /26 gets a zone of its own, and that is a
      deliberate arrangement with whoever delegates the /24, not something to
      guess at from a prefix length.)
    """
    net = parse_network(network)
    label_bits = _LABEL_BITS[net.version]
    smallest = _SMALLEST_ZONE_PREFIX[net.version]

    if net.prefixlen >= smallest:
        return [_zone_name(net.network_address, smallest)]

    # Round up to the next label boundary: the zones then cover the network
    # exactly, where rounding down would name a zone holding more than was
    # asked for.
    boundary = -(-net.prefixlen // label_bits) * label_bits
    count = 1 << (boundary - net.prefixlen)
    if count > MAX_ZONES_PER_NETWORK:
        raise ReverseError(
            f"{net} needs {count} reverse zones, more than the "
            f"{MAX_ZONES_PER_NETWORK} this form creates at once. "
            "Enter the individual networks instead."
        )
    return [
        _zone_name(subnet.network_address, boundary) for subnet in net.subnets(new_prefix=boundary)
    ]


def reverse_zones_for_networks(raw: str) -> tuple[list[str], list[str]]:
    """Every reverse zone named by a block of CIDRs, and what went wrong.

    Takes the textarea from the new-zone form -- one network per line, commas
    tolerated -- and returns ``(zone names, problems)``. Duplicates collapse,
    which is what two networks inside one /24 should do.
    """
    zones: list[str] = []
    problems: list[str] = []
    for entry in [
        part.strip() for part in (raw or "").replace(",", "\n").splitlines() if part.strip()
    ]:
        try:
            for zone in reverse_zones_for_network(entry):
                if zone not in zones:
                    zones.append(zone)
        except ReverseError as exc:
            problems.append(str(exc))
    return zones, problems


def parse_network(network: str):
    """``network`` as an :mod:`ipaddress` network, with a usable error."""
    raw = (network or "").strip()
    if not raw:
        raise ReverseError("Enter a network in CIDR notation, for example 192.0.2.0/24.")
    try:
        # strict=False so 192.0.2.5/24 is read as the /24 it sits in rather
        # than rejected: an operator naming their own address is being clear
        # about which network they mean, not making a mistake.
        return ipaddress.ip_network(raw, strict=False)
    except ValueError as exc:
        raise ReverseError(f"{raw!r} is not a network in CIDR notation: {exc}") from exc


def _zone_name(address, boundary_bits: int) -> str:
    """The reverse zone for ``address`` truncated to ``boundary_bits``."""
    labels = canonical(address.reverse_pointer).split(".")
    suffix = labels[-3:]  # ['in-addr', 'arpa', ''] or ['ip6', 'arpa', '']
    body = labels[: len(labels) - 3]
    keep = boundary_bits // (8 if address.version == 4 else 4)
    # reverse_pointer is least-significant first, so the labels to keep are the
    # last `keep` of the body.
    return ".".join(body[len(body) - keep :] + suffix)


def enclosing_zone(name: str, zone_names: Iterable[str]) -> str | None:
    """The most specific zone in ``zone_names`` that ``name`` belongs to."""
    target = canonical(name)
    best: str | None = None
    for candidate in zone_names:
        zone = canonical(candidate)
        if not zone:
            continue
        matches = target == zone or target.endswith("." + zone)
        if matches and (best is None or len(zone) > len(best)):
            best = zone
    return best


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """What a sync did, in terms the operator can be told about."""

    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Addresses with no reverse zone on this server -- expected, not an error.
    no_zone: list[str] = field(default_factory=list)
    #: Reverse zones the user may not write to.
    denied: list[str] = field(default_factory=list)
    #: PTRs taken over from another forward record.
    stolen: list[str] = field(default_factory=list)
    #: Failures from the API, as messages.
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written or self.removed)

    def messages(self) -> list[tuple[str, str]]:
        """``(text, category)`` pairs for flashing, most useful first."""
        out: list[tuple[str, str]] = []
        if self.written:
            out.append((f"Reverse record {_join(self.written)} has been updated.", "success"))
        if self.removed:
            out.append((f"Reverse record {_join(self.removed)} has been removed.", "info"))
        if self.stolen:
            out.append(
                (
                    f"{_join(self.stolen)} pointed at another record and now points here.",
                    "warning",
                )
            )
        for address in self.no_zone:
            out.append(
                (
                    f"No reverse zone on this server covers {address}, so no PTR was "
                    "created. Create the reverse zone first.",
                    "warning",
                )
            )
        for zone in self.denied:
            out.append((f"You do not have access to {zone}, so its PTR was left alone.", "warning"))
        for message in self.failed:
            out.append((f"The reverse record could not be updated: {message}", "danger"))
        return out


def _join(names: Sequence[str]) -> str:
    return ", ".join(name.rstrip(".") for name in names)


def links_for_record(zone: str, name: str, rtype: str) -> list[ReverseLink]:
    """Links owned by one forward record set."""
    db = get_session()
    return list(
        db.scalars(
            select(ReverseLink).where(
                ReverseLink.forward_zone == canonical(zone),
                ReverseLink.forward_name == canonical(name),
                ReverseLink.forward_type == rtype.upper(),
            )
        )
    )


def links_for_zone(zone: str) -> list[ReverseLink]:
    """Every link touching ``zone``, from either side.

    A zone holds forward records, PTRs, or -- for a reverse zone whose PTRs
    point at names inside itself -- both, so both columns are searched.
    """
    db = get_session()
    zone_c = canonical(zone)
    return list(
        db.scalars(
            select(ReverseLink).where(
                (ReverseLink.forward_zone == zone_c) | (ReverseLink.reverse_zone == zone_c)
            )
        )
    )


def link_for_ptr(ptr: str) -> ReverseLink | None:
    """The link owning ``ptr``, if the panel created it."""
    db = get_session()
    return db.scalar(select(ReverseLink).where(ReverseLink.ptr_name == canonical(ptr)))


def forget_ptr(ptr: str) -> bool:
    """Drop the link for ``ptr``, leaving the record itself alone.

    Used when an operator edits or deletes a linked PTR by hand: they have
    taken the record back, so the panel stops writing to it.
    """
    db = get_session()
    link = db.scalar(select(ReverseLink).where(ReverseLink.ptr_name == canonical(ptr)))
    if link is None:
        return False
    db.delete(link)
    db.commit()
    return True


def forget_zone(zone: str) -> None:
    """Drop every link that mentions ``zone``, on either side.

    Called after a zone is deleted: its records are gone, and so is anything
    the panel could still do about them.
    """
    db = get_session()
    zone_c = canonical(zone)
    for link in db.scalars(
        select(ReverseLink).where(
            (ReverseLink.forward_zone == zone_c) | (ReverseLink.reverse_zone == zone_c)
        )
    ):
        db.delete(link)
    db.commit()


def sync_record(
    client,
    user,
    *,
    zone: str,
    name: str,
    rtype: str,
    ttl: int,
    addresses: Sequence[str],
    enabled: bool,
    zone_names: Sequence[str] | None = None,
) -> SyncResult:
    """Make the reverse side of one forward record set match the forward side.

    ``enabled`` is the operator's choice on the form. When it is off, or the
    record is not an address record, the desired set of PTRs is empty -- which
    is also how a deleted record and an unticked box are handled, so all three
    paths converge here.
    """
    result = SyncResult()
    zone_c, name_c, rtype = canonical(zone), canonical(name), rtype.upper()
    existing = {link.ptr_name: link for link in links_for_record(zone_c, name_c, rtype)}

    desired: dict[str, tuple[str, str]] = {}  # ptr -> (reverse zone, address)
    if enabled and rtype in ADDRESS_TYPES and addresses:
        if zone_names is None:
            zone_names = [entry.get("name", "") for entry in client.list_zones()]
        for address in addresses:
            try:
                ptr = ptr_name(address)
            except ReverseError:
                continue  # Not an address; validation has already said so.
            reverse_zone = enclosing_zone(ptr, zone_names)
            if reverse_zone is None:
                result.no_zone.append(address)
                continue
            if not user.can_edit_zone(reverse_zone):
                result.denied.append(reverse_zone)
                continue
            desired[ptr] = (reverse_zone, address)

    db = get_session()

    for ptr, (reverse_zone, address) in desired.items():
        try:
            client.replace_rrset(
                reverse_zone,
                name=ptr,
                rtype="PTR",
                ttl=ttl,
                contents=[name_c],
                comment=f"Linked to {name_c.rstrip('.')} {rtype}",
                account=user.username,
            )
        except PdnsError as exc:
            log.error("could not write PTR %s in %s: %s", ptr, reverse_zone, exc)
            result.failed.append(str(exc))
            continue

        link = db.scalar(select(ReverseLink).where(ReverseLink.ptr_name == ptr))
        if link is None:
            link = ReverseLink(ptr_name=ptr)
            db.add(link)
        elif (link.forward_name, link.forward_type) != (name_c, rtype):
            # One PTR, one owner: two forward records on the same address
            # cannot both be answered for, so the most recent save wins and
            # the operator is told rather than left to find out later.
            result.stolen.append(ptr)
        link.forward_zone = zone_c
        link.forward_name = name_c
        link.forward_type = rtype
        link.address = address
        link.reverse_zone = reverse_zone
        result.written.append(ptr)

    for ptr, link in existing.items():
        if ptr in desired:
            continue
        if _delete_if_ours(client, link, result):
            result.removed.append(ptr)
        db.delete(link)

    db.commit()
    return result


def _delete_if_ours(client, link: ReverseLink, result: SyncResult) -> bool:
    """Remove a PTR we no longer own, unless it has been changed by hand.

    A PTR whose content no longer names the forward record is someone's
    deliberate edit. Dropping the link is right; deleting their record is not.
    """
    try:
        current = client.get_rrset(link.reverse_zone, link.ptr_name, "PTR")
    except PdnsError as exc:
        log.warning("could not read PTR %s: %s", link.ptr_name, exc)
        result.failed.append(str(exc))
        return False
    if current is None:
        return False
    if [canonical(content) for content in current.contents] != [link.forward_name]:
        return False
    try:
        client.delete_rrset(link.reverse_zone, link.ptr_name, "PTR")
    except PdnsError as exc:
        log.error("could not delete PTR %s: %s", link.ptr_name, exc)
        result.failed.append(str(exc))
        return False
    return True
