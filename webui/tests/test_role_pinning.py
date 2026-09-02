"""A role an administrator sets by hand must survive the next sign-in.

Without this the group mapping recomputes the role on every login, so promoting
a directory user in the panel appeared to work and then silently reverted.
"""

from __future__ import annotations

from conftest import csrf_from
from sqlalchemy import inspect, select, text

from app.auth.provisioning import IdentityClaim, resolve_identity
from app.config import AUTH_LDAP, ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER, GroupRoleMap
from app.database import get_session
from app.models import AuditLog, User

#: Nobody in this mapping is an admin, so any admin role can only be manual.
PLAIN = GroupRoleMap(admin_groups=("dns-admins",), default_role=ROLE_USER)


def sign_in_from_ldap(app, groups=("staff",), username="jdoe"):
    claim = IdentityClaim(username, AUTH_LDAP, "ldap", f"uid={username}", groups=list(groups))
    with app.test_request_context():
        return resolve_identity(claim, PLAIN).id


class TestProvisioningRespectsAPinnedRole:
    def test_a_pinned_role_survives_the_next_sign_in(self, app):
        user_id = sign_in_from_ldap(app)
        with app.app_context():
            session = get_session()
            user = session.get(User, user_id)
            assert user.role == ROLE_USER
            user.role, user.role_locked = ROLE_ADMIN, True
            session.commit()

        sign_in_from_ldap(app)

        with app.app_context():
            assert get_session().get(User, user_id).role == ROLE_ADMIN

    def test_an_unpinned_role_still_follows_the_directory(self, app):
        """The pin is opt-in: without it the group mapping stays authoritative."""
        user_id = sign_in_from_ldap(app, groups=["dns-admins"])
        with app.app_context():
            assert get_session().get(User, user_id).role == ROLE_ADMIN

        sign_in_from_ldap(app, groups=["staff"])

        with app.app_context():
            assert get_session().get(User, user_id).role == ROLE_USER

    def test_pinning_does_not_keep_access_after_losing_every_group(self, app):
        """Admission is separate from the role: someone removed from all mapped
        groups is refused even with a pinned role, otherwise pinning would be a
        way to keep access after being taken out of the directory."""
        import pytest

        from app.auth.provisioning import ProvisioningError

        strict = GroupRoleMap(admin_groups=("dns-admins",), default_role=None)
        claim = IdentityClaim("pinned", AUTH_LDAP, "ldap", "uid=pinned", groups=["dns-admins"])
        with app.test_request_context():
            user_id = resolve_identity(claim, strict).id
        with app.app_context():
            session = get_session()
            session.get(User, user_id).role_locked = True
            session.commit()

        gone = IdentityClaim("pinned", AUTH_LDAP, "ldap", "uid=pinned", groups=["nothing"])
        with app.test_request_context(), pytest.raises(ProvisioningError):
            resolve_identity(gone, strict)

    def test_a_kept_role_is_not_logged_as_a_role_change(self, app):
        user_id = sign_in_from_ldap(app)
        with app.app_context():
            session = get_session()
            user = session.get(User, user_id)
            user.role, user.role_locked = ROLE_OPERATOR, True
            session.commit()

        sign_in_from_ldap(app)

        with app.app_context():
            entries = list(
                get_session().scalars(
                    select(AuditLog).filter(AuditLog.action == "user.role_change")
                )
            )
            assert entries == []


