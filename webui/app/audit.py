"""Audit trail for everything that changes state."""

from __future__ import annotations

import logging

from flask import has_request_context, request

from .database import get_session
from .models import AuditLog, User

log = logging.getLogger(__name__)


def _remote_addr() -> str:
    if not has_request_context():
        return ""
    # request.remote_addr already accounts for ProxyFix when TRUSTED_PROXY_COUNT
    # is configured; without it, X-Forwarded-For is deliberately ignored because
    # any client can set that header.
    return (request.remote_addr or "")[:64]


def record(
    action: str,
    target: str = "",
    detail: str = "",
    actor: User | None = None,
    success: bool = True,
    commit: bool = True,
) -> None:
    """Append one entry to the audit log.

    Never raises: an audit failure must not turn a successful change into a
    500. The entry is logged to stderr as a fallback if the insert fails.
    """
    try:
        db = get_session()
        db.add(
            AuditLog(
                actor_id=actor.id if actor else None,
                actor_name=(actor.username if actor else "system")[:190],
                action=action[:64],
                target=(target or "")[:255],
                detail=detail or "",
                remote_addr=_remote_addr(),
                success=success,
            )
        )
        if commit:
            db.commit()
    except Exception:  # pragma: no cover - defensive
        log.exception(
            "could not write audit entry action=%s target=%s actor=%s",
            action,
            target,
            actor.username if actor else "system",
        )


def recent(limit: int = 200, actor_id: int | None = None) -> list[AuditLog]:
    from sqlalchemy import select

    db = get_session()
    statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if actor_id is not None:
        statement = statement.filter(AuditLog.actor_id == actor_id)
    return list(db.scalars(statement))
