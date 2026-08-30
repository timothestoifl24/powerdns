"""First-run bootstrap of the initial administrator."""

from __future__ import annotations

import os

from app.cli import bootstrap_admin
from app.config import AUTH_LOCAL, ROLE_ADMIN
from app.database import get_session
from app.models import User
from app.security import verify_password


class TestBootstrapAdmin:
    def test_creates_the_first_administrator(self, make_app):
        app = make_app(BOOTSTRAP_ADMIN_PASSWORD="first-run-secret-value")
        bootstrap_admin(app)
        with app.app_context():
            admin = get_session().query(User).one()
            assert admin.username == "admin"
            assert admin.role == ROLE_ADMIN
            assert admin.auth_source == AUTH_LOCAL
            assert verify_password(admin, "first-run-secret-value")

    def test_honours_a_custom_username(self, make_app):
        app = make_app(
            BOOTSTRAP_ADMIN_PASSWORD="first-run-secret-value",
            BOOTSTRAP_ADMIN_USERNAME="RootAdmin",
        )
        bootstrap_admin(app)
        with app.app_context():
            assert get_session().query(User).one().username == "rootadmin"

    def test_does_nothing_when_users_already_exist(self, app, users):
        """Otherwise the bootstrap password would be a permanent back door."""
        # Set on the existing app: a second app would get its own database and
        # would not see the users fixture at all.
        app.config["BOOTSTRAP_ADMIN_PASSWORD"] = "first-run-secret-value"
        with app.app_context():
            before = get_session().query(User).count()

        bootstrap_admin(app)

        with app.app_context():
            assert get_session().query(User).count() == before
            existing = get_session().query(User).filter_by(username="admin").one()
            # The pre-existing admin keeps its own password.
            assert not verify_password(existing, "first-run-secret-value")

    def test_without_a_password_no_user_is_created(self, make_app):
        app = make_app()
        os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
        bootstrap_admin(app)
        with app.app_context():
            assert get_session().query(User).count() == 0

    def test_password_can_come_from_a_file(self, make_app, tmp_path):
        secret = tmp_path / "admin_password"
        secret.write_text("password-from-a-secret-file\n")
        app = make_app(BOOTSTRAP_ADMIN_PASSWORD_FILE=str(secret))
        bootstrap_admin(app)
        with app.app_context():
            admin = get_session().query(User).one()
            # The trailing newline must not become part of the password.
            assert verify_password(admin, "password-from-a-secret-file")
