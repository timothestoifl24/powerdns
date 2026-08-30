"""User administration, role safety rails and the audit trail."""

from __future__ import annotations

from app.config import ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER
from app.database import get_session
from app.models import AuditLog, User, ZoneAccess


class TestAccessControl:
    def test_admin_pages_need_the_admin_role(self, client, users, login):
        login("operator")
        for path in ("/admin/users", "/admin/audit", "/admin/settings"):
            assert client.get(path).status_code == 403, path

    def test_admin_can_reach_them(self, client, users, login):
        login("admin")
        for path in ("/admin/users", "/admin/audit", "/admin/settings"):
            assert client.get(path).status_code == 200, path

    def test_plain_user_cannot_create_users(self, client, users, login, token):
        login("viewer")
        assert client.get("/admin/users/new").status_code == 403


class TestUserCreation:
    def test_creates_a_local_user(self, app, client, users, login, token):
        login("admin")
        response = client.post(
            "/admin/users/new",
            data={
                "csrf_token": token("/admin/users/new"),
                "username": "NewPerson",
                "password": "corr3ct-horse-battery",
                "confirm_password": "corr3ct-horse-battery",
                "role": ROLE_OPERATOR,
                "email": "new@example.com",
            },
        )
        assert response.status_code == 302
        with app.app_context():
            created = get_session().query(User).filter_by(username="newperson").one()
            assert created.role == ROLE_OPERATOR
            assert created.password_hash is not None

    def test_duplicate_username_is_refused(self, client, users, login, token):
        login("admin")
        response = client.post(
            "/admin/users/new",
            data={
                "csrf_token": token("/admin/users/new"),
                "username": "admin",
                "password": "corr3ct-horse-battery",
                "confirm_password": "corr3ct-horse-battery",
                "role": ROLE_USER,
            },
        )
        assert response.status_code == 400
        assert b"already exists" in response.data

    def test_mismatched_passwords_refused(self, client, users, login, token):
        login("admin")
        response = client.post(
            "/admin/users/new",
            data={
                "csrf_token": token("/admin/users/new"),
                "username": "person",
                "password": "corr3ct-horse-battery",
                "confirm_password": "different-horse-battery",
                "role": ROLE_USER,
            },
        )
        assert response.status_code == 400
        assert b"do not match" in response.data

    def test_weak_password_refused(self, client, users, login, token):
        login("admin")
        response = client.post(
            "/admin/users/new",
            data={
                "csrf_token": token("/admin/users/new"),
                "username": "person",
                "password": "short",
                "confirm_password": "short",
                "role": ROLE_USER,
            },
        )
        assert response.status_code == 400
        assert b"at least 12" in response.data


class TestLastAdministratorProtection:
    def _demote(self, client, token, user_id, role=ROLE_USER, active="on"):
        return client.post(
            f"/admin/users/{user_id}",
            data={
                "csrf_token": token(f"/admin/users/{user_id}"),
                "role": role,
                "is_active": active,
            },
            follow_redirects=True,
        )

    def test_last_admin_cannot_be_demoted(self, app, client, users, login, token):
        login("admin")
        response = self._demote(client, token, users["admin"])
        assert b"last active administrator" in response.data
        with app.app_context():
            assert get_session().get(User, users["admin"]).role == ROLE_ADMIN

    def test_demotion_allowed_once_another_admin_exists(self, app, client, users, login, token):
        login("admin")
        client.post(
            f"/admin/users/{users['operator']}",
            data={
                "csrf_token": token(f"/admin/users/{users['operator']}"),
                "role": ROLE_ADMIN,
                "is_active": "on",
            },
        )
        self._demote(client, token, users["admin"])
        with app.app_context():
            assert get_session().get(User, users["admin"]).role == ROLE_USER

    def test_sole_admin_deactivating_themselves_is_blocked(self, client, users, login, token):
        """The last-admin guard fires first here, which is the more useful message."""
        login("admin")
        response = self._demote(client, token, users["admin"], role=ROLE_ADMIN, active="")
        assert b"last active administrator" in response.data

    def test_cannot_deactivate_yourself(self, app, client, users, login, token):
        login("admin")
        # A second administrator, so the last-admin guard is not what stops us.
        client.post(
            f"/admin/users/{users['operator']}",
            data={
                "csrf_token": token(f"/admin/users/{users['operator']}"),
                "role": ROLE_ADMIN,
                "is_active": "on",
            },
        )
        response = self._demote(client, token, users["admin"], role=ROLE_ADMIN, active="")
        assert b"cannot deactivate your own account" in response.data
        with app.app_context():
            assert get_session().get(User, users["admin"]).is_active is True

    def test_cannot_delete_yourself(self, app, client, users, login, token):
        login("admin")
        response = client.post(
            f"/admin/users/{users['admin']}/delete",
            data={"csrf_token": token(f"/admin/users/{users['admin']}")},
            follow_redirects=True,
        )
        assert b"cannot delete your own account" in response.data
        with app.app_context():
            assert get_session().get(User, users["admin"]) is not None


