"""The signed-in user's own account."""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from .. import audit
from ..database import get_session
from ..security import (
    current_user,
    flash_errors,
    hash_password,
    login_required,
    password_problems,
    verify_password,
)

bp = Blueprint("profile", __name__, url_prefix="/profile")


@bp.route("/")
@login_required
def index():
    user = current_user()
    return render_template("profile.html", user=user, history=audit.recent(25, actor_id=user.id))


@bp.route("/", methods=["POST"])
@login_required
def update():
    user = current_user()
    assert user is not None

    if not user.is_local:
        # Name and e-mail are overwritten from the directory on every sign-in,
        # so letting them be edited here would be misleading.
        flash(
            "Your name and e-mail address come from your identity provider and "
            "cannot be changed here.",
            "warning",
        )
        return redirect(url_for("profile.index"))

    user.display_name = (request.form.get("display_name") or "").strip()[:190]
    user.email = (request.form.get("email") or "").strip()[:254]
    get_session().commit()
    audit.record("profile.update", target=user.username, actor=user)
    flash("Your profile has been saved.", "success")
    return redirect(url_for("profile.index"))


@bp.route("/password", methods=["POST"])
@login_required
def change_password():
    user = current_user()
    assert user is not None

    if not user.is_local:
        flash("Your password is managed by your identity provider.", "warning")
        return redirect(url_for("profile.index"))

    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not verify_password(user, current):
        audit.record("profile.password_change", target=user.username, actor=user, success=False)
        flash("Your current password is not correct.", "danger")
        return redirect(url_for("profile.index"))

    if new != confirm:
        flash("The new passwords do not match.", "danger")
        return redirect(url_for("profile.index"))

    problems = password_problems(new, user.username)
    if problems:
        flash_errors(problems)
        return redirect(url_for("profile.index"))

    user.password_hash = hash_password(new)
    get_session().commit()
    audit.record("profile.password_change", target=user.username, actor=user)
    flash("Your password has been changed.", "success")
    return redirect(url_for("profile.index"))