class TestDirectoryGroupsAreRecorded:
    def test_the_reported_groups_are_stored(self, app):
        user_id = sign_in_from_ldap(app, groups=["CN=admin-ldap,OU=Groups,DC=x", "staff"])
        with app.app_context():
            user = get_session().get(User, user_id)
            assert user.directory_groups == ["CN=admin-ldap,OU=Groups,DC=x", "staff"]

    def test_they_are_refreshed_not_appended(self, app):
        user_id = sign_in_from_ldap(app, groups=["old-group"])
        sign_in_from_ldap(app, groups=["new-group"])
        with app.app_context():
            assert get_session().get(User, user_id).directory_groups == ["new-group"]

    def test_duplicates_and_junk_are_dropped(self, app):
        claim = IdentityClaim(
            "messy", AUTH_LDAP, "ldap", "uid=messy", groups=["a", "a", "  ", None, "b"]
        )
        with app.test_request_context():
            user_id = resolve_identity(claim, PLAIN).id
        with app.app_context():
            assert get_session().get(User, user_id).directory_groups == ["a", "b"]

    def test_the_list_is_capped(self, app):
        from app.auth.provisioning import MAX_RECORDED_GROUPS

        many = [f"group-{n}" for n in range(MAX_RECORDED_GROUPS + 50)]
        claim = IdentityClaim("busy", AUTH_LDAP, "ldap", "uid=busy", groups=many)
        with app.test_request_context():
            user_id = resolve_identity(claim, PLAIN).id
        with app.app_context():
            groups = get_session().get(User, user_id).directory_groups
            assert len(groups) == MAX_RECORDED_GROUPS


class TestAdminUiPinsTheRole:
    def _post_role(self, client, user_id, role, **extra):
        page = client.get(f"/admin/users/{user_id}")
        return client.post(
            f"/admin/users/{user_id}",
            data={"role": role, "is_active": "on", "csrf_token": csrf_from(page.data), **extra},
        )

    def test_changing_a_directory_users_role_pins_it(self, app, client, users, login, pdns):
        user_id = sign_in_from_ldap(app)
        login("admin")
        self._post_role(client, user_id, ROLE_ADMIN)
        with app.app_context():
            user = get_session().get(User, user_id)
            assert user.role == ROLE_ADMIN
            assert user.role_locked is True

    def test_the_pin_can_be_handed_back_to_the_directory(self, app, client, users, login, pdns):
        user_id = sign_in_from_ldap(app)
        login("admin")
        self._post_role(client, user_id, ROLE_ADMIN)
        self._post_role(client, user_id, ROLE_ADMIN, role_from_directory="on")
        with app.app_context():
            assert get_session().get(User, user_id).role_locked is False

    def test_a_local_users_role_is_never_pinned(self, app, client, users, login, pdns):
        """Local accounts have no group mapping, so there is nothing to pin."""
        login("admin")
        self._post_role(client, users["viewer"], ROLE_OPERATOR)
        with app.app_context():
            user = get_session().get(User, users["viewer"])
            assert user.role == ROLE_OPERATOR
            assert user.role_locked is False

    def test_the_edit_page_shows_the_reported_groups(self, app, client, users, login, pdns):
        user_id = sign_in_from_ldap(app, groups=["CN=helpdesk,OU=Groups,DC=example,DC=com"])
        login("admin")
        page = client.get(f"/admin/users/{user_id}")
        assert b"Groups reported at the last sign-in" in page.data
        assert b"CN=helpdesk,OU=Groups,DC=example,DC=com" in page.data


class TestAdditiveMigration:
    """create_all makes tables, not columns: a model column added after a
    deployment is running would otherwise never appear."""

    def test_a_missing_column_is_added_to_an_existing_table(self, app):
        from app.database import add_missing_columns, get_engine

        with app.app_context():
            engine = get_engine()
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users DROP COLUMN role_locked"))
            assert "role_locked" not in {
                column["name"] for column in inspect(engine).get_columns("users")
            }

            add_missing_columns(engine)

            assert "role_locked" in {
                column["name"] for column in inspect(engine).get_columns("users")
            }

    def test_existing_rows_get_the_default_rather_than_null(self, app, users):
        from app.database import add_missing_columns, get_engine

        with app.app_context():
            engine = get_engine()
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users DROP COLUMN role_locked"))
            add_missing_columns(engine)
            with engine.begin() as connection:
                values = connection.execute(text("SELECT role_locked FROM users")).scalars().all()
            assert values and all(value in (0, False) for value in values)
