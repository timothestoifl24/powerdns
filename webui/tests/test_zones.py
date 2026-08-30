"""Zone and record management, and the access rules around them."""

from __future__ import annotations

import pytest

from app.database import get_session
from app.models import ZoneAccess
from app.pdns import PdnsClient, PdnsError, absolute_name, canonical, relative_name


@pytest.fixture
def zone(pdns):
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


class TestNameHelpers:
    @pytest.mark.parametrize(
        "raw,expected",
        [("Example.COM", "example.com."), ("example.com.", "example.com."), ("", ""), (".", ".")],
    )
    def test_canonical(self, raw, expected):
        assert canonical(raw) == expected

    def test_relative_name(self):
        assert relative_name("www.example.com.", "example.com") == "www"
        assert relative_name("example.com.", "example.com") == "@"

    def test_absolute_name(self):
        assert absolute_name("@", "example.com") == "example.com."
        assert absolute_name("www", "example.com") == "www.example.com."
        assert absolute_name("other.net.", "example.com") == "other.net."


class TestPdnsClient:
    def _client(self, pdns):
        return PdnsClient("http://pdns.test:8081", pdns.api_key, session=pdns)

    def test_wrong_api_key_gives_a_clear_message(self, pdns):
        client = PdnsClient("http://pdns.test:8081", "wrong-key", session=pdns)
        with pytest.raises(PdnsError, match="rejected the API key"):
            client.server_info()

    def test_ping_reports_failure_rather_than_raising(self, pdns):
        assert PdnsClient("http://x", "wrong", session=pdns).ping() is False

    def test_missing_zone_is_reported_as_not_found(self, pdns):
        with pytest.raises(PdnsError) as caught:
            self._client(pdns).get_zone("nope.example.")
        assert caught.value.is_not_found

    def test_error_body_is_surfaced(self, pdns, zone):
        with pytest.raises(PdnsError, match="already exists"):
            self._client(pdns).create_zone("example.com")

    def test_statistics_are_flattened(self, pdns):
        assert self._client(pdns).statistics()["udp-queries"] == "42"

    def test_slave_zone_sends_masters_and_no_nameservers(self, pdns):
        client = self._client(pdns)
        client.create_zone("slave.test", kind="Slave", masters=["192.0.2.53"], nameservers=["ns1."])
        created = pdns.zones["slave.test."]
        assert created["masters"] == ["192.0.2.53"]
        assert not any(rrset["type"] == "NS" for rrset in created["rrsets"])

    def test_replace_rrset_clears_a_stale_comment(self, pdns, zone):
        """Omitting comments would leave the old note attached to new content."""
        client = self._client(pdns)
        client.replace_rrset(
            "example.com.", "www.example.com.", "A", 60, ["192.0.2.9"], comment="note"
        )
        assert pdns.rrset("example.com", "www.example.com", "A")["comments"][0]["content"] == "note"
        client.replace_rrset("example.com.", "www.example.com.", "A", 60, ["192.0.2.9"])
        assert pdns.rrset("example.com", "www.example.com", "A")["comments"] == []


class TestZoneAccess:
    def test_operator_sees_every_zone(self, client, users, login, zone):
        login("operator")
        # Assert the zone is linked, not merely that the string "example.com"
        # appears somewhere: a bare substring would also match an unrelated
        # mention, or a lookalike such as notexample.com.
        assert b'href="/zones/example.com."' in client.get("/zones/").data

    def test_plain_user_sees_no_zones_without_a_grant(self, client, users, login, zone):
        login("viewer")
        page = client.get("/zones/")
        assert b"No zones have been shared" in page.data

    def test_plain_user_sees_a_granted_zone(self, app, client, users, login, zone):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        assert b'href="/zones/example.com."' in client.get("/zones/").data

    def test_plain_user_cannot_open_an_ungranted_zone(self, client, users, login, zone):
        login("viewer")
        assert client.get("/zones/example.com.").status_code == 403

    def test_plain_user_cannot_create_a_zone(self, client, users, login):
        login("viewer")
        assert client.get("/zones/new").status_code == 403

    def test_plain_user_cannot_delete_a_zone(self, client, users, login, token, zone):
        login("viewer")
        response = client.post(
            "/zones/example.com./delete",
            data={"csrf_token": token("/"), "confirm": "example.com"},
        )
        assert response.status_code == 403

    def test_anonymous_is_redirected_to_login(self, client):
        response = client.get("/zones/")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


class TestZoneCreation:
    def test_operator_creates_a_zone(self, client, users, login, token, pdns):
        login("operator")
        response = client.post(
            "/zones/new",
            data={
                "csrf_token": token("/zones/new"),
                "name": "newzone.test",
                "kind": "Native",
                "nameservers": "ns1.example.com\nns2.example.com",
            },
        )
        assert response.status_code == 302
        assert "newzone.test." in pdns.zones
        ns = pdns.rrset("newzone.test", "newzone.test", "NS")
        assert len(ns["records"]) == 2

    def test_missing_name_is_rejected(self, client, users, login, token, pdns):
        login("operator")
        response = client.post(
            "/zones/new", data={"csrf_token": token("/zones/new"), "kind": "Native"}
        )
        assert response.status_code == 400
        assert b"Enter a zone name" in response.data
        assert pdns.zones == {}

    def test_slave_without_a_master_is_rejected(self, client, users, login, token, pdns):
        login("operator")
        response = client.post(
            "/zones/new",
            data={"csrf_token": token("/zones/new"), "name": "s.test", "kind": "Slave"},
        )
        assert response.status_code == 400
        assert b"needs at least one master" in response.data

    def test_duplicate_zone_surfaces_the_api_error(self, client, users, login, token, zone):
        login("operator")
        response = client.post(
            "/zones/new",
            data={
                "csrf_token": token("/zones/new"),
                "name": "example.com",
                "kind": "Native",
                "nameservers": "ns1.example.com",
            },
        )
        assert response.status_code == 400
        assert b"already exists" in response.data


