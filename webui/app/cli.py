"""Start-up tasks: create the schema and the first administrator.

Run by the container entrypoint before gunicorn forks, so workers never race
each other to CREATE TABLE.

    python -m app.cli init
"""

from __future__ import annotations

import logging
import sys
import time

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from . import create_app
from .config import AUTH_LOCAL, ROLE_ADMIN
from .database import create_all, get_session
from .models import User
from .security import hash_password

log = logging.getLogger("app.cli")


def wait_for_database(app, attempts: int = 30, delay: float = 2.0) -> None:
    """Block until the database accepts a connection.

    compose's healthcheck already gates start-up, but a database that is
    restarting under us should produce a retry, not a crash loop.
    """
    from sqlalchemy import text

    engine = app.extensions["pdnsadmin.engine"]
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            last_error = exc
            if attempt % 5 == 0 or attempt == 1:
                log.info("waiting for the database (attempt %d/%d)", attempt, attempts)
            time.sleep(delay)
    raise SystemExit(f"database never became available: {last_error}")


def bootstrap_admin(app) -> None:
    """Create the initial administrator, but only while there are no users.

    Once anyone exists this is a no-op, so the bootstrap password cannot be
    used to resurrect access after the account is renamed or removed.
    """
    with app.app_context():
        db = get_session()
        existing = db.scalar(select(func.count()).select_from(User))
        if existing:
            log.info("%d user(s) already exist; skipping bootstrap", existing)
            return

        username = (app.config["BOOTSTRAP_ADMIN_USERNAME"] or "admin").strip().lower()
        password = app.config["BOOTSTRAP_ADMIN_PASSWORD"]
        if not password:
            log.warning(
                "No users exist and BOOTSTRAP_ADMIN_PASSWORD is not set. Nobody can "
                "sign in. Set it (or BOOTSTRAP_ADMIN_PASSWORD_FILE) and restart."
            )
            return

        db.add(
            User(
                username=username,
                email=app.config["BOOTSTRAP_ADMIN_EMAIL"] or "",
                display_name="Administrator",
                auth_source=AUTH_LOCAL,
                password_hash=hash_password(password),
                role=ROLE_ADMIN,
                is_active=True,
            )
        )
        db.commit()
        log.info("created the initial administrator %r", username)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    command = argv[0] if argv else "init"

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if command != "init":
        print(f"unknown command {command!r}; the only command is 'init'", file=sys.stderr)
        return 2

    app = create_app()
    wait_for_database(app)
    create_all(app)
    bootstrap_admin(app)
    log.info("initialisation complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
