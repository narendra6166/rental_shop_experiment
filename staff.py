from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import SALARY_KINDS, ATTENDANCE, get_db, parse_date, parse_month, salary_summary, shift_month
from i18n import _

staff_bp = Blueprint("staff", __name__)


def get_staff(staff_id):
    return get_db().execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()


@staff_bp.route("/staff")
def staff_list():
    db = get_db()
    today = date.today().isoformat()
    month = date.today().strftime("%Y-%m")
    rows = db.execute("SELECT * FROM staff ORDER BY active DESC, name").fetchall()
    people = []
    for s in rows:
        att = db.execute(
            "SELECT status FROM attendance WHERE staff_id = ? AND day = ?", (s["id"], today)
        ).fetchone()
        people.append({"s": s, "today": att["status"] if att else None,
                       "sum": salary_summary(db, s, month)})
    return render_template("staff.html", people=people, month=month)


@staff_bp.route("/staff/new", methods=["GET", "POST"])
@staff_bp.route("/staff/<int:staff_id>/edit", methods=["GET", "POST"])
def staff_form(staff_id=None):
    person = get_staff(staff_id) if staff_id else None
    if staff_id and not person:
        return redirect(url_for("staff.staff_list"))
    if request.method == "POST":
        try:
            name = request.form["name"].strip()
            role = request.form["role"].strip()
            salary = float(request.form.get("monthly_salary") or 0)
            if not name or not role or salary < 0:
                raise ValueError
        except (KeyError, ValueError):
            flash(_("Enter name, role and a valid salary."), "error")
            return render_template("staff_form.html", p=request.form, staff_id=staff_id)
        join = request.form.get("join_date") or date.today().isoformat()
        active = 1 if request.form.get("active", "1") == "1" else 0
        db = get_db()
        data = (name, request.form.get("phone", "").strip(), role, salary, join, active)
        if staff_id:
            db.execute(
                "UPDATE staff SET name=?, phone=?, role=?, monthly_salary=?, join_date=?, active=? WHERE id=?",
                data + (staff_id,),
            )
        else:
            db.execute(
                "INSERT INTO staff (name, phone, role, monthly_salary, join_date, active) VALUES (?,?,?,?,?,?)",
                data,
            )
        db.commit()
        flash(_("Staff saved."), "ok")
        return redirect(url_for("staff.staff_list"))
    return render_template("staff_form.html", p=dict(person) if person else {}, staff_id=staff_id)


@staff_bp.route("/staff/<int:staff_id>")
def staff_view(staff_id):
    person = get_staff(staff_id)
    if not person:
        return redirect(url_for("staff.staff_list"))
    db = get_db()
    month = parse_month(request.args.get("month"))
    days = {
        r["day"]: r["status"]
        for r in db.execute(
            "SELECT day, status FROM attendance WHERE staff_id = ? AND substr(day,1,7) = ? ORDER BY day",
            (staff_id, month),
        )
    }
    return render_template(
        "staff_view.html", p=person, month=month, days=days,
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        s=salary_summary(db, person, month), kinds=SALARY_KINDS,
    )


@staff_bp.route("/staff/<int:staff_id>/pay", methods=["POST"])
def staff_pay(staff_id):
    month = parse_month(request.form.get("month"))
    try:
        amount = float(request.form["amount"])
        if amount <= 0:
            raise ValueError
    except (KeyError, ValueError):
        flash(_("Enter a valid amount."), "error")
        return redirect(url_for("staff.staff_view", staff_id=staff_id, month=month))
    kind = request.form.get("kind")
    if kind not in SALARY_KINDS:
        kind = "Salary"
    db = get_db()
    db.execute(
        "INSERT INTO salary_payments (staff_id, month, amount, kind, paid_on, note) VALUES (?,?,?,?,?,?)",
        (staff_id, month, amount, kind, date.today().isoformat(), request.form.get("note", "").strip()),
    )
    db.commit()
    flash(_("{kind} recorded.", kind=_(kind)), "ok")
    return redirect(url_for("staff.staff_view", staff_id=staff_id, month=month))


@staff_bp.route("/staff/<int:staff_id>/pay/<int:pay_id>/delete", methods=["POST"])
def staff_pay_delete(staff_id, pay_id):
    db = get_db()
    row = db.execute("SELECT month FROM salary_payments WHERE id = ? AND staff_id = ?",
                     (pay_id, staff_id)).fetchone()
    if row:
        db.execute("DELETE FROM salary_payments WHERE id = ?", (pay_id,))
        db.commit()
        flash(_("Payment removed."), "ok")
    return redirect(url_for("staff.staff_view", staff_id=staff_id, month=row["month"] if row else None))


@staff_bp.route("/attendance")
def attendance():
    db = get_db()
    day = parse_date(request.args.get("day")) or date.today()
    people = db.execute("SELECT * FROM staff WHERE active = 1 ORDER BY name").fetchall()
    marked = {
        r["staff_id"]: r["status"]
        for r in db.execute("SELECT staff_id, status FROM attendance WHERE day = ?", (day.isoformat(),))
    }
    return render_template("attendance.html", day=day, people=people, marked=marked,
                           statuses=ATTENDANCE)


@staff_bp.route("/attendance/mark", methods=["POST"])
def attendance_mark():
    day = parse_date(request.form.get("day")) or date.today()
    status = request.form.get("status")
    db = get_db()
    if status in ATTENDANCE:
        if request.form.get("staff_id") == "all":
            ids = [r["id"] for r in db.execute("SELECT id FROM staff WHERE active = 1")]
        elif request.form.get("staff_id", "").isdigit():
            ids = [int(request.form["staff_id"])]
        else:
            ids = []
        for sid in ids:
            db.execute(
                """INSERT INTO attendance (staff_id, day, status) VALUES (?,?,?)
                   ON CONFLICT(staff_id, day) DO UPDATE SET status = excluded.status""",
                (sid, day.isoformat(), status),
            )
        db.commit()
    return redirect(url_for("staff.attendance", day=day.isoformat()))
