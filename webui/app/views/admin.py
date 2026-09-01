"""User administration, access grants, audit trail and a settings overview."""

from __future__ import annotations

import logging

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .. import audit
from ..config import AUTH_LOCAL, ROLE_ADMIN, ROLE_DESCRIPTIONS, ROLES
from ..database import get_session
from ..models import User, ZoneAccess
from ..pdns import PdnsError, canonical, client_from_config
from ..security import (
    admin_required,
    current_user,
    flash_errors,
    hash_password,
    password_problems,
)

log = logging.getLogger(__name__)

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/users")
@admin_required
def users():
    db = get_session()
    all_users = list(db.scalars(select(User).order_by(User.username)))
    return render_template(
        "admin/users.html", users=all_users, roles=ROLES, role_descriptions=ROLE_DESCRIPTIONS
    )


@bp.route("/users/new", methods=["GET", "POST"])
@admin_required
def create_user():
    actor = current_user()

    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        role = (request.form.get("role") or "user").strip()

        problems: list[str] = []
        if not username:
            problems.append("Enter a username.")
        if role not in ROLES:
            problems.append("Choose a valid role.")
        if password != confirm:
            problems.append("The passwords do not match.")
        problems.extend(password_problems(password, username))

        db = get_session()
        if (
            username
            and db.scalars(select(User).filter(func.lower(User.username) == username)).first()
        ):
            problems.append(f"A user named {username!r} already exists.")

        if problems:
            flash_errors(problems)
            return render_template("admin/user_form.html", user=None, form=request.form), 400

        user = User(
            username=username,
            email=(request.form.get("email") or "").strip()[:254],
            display_name=(request.form.get("display_name") or "").strip()[:190],
            auth_source=AUTH_LOCAL,
            password_hash=hash_password(password),
            role=role,
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            flash(f"A user named {username!r} already exists.", "danger")
            return render_template("admin/user_form.html", user=None, form=request.form), 400

        audit.record("user.create", target=username, detail=f"role={role}", actor=actor)
        flash(f"User {username} has been created.", "success")
        return redirect(url_for("admin.edit_user", user_id=user.id))

    return render_template("admin/user_form.html", user=None, form={})


def _get_user_or_404(user_id: int) -> User:
    user = get_session().get(User, user_id)
    if user is None:
        abort(404)
    return user


@bp.route("/users/<int:user_id>")
@admin_required
def edit_user(user_id: int):
    user = _get_user_or_404(user_id)
    zones: list[str] = []
    try:
        client = client_from_config(current_app.config)
        zones = [zone.get("name", "") for zone in client.list_zones()]
    except PdnsError as exc:
        log.warning("could not list zones for the access form: %s", exc)
        flash(f"Zones could not be listed: {exc}", "warning")

    return render_template(
        "admin/user_form.html",
        user=user,
        form={},
        roles=ROLES,
        role_descriptions=ROLE_DESCRIPTIONS,
        all_zones=sorted(zones),
        granted=set(user.granted_zones),
    )


@bp.route("/users/<int:user_id>", methods=["POST"])
@admin_required
def update_user(user_id: int):
    actor = current_user()
    assert actor is not None
    user = _get_user_or_404(user_id)
    db = get_session()

    role = (request.form.get("role") or user.role).strip()
    is_active = request.form.get("is_active") == "on"

    if role not in ROLES:
        flash("Choose a valid role.", "danger")
        return redirect(url_for("admin.edit_user", user_id=user_id))

    # Losing the last administrator would lock everyone out of user management.
    if user.is_admin and (role != ROLE_ADMIN or not is_active):
        remaining = db.scalar(
            select(func.count())
            .select_from(User)
            .filter(User.role == ROLE_ADMIN, User.is_active.is_(True), User.id != user.id)
        )
        if not remaining:
            flash(
                "This is the last active administrator. Promote another user first.",
                "danger",
            )
            return redirect(url_for("admin.edit_user", user_id=user_id))

    if user.id == actor.id and not is_active:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("admin.edit_user", user_id=user_id))

    changes: list[str] = []
    if user.role != role:
        changes.append(f"role {user.role}->{role}")
        user.role = role
        if not user.is_local:
            # Without this the group mapping recomputes the role at this
            # person's next sign-in and the change silently disappears. An
            # administrator picking a role by hand means it, so pin it; the
            # checkbox below is how they hand it back to the directory.
            user.role_locked = True
    if user.is_active != is_active:
        changes.append("activated" if is_active else "deactivated")
        user.is_active = is_active

    if not user.is_local and request.form.get("role_from_directory") == "on":
        # Explicitly handing the role back: the group mapping applies again
        # from the next sign-in, including demoting this account.
        if user.role_locked:
            changes.append("role handed back to the group mapping")
        user.role_locked = False

    if user.is_local:
        user.display_name = (request.form.get("display_name") or "").strip()[:190]
        user.email = (request.form.get("email") or "").strip()[:254]

        new_password = request.form.get("new_password") or ""
        if new_password:
            problems = password_problems(new_password, user.username)
            if problems:
                flash_errors(problems)
                return redirect(url_for("admin.edit_user", user_id=user_id))
            user.password_hash = hash_password(new_password)
            changes.append("password reset")

    db.commit()
    audit.record(
        "user.update", target=user.username, detail="; ".join(changes) or "no changes", actor=actor
    )
    flash(f"User {user.username} has been saved.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.route("/users/<int:user_id>/zones", methods=["POST"])
@admin_required
def update_zone_access(user_id: int):
    actor = current_user()
    user = _get_user_or_404(user_id)
    db = get_session()

    selected = {canonical(zone) for zone in request.form.getlist("zones") if zone.strip()}
    existing = {grant.zone: grant for grant in user.zone_grants}

    for zone, grant in existing.items():
        if zone not in selected:
            db.delete(grant)
    for zone in selected - set(existing):
        db.add(ZoneAccess(user_id=user.id, zone=zone))

    db.commit()
    audit.record(
        "user.zone_access",
        target=user.username,
        detail=f"{len(selected)} zone(s): {', '.join(sorted(selected)) or 'none'}",
        actor=actor,
    )
    flash(f"Zone access for {user.username} has been updated.", "success")
    return redirect(url_for("admin.edit_user", user_id=user_id))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    actor = current_user()
    assert actor is not None
    user = _get_user_or_404(user_id)
    db = get_session()

    if user.id == actor.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("admin.edit_user", user_id=user_id))

    if user.is_admin:
        remaining = db.scalar(
            select(func.count())
            .select_from(User)
            .filter(User.role == ROLE_ADMIN, User.is_active.is_(True), User.id != user.id)
        )
        if not remaining:
            flash("This is the last active administrator and cannot be deleted.", "danger")
            return redirect(url_for("admin.edit_user", user_id=user_id))

    username = user.username
    db.delete(user)
    db.commit()
    audit.record("user.delete", target=username, actor=actor)
    flash(f"User {username} has been deleted.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/audit")
