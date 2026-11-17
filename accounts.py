import re
import secrets
import sqlite3
import time
from datetime import timedelta
from urllib.parse import urlsplit

import click
from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from db import commit_changes, get_db
from i18n import _

accounts_bp = Blueprint("accounts", __name__)
ROLES = ("Owner", "Partner", "Agent", "Accounts", "Staff", "Worker")
WORK_STATUSES = ("Assigned", "In progress", "Done")
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))
READ_ENDPOINTS = {
    "dashboard", "bookings", "booking_view", "invoice", "customers", "items",
    "stock.stock", "stock.damages", "staff.staff_list", "staff.staff_view",
    "staff.attendance", "staff.attendance_history", "expenses", "finance", "operations.receipt",
    "operations.ledger", "operations.daily",
    "changes.history", "changes.queue",
}
AGENT_ENDPOINTS = {
    "bookings", "booking_view", "booking_new", "booking_status",
    "customers", "customer_new", "stock.stock", "stock.damages", "items",
    "operations.movement",
    "operations.daily",
    "changes.history", "changes.amend", "changes.settlement", "changes.queue",
}
ACCOUNTS_ENDPOINTS = {
    "finance", "expenses", "expense_new", "expense_delete", "bookings",
    "booking_view", "invoice", "booking_payment", "booking_cancel_fee",
    "customers", "stock.stock", "items", "staff.staff_list", "staff.staff_view",
    "staff.staff_pay", "staff.staff_pay_delete",
    "operations.receipt",
    "operations.ledger", "operations.daily",
    "changes.history", "changes.amend", "changes.settlement", "changes.review", "changes.queue",
    "stock.damages", "stock.damage_new", "stock.damage_resolve",
}


def allowed(endpoint, method="GET"):
    user = g.get("user")
    if not user:
        return False
    if endpoint in {"more", "language", "accounts.password", "accounts.logout", "accounts.live"}:
        return True
    if user["role"] == "Owner":
        return True
    if user["role"] == "Partner":
        return method in ("GET", "HEAD") and endpoint in READ_ENDPOINTS
    if user["role"] == "Agent":
        return endpoint in AGENT_ENDPOINTS
    if user["role"] == "Accounts":
        if endpoint in {"bookings", "booking_view", "invoice", "customers", "stock.stock",
                        "items", "staff.staff_list", "staff.staff_view", "finance", "expenses"}:
            return method in ("GET", "HEAD")
        return endpoint in ACCOUNTS_ENDPOINTS
    if user["role"] in ("Staff", "Worker"):
        return endpoint in {"accounts.work", "accounts.work_update"}
    return False


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_input():
    return Markup('<input type="hidden" name="csrf_token" value="{}">').format(csrf_token())


def landing():
    role = g.user["role"]
    return {"Agent": "bookings", "Accounts": "finance",
            "Staff": "accounts.work", "Worker": "accounts.work"}.get(role, "dashboard")


