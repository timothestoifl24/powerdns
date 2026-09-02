"""Forward zones and global forwarders.

Forwarding is a PowerDNS Recursor feature: the Authoritative Server dropped its
`recursor=` setting in 4.1 and cannot forward at all. These exercise the panel's
recursor client and the Forwarding page against the fake transport.
"""

from __future__ import annotations

import pytest
from conftest import RECURSOR_API_KEY, csrf_from

from app.recursor import (
    ForwardZone,
    RecursorNotConfigured,
    SyncResult,
    normalise_server,
    parse_servers,
    sync_local_zones,
    zone_id_for,
)

LOCAL_TARGET = "172.29.0.10:53"


def client_for(fake):
    from app.recursor import RecursorClient

    return RecursorClient(
        base_url="http://recursor.test:8082", api_key=RECURSOR_API_KEY, session=fake
    )


class TestZoneIdEncoding:
    """Mirrors apiNameToId in the PowerDNS source; verified against a live
    recursor, which answers `=2E` for the root and `=5Fsub...` for a leading
    underscore."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (".", "=2E"),
            ("example.com", "example.com."),
            ("example.com.", "example.com."),
            ("Example.COM", "example.com."),
            ("_sub.example.com", "=5Fsub.example.com."),
            ("10.in-addr.arpa", "10.in-addr.arpa."),
        ],
    )
    def test_encoding(self, name, expected):
        assert zone_id_for(name) == expected


class TestForwarderAddresses:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("10.0.0.1", "10.0.0.1:53"),
            (" 10.0.0.1 ", "10.0.0.1:53"),
            ("10.0.0.1:5353", "10.0.0.1:5353"),
            ("2001:db8::1", "[2001:db8::1]:53"),
            ("[2001:db8::1]:5353", "[2001:db8::1]:5353"),
        ],
    )
    def test_accepted(self, given, expected):
        assert normalise_server(given) == expected

    def test_a_host_name_is_refused_with_the_reason(self):
        """The recursor reads forwarders from its config before it can resolve
        anything, so a name here would produce a zone that never answers."""
        with pytest.raises(ValueError, match="not an IP address"):
            normalise_server("dns.example.com")

    @pytest.mark.parametrize("bad", ["", "   ", "10.0.0.1:0", "10.0.0.1:70000", "10.0.0.1:abc"])
    def test_rejected(self, bad):
        with pytest.raises(ValueError):
            normalise_server(bad)

    def test_a_list_is_split_on_commas_and_newlines(self):
        assert parse_servers("1.1.1.1, 9.9.9.9\n8.8.8.8") == [
            "1.1.1.1:53",
            "9.9.9.9:53",
            "8.8.8.8:53",
        ]

    def test_duplicates_are_dropped_but_order_is_kept(self):
        """The order is the query order, so it must not be sorted away."""
        assert parse_servers("9.9.9.9\n1.1.1.1\n9.9.9.9") == ["9.9.9.9:53", "1.1.1.1:53"]

    def test_an_empty_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one"):
            parse_servers("  \n , ")


class TestRecursorClient:
    def test_built_in_reverse_zones_are_not_listed_as_forwarding(self, recursor):
        """A recursor reports ~20 RFC 1918 reverse zones as Native. Showing
        them on the page would bury the operator's own rules."""
        assert client_for(recursor).forward_zones() == []

    def test_a_forward_zone_round_trips(self, recursor):
        client = client_for(recursor)
        client.save_forward_zone("corp.internal", ["10.0.0.5:53"])
        zones = client.forward_zones()
        assert [zone.name for zone in zones] == ["corp.internal."]
        assert zones[0].servers == ("10.0.0.5:53",)

    def test_saving_twice_replaces_rather_than_failing(self, recursor):
        """POST is refused for a name the recursor knows, so save falls back to
        PUT. Without that, editing a forward zone would be impossible."""
        client = client_for(recursor)
        client.save_forward_zone("corp.internal", ["10.0.0.5:53"])
        client.save_forward_zone("corp.internal", ["10.0.0.6:53", "10.0.0.7:53"])
        assert recursor.forwarded["corp.internal."] == ["10.0.0.6:53", "10.0.0.7:53"]

    def test_a_built_in_reverse_zone_can_be_taken_over(self, recursor):
        """Pointing 192.168.in-addr.arpa at an internal server is a normal
        thing to want, and the recursor already owns that name."""
        client = client_for(recursor)
        client.save_forward_zone("168.192.in-addr.arpa", ["10.0.0.5:53"])
        assert recursor.forwarded["168.192.in-addr.arpa."] == ["10.0.0.5:53"]

    def test_deleting_uses_the_id_the_recursor_stored(self, recursor):
        """Zone ids keep their original case and DELETE matches the stored id
        exactly, so an id we lower-cased ourselves reads back fine and then
        fails to delete with a 422."""
        recursor.add_forward_zone("Mixed.Case.Test.", ["10.0.0.5:53"])
        client = client_for(recursor)
        client.delete_forward_zone("mixed.case.TEST")
        assert "Mixed.Case.Test." not in recursor.forwarded

    def test_global_forwarders_are_the_root_zone(self, recursor):
        client = client_for(recursor)
        client.save_forward_zone(".", ["1.1.1.1:53"], recursion_desired=True)
        zone = client.get_forward_zone(".")
        assert zone is not None
        assert zone.is_global and zone.recursion_desired
        assert recursor.zones["=2E"]["name"] == "."

    def test_global_forwarders_sort_first(self, recursor):
        client = client_for(recursor)
        client.save_forward_zone("zzz.test", ["10.0.0.5:53"])
        client.save_forward_zone(".", ["1.1.1.1:53"], recursion_desired=True)
        client.save_forward_zone("aaa.test", ["10.0.0.5:53"])
        assert [zone.name for zone in client.forward_zones()] == [".", "aaa.test.", "zzz.test."]

    def test_a_missing_zone_reads_as_none(self, recursor):
        assert client_for(recursor).get_forward_zone("nowhere.test") is None

    def test_saving_with_no_servers_is_refused_before_the_request(self, recursor):
        with pytest.raises(ValueError):
            client_for(recursor).save_forward_zone("corp.internal", [])

    def test_a_wrong_key_says_which_key(self, recursor):
        """Pointing an operator at PDNS_API_KEY when the recursor is the one
        refusing sends them to the wrong container."""
        from app.pdns import PdnsError
        from app.recursor import RecursorClient

        client = RecursorClient(
            base_url="http://recursor.test:8082", api_key="wrong", session=recursor
        )
        with pytest.raises(PdnsError, match="RECURSOR_API_KEY"):
            client.forward_zones()


