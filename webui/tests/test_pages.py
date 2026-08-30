"""Every page renders.

Template errors only show up when a page is actually visited, so this walks
each GET route as each role. It is deliberately dumb: no assertions about
content, just "this does not raise".
"""

from __future__ import annotations

import pytest

from app.database import get_session
from app.models import ZoneAccess


@pytest.fixture
def zone(pdns):
    pdns.add_zone(
        "example.com",
        dnssec=True,
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
                "ttl": 300,
                "records": [{"content": "192.0.2.1", "disabled": False}],
                "comments": [{"content": "the web server", "account": "admin"}],
            },
            {
                "name": "disabled.example.com.",
                "type": "A",
                "ttl": 300,
                "records": [{"content": "192.0.2.2", "disabled": True}],
                "comments": [],
            },
            {
                # A managed type, rendered in its own read-only section.
                "name": "example.com.",
                "type": "RRSIG",
                "ttl": 3600,
                "records": [
                    {
                        "content": "A 13 2 300 20260101000000 20251201000000 1 example.com. AAAA",
                        "disabled": False,
                    }
                ],
                "comments": [],
            },
        ],
    )
    pdns.cryptokeys["example.com."] = [
        {
            "id": 1,
            "keytype": "csk",
            "active": True,
            "algorithm": "ECDSAP256SHA256",
            "bits": 256,
            "ds": ["1 13 2 " + "ab" * 32],
        }
    ]
    return pdns.zones["example.com."]


ADMIN_PAGES = [
    "/",
    "/zones/",
    "/zones/?q=example",
    "/zones/new",
    "/zones/example.com.",
    "/zones/example.com./dnssec",
    "/zones/example.com./export",
    "/profile/",
    "/admin/users",
    "/admin/users/new",
    "/admin/audit",
    "/admin/audit?limit=500",
    "/admin/settings",
    "/healthz",
    "/readyz",
]

OPERATOR_PAGES = [
    "/",
    "/zones/",
    "/zones/new",
    "/zones/example.com.",
    "/zones/example.com./dnssec",
    "/profile/",
]

USER_PAGES = ["/", "/zones/", "/profile/"]


class TestPagesRender:
    @pytest.mark.parametrize("path", ADMIN_PAGES)
    def test_admin_pages(self, client, users, login, zone, path):
        login("admin")
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"

    @pytest.mark.parametrize("path", OPERATOR_PAGES)
    def test_operator_pages(self, client, users, login, zone, path):
        login("operator")
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"

    @pytest.mark.parametrize("path", USER_PAGES)
    def test_plain_user_pages(self, client, users, login, zone, path):
        login("viewer")
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"

    def test_user_edit_page(self, client, users, login, zone):
        login("admin")
        assert client.get(f"/admin/users/{users['viewer']}").status_code == 200

    def test_granted_user_sees_their_zone_page(self, app, client, users, login, zone):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()
        login("viewer")
        assert client.get("/zones/example.com.").status_code == 200

    def test_login_page_renders_for_anonymous(self, client):
        assert client.get("/auth/login").status_code == 200


class TestReadiness:
    def test_readyz_is_ok_when_both_dependencies_answer(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.get_json() == {"status": "ok", "database": True, "powerdns": True}

    def test_readyz_reports_degraded_when_powerdns_is_down(self, client, monkeypatch):
        """The container healthcheck depends on this being honest."""
        import app as app_package
        from app.pdns import PdnsError

        def unreachable(_config):
            class Dead:
                def ping(self):
                    raise PdnsError("connection refused")

            return Dead()

        monkeypatch.setattr(app_package, "client_from_config", unreachable)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.get_json()["powerdns"] is False
        # The database half must still be reported truthfully.
        assert response.get_json()["database"] is True

    def test_healthz_needs_no_authentication(self, client):
        """The container healthcheck runs without a session."""
        assert client.get("/healthz").status_code == 200


class TestErrorPages:
    def test_missing_zone_is_a_clean_404(self, client, users, login):
        login("admin")
        response = client.get("/zones/nosuchzone.test.")
        # Redirected back to the list with an explanation, not a stack trace.
        assert response.status_code in (302, 404)

    def test_unknown_url_renders_the_404_page(self, client, users, login):
        login("admin")
        response = client.get("/no/such/page")
        assert response.status_code == 404
        assert b"That page does not exist" in response.data

    def test_forbidden_renders_the_403_page(self, client, users, login):
        login("viewer")
        response = client.get("/admin/users")
        assert response.status_code == 403
        assert b"does not have access" in response.data