def install_accounts(app):
    app.register_blueprint(accounts_bp)
    app.config.update(PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    app.jinja_env.globals.update(can=allowed, csrf_input=csrf_input, roles=ROLES)

    @app.before_request
    def authorize():
        g.user = None
        if session.get("user_id"):
            user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1",
                                    (session["user_id"],)).fetchone()
            if user and user["session_version"] == session.get("session_version"):
                g.user = user
            else:
                session.clear()
        if request.endpoint in {"static", "set_language"} or request.endpoint is None:
            return
        if not g.user and request.endpoint != "accounts.login":
            return redirect(url_for("accounts.login", next=request.full_path.rstrip("?")))
        if g.user and request.endpoint != "accounts.login":
            if request.endpoint != "accounts.live":
                session.modified = True
            if request.endpoint == "dashboard" and landing() != "dashboard":
                return redirect(url_for(landing()))
            if not allowed(request.endpoint, request.method):
                abort(403, description=_("You do not have permission to access this page."))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            token = request.form.get("csrf_token", "")
            if not token or not secrets.compare_digest(token.encode(), session.get("_csrf", "").encode()):
                abort(400, description=_("Your form expired. Reload the page and try again."))

    @app.context_processor
    def account_context():
        return {"current_user": g.get("user")}

    @app.context_processor
    def live_context():
        return {"live_enabled": bool(g.get("user")) and request.endpoint not in {
            "language", "accounts.password", "accounts.login", "invoice", "operations.receipt"}}

    @app.after_request
    def private_responses(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.cli.command("create-owner")
    @click.option("--username", prompt=True)
    @click.option("--name", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def create_owner(username, name, password):
        """Create an owner account from the server console."""
        error = account_error(username, name, "Owner", password)
        if error:
            raise click.ClickException(error)
        try:
            get_db().execute(
                "INSERT INTO users (username,name,password_hash,role) VALUES (?,?,?,'Owner')",
                (username.strip(), name.strip(), generate_password_hash(password)),
            )
            commit_changes(get_db())
        except sqlite3.IntegrityError:
            get_db().rollback()
            raise click.ClickException("Username already exists.") from None
        click.echo("Owner created.")

    @app.cli.command("reset-password")
    @click.option("--username", prompt=True)
    @click.password_option(confirmation_prompt=True)
    def reset_password(username, password):
        """Recover an account password from the server console."""
        if len(password) < 12:
            raise click.ClickException("Use at least 12 characters for the password.")
        cur = get_db().execute(
            "UPDATE users SET password_hash=?, session_version=session_version+1 WHERE username=?",
            (generate_password_hash(password), username.strip()),
        )
        commit_changes(get_db())
        if not cur.rowcount:
            raise click.ClickException("Account not found.")
        click.echo("Password reset; existing sessions invalidated.")


def bootstrap_owner(app, username, password):
    if not password:
        return
    with app.app_context():
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            db.rollback()
            return
        error = account_error(username, username, "Owner", password)
        if error:
            raise RuntimeError(f"Cannot create initial owner: {error}")
        db.execute(
            "INSERT INTO users (username,name,password_hash,role) VALUES (?,?,?,'Owner')",
            (username.strip(), username.strip(), generate_password_hash(password)),
        )
        commit_changes(db)


def account_error(username, name, role, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username.strip()) or not name.strip():
        return "Enter a name and a username of 3-64 letters, numbers, dots, underscores or hyphens."
    if role not in ROLES:
        return "Select a valid role."
    if password is not None and len(password) < 12:
        return "Use at least 12 characters for the password."
    return None


@accounts_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for(landing()))
    configured = get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    if not configured:
        return render_template("setup_required.html"), 503
    if request.method == "POST":
        db = get_db()
        address = request.remote_addr or "unknown"
        now = time.time()
        attempt = db.execute("SELECT * FROM login_attempts WHERE address=?", (address,)).fetchone()
        if attempt and now - attempt["started_at"] < 900 and attempt["failures"] >= 10:
            flash(_("Too many login attempts. Try again in 15 minutes."), "error")
            return render_template("login.html"), 429
        user = db.execute("SELECT * FROM users WHERE username=?",
                          (request.form.get("user", "").strip(),)).fetchone()
        password_ok = check_password_hash(user["password_hash"] if user else DUMMY_PASSWORD_HASH,
                                         request.form.get("password", ""))
        if user and password_ok and user["active"]:
            db.execute("DELETE FROM login_attempts WHERE address=?", (address,))
            commit_changes(db)
            session.clear()
            session.update(user_id=user["id"], session_version=user["session_version"])
            session.permanent = True
            g.user = user
            nxt = request.args.get("next", "")
            # Reject browser-normalized backslashes and external URLs.
            safe = nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt and not urlsplit(nxt).netloc
            return redirect(nxt if safe else url_for(landing()))
        db.execute(
            """INSERT INTO login_attempts (address,failures,started_at) VALUES (?,1,?)
               ON CONFLICT(address) DO UPDATE SET
               failures=CASE WHEN ?-started_at >= 900 THEN 1 ELSE failures+1 END,
               started_at=CASE WHEN ?-started_at >= 900 THEN ? ELSE started_at END""",
            (address, now, now, now, now),
        )
        commit_changes(db)
        flash(_("Wrong ID or password."), "error")
    return render_template("login.html")


@accounts_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("accounts.login"))


@accounts_bp.route("/account/password", methods=["GET", "POST"])
def password():
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")):
            flash(_("Current password is incorrect."), "error")
        elif len(new) < 12:
            flash(_("Use at least 12 characters for the password."), "error")
        else:
            get_db().execute("UPDATE users SET password_hash=?, session_version=session_version+1 WHERE id=?",
                             (generate_password_hash(new), g.user["id"]))
            commit_changes(get_db())
            session["session_version"] = g.user["session_version"] + 1
            flash(_("Password updated."), "ok")
            return redirect(url_for("more"))
    return render_template("password.html")


@accounts_bp.route("/accounts", methods=["GET"])
def users():
    return render_template("accounts.html", users=get_db().execute(
        "SELECT id,username,name,role,active FROM users ORDER BY name").fetchall())