class TestCacheIsFlushedOnChange:
    """A resolver does not re-evaluate what it already has cached, and that
    cache includes failures. Without a flush, adding a forward zone and
    immediately querying it returns the answer from before the change -- which
    is indistinguishable from the forward zone not working."""

    def test_saving_flushes_the_zone(self, recursor):
        client_for(recursor).save_forward_zone("corp.internal", ["10.0.0.5:53"])
        assert recursor.flushed == [("corp.internal.", "true")]

    def test_deleting_flushes_the_zone(self, recursor):
        recursor.add_forward_zone("corp.internal", ["10.0.0.5:53"])
        client = client_for(recursor)
        recursor.flushed.clear()
        client.delete_forward_zone("corp.internal")
        assert recursor.flushed == [("corp.internal.", "true")]

    def test_global_forwarders_flush_the_whole_tree(self, recursor):
        """Changing where everything goes invalidates everything."""
        client_for(recursor).save_forward_zone(".", ["1.1.1.1:53"], recursion_desired=True)
        assert recursor.flushed == [(".", "true")]

    def test_a_failed_flush_does_not_fail_the_save(self, recursor):
        """The forwarding change is already made and correct; a resolver that
        will not drop its cache is a slow rollout, not a failed save."""
        recursor.flush_fails = True
        client_for(recursor).save_forward_zone("corp.internal", ["10.0.0.5:53"])
        assert recursor.forwarded["corp.internal."] == ["10.0.0.5:53"]

    def test_the_sync_flushes_each_zone_it_changes(self, recursor):
        client = client_for(recursor)
        sync_local_zones(["example.com"], client, LOCAL_TARGET)
        assert ("example.com.", "true") in recursor.flushed

    def test_an_unchanged_sync_flushes_nothing(self, recursor):
        """Flushing on every page load would throw away a warm cache."""
        client = client_for(recursor)
        sync_local_zones(["example.com"], client, LOCAL_TARGET)
        recursor.flushed.clear()
        sync_local_zones(["example.com"], client, LOCAL_TARGET)
        assert recursor.flushed == []


