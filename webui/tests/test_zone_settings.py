"""The per-zone settings page: nameservers, the SOA, and the reverse zone link."""

from __future__ import annotations

import re

import pytest

from app.database import get_session
from app.dnsutil import Soa, email_to_rname, parse_soa, rname_to_email, validate_email
from app.models import ZoneAccess, ZoneReverseLink

SOA_CONTENT = "ns1.example.com. hostmaster.example.com. 7 10800 3600 604800 3600"


@pytest.fixture
def zone(pdns):
    pdns.add_zone(
        "example.com",
        rrsets=[
            {
                "name": "example.com.",
                "type": "SOA",
                "ttl": 3600,
                "records": [{"content": SOA_CONTENT, "disabled": False}],
                "comments": [],
            },
            {
                "name": "example.com.",
                "type": "NS",
                "ttl": 3600,
                "records": [
                    {"content": "ns1.example.com.", "disabled": False},
                    {"content": "ns2.example.com.", "disabled": False},
                ],
                "comments": [],
            },
        ],
    )
    return pdns.zones["example.com."]


@pytest.fixture
def reverse_zone(pdns):
    pdns.add_zone("2.0.192.in-addr.arpa")
    return pdns.zones["2.0.192.in-addr.arpa."]


def save(client, token, **form):
    """Post the settings form, filled in with what a save actually sends."""
    data = {
        "csrf_token": token("/zones/example.com./settings"),
        "kind": "Native",
        "nameservers": "ns1.example.com\nns2.example.com",
        "ns_ttl": "3600",
        "mname": "ns1.example.com.",
        "email": "hostmaster@example.com",
        "refresh": "10800",
        "retry": "3600",
        "expire": "604800",
        "minimum": "3600",
        "soa_ttl": "3600",
    }
    data.update(form)
    return client.post("/zones/example.com./settings", data=data, follow_redirects=True)


class TestSoaHelpers:
    def test_the_seven_fields_round_trip(self):
        soa = parse_soa(SOA_CONTENT)
        assert soa == Soa(
            mname="ns1.example.com.",
            rname="hostmaster.example.com.",
            serial=7,
            refresh=10800,
            retry=3600,
            expire=604800,
            minimum=3600,
        )
        assert soa.to_content() == SOA_CONTENT

    @pytest.mark.parametrize("content", ["", "not an soa", "ns1. host. 1 2 3", "a b c d e f g"])
    def test_unparseable_content_is_none(self, content):
        assert parse_soa(content) is None

    def test_the_address_is_shown_as_an_address(self):
        assert parse_soa(SOA_CONTENT).email == "hostmaster@example.com"

    def test_a_dot_in_the_local_part_is_escaped(self):
        """first.last@ is one label with an escaped dot, not two labels."""
        assert email_to_rname("first.last@example.com") == r"first\.last.example.com."
        assert rname_to_email(r"first\.last.example.com.") == "first.last@example.com"

    def test_an_rname_that_is_not_an_address_is_left_alone(self):
        assert rname_to_email("hostmaster") == "hostmaster"

    @pytest.mark.parametrize("value", ["", "nope", "a@b", "two@at@signs.com"])
    def test_bad_addresses_are_rejected(self, value):
        assert validate_email(value) is not None

    def test_a_good_address_is_accepted(self):
        assert validate_email("hostmaster@example.com") is None


