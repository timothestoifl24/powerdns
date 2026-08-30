"""Record validation."""

from __future__ import annotations

import pytest

from app.dnsutil import validate_content, validate_name, validate_rrset, validate_ttl


class TestContent:
    @pytest.mark.parametrize(
        "rtype,content",
        [
            ("A", "192.0.2.1"),
            ("AAAA", "2001:db8::1"),
            ("CNAME", "target.example.com."),
            ("MX", "10 mail.example.com."),
            ("TXT", '"v=spf1 -all"'),
            ("SRV", "10 20 5060 sip.example.com."),
            ("CAA", '0 issue "letsencrypt.org"'),
            ("NS", "ns1.example.com."),
            ("PTR", "host.example.com."),
        ],
    )
    def test_valid_content_accepted(self, rtype, content):
        assert validate_content(rtype, content) is None

    @pytest.mark.parametrize(
        "rtype,content",
        [
            ("A", "not-an-ip"),
            ("A", "2001:db8::1"),
            ("AAAA", "192.0.2.1"),
            ("MX", "mail.example.com."),
            ("SRV", "sip.example.com."),
        ],
    )
    def test_invalid_content_rejected(self, rtype, content):
        assert validate_content(rtype, content) is not None

    def test_unknown_type_rejected(self):
        assert "not a known record type" in validate_content("NOPE", "x")

    def test_empty_content_rejected(self):
        assert "must not be empty" in validate_content("A", "")

    def test_unqualified_hostname_rejected(self):
        """PowerDNS stores content verbatim, so a missing dot silently breaks resolution."""
        problem = validate_content("CNAME", "target.example.com")
        assert problem and "fully qualified" in problem


class TestName:
    def test_valid_name(self):
        assert validate_name("www.example.com") is None

    def test_empty_name_rejected(self):
        assert "must not be empty" in validate_name("")

    def test_overlong_label_rejected(self):
        problem = validate_name("a" * 64 + ".example.com")
        assert problem and "63" in problem


class TestTtl:
    def test_valid(self):
        assert validate_ttl("3600") == (3600, None)

    def test_non_numeric_rejected(self):
        assert validate_ttl("soon")[1] is not None

    def test_negative_rejected(self):
        assert validate_ttl("-1")[1] is not None

    def test_absurdly_large_rejected(self):
        assert validate_ttl("99999999999")[1] is not None

    def test_zero_is_allowed(self):
        assert validate_ttl("0") == (0, None)


class TestRrset:
    def test_valid_set(self):
        assert validate_rrset("www.example.com.", "A", ["192.0.2.1"], "example.com.") == []

    def test_name_outside_the_zone_rejected(self):
        problems = validate_rrset("www.other.net.", "A", ["192.0.2.1"], "example.com.")
        assert any("does not belong to the zone" in problem for problem in problems)

    def test_apex_is_inside_the_zone(self):
        assert validate_rrset("example.com.", "A", ["192.0.2.1"], "example.com.") == []

    def test_empty_content_list_rejected(self):
        problems = validate_rrset("www.example.com.", "A", [], "example.com.")
        assert any("at least one record" in problem for problem in problems)

    def test_cname_at_apex_rejected(self):
        problems = validate_rrset("example.com.", "CNAME", ["t.example.net."], "example.com.")
        assert any("zone apex" in problem for problem in problems)

    def test_multiple_cnames_rejected(self):
        problems = validate_rrset(
            "www.example.com.", "CNAME", ["a.example.net.", "b.example.net."], "example.com."
        )
        assert any("only one CNAME" in problem for problem in problems)

    def test_cname_alongside_other_types_rejected(self):
        """RFC 1034 4.3.5: a CNAME must be the only record type at its name."""
        problems = validate_rrset(
            "www.example.com.", "CNAME", ["t.example.net."], "example.com.", existing_types={"A"}
        )
        assert any("cannot coexist" in problem for problem in problems)

    def test_other_type_alongside_a_cname_rejected(self):
        problems = validate_rrset(
            "www.example.com.", "A", ["192.0.2.1"], "example.com.", existing_types={"CNAME"}
        )
        assert any("already has a CNAME" in problem for problem in problems)

    def test_dnssec_types_do_not_block_a_cname(self):
        """RRSIG and NSEC coexist with a CNAME by design."""
        problems = validate_rrset(
            "www.example.com.",
            "CNAME",
            ["t.example.net."],
            "example.com.",
            existing_types={"RRSIG", "NSEC"},
        )
        assert problems == []