class TestLocalZoneSync:
    """The recursor is the front door, so a zone this stack hosts is answered
    from the public internet unless a rule sends it to the authoritative
    server."""

    def test_every_authoritative_zone_gets_a_rule(self, recursor):
        client = client_for(recursor)
        result = sync_local_zones(["example.com", "corp.internal"], client, LOCAL_TARGET)
        assert result.added == ("corp.internal.", "example.com.")
        assert recursor.forwarded["example.com."] == [LOCAL_TARGET]

    def test_it_is_idempotent(self, recursor):
        client = client_for(recursor)
        sync_local_zones(["example.com"], client, LOCAL_TARGET)
        again = sync_local_zones(["example.com"], client, LOCAL_TARGET)
        assert not again.changed
        assert again.summary() == "no changes"

    def test_a_deleted_zone_stops_being_forwarded(self, recursor):
        client = client_for(recursor)
        sync_local_zones(["example.com", "gone.test"], client, LOCAL_TARGET)
        result = sync_local_zones(["example.com"], client, LOCAL_TARGET)
        assert result.removed == ("gone.test.",)
        assert "gone.test." not in recursor.forwarded

    def test_a_rule_pointing_elsewhere_is_never_touched(self, recursor):
        """An operator's deliberate forward for a name that happens to match an
        authoritative zone must not be silently redirected or deleted."""
        recursor.add_forward_zone("corp.internal", ["10.9.9.9:53"])
        client = client_for(recursor)
        result = sync_local_zones(["corp.internal"], client, LOCAL_TARGET)
        assert not result.changed
        assert recursor.forwarded["corp.internal."] == ["10.9.9.9:53"]

    def test_global_forwarders_survive_a_sync(self, recursor):
        client = client_for(recursor)
        client.save_forward_zone(".", [LOCAL_TARGET], recursion_desired=True)
        sync_local_zones(["example.com"], client, LOCAL_TARGET)
        assert client.get_forward_zone(".") is not None

    def test_the_summary_names_what_changed(self):
        result = SyncResult(added=("a.test.",), removed=("b.test.",))
        assert "started forwarding a.test." in result.summary()
        assert "stopped forwarding b.test." in result.summary()


