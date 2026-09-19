"""Record validation, so mistakes are caught before PowerDNS sees them.

PowerDNS validates too, but its errors are terse and arrive after a round
trip. Parsing the record here with dnspython gives a precise message next to
the field the operator is editing.
"""

from __future__ import annotations

from dataclasses import dataclass

import dns.exception
import dns.name
import dns.rdata
import dns.rdataclass
import dns.rdatatype

from .pdns import canonical

#: Types whose content is a hostname that people routinely forget to
#: fully-qualify. A relative name here silently becomes something unintended.
_HOSTNAME_TYPES = frozenset({"CNAME", "NS", "PTR", "MX", "SRV", "ALIAS", "DNAME"})


def validate_name(name: str) -> str | None:
    """Error message, or ``None`` if ``name`` is a usable DNS name."""
    if not name or not name.strip():
        return "The name must not be empty."
    try:
        parsed = dns.name.from_text(canonical(name))
    except dns.exception.DNSException as exc:
        return f"{name!r} is not a valid DNS name: {exc}"
    if len(parsed.to_text()) > 255:
        return "The name is longer than the 255 character limit."
    for label in parsed.labels:
        if len(label) > 63:
            return f"The label {label.decode(errors='replace')!r} is longer than 63 characters."
    return None


def validate_content(rtype: str, content: str) -> str | None:
    """Error message, or ``None`` if ``content`` parses as ``rtype`` data."""
    rtype = (rtype or "").upper().strip()
    content = (content or "").strip()

    if not content:
        return "The record content must not be empty."
    if not rtype:
        return "Choose a record type."

    try:
        rdtype = dns.rdatatype.from_text(rtype)
    except dns.rdatatype.UnknownRdatatype:
        return f"{rtype!r} is not a known record type."

    try:
        dns.rdata.from_text(dns.rdataclass.IN, rdtype, content, relativize=False)
    except dns.exception.SyntaxError as exc:
        return f"{content!r} is not valid {rtype} content: {exc}"
    except dns.exception.DNSException as exc:
        return f"{content!r} could not be parsed as {rtype}: {exc}"
    except ValueError as exc:
        return f"{content!r} is not valid {rtype} content: {exc}"

    if rtype in _HOSTNAME_TYPES and not content.endswith("."):
        # PowerDNS stores content verbatim, so "mail.example.com" without the
        # dot is served as-is and resolvers will not expand it.
        target = content.split()[-1] if " " in content else content
        if not target.endswith(".") and not target.isdigit():
            return (
                f"{rtype} content must be a fully qualified name ending with a dot, "
                f"for example {target}."
            )
    return None


def validate_rrset(
    name: str, rtype: str, contents: list[str], zone: str, existing_types: set[str] | None = None
) -> list[str]:
    """Every problem with a proposed record set, as human-readable messages."""
    problems: list[str] = []
    rtype = (rtype or "").upper().strip()

    name_error = validate_name(name)
    if name_error:
        problems.append(name_error)

    if not contents:
        problems.append("Add at least one record.")

    for content in contents:
        content_error = validate_content(rtype, content)
        if content_error:
            problems.append(content_error)

    name_c, zone_c = canonical(name), canonical(zone)

    if name_c and zone_c and name_c != zone_c and not name_c.endswith("." + zone_c):
        problems.append(f"{name_c.rstrip('.')} does not belong to the zone {zone_c.rstrip('.')}.")

    if rtype == "SOA":
        if len(contents) > 1:
            problems.append("A zone has exactly one SOA record.")
        if name_c and zone_c and name_c != zone_c:
            # The UI does not offer this, but the form can still be posted by
            # hand, and an SOA below the apex is not a valid zone.
            problems.append(
                f"The SOA record belongs at the zone apex ({zone_c.rstrip('.')}), "
                f"not at {name_c.rstrip('.')}."
            )

    if rtype == "CNAME":
        if len(contents) > 1:
            problems.append("A name can have only one CNAME record.")
        if name_c and zone_c and name_c == zone_c:
            problems.append(
                "A CNAME cannot be placed at the zone apex. Use an ALIAS record instead."
            )
        # A CNAME must be the only record type at its name (RFC 1034 4.3.5).
        conflicting = (existing_types or set()) - {"CNAME", "RRSIG", "NSEC", "NSEC3"}
        if conflicting:
            problems.append(
                "A CNAME cannot coexist with other record types at the same name "
                f"(this name already has: {', '.join(sorted(conflicting))})."
            )
    elif existing_types and "CNAME" in existing_types and rtype not in ("CNAME", "SOA"):
        article = "an" if rtype[:1] in "AEIOU" else "a"
        problems.append(
            f"This name already has a CNAME, so {article} {rtype} record cannot be added to it."
        )

    return problems


