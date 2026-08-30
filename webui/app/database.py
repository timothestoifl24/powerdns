"""Engine and session lifecycle.

One engine per process, one session per request. The session is created lazily
on first use and closed by Flask's teardown handler, so a request that never
touches the database never opens a connection.
"""

from __future__ import annotations

import logging

from flask import Flask, current_app, g
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

log = logging.getLogger(__name__)

_ENGINE_KEY = "pdnsadmin.engine"
_SESSIONMAKER_KEY = "pdnsadmin.sessionmaker"


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def make_engine(url: str, schema: str) -> Engine:
    """Create the engine, pinning PostgreSQL connections to ``schema``.

    Setting search_path at connect time keeps the models free of hard-coded
    schema names, so the same mappings run against SQLite in the test suite.
    """
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if _is_postgres(url):
        kwargs["connect_args"] = {"options": f"-csearch_path={schema},public"}
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 5
        kwargs["pool_recycle"] = 1800
    elif url.startswith("sqlite"):
        # Used by the tests; keeps one in-memory database across connections.
        from sqlalchemy.pool import StaticPool

        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_app(app: Flask) -> None:
    url = app.config["SQLALCHEMY_DATABASE_URI"]
    schema = app.config["DB_SCHEMA"]
    engine = make_engine(url, schema)
    app.extensions[_ENGINE_KEY] = engine
    app.extensions[_SESSIONMAKER_KEY] = sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )

    @app.teardown_appcontext
    def _close_session(exception: BaseException | None) -> None:
        session: Session | None = g.pop("db_session", None)
        if session is None:
            return
        try:
            if exception is not None:
                session.rollback()
        finally:
            session.close()


def get_engine() -> Engine:
    return current_app.extensions[_ENGINE_KEY]


def get_session() -> Session:
    """The session for the current request, created on first use."""
    session: Session | None = g.get("db_session")
    if session is None:
        session = current_app.extensions[_SESSIONMAKER_KEY]()
        g.db_session = session
    return session


def create_all(app: Flask) -> None:
    """Create missing tables.

    The panel owns only its own schema, so this is safe to run on every start:
    SQLAlchemy issues CREATE TABLE IF NOT EXISTS semantics via `checkfirst`.
    """
    engine = app.extensions[_ENGINE_KEY]
    schema = app.config["DB_SCHEMA"]
    url = app.config["SQLALCHEMY_DATABASE_URI"]

    if _is_postgres(url):
        # Normally created by the database image's init script. Doing it here
        # as well means a hand-provisioned database works too, as long as the
        # role may create schemas. A failure here is not fatal: create_all
        # below will report the real problem if the schema is genuinely absent.
        # The statement runs on its own connection so a permission error cannot
        # poison the transaction that creates the tables.
        try:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        except Exception as exc:  # pragma: no cover - depends on database grants
            log.debug("could not ensure schema %s exists: %s", schema, exc)

    Base.metadata.create_all(engine, checkfirst=True)
    log.info("database schema is up to date")