class TestThePage:
    def test_it_shows_the_current_settings(self, client, users, login, zone):
        login("operator")
        page = client.get("/zones/example.com./settings").data
        assert b'value="hostmaster@example.com"' in page
        assert b"ns1.example.com" in page
        assert b'value="604800"' in page

    def test_it_is_linked_from_the_zone_page(self, client, users, login, zone):
        login("operator")
        assert b"/zones/example.com./settings" in client.get("/zones/example.com.").data

    def test_a_user_without_access_is_refused(self, client, users, login, zone):
        login("viewer")
        assert client.get("/zones/example.com./settings").status_code == 403

    def test_a_granted_user_cannot_change_the_kind(
        self, app, client, users, login, token, pdns, zone
    ):
        """Editing records is one thing; changing how the zone is served is not."""
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        save(client, token, kind="Master", email="dns-team@example.net")
        assert pdns.zones["example.com."]["kind"] == "Native"
        # Their SOA edit still went through.
        soa = parse_soa(pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"])
        assert soa.email == "dns-team@example.net"

    def test_a_granted_user_may_open_it(self, app, client, users, login, zone):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        assert client.get("/zones/example.com./settings").status_code == 200

    def test_a_missing_zone_is_a_404(self, client, users, login, pdns):
        login("operator")
        assert client.get("/zones/nope.example./settings").status_code == 404


class TestNameservers:
    def test_saving_replaces_the_whole_set(self, client, users, login, token, pdns, zone):
        login("operator")
        save(client, token, nameservers="ns3.example.net\nns4.example.net")
        ns = pdns.rrset("example.com", "example.com", "NS")
        assert [record["content"] for record in ns["records"]] == [
            "ns3.example.net.",
            "ns4.example.net.",
        ]

    def test_the_trailing_dot_is_added(self, client, users, login, token, pdns, zone):
        login("operator")
        save(client, token, nameservers="ns1.example.com")
        ns = pdns.rrset("example.com", "example.com", "NS")
        assert ns["records"][0]["content"] == "ns1.example.com."

    def test_the_ttl_is_saved(self, client, users, login, token, pdns, zone):
        login("operator")
        save(client, token, ns_ttl="60")
        assert pdns.rrset("example.com", "example.com", "NS")["ttl"] == 60

    def test_an_empty_list_is_refused(self, client, users, login, token, pdns, zone):
        login("operator")
        page = save(client, token, nameservers="  ")
        assert page.status_code == 400
        # The old set is still there.
        assert len(pdns.rrset("example.com", "example.com", "NS")["records"]) == 2


class TestSoa:
    def test_the_administrator_address_is_saved_as_an_rname(
        self, client, users, login, token, pdns, zone
    ):
        login("operator")
        save(client, token, email="dns-team@example.net")
        soa = parse_soa(pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"])
        assert soa.rname == "dns-team.example.net."
        assert soa.email == "dns-team@example.net"

    def test_the_serial_is_carried_across_rather_than_reset(
        self, client, users, login, token, pdns, zone
    ):
        """PowerDNS owns the serial; the form must not send it backwards."""
        login("operator")
        save(client, token, email="dns-team@example.net")
        soa = parse_soa(pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"])
        assert soa.serial == 7

    def test_the_timers_are_saved(self, client, users, login, token, pdns, zone):
        login("operator")
        save(client, token, refresh="7200", retry="1800", expire="1209600", minimum="300")
        soa = parse_soa(pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"])
        assert (soa.refresh, soa.retry, soa.expire, soa.minimum) == (7200, 1800, 1209600, 300)

    def test_the_primary_nameserver_is_saved(self, client, users, login, token, pdns, zone):
        login("operator")
        save(client, token, mname="ns-primary.example.net")
        soa = parse_soa(pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"])
        assert soa.mname == "ns-primary.example.net."

    def test_a_bad_address_is_refused_and_nothing_is_written(
        self, client, users, login, token, pdns, zone
    ):
        login("operator")
        page = save(client, token, email="not-an-address")
        assert page.status_code == 400
        assert pdns.rrset("example.com", "example.com", "SOA")["records"][0]["content"] == (
            SOA_CONTENT
        )

    def test_a_negative_timer_is_refused(self, client, users, login, token, pdns, zone):
        login("operator")
        assert save(client, token, refresh="-1").status_code == 400


class TestSlaveZones:
    @pytest.fixture
    def slave(self, pdns):
        pdns.add_zone("slave.example", kind="Slave")
        return pdns.zones["slave.example."]

    def test_the_content_fields_are_not_offered(self, client, users, login, slave):
        login("operator")
        page = client.get("/zones/slave.example./settings").data
        # The card is rendered but hidden: the master supplies these.
        assert b"transfers its contents from a master" in page

    def test_masters_can_be_changed(self, client, users, login, token, pdns, slave):
        login("operator")
        client.post(
            "/zones/slave.example./settings",
            data={
                "csrf_token": token("/zones/slave.example./settings"),
                "kind": "Slave",
                "masters": "192.0.2.53\n2001:db8::53",
            },
            follow_redirects=True,
        )
        assert pdns.zones["slave.example."]["masters"] == ["192.0.2.53", "2001:db8::53"]

    def test_a_slave_without_a_master_is_refused(self, client, users, login, token, slave):
        login("operator")
        response = client.post(
            "/zones/slave.example./settings",
            data={
                "csrf_token": token("/zones/slave.example./settings"),
                "kind": "Slave",
                "masters": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 400

    def test_no_soa_is_written_for_a_slave(self, client, users, login, token, pdns, slave):
        login("operator")
        client.post(
            "/zones/slave.example./settings",
            data={
                "csrf_token": token("/zones/slave.example./settings"),
                "kind": "Slave",
                "masters": "192.0.2.53",
                # A hand-posted SOA must not be written over the transfer.
                "email": "someone@example.net",
                "mname": "ns1.example.net.",
            },
            follow_redirects=True,
        )
        # The save went through -- the masters prove it -- and still wrote no
        # SOA or NS, which are the master's to supply.
        assert pdns.zones["slave.example."]["masters"] == ["192.0.2.53"]
        assert pdns.rrset("slave.example", "slave.example", "SOA") is None
        assert pdns.rrset("slave.example", "slave.example", "NS") is None


class TestTheReverseZoneLink:
    def test_linking_stores_the_pair(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")
        with app.app_context():
            link = get_session().query(ZoneReverseLink).one()
            assert (link.forward_zone, link.reverse_zone) == (
                "example.com.",
                "2.0.192.in-addr.arpa.",
            )

    def test_unlinking_removes_it(self, app, client, users, login, token, pdns, zone, reverse_zone):
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")
        save(client, token)
        with app.app_context():
            assert get_session().query(ZoneReverseLink).count() == 0

    def test_a_zone_that_is_not_a_reverse_zone_is_refused(
        self, app, client, users, login, token, pdns, zone
    ):
        pdns.add_zone("other.example")
        login("operator")
        page = save(client, token, reverse_zones="other.example.")
        assert page.status_code == 400
        with app.app_context():
            assert get_session().query(ZoneReverseLink).count() == 0

    def test_a_reverse_zone_the_user_cannot_see_is_refused(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        assert save(client, token, reverse_zones="2.0.192.in-addr.arpa.").status_code == 400

    def test_an_operator_can_create_and_link_in_one_step(
        self, app, client, users, login, token, pdns, zone
    ):
        login("operator")
        page = save(client, token, reverse_networks="192.0.2.0/24")
        assert "2.0.192.in-addr.arpa." in pdns.zones
        assert b"has been created" in page.data
        with app.app_context():
            assert (
                get_session().query(ZoneReverseLink).one().reverse_zone == "2.0.192.in-addr.arpa."
            )

    def test_a_plain_user_cannot_create_one(self, app, client, users, login, token, pdns, zone):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        page = save(client, token, reverse_networks="192.0.2.0/24")
        assert page.status_code == 400
        assert "2.0.192.in-addr.arpa." not in pdns.zones

    def test_a_mistyped_network_changes_nothing(self, app, client, users, login, token, pdns, zone):
        login("operator")
        assert save(client, token, reverse_networks="banana").status_code == 400
        with app.app_context():
            assert get_session().query(ZoneReverseLink).count() == 0

    def test_both_zone_pages_show_the_pairing(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")
        forward = client.get("/zones/example.com.").data
        assert b"2.0.192.in-addr.arpa" in forward
        reverse_page = client.get("/zones/2.0.192.in-addr.arpa.").data
        assert b"reverse zone for" in reverse_page
        assert b"example.com" in reverse_page


class TestTheRecordEditorFollowsTheLink:
    def test_the_reverse_record_is_offered_by_default_when_linked(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")
        page = client.get("/zones/example.com.").data
        assert b'data-record-default="1"' in page
        assert b"This zone is linked to" in page
        # Ticked server-side as well, so the form works without JavaScript.
        checkbox = re.search(rb'<input[^>]*name="sync_ptr"[^>]*>', page)
        assert checkbox and b"checked" in checkbox.group(0)

    def test_it_is_not_ticked_without_a_link(self, client, users, login, zone, reverse_zone):
        login("operator")
        page = client.get("/zones/example.com.").data
        assert b'data-record-default=""' in page
        assert b"Link one on the" in page

    def test_the_linked_zone_wins_over_a_more_specific_unlinked_one(
        self, app, client, users, login, token, pdns, zone
    ):
        """The pairing is a statement about where PTRs belong, so it is used first."""
        pdns.add_zone("0.192.in-addr.arpa")  # the linked /16
        pdns.add_zone("2.0.192.in-addr.arpa")  # a /24 nobody linked
        login("operator")
        save(client, token, reverse_zones="0.192.in-addr.arpa.")

        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "mail",
                "type": "A",
                "ttl": "3600",
                "content": "192.0.2.25",
                "sync_ptr": "on",
            },
            follow_redirects=True,
        )
        assert pdns.rrset("0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is not None
        assert pdns.rrset("2.0.192.in-addr.arpa", "25.2.0.192.in-addr.arpa", "PTR") is None

    def test_an_address_outside_the_linked_zone_still_finds_one(
        self, client, users, login, token, pdns, zone, reverse_zone
    ):
        """Linking a zone must not make the other reverse zones unusable."""
        pdns.add_zone("100.51.198.in-addr.arpa")
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")

        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "far",
                "type": "A",
                "ttl": "3600",
                "content": "198.51.100.9",
                "sync_ptr": "on",
            },
            follow_redirects=True,
        )
        assert pdns.rrset("100.51.198.in-addr.arpa", "9.100.51.198.in-addr.arpa", "PTR") is not None

    def test_deleting_the_zone_drops_the_pairing(
        self, app, client, users, login, token, pdns, zone, reverse_zone
    ):
        login("operator")
        save(client, token, reverse_zones="2.0.192.in-addr.arpa.")
        client.post(
            "/zones/example.com./delete",
            data={"csrf_token": token("/zones/example.com."), "confirm": "example.com"},
            follow_redirects=True,
        )
        with app.app_context():
            assert get_session().query(ZoneReverseLink).count() == 0