class TestUserDeletion:
    def test_deleting_a_user_removes_their_zone_grants(self, app, client, users, login, token):
        with app.app_context():
            session = get_session()
            session.add(ZoneAccess(user_id=users["viewer"], zone="example.com."))
            session.commit()

        login("admin")
        client.post(
            f"/admin/users/{users['viewer']}/delete",
            data={"csrf_token": token(f"/admin/users/{users['viewer']}")},
        )
        with app.app_context():
            session = get_session()
            assert session.get(User, users["viewer"]) is None
            assert session.query(ZoneAccess).filter_by(user_id=users["viewer"]).count() == 0

    def test_audit_entries_survive_the_user(self, app, client, users, login, token):
        """The trail must outlive the account that made the changes."""
        login("viewer")
        login_entries = None
        with app.app_context():
            login_entries = (
                get_session().query(AuditLog).filter_by(actor_id=users["viewer"]).count()
            )
        assert login_entries >= 1

        client.post("/auth/logout", data={"csrf_token": token("/")})
        login("admin")
        client.post(
            f"/admin/users/{users['viewer']}/delete",
            data={"csrf_token": token(f"/admin/users/{users['viewer']}")},
        )
        with app.app_context():
            session = get_session()
            orphaned = session.query(AuditLog).filter_by(actor_name="viewer").all()
            assert orphaned, "audit entries were deleted with the user"
            assert all(entry.actor_id is None for entry in orphaned)


class TestZoneGrants:
    def test_grants_are_replaced_wholesale(self, app, client, users, login, token, pdns):
        pdns.add_zone("one.test")
        pdns.add_zone("two.test")
        login("admin")
        path = f"/admin/users/{users['viewer']}/zones"

        client.post(
            path,
            data={
                "csrf_token": token(f"/admin/users/{users['viewer']}"),
                "zones": ["one.test.", "two.test."],
            },
        )
        with app.app_context():
            user = get_session().get(User, users["viewer"])
            assert set(user.granted_zones) == {"one.test.", "two.test."}

        client.post(
            path,
            data={"csrf_token": token(f"/admin/users/{users['viewer']}"), "zones": ["one.test."]},
        )
        with app.app_context():
            user = get_session().get(User, users["viewer"])
            assert set(user.granted_zones) == {"one.test."}


class TestAuditLog:
    def test_successful_login_is_recorded(self, app, client, users, login):
        login("admin")
        with app.app_context():
            entry = (
                get_session()
                .query(AuditLog)
                .filter_by(action="login.success", actor_name="admin")
                .one()
            )
            assert entry.success is True

    def test_failed_login_is_recorded(self, app, client, users, login):
        login("admin", "wrong-password")
        with app.app_context():
            entry = get_session().query(AuditLog).filter_by(action="login.failed").one()
            assert entry.success is False
            assert entry.target == "admin"

    def test_record_changes_are_recorded(self, app, client, users, login, token, pdns):
        pdns.add_zone("example.com")
        login("operator")
        client.post(
            "/zones/example.com./records",
            data={
                "csrf_token": token("/zones/example.com."),
                "name": "www",
                "type": "A",
                "ttl": "3600",
                "content": "192.0.2.1",
            },
        )
        with app.app_context():
            entry = get_session().query(AuditLog).filter_by(action="record.save").one()
            assert entry.target == "www.example.com. A"
            assert entry.actor_name == "operator"

    def test_audit_page_lists_entries(self, client, users, login):
        login("admin")
        assert b"login.success" in client.get("/admin/audit").data


class TestSettingsPage:
    def test_shows_powerdns_status(self, client, users, login):
        login("admin")
        page = client.get("/admin/settings")
        assert b"Reachable" in page.data
        assert b"4.9.7" in page.data

    def test_shows_which_backends_are_enabled(self, make_app):
        app = make_app(
            LDAP_ENABLED="true",
            LDAP_URI="ldaps://dc.example.com",
            LDAP_BASE_DN="DC=example,DC=com",
            LDAP_ADMIN_GROUP="DNS-Admins",
            LDAP_DEFAULT_ROLE="none",
        )
        client = app.test_client()
        from app.config import AUTH_LOCAL
        from app.database import get_session as session_for
        from app.models import User as UserModel
        from app.security import hash_password

        with app.app_context():
            session = session_for()
            session.add(
                UserModel(
                    username="admin",
                    auth_source=AUTH_LOCAL,
                    password_hash=hash_password("admin-password-123"),
                    role=ROLE_ADMIN,
                )
            )
            session.commit()

        from conftest import csrf_from

        page = client.get("/auth/login")
        client.post(
            "/auth/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": csrf_from(page.data),
            },
        )
        settings = client.get("/admin/settings")
        # The full configured URI, not a substring of it: "ldaps://dc.example.com"
        # would also match a truncated or lookalike value being rendered.
        assert (
            b'<div class="text-secondary small font-monospace">ldaps://dc.example.com</div>'
            in (settings.data.replace(b"\n", b"").replace(b"  ", b""))
            or b">ldaps://dc.example.com<" in settings.data
        )
        assert b"DNS-Admins" in settings.data
        assert b"access refused" in settings.data