def validate_ttl(raw: str | int) -> tuple[int, str | None]:
    """Parse a TTL, returning ``(value, error)``."""
    try:
        ttl = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0, "The TTL must be a whole number of seconds."
    if ttl < 0:
        return 0, "The TTL cannot be negative."
    if ttl > 2147483647:
        return 0, "The TTL is larger than the maximum of 2147483647."
    return ttl, None


# ---------------------------------------------------------------------------
# SOA records
# ---------------------------------------------------------------------------

#: Field order in SOA content, after the two names.
SOA_TIMERS = ("refresh", "retry", "expire", "minimum")

#: What a zone starts with when its SOA cannot be read. These are the values
#: PowerDNS itself seeds a new zone with.
SOA_DEFAULTS = {"refresh": 10800, "retry": 3600, "expire": 604800, "minimum": 3600}


@dataclass(frozen=True)
class Soa:
    """The seven fields of an SOA record, as the settings form edits them."""

    mname: str = ""
    rname: str = ""
    serial: int = 0
    refresh: int = SOA_DEFAULTS["refresh"]
    retry: int = SOA_DEFAULTS["retry"]
    expire: int = SOA_DEFAULTS["expire"]
    minimum: int = SOA_DEFAULTS["minimum"]

    @property
    def email(self) -> str:
        """The administrator address, in the form a person writes it."""
        return rname_to_email(self.rname)

    def to_content(self) -> str:
        return (
            f"{canonical(self.mname)} {self.rname} {self.serial} "
            f"{self.refresh} {self.retry} {self.expire} {self.minimum}"
        )


def parse_soa(content: str) -> Soa | None:
    """Read an SOA record's content, or ``None`` if it is not one.

    PowerDNS stores the content verbatim, so this has to cope with whatever is
    in the zone rather than with a canonical form.
    """
    parts = (content or "").split()
    if len(parts) < 7:
        return None
    try:
        numbers = [int(part) for part in parts[2:7]]
    except ValueError:
        return None
    return Soa(
        mname=parts[0],
        rname=parts[1],
        serial=numbers[0],
        refresh=numbers[1],
        retry=numbers[2],
        expire=numbers[3],
        minimum=numbers[4],
    )


def email_to_rname(email: str) -> str:
    """``hostmaster@example.com`` -> ``hostmaster.example.com.``

    The local part keeps its dots, escaped, because the whole thing becomes a
    DNS name: ``first.last@example.com`` is ``first\\.last.example.com.`` and
    not a name with an extra label.
    """
    value = (email or "").strip()
    if not value:
        return ""
    if "@" not in value:
        # Already in DNS form, or something the caller validated separately.
        return canonical(value)
    local, _, domain = value.rpartition("@")
    escaped = local.replace(".", "\\.")
    return canonical(f"{escaped}.{domain}")


def rname_to_email(rname: str) -> str:
    """The inverse of :func:`email_to_rname`, for showing it in a form."""
    value = (rname or "").strip().rstrip(".")
    if not value:
        return ""
    local: list[str] = []
    rest = value
    index = 0
    while index < len(rest):
        char = rest[index]
        if char == "\\" and index + 1 < len(rest):
            # An escaped dot is part of the local part, not a separator.
            local.append(rest[index + 1])
            index += 2
            continue
        if char == ".":
            return f"{''.join(local)}@{rest[index + 1 :]}"
        local.append(char)
        index += 1
    # No separator at all: not an address, so hand it back untouched.
    return value


def validate_email(email: str) -> str | None:
    """Error message, or ``None`` if ``email`` can become an SOA RNAME."""
    value = (email or "").strip()
    if not value:
        return "Enter the administrator's email address."
    if value.count("@") != 1:
        return f"{value!r} is not an email address, for example hostmaster@example.com."
    local, _, domain = value.partition("@")
    if not local or not domain or "." not in domain:
        return f"{value!r} is not an email address, for example hostmaster@example.com."
    return validate_name(email_to_rname(value))
