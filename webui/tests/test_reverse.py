"""Reverse zones, and PTR records linked to the forward record they answer for."""

from __future__ import annotations

import pytest

from app.database import get_session
from app.models import ReverseLink, ZoneAccess
from app.reverse import (
    ReverseError,
    enclosing_zone,
    is_reverse_zone,
    ptr_name,
    reverse_zones_for_network,
    reverse_zones_for_networks,
)


@pytest.fixture
def zone(pdns):
    """A forward zone with one address record in it."""
    pdns.add_zone(
        "example.com",
        rrsets=[
            {
                "name": "example.com.",
                "type": "SOA",
                "ttl": 3600,
                "records": [
                    {
                        "content": "ns1.example.com. hostmaster.example.com. 1 10800 3600 604800 3600",
                        "disabled": False,
                    }
                ],
                "comments": [],
            },
            {
                "name": "www.example.com.",
                "type": "A",
                "ttl": 3600,
                "records": [{"content": "192.0.2.1", "disabled": False}],
                "comments": [],
            },
        ],
    )
    return pdns.zones["example.com."]


@pytest.fixture
def reverse_zone(pdns):
    """The /24 that example.com's addresses live in."""
    pdns.add_zone("2.0.192.in-addr.arpa")
    return pdns.zones["2.0.192.in-addr.arpa."]


def save_record(client, token, **form):
    data = {"csrf_token": token("/zones/example.com."), **form}
    return client.post("/zones/example.com./records", data=data, follow_redirects=True)


class TestZoneNames:
    @pytest.mark.parametrize(
        "network,expected",
        [
            ("192.0.2.0/24", ["2.0.192.in-addr.arpa."]),
            # An address with a prefix names the network it sits in.
            ("192.0.2.17/24", ["2.0.192.in-addr.arpa."]),
            ("10.0.0.0/16", ["0.10.in-addr.arpa."]),
            # Longer than a /24: the zone is the /24 it belongs to.
            ("198.51.100.64/26", ["100.51.198.in-addr.arpa."]),
            ("198.51.100.7/32", ["100.51.198.in-addr.arpa."]),
            ("2001:db8::/32", ["8.b.d.0.1.0.0.2.ip6.arpa."]),
            ("2001:db8::/48", ["0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa."]),
            # Longer than a /64: the zone is the /64.
            (
                "2001:db8::1/128",
                ["0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa."],
            ),
        ],
    )
    def test_one_zone_per_network(self, network, expected):
        assert reverse_zones_for_network(network) == expected

    def test_a_network_spanning_boundaries_becomes_several_zones(self):
        assert reverse_zones_for_network("203.0.113.0/23") == [
            "112.0.203.in-addr.arpa.",
            "113.0.203.in-addr.arpa.",
        ]

    def test_a_whole_octet_is_still_one_zone(self):
        assert reverse_zones_for_network("10.0.0.0/8") == ["10.in-addr.arpa."]

    def test_an_oversized_network_is_refused_rather_than_expanded(self):
        # A /11 would be 32 zones, which is far more likely to be a typo than
        # a request.
        with pytest.raises(ReverseError, match="more than the 16"):
            reverse_zones_for_network("10.0.0.0/11")

    def test_nonsense_is_rejected_with_the_value_quoted(self):
        with pytest.raises(ReverseError, match="not a network"):
            reverse_zones_for_network("192.0.2.256/24")

    def test_several_networks_deduplicate(self):
        zones, problems = reverse_zones_for_networks("192.0.2.0/25, 192.0.2.128/25\n10.0.0.0/16")
        assert zones == ["2.0.192.in-addr.arpa.", "0.10.in-addr.arpa."]
        assert problems == []

    def test_a_bad_line_is_reported_and_the_rest_kept(self):
        zones, problems = reverse_zones_for_networks("192.0.2.0/24\nnot-a-network")
        assert zones == ["2.0.192.in-addr.arpa."]
        assert len(problems) == 1

    def test_ptr_names(self):
        assert ptr_name("192.0.2.10") == "10.2.0.192.in-addr.arpa."
        assert ptr_name("2001:db8::1").endswith(".8.b.d.0.1.0.0.2.ip6.arpa.")

    def test_a_hostname_is_not_an_address(self):
        with pytest.raises(ReverseError):
            ptr_name("www.example.com")

    def test_enclosing_zone_prefers_the_most_specific(self):
        zones = ["0.10.in-addr.arpa.", "2.1.10.in-addr.arpa.", "example.com."]
        assert enclosing_zone("3.2.1.10.in-addr.arpa.", zones) == "2.1.10.in-addr.arpa."

    def test_enclosing_zone_is_none_when_nothing_covers_it(self):
        assert enclosing_zone("1.2.0.192.in-addr.arpa.", ["example.com."]) is None

    def test_is_reverse_zone(self):
        assert is_reverse_zone("2.0.192.in-addr.arpa")
        assert is_reverse_zone("8.b.d.0.1.0.0.2.ip6.arpa.")
        assert not is_reverse_zone("example.com.")