@accounts_bp.route("/accounts/new", methods=["GET", "POST"])
@accounts_bp.route("/accounts/<int:user_id>/edit", methods=["GET", "POST"])
def user_form(user_id=None):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    if user_id and not user:
        abort(404)
    if request.method == "POST":
        form = request.form
        username, name = form.get("username", "").strip(), form.get("name", "").strip()
        role, new_password = form.get("role", ""), form.get("password", "")
        error = account_error(username, name, role, new_password if new_password or not user else None)
        active_value = form.get("active", "1")
        if active_value not in ("0", "1"):
            error = "Select a valid account status."
        if not error:
            try:
                # Serialize last-owner checks with account updates.
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
                active = int(active_value)
                owners = db.execute("SELECT COUNT(*) FROM users WHERE role='Owner' AND active=1").fetchone()[0]
                if current and current["role"] == "Owner" and current["active"] and owners <= 1 and (role != "Owner" or not active):
                    error = "Keep at least one active owner."
                    db.rollback()
                else:
                    if user:
                        hashed = generate_password_hash(new_password) if new_password else current["password_hash"]
                        db.execute(
                            "UPDATE users SET username=?,name=?,role=?,active=?,password_hash=?,session_version=session_version+1 WHERE id=?",
                            (username, name, role, active, hashed, user_id),
                        )
                    else:
                        db.execute("INSERT INTO users (username,name,role,active,password_hash) VALUES (?,?,?,?,?)",
                                   (username, name, role, active, generate_password_hash(new_password)))
                    commit_changes(db)
                    if user_id == g.user["id"]:
                        session["session_version"] = current["session_version"] + 1
                    flash(_("Account saved."), "ok")
                    return redirect(url_for("accounts.users"))
            except sqlite3.IntegrityError:
                db.rollback()
                error = "Username already exists."
        flash(_(error), "error")
        return render_template("account_form.html", person=form, editing=bool(user_id))
    return render_template("account_form.html", person=dict(user) if user else {}, editing=bool(user_id))


@accounts_bp.route("/work")
def work():
    rows = get_db().execute(
        """SELECT a.*, b.start_date,b.end_date,b.event_type,b.status AS booking_status,
                  c.name AS customer,c.phone,c.address FROM work_assignments a
           JOIN bookings b ON b.id=a.booking_id JOIN customers c ON c.id=b.customer_id
           WHERE a.user_id=? ORDER BY b.start_date,a.id""", (g.user["id"],)).fetchall()
    return render_template("work.html", assignments=rows, statuses=WORK_STATUSES)


@accounts_bp.route("/work/<int:assignment_id>/status", methods=["POST"])
def work_update(assignment_id):
    status = request.form.get("status", "")
    if status not in WORK_STATUSES:
        abort(400, description=_("Select a valid work status."))
    cur = get_db().execute("UPDATE work_assignments SET status=? WHERE id=? AND user_id=?",
                           (status, assignment_id, g.user["id"]))
    if not cur.rowcount:
        abort(404)
    row = get_db().execute("SELECT booking_id FROM work_assignments WHERE id=?", (assignment_id,)).fetchone()
    g.activity_resource = {"booking_id": row["booking_id"], "user_id": g.user["id"],
                           "assignment_id": assignment_id}
    commit_changes(get_db())
    flash(_("Work status updated."), "ok")
    return redirect(url_for("accounts.work"))


@accounts_bp.route("/bookings/<int:booking_id>/assign", methods=["POST"])
def assign(booking_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM bookings WHERE id=?", (booking_id,)).fetchone():
        abort(404)
    user_id = request.form.get("user_id", "")
    worker = db.execute("SELECT id FROM users WHERE id=? AND active=1 AND role IN ('Staff','Worker')",
                         (user_id,)).fetchone()
    task = request.form.get("task", "").strip()
    if not worker or not task or len(task) > 500:
        flash(_("Choose a staff/worker account and enter a task (up to 500 characters)."), "error")
    else:
        db.execute(
            """INSERT INTO work_assignments (booking_id,user_id,task) VALUES (?,?,?)
               ON CONFLICT(booking_id,user_id) DO UPDATE SET task=excluded.task, status='Assigned'""",
            (booking_id, worker["id"], task),
        )
        g.activity_resource = {"booking_id": booking_id, "user_id": worker["id"]}
        commit_changes(db)
        flash(_("Work assigned."), "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@accounts_bp.route("/work/<int:assignment_id>/remove", methods=["POST"])
def unassign(assignment_id):
    db = get_db()
    row = db.execute("SELECT booking_id,user_id FROM work_assignments WHERE id=?", (assignment_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("DELETE FROM work_assignments WHERE id=?", (assignment_id,))
    g.activity_resource = dict(row)
    commit_changes(db)
    flash(_("Assignment removed."), "ok")
    return redirect(url_for("booking_view", booking_id=row["booking_id"]))


@accounts_bp.route("/live")
def live():
    return jsonify(revision=str(g.user["live_revision"]))