class TestForwardingPage:
    def _login(self, client, username="admin"):
        page = client.get("/auth/login")
        return client.post(
            "/auth/login",
            data={
                "username": username,
                "password": f"{username}-password-123",
                "csrf_token": csrf_from(page.data),
            },
        )

    @pytest.fixture
    def users(self, forwarding_app):
        from app.config import AUTH_LOCAL, ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER
        from app.database import get_session
        from app.models import User
        from app.security import hash_password

        with forwarding_app.app_context():
            session = get_session()
            for username, role in (
                ("admin", ROLE_ADMIN),
                ("operator", ROLE_OPERATOR),
                ("viewer", ROLE_USER),
            ):
                session.add(
                    User(
                        username=username,
                        auth_source=AUTH_LOCAL,
                        password_hash=hash_password(f"{username}-password-123"),
                        role=role,
                        is_active=True,
                    )
                )
            session.commit()

    def test_it_lists_forward_zones(self, forwarding_client, users, recursor):
        recursor.add_forward_zone("corp.internal", ["10.0.0.5:53"])
        self._login(forwarding_client)
        page = forwarding_client.get("/forwarding/")
        assert page.status_code == 200
        assert b"corp.internal." in page.data
        assert b"10.0.0.5:53" in page.data

    def test_built_in_reverse_zones_are_not_shown(self, forwarding_client, users, recursor):
        self._login(forwarding_client)
        page = forwarding_client.get("/forwarding/")
        assert b"127.in-addr.arpa" not in page.data

    def test_a_plain_user_may_not_reach_it(self, forwarding_client, users):
        self._login(forwarding_client, "viewer")
        assert forwarding_client.get("/forwarding/").status_code == 403

    def test_an_operator_may(self, forwarding_client, users):
        self._login(forwarding_client, "operator")
        assert forwarding_client.get("/forwarding/").status_code == 200

    def test_creating_a_forward_zone(self, forwarding_client, users, recursor):
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/new").data)
        response = forwarding_client.post(
            "/forwarding/new",
            data={"name": "corp.internal", "servers": "10.0.0.5, 10.0.0.6", "csrf_token": token},
        )
        assert response.status_code == 302
        assert recursor.forwarded["corp.internal."] == ["10.0.0.5:53", "10.0.0.6:53"]

    def test_a_host_name_forwarder_is_rejected_with_the_reason(
        self, forwarding_client, users, recursor
    ):
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/new").data)
        response = forwarding_client.post(
            "/forwarding/new",
            data={"name": "corp.internal", "servers": "dns.example.com", "csrf_token": token},
        )
        assert response.status_code == 400
        assert b"not an IP address" in response.data
        assert "corp.internal." not in recursor.forwarded

    def test_setting_global_forwarders(self, forwarding_client, users, recursor):
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/").data)
        response = forwarding_client.post(
            "/forwarding/global", data={"servers": "1.1.1.1\n9.9.9.9", "csrf_token": token}
        )
        assert response.status_code == 302
        assert recursor.forwarded["."] == ["1.1.1.1:53", "9.9.9.9:53"]
        # Public resolvers refuse a query that does not ask them to recurse.
        assert recursor.zones["=2E"]["recursion_desired"] is True

    def test_clearing_global_forwarders(self, forwarding_client, users, recursor):
        recursor.add_forward_zone(".", ["1.1.1.1:53"], recursion_desired=True)
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/").data)
        forwarding_client.post("/forwarding/global", data={"servers": "", "csrf_token": token})
        assert "." not in recursor.forwarded

    def test_renaming_removes_the_old_zone(self, forwarding_client, users, recursor):
        """Both rules would otherwise stay in effect and the more specific one
        would silently win."""
        recursor.add_forward_zone("old.test", ["10.0.0.5:53"])
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/old.test./edit").data)
        forwarding_client.post(
            "/forwarding/old.test./edit",
            data={"name": "new.test", "servers": "10.0.0.5", "csrf_token": token},
        )
        assert "old.test." not in recursor.forwarded
        assert recursor.forwarded["new.test."] == ["10.0.0.5:53"]

    def test_deleting(self, forwarding_client, users, recursor):
        recursor.add_forward_zone("corp.internal", ["10.0.0.5:53"])
        self._login(forwarding_client)
        token = csrf_from(forwarding_client.get("/forwarding/").data)
        forwarding_client.post("/forwarding/corp.internal./delete", data={"csrf_token": token})
        assert "corp.internal." not in recursor.forwarded

    def test_an_unreachable_recursor_is_reported_not_a_500(
        self, forwarding_client, users, recursor
    ):
        recursor.unreachable = True
        self._login(forwarding_client)
        page = forwarding_client.get("/forwarding/")
        assert page.status_code == 200
        assert b"not answering" in page.data

    def test_opening_the_page_brings_local_zones_up_to_date(
        self, forwarding_client, users, recursor, pdns
    ):
        pdns.add_zone("example.com")
        self._login(forwarding_client)
        forwarding_client.get("/forwarding/")
        assert recursor.forwarded["example.com."] == [LOCAL_TARGET]