@admin_required
def audit_log():
    try:
        limit = min(int(request.args.get("limit", 200)), 1000)
    except ValueError:
        limit = 200
    return render_template("admin/audit.html", entries=audit.recent(limit), limit=limit)


@bp.route("/settings")
@admin_required
def settings():
    """Read-only view of how the panel is configured.

    Editing happens in the environment, so this exists to answer "is LDAP
    actually on, and which groups map to admin?" without shell access.
    """
    config = current_app.config
    client = client_from_config(config)

    pdns_status: dict[str, object] = {"reachable": False, "version": "unknown", "error": None}
    try:
        info = client.server_info()
        pdns_status["reachable"] = True
        pdns_status["version"] = info.get("version", "unknown")
    except PdnsError as exc:
        pdns_status["error"] = str(exc)

    return render_template(
        "admin/settings.html",
        auth=config["AUTH"],
        pdns_status=pdns_status,
        pdns_url=config["PDNS_API_URL"],
        base_url=config["BASE_URL"],
        default_nameservers=config["DEFAULT_NAMESERVERS"],
        session_minutes=config["SESSION_LIFETIME_MINUTES"],
        cookie_secure=config["SESSION_COOKIE_SECURE"],
        proxy_count=config["TRUSTED_PROXY_COUNT"],
    )