class TestTheForms:
    def test_the_new_zone_form_offers_the_reverse_zone(self, client, users, login):
        login("operator")
        page = client.get("/zones/new").data
        assert b'name="create_reverse"' in page
        assert b'name="reverse_networks"' in page

    def test_the_record_editor_offers_the_ptr(self, client, users, login, zone):
        login("operator")
        page = client.get("/zones/example.com.").data
        assert b'name="sync_ptr"' in page
        # Shown for address records only; the dropdown reveals it.
        assert b'data-record-when-type="A,AAAA"' in page


class TestCreatingZonesWithTheirReverse:
    def _create(self, client, token, **extra):
        return client.post(
            "/zones/new",
            data={
                "csrf_token": token("/zones/new"),
                "name": "example.org",
                "kind": "Native",
                "nameservers": "ns1.example.com",
                **extra,
            },
            follow_redirects=True,
        )

    def test_the_reverse_zone_is_created_alongside(self, client, users, login, token, pdns):
        login("operator")
        page = self._create(client, token, create_reverse="on", reverse_networks="192.0.2.0/24")
        assert page.status_code == 200
        assert "example.org." in pdns.zones
        assert "2.0.192.in-addr.arpa." in pdns.zones
        assert b"Reverse zone 2.0.192.in-addr.arpa. has been created." in page.data

    def test_the_reverse_zone_inherits_kind_and_nameservers(
        self, client, users, login, token, pdns
    ):
        login("operator")
        self._create(
            client,
            token,
            kind="Master",
            create_reverse="on",
            reverse_networks="192.0.2.0/24",
            dnssec="on",
        )
        created = pdns.zones["2.0.192.in-addr.arpa."]
        assert created["kind"] == "Master"
        assert created["dnssec"] is True
        ns = [rrset for rrset in created["rrsets"] if rrset["type"] == "NS"]
        assert ns and ns[0]["records"][0]["content"] == "ns1.example.com."

    def test_several_zones_from_one_network(self, client, users, login, token, pdns):
        login("operator")
        self._create(client, token, create_reverse="on", reverse_networks="203.0.113.0/23")
        assert "112.0.203.in-addr.arpa." in pdns.zones
        assert "113.0.203.in-addr.arpa." in pdns.zones

    def test_a_bad_network_stops_the_whole_form(self, client, users, login, token, pdns):
        login("operator")
        page = self._create(client, token, create_reverse="on", reverse_networks="banana")
        assert page.status_code == 400
        # Nothing was created: a typo should not leave a half-made pair behind.
        assert "example.org." not in pdns.zones

    def test_ticking_the_box_without_a_network_is_an_error(self, client, users, login, token, pdns):
        login("operator")
        page = self._create(client, token, create_reverse="on", reverse_networks="  ")
        assert page.status_code == 400
        assert b"192.0.2.0/24" in page.data
        assert "example.org." not in pdns.zones

    def test_an_existing_reverse_zone_does_not_fail_the_forward_one(
        self, client, users, login, token, pdns, reverse_zone
    ):
        login("operator")
        page = self._create(client, token, create_reverse="on", reverse_networks="192.0.2.0/24")
        assert "example.org." in pdns.zones
        assert b"could not be created" in page.data
        assert b"already exists" in page.data

    def test_nothing_happens_without_the_box(self, client, users, login, token, pdns):
        login("operator")
        self._create(client, token, reverse_networks="192.0.2.0/24")
        assert "example.org." in pdns.zones
        assert "2.0.192.in-addr.arpa." not in pdns.zones