class TestRecords:
    def test_add_a_record(self, client, users, login, token, pdns, zone):
        login("operator")
        response = client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "mail",
                "type": "A",
                "ttl": "300",
                "content": "192.0.2.25",
            },
        )
        assert response.status_code == 302
        record = pdns.rrset("example.com", "mail.example.com", "A")
        assert record["ttl"] == 300
        assert record["records"][0]["content"] == "192.0.2.25"

    def test_multiple_values_become_one_record_set(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "@",
                "type": "MX",
                "ttl": "3600",
                "content": "10 mail1.example.com.\n20 mail2.example.com.",
            },
        )
        assert len(pdns.rrset("example.com", "example.com", "MX")["records"]) == 2

    def test_invalid_content_is_rejected_before_reaching_powerdns(
        self, client, users, login, token, pdns, zone
    ):
        login("operator")
        before = len(pdns.requests)
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "bad",
                "type": "A",
                "ttl": "3600",
                "content": "not-an-ip-address",
            },
        )
        assert pdns.rrset("example.com", "bad.example.com", "A") is None
        assert ("PATCH", "/servers/localhost/zones/example.com.") not in pdns.requests[before:]

    def test_record_outside_the_zone_is_rejected(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "www.other.net.",
                "type": "A",
                "ttl": "3600",
                "content": "192.0.2.1",
            },
        )
        assert pdns.rrset("example.com", "www.other.net", "A") is None

    def test_cname_at_the_apex_is_rejected(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "@",
                "type": "CNAME",
                "ttl": "3600",
                "content": "target.example.net.",
            },
        )
        assert pdns.rrset("example.com", "example.com", "CNAME") is None

    def test_renaming_removes_the_old_record_set(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "web",
                "type": "A",
                "ttl": "3600",
                "content": "192.0.2.1",
                "original_name": "www",
                "original_type": "A",
            },
        )
        assert pdns.rrset("example.com", "www.example.com", "A") is None
        assert pdns.rrset("example.com", "web.example.com", "A") is not None

    def test_delete_a_record(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records/delete",
            data={"csrf_token": token("/zones/example.com."), "name": "www", "type": "A"},
        )
        assert pdns.rrset("example.com", "www.example.com", "A") is None

    def test_soa_cannot_be_deleted(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records/delete",
            data={"csrf_token": token("/zones/example.com."), "name": "@", "type": "SOA"},
        )
        assert pdns.rrset("example.com", "example.com", "SOA") is not None

    def test_dnssec_records_cannot_be_edited(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "@",
                "type": "RRSIG",
                "ttl": "3600",
                "content": "whatever",
            },
        )
        assert pdns.rrset("example.com", "example.com", "RRSIG") is None

    def test_granted_user_can_edit_their_zone(self, app, client, users, login, token, pdns, zone):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "ok",
                "type": "A",
                "ttl": "3600",
                "content": "192.0.2.7",
            },
        )
        assert pdns.rrset("example.com", "ok.example.com", "A") is not None


class TestZoneActions:
    def test_delete_needs_the_exact_name(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./delete",
            data={"csrf_token": token("/zones/example.com."), "confirm": "wrong.com"},
        )
        assert "example.com." in pdns.zones

        client.post(
            "/zones/example.com./delete",
            data={"csrf_token": token("/zones/example.com."), "confirm": "example.com"},
        )
        assert "example.com." not in pdns.zones

    def test_export_returns_a_zone_file(self, client, users, login, zone):
        login("operator")
        response = client.get("/zones/example.com./export")
        assert response.status_code == 200
        assert b"www.example.com." in response.data
        assert "attachment" in response.headers["Content-Disposition"]

    def test_notify(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post("/zones/example.com./notify", data={"csrf_token": token("/zones/example.com.")})
        assert "example.com." in pdns.notified

    def test_enabling_dnssec_creates_a_key(self, client, users, login, token, pdns, zone):
        login("operator")
        client.post(
            "/zones/example.com./dnssec",
            data={"csrf_token": token("/zones/example.com."), "action": "enable"},
        )
        assert pdns.zones["example.com."]["dnssec"] is True
        assert len(pdns.cryptokeys["example.com."]) == 1

    def test_disabling_dnssec_removes_the_keys(self, client, users, login, token, pdns, zone):
        login("operator")
        csrf = token("/zones/example.com.")
        client.post("/zones/example.com./dnssec", data={"csrf_token": csrf, "action": "enable"})
        client.post("/zones/example.com./dnssec", data={"csrf_token": csrf, "action": "disable"})
        assert pdns.zones["example.com."]["dnssec"] is False
        assert pdns.cryptokeys["example.com."] == []


class TestPowerDnsOutage:
    def test_dashboard_explains_an_unreachable_api(self, client, users, login, monkeypatch):
        login("admin")
        import app.views.dashboard as dashboard

        def unreachable(_config):
            class Dead:
                def list_zones(self):
                    raise PdnsError("Cannot reach the PowerDNS API at http://pdns:8081")

            return Dead()

        monkeypatch.setattr(dashboard, "client_from_config", unreachable)
        page = client.get("/")
        assert page.status_code == 200
        assert b"PowerDNS is not reachable" in page.data
