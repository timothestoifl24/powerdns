"""Database models for the admin panel.

These live in their own PostgreSQL schema (``pdnsadmin`` by default) and are
completely separate from the PowerDNS tables. The panel never reads or writes
zone data here -- that goes through the PowerDNS HTTP API -- so the two schemas
can be backed up, migrated and permissioned independently.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import AUTH_LOCAL, ROLE_ADMIN, ROLE_OPERATOR, ROLE_USER


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Canonical (lower-cased) login name. Users from external directories keep
    #: the name the directory gave them, so an LDAP "jdoe" and a local "jdoe"
    #: are the same row -- see auth.provisioning for how conflicts are handled.
    username: Mapped[str] = mapped_column(String(190), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(190), nullable=False, default="")

    #: One of config.AUTH_LOCAL / AUTH_LDAP / AUTH_OAUTH / AUTH_SAML.
    auth_source: Mapped[str] = mapped_column(String(20), nullable=False, default=AUTH_LOCAL)
    #: Which configured provider produced this user, e.g. the OAUTH_PROVIDERS
    #: entry name. Empty for local and LDAP accounts.
    auth_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Stable subject identifier from the IdP. Preferred over the username when
    #: matching a returning user, because usernames and e-mail addresses change.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Only ever set for local accounts.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)

    #: Set when an administrator picks this user's role by hand. External
    #: accounts normally have their role recomputed from directory groups on
    #: every sign-in; this pins it so the manual choice survives. Clearing it
    #: hands the role back to the group mapping.
    role_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: The groups the directory or IdP reported at the last sign-in, newline
    #: separated. Kept purely so an administrator can see what the panel was
    #: given: "user is in the admin group but is not an admin" is otherwise
    #: only answerable by reading container logs.
    last_groups: Mapped[str] = mapped_column(Text, nullable=False, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    zone_grants: Mapped[list[ZoneAccess]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # A given IdP subject maps to exactly one account.
        UniqueConstraint("auth_provider", "external_id", name="uq_users_provider_external_id"),
        Index("ix_users_auth_source", "auth_source"),
    )

    # -- role helpers -----------------------------------------------------

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_operator(self) -> bool:
        """Operators and admins both have write access to every zone."""
        return self.role in (ROLE_ADMIN, ROLE_OPERATOR)

    @property
    def is_local(self) -> bool:
        return self.auth_source == AUTH_LOCAL

    @property
    def directory_groups(self) -> list[str]:
        """Groups seen at the last sign-in, one per line, blanks dropped."""
        return [line.strip() for line in (self.last_groups or "").splitlines() if line.strip()]

    @property
    def role_is_directory_managed(self) -> bool:
        """Whether the next sign-in will recompute this user's role."""
        return not self.is_local and not self.role_locked

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def initials(self) -> str:
        parts = [part for part in (self.display_name or self.username).split() if part]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def can_see_zone(self, zone_name: str) -> bool:
        """Whether this user may view ``zone_name`` (canonical, trailing dot)."""
        if self.is_operator:
            return True
        wanted = zone_name.lower().rstrip(".")
        return any(grant.zone.lower().rstrip(".") == wanted for grant in self.zone_grants)

    #: Editing and viewing are the same permission today; kept separate so a
    #: read-only grant can be added without touching every call site.
    can_edit_zone = can_see_zone

    @property
    def granted_zones(self) -> list[str]:
        return sorted(grant.zone for grant in self.zone_grants)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<User {self.username} role={self.role} source={self.auth_source} "
            f"locked={self.role_locked}>"
        )


class ZoneAccess(Base):
    """Grants a non-operator user access to one zone."""

    __tablename__ = "zone_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Canonical zone name including the trailing dot, as PowerDNS reports it.
    zone: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    user: Mapped[User] = relationship(back_populates="zone_grants")

    __table_args__ = (UniqueConstraint("user_id", "zone", name="uq_zone_access_user_zone"),)


class AuditLog(Base):
    """Append-only record of every change made through the panel."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    # Kept as SET NULL plus a denormalised name so the trail survives the
    # deletion of the user who made the change.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(190), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    remote_addr: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AuthProviderConfig(Base):
    """An external identity provider configured through the web UI.

    Providers can also come from the environment, which is what a
    configuration-as-code deployment wants. Those stay authoritative: a row
    here that collides with an environment-defined provider is ignored, and
    the UI says so rather than pretending to have taken effect.

    ``settings`` and ``secrets`` are JSON blobs because the three kinds have
    very different shapes, and a column per field would make a wide, mostly
    NULL table. Everything in ``secrets`` is encrypted -- see app/crypto.py.
    """

    __tablename__ = "auth_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: AUTH_LDAP / AUTH_OAUTH / AUTH_SAML.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    #: URL-safe slug. For OAuth this appears in the callback URL, so changing
    #: it means re-registering the redirect URI with the provider.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(190), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    secrets_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # -- JSON helpers -----------------------------------------------------

    @property
    def settings(self) -> dict:
        return self._load(self.settings_json)

    @settings.setter
    def settings(self, value: dict) -> None:
        self.settings_json = json.dumps(value or {}, sort_keys=True)

    @property
    def secrets(self) -> dict:
        return self._load(self.secrets_json)

    @secrets.setter
    def secrets(self, value: dict) -> None:
        self.secrets_json = json.dumps(value or {}, sort_keys=True)

    @staticmethod
    def _load(raw: str) -> dict:
        """Tolerate a corrupt or hand-edited blob rather than failing sign-in."""
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def setting(self, key: str, default=""):
        """One stored setting, falling back to ``default``.

        A blank value counts as absent. The provider form posts every field it
        renders, so clearing one stores ``""`` rather than removing the key --
        and without this a cleared "Group attribute" would mean "no attribute"
        instead of "the default", quietly costing every user their group
        membership and therefore their role.
        """
        value = self.settings.get(key, default)
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuthProviderConfig {self.kind}:{self.name} enabled={self.enabled}>"


class AppSetting(Base):
    """Runtime-editable settings, for the few things worth changing without a redeploy."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


__all__ = [
    "AppSetting",
    "AuditLog",
    "AuthProviderConfig",
    "Base",
    "User",
    "ZoneAccess",
    "ROLE_ADMIN",
    "ROLE_OPERATOR",
    "ROLE_USER",
    "utcnow",
]