class TestForwardingWithoutARecursor:
    def test_the_page_explains_instead_of_erroring(self, client, users, login):
        login("admin")
        page = client.get("/forwarding/")
        assert page.status_code == 200
        assert b"No recursor is configured" in page.data

    def test_it_still_requires_signing_in(self, client, users):
        """The explanation must not be reachable before authentication."""
        assert client.get("/forwarding/").status_code == 302

    def test_the_nav_entry_is_hidden(self, client, users, login):
        login("admin")
        assert b'href="/forwarding/"' not in client.get("/").data

    def test_the_nav_entry_is_shown_when_configured(self, forwarding_client, forwarding_app):
        from app.config import AUTH_LOCAL, ROLE_ADMIN
        from app.database import get_session
        from app.models import User
        from app.security import hash_password

        with forwarding_app.app_context():
            session = get_session()
            session.add(
                User(
                    username="admin",
                    auth_source=AUTH_LOCAL,
                    password_hash=hash_password("admin-password-123"),
                    role=ROLE_ADMIN,
                    is_active=True,
                )
            )
            session.commit()
        page = forwarding_client.get("/auth/login")
        forwarding_client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": csrf_from(page.data),
            },
        )
        assert b'href="/forwarding/"' in forwarding_client.get("/").data

    def test_the_client_refuses_with_an_explanation(self, app):
        from app.recursor import client_from_config

        with pytest.raises(RecursorNotConfigured, match="RECURSOR_API_URL"):
            client_from_config(app.config)


class TestZoneChangesUpdateForwarding:
    """A new zone must resolve straight away, not at the next visit to the
    Forwarding page."""

    @pytest.fixture
    def signed_in(self, forwarding_app, forwarding_client):
        from app.config import AUTH_LOCAL, ROLE_ADMIN
        from app.database import get_session
        from app.models import User
        from app.security import hash_password

        with forwarding_app.app_context():
            session = get_session()
            session.add(
                User(
                    username="admin",
                    auth_source=AUTH_LOCAL,
                    password_hash=hash_password("admin-password-123"),
                    role=ROLE_ADMIN,
                    is_active=True,
                )
            )
            session.commit()
        page = forwarding_client.get("/auth/login")
        forwarding_client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": csrf_from(page.data),
            },
        )
        return forwarding_client

    def test_creating_a_zone_starts_forwarding_it(self, signed_in, recursor, pdns):
        token = csrf_from(signed_in.get("/zones/new").data)
        signed_in.post(
            "/zones/new",
            data={
                "name": "brandnew.test",
                "kind": "Native",
                "nameservers": "ns1.example.com",
                "csrf_token": token,
            },
        )
        assert recursor.forwarded["brandnew.test."] == [LOCAL_TARGET]

    def test_deleting_a_zone_stops_forwarding_it(self, signed_in, recursor, pdns):
        pdns.add_zone("doomed.test")
        recursor.add_forward_zone("doomed.test", [LOCAL_TARGET])
        token = csrf_from(signed_in.get("/zones/doomed.test.").data)
        signed_in.post(
            "/zones/doomed.test./delete",
            data={"confirm": "doomed.test.", "csrf_token": token},
        )
        assert "doomed.test." not in recursor.forwarded

    def test_a_broken_recursor_does_not_fail_the_zone_creation(self, signed_in, recursor, pdns):
        """The zone was created successfully; refusing to report that because
        a second system is down would be the wrong trade."""
        recursor.unreachable = True
        token = csrf_from(signed_in.get("/zones/new").data)
        response = signed_in.post(
            "/zones/new",
            data={
                "name": "resilient.test",
                "kind": "Native",
                "nameservers": "ns1.example.com",
                "csrf_token": token,
            },
        )
        assert response.status_code == 302
        assert "resilient.test." in pdns.zones


class TestForwardZoneModel:
    def test_the_root_zone_is_labelled_for_humans(self):
        zone = ForwardZone(name=".", servers=("1.1.1.1:53",), recursion_desired=True)
        assert zone.is_global
        assert zone.display_name == "Global forwarders"

    def test_an_ordinary_zone_keeps_its_name(self):
        zone = ForwardZone(name="corp.internal.", servers=(), recursion_desired=False)
        assert not zone.is_global
        assert zone.display_name == "corp.internal."
