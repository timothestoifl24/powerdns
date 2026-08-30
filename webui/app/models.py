"""Database models for the admin panel.

These live in their own PostgreSQL schema (``pdnsadmin`` by default) and are
completely separate from the PowerDNS tables. The panel never reads or writes
zone data here -- that goes through the PowerDNS HTTP API -- so the two schemas
can be backed up, migrated and permissioned independently.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    return datetime.now(timezone.utc)


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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    zone_grants: Mapped[list["ZoneAccess"]] = relationship(
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
        return f"<User {self.username} role={self.role} source={self.auth_source}>"


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
    "Base",
    "User",
    "ZoneAccess",
    "ROLE_ADMIN",
    "ROLE_OPERATOR",
    "ROLE_USER",
    "utcnow",
]
