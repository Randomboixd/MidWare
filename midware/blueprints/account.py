"""Public sign-in and sign-out pages for the human-facing UI."""

from __future__ import annotations

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from .. import adminauth

bp = Blueprint("account", __name__)


def _page(next_target: str, username: str = "") -> str:
    return render_template(
        "login.html",
        next=next_target,
        username=username,
        session_days=current_app.config["ADMIN_SESSION_DAYS"],
    )


@bp.route("/signin", methods=["GET", "POST"])
def signin():
    if not adminauth.enabled():
        return redirect(url_for("dashboard.index"))
    if not adminauth.credentials_configured():
        return redirect(url_for("admin.setup"))

    target = adminauth.safe_next(request.values.get("next"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if adminauth.login_blocked(username):
            flash("Too many failed attempts. Wait a few minutes and try again.", "error")
            return _page(target, username), 429
        if adminauth.verify(username, password):
            adminauth.clear_login_failures(username)
            adminauth.login(username)
            return redirect(target)
        adminauth.record_login_failure(username)
        flash("Incorrect username or password.", "error")
        return _page(target, username), 401

    if adminauth.is_authenticated():
        return redirect(target)
    return _page(target)


@bp.post("/signout")
def signout():
    adminauth.logout()
    return redirect(url_for("account.signin"))