class TestLinkedPtrRecords:
    def test_saving_an_address_record_writes_the_ptr(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        page = save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        ptr = pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR")
        assert ptr is not None
        assert ptr["records"][0]["content"] == "mail.example.com."
        assert b"Reverse record 25.2.0.192.in-addr.arpa has been updated." in page.data
        with app.app_context():
            link = get_session().query(ReverseLink).one()
            assert link.forward_name == "mail.example.com."
            assert link.reverse_zone == "2.0.192.in-addr.arpa."

    def test_the_ptr_uses_the_forward_record_ttl(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="120", content="192.0.2.25", sync_ptr="on"
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR")["ttl"] == 120

    def test_ipv6_records_are_linked_too(self, client, users, login, token, pdns, zone):
        pdns.add_zone("8.b.d.0.1.0.0.2.ip6.arpa")
        login("operator")
        save_record(
            client,
            token,
            name="v6",
            type="AAAA",
            ttl="3600",
            content="2001:db8::1",
            sync_ptr="on",
        )
        zone_data = pdns.zones["8.b.d.0.1.0.0.2.ip6.arpa."]
        ptrs = [rrset for rrset in zone_data["rrsets"] if rrset["type"] == "PTR"]
        assert ptrs and ptrs[0]["records"][0]["content"] == "v6.example.com."

    def test_changing_the_address_moves_the_ptr(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        save_record(
            client,
            token,
            name="mail",
            type="A",
            ttl="3600",
            content="192.0.2.26",
            sync_ptr="on",
            original_name="mail",
            original_type="A",
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None
        new = pdns.rrset("2.0.192.in-addr.arpa", "26.2.0.192.in-addr.arpa", "PTR")
        assert new["records"][0]["content"] == "mail.example.com."

    def test_renaming_the_record_repoints_the_ptr(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        save_record(
            client,
            token,
            name="smtp",
            type="A",
            ttl="3600",
            content="192.0.2.25",
            sync_ptr="on",
            original_name="mail",
            original_type="A",
        )
        ptr = pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR")
        assert ptr["records"][0]["content"] == "smtp.example.com."

    def test_a_record_set_with_two_addresses_gets_two_ptrs(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client,
            token,
            name="pair",
            type="A",
            ttl="3600",
            content="192.0.2.31\n192.0.2.32",
            sync_ptr="on",
        )
        for last in ("31", "32"):
            assert (
                pdns.rrset("2.0.192.in-addr.arpa", f"{last}.2.0.192.in-addr.arpa", "PTR")
                is not None
            )

    def test_unticking_the_box_removes_the_ptr(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        page = save_record(
            client,
            token,
            name="mail",
            type="A",
            ttl="3600",
            content="192.0.2.25",
            original_name="mail",
            original_type="A",
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None
        assert b"has been removed" in page.data
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0

    def test_disabling_the_record_retires_the_ptr(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        save_record(
            client,
            token,
            name="mail",
            type="A",
            ttl="3600",
            content="192.0.2.25",
            sync_ptr="on",
            disabled="on",
            original_name="mail",
            original_type="A",
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None

    def test_deleting_the_record_deletes_the_ptr(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        client.post(
            "/zones/example.com./records/delete",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "mail",
                "type": "A",
            },
            follow_redirects=True,
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0

    def test_a_ptr_edited_by_hand_is_left_alone(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        """Taking a PTR over by editing it ends the link, without a surprise revert."""
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        client.post(
            "/zones/2.0.192.in-addr.arpa./records",
            data={
                "csrf_token": token("/zones/2.0.192.in-addr.arpa."),
                "name": "25",
                "type": "PTR",
                "ttl": "3600",
                "content": "elsewhere.example.com.",
                "original_name": "25",
                "original_type": "PTR",
            },
            follow_redirects=True,
        )
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0

        # The forward record is deleted; the PTR someone took over stays.
        client.post(
            "/zones/example.com./records/delete",
            data={"csrf_token": token("/zones/example.com."), "name": "mail", "type": "A"},
            follow_redirects=True,
        )
        ptr = pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR")
        assert ptr["records"][0]["content"] == "elsewhere.example.com."

    def test_no_reverse_zone_says_so_and_still_saves(
        self, app, client, users, login, token, pdns, zone
    ):
        login("operator")
        page = save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        assert pdns.rrset("example.com", "mail.example.com", "A") is not None
        assert b"No reverse zone on this server covers 192.0.2.25" in page.data
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0

    def test_a_second_record_taking_the_address_is_reported(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        page = save_record(
            client, token, name="smtp", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        assert b"now points here" in page.data
        ptr = pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR")
        assert ptr["records"][0]["content"] == "smtp.example.com."
        with app.app_context():
            assert get_session().query(ReverseLink).one().forward_name == "smtp.example.com."

    def test_deleting_the_reverse_zone_drops_the_links(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        client.post(
            "/zones/2.0.192.in-addr.arpa./delete",
            data={
                "csrf_token": token("/zones/2.0.192.in-addr.arpa."),
                "confirm": "2.0.192.in-addr.arpa",
            },
            follow_redirects=True,
        )
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0

    def test_the_zone_page_shows_both_ends_of_a_link(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        forward_page = client.get("/zones/example.com.")
        assert b'data-record-sync-ptr="1"' in forward_page.data
        reverse_page = client.get("/zones/2.0.192.in-addr.arpa.")
        assert b"linked" in reverse_page.data

    def test_a_user_without_the_reverse_zone_is_told_rather_than_denied(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        page = save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        assert pdns.rrset("example.com", "mail.example.com", "A") is not None
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None
        assert b"You do not have access to 2.0.192.in-addr.arpa." in page.data

    def test_a_granted_user_can_link_across_both_zones(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.add(ZoneAccess(user_id=users["viewer"], zone="2.0.192.in-addr.arpa."))
            session.commit()
        login("viewer")
        save_record(
            client, token, name="mail", type="A", ttl="3600", content="192.0.2.25", sync_ptr="on"
        )
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is not None

    def test_a_non_address_record_is_never_linked(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save_record(
            client,
            token,
            name="alias",
            type="CNAME",
            ttl="3600",
            content="www.example.com.",
            sync_ptr="on",
        )
        with app.app_context():
            assert get_session().query(ReverseLink).count() == 0
