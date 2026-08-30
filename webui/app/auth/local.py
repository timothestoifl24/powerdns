"""Local account authentication.

Unlike the external backends there is no provisioning step: a local account
exists because an administrator created it, so an unknown username is simply a
failed login.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from ..config import AUTH_LOCAL
from ..database import get_session
from ..models import User
from ..security import verify_password

log = logging.getLogger(__name__)


def find_local_user(username: str) -> User | None:
    db = get_session()
    return db.scalars(
        select(User).filter(
            func.lower(User.username) == (username or "").strip().lower(),
            User.auth_source == AUTH_LOCAL,
        )
    ).first()


def authenticate(username: str, password: str) -> User | None:
    """Return the user on a correct password, otherwise ``None``.

    ``verify_password`` runs a hash comparison even when the account does not
    exist, so the response time does not reveal which usernames are real.
    """
    user = find_local_user(username)
    if not verify_password(user, password):
        return None
    assert user is not None
    if not user.is_active:
        log.info("login refused for deactivated local user %s", user.username)
        return None
    return user
