"""Turning an external identity into a local user row.

Every external backend funnels through :func:`resolve_identity`, so the rules
about who is allowed in, and with what role, are written once.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import func, select

from .. import audit
from ..config import AUTH_LOCAL, GroupRoleMap
from ..database import get_session
from ..models import User, utcnow

log = logging.getLogger(__name__)

#: Conservative: a username ends up in URLs, log lines and LDAP filters.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,190}$")


class ProvisioningError(Exception):
    """The identity is valid but the user may not be admitted."""


class IdentityClaim:
    """What a backend learned about the person signing in."""

    def __init__(
        self,
        username: str,
        auth_source: str,
        provider: str = "",
        external_id: str | None = None,
        email: str = "",
        display_name: str = "",
        groups: list[str] | None = None,
    ):
        self.username = (username or "").strip()
        self.auth_source = auth_source
        self.provider = provider or ""
        self.external_id = (external_id or "").strip() or None
        self.email = (email or "").strip()
        self.display_name = (display_name or "").strip()
        self.groups = groups or []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<IdentityClaim {self.username!r} source={self.auth_source} "
            f"provider={self.provider} groups={self.groups}>"
        )


def normalise_username(raw: str) -> str:
    """Canonical login name: trimmed and lower-cased.

    Directories are inconsistent about case, and treating ``JDoe`` and ``jdoe``
    as two accounts would silently split one person's permissions in two.
    """
    username = (raw or "").strip().lower()
    # An e-mail address as the username is common with OIDC; keep it whole.
    if not username:
        raise ProvisioningError("The identity provider did not supply a username.")
    if not USERNAME_RE.match(username):
        raise ProvisioningError(f"The username {raw!r} contains characters that are not allowed.")
    return username


#: Enough to diagnose a mapping problem without storing an unbounded blob for
#: someone who is a member of several hundred Active Directory groups.
MAX_RECORDED_GROUPS = 200


def _group_summary(groups: list[str]) -> str:
    """The groups a directory reported, deduplicated, one per line.

    Non-string entries are dropped rather than raising: identity providers do
    put nulls in a groups array, and that must not break a sign-in.
    """
    seen = dict.fromkeys(
        group.strip() for group in (groups or ()) if isinstance(group, str) and group.strip()
    )
    return "\n".join(list(seen)[:MAX_RECORDED_GROUPS])


def resolve_identity(claim: IdentityClaim, roles: GroupRoleMap) -> User:
    """Find or create the user for ``claim``, applying the role mapping.

    Raises :class:`ProvisioningError` when the person is not entitled to access,
    which is what a ``*_DEFAULT_ROLE=none`` setting means for someone who
    matched no group.

    The group mapping decides the role on every sign-in, so a change at the
    directory takes effect immediately. The one exception is an account whose
    role an administrator set by hand: see ``User.role_locked``. Admission is
    still decided by the mapping even then -- pinning a role must not turn into
    a way to keep access after being removed from every group that grants it.
    """
    role = roles.resolve(claim.groups)
    if role is None:
        raise ProvisioningError(
            "Your account is not a member of any group that grants access to this panel."
        )

    username = normalise_username(claim.username)
    db = get_session()

    user: User | None = None

    # Prefer the stable IdP subject: usernames and e-mail addresses change,
    # subjects do not, and matching on them keeps a renamed user's grants.
    if claim.external_id and claim.provider:
        user = db.scalars(
            select(User).filter(
                User.auth_provider == claim.provider,
                User.external_id == claim.external_id,
            )
        ).first()

    if user is None:
        user = db.scalars(select(User).filter(func.lower(User.username) == username)).first()
        if user is not None and user.auth_source == AUTH_LOCAL:
            # A local account already owns this name. Silently converting it to
            # an SSO account would let anyone who can create a matching name at
            # the IdP take over a local administrator.
            raise ProvisioningError(
                f"A local account named {username!r} already exists. An administrator "
                "must rename or remove it before this name can be used for single sign-on."
            )

    if user is None:
        user = User(
            username=username,
            auth_source=claim.auth_source,
            auth_provider=claim.provider,
            external_id=claim.external_id,
            role=role,
            is_active=True,
        )
        db.add(user)
        # Auto-creating an account is a security-relevant event, so it belongs
        # in the audit table rather than on stderr: that table is
        # access-controlled, queryable, and survives log rotation. commit=False
        # lets it land in the same transaction as the user row below, so there
        # can be no audit entry for a user that failed to save, or vice versa.
        audit.record(
            "user.provision",
            target=username,
            detail=f"source={claim.auth_source} role={role}",
            commit=False,
        )
    elif not user.is_active:
        raise ProvisioningError("This account has been deactivated.")

    # Directory attributes are authoritative on every login, so a rename or a
    # group change at the IdP takes effect immediately rather than at next sync.
    user.username = username
    user.auth_source = claim.auth_source
    user.auth_provider = claim.provider
    if claim.external_id:
        user.external_id = claim.external_id
    if claim.email:
        user.email = claim.email
    if claim.display_name:
        user.display_name = claim.display_name
    # Recorded whatever happens to the role: this is the only place the groups
    # the directory actually sent are visible, and "I am in the admin group but
    # I am not an admin" is otherwise unanswerable without container logs.
    user.last_groups = _group_summary(claim.groups)

    if user.role_locked:
        if user.role != role:
            log.info(
                "%s: keeping the manually assigned role %r; the group mapping would have given %r",
                username,
                user.role,
                role,
            )
    elif user.role != role:
        audit.record(
            "user.role_change",
            target=username,
            detail=f"{user.role} -> {role} (from group mapping)",
            commit=False,
        )
        user.role = role
    user.updated_at = utcnow()

    db.commit()
    return user
