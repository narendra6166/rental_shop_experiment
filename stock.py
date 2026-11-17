import math
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from db import ITEM_CATEGORIES, OUT_STATUSES, booked_qty, commit_changes, damaged_open, get_db, parse_date
from i18n import _

stock_bp = Blueprint("stock", __name__)


@stock_bp.route("/stock")
def stock():
    db = get_db()
    today = date.today().isoformat()
    start_s = request.args.get("start_date") or today
    end_s = request.args.get("end_date") or start_s
    start, end = parse_date(start_s), parse_date(end_s)
    if not start or not end or end < start:
        flash(_("End date must be on or after start date."), "error")
        start_s = end_s = today
    rows = []
    for i in db.execute("SELECT * FROM items ORDER BY category, name").fetchall():
        # Everything below is for the selected dates (peak on any single day).
        booked = booked_qty(db, i["id"], start_s, end_s, quantity="pending")
        on_rent = booked_qty(db, i["id"], start_s, end_s, statuses=OUT_STATUSES, quantity="out")
        taken = booked_qty(db, i["id"], start_s, end_s)
        damaged = damaged_open(db, i["id"])
        rows.append({"i": i, "booked": booked, "on_rent": on_rent, "taken": taken,
                     "damaged": damaged, "free": i["total_qty"] - damaged - taken})
    pending = db.execute(
        """SELECT b.id, b.end_date, b.start_date, c.name AS customer, c.phone,
                  GROUP_CONCAT(i.name || ' × ' || (bi.dispatched_qty-bi.returned_qty-bi.lost_qty), ', ') AS items,
                  SUM(bi.dispatched_qty-bi.returned_qty-bi.lost_qty) AS qty
           FROM bookings b JOIN customers c ON c.id = b.customer_id
           JOIN booking_items bi ON bi.booking_id = b.id JOIN items i ON i.id = bi.item_id
           WHERE b.status IN ('Delivered', 'WorkDone') AND bi.dispatched_qty>bi.returned_qty+bi.lost_qty
           GROUP BY b.id ORDER BY b.end_date""").fetchall()
    summary = {
        "total": sum(r["i"]["total_qty"] for r in rows),
        "free": sum(max(r["free"], 0) for r in rows),
        "taken": sum(r["taken"] for r in rows),
        "damaged": sum(r["damaged"] for r in rows),
    }
    category_rows = {category: [] for category in ITEM_CATEGORIES}
    for row in rows:
        category_rows.setdefault(row["i"]["category"], []).append(row)
    return render_template("stock.html", category_rows=category_rows, start=start_s, end=end_s,
                           pending=pending, summary=summary, today=today)


@stock_bp.route("/damages")
def damages():
    show = request.args.get("show", "open")
    sql = """SELECT d.*, i.name AS item, b.id AS bid, c.name AS customer
             FROM damages d JOIN items i ON i.id = d.item_id
             LEFT JOIN bookings b ON b.id = d.booking_id
             LEFT JOIN customers c ON c.id = b.customer_id"""
    args = ()
    if show == "open":
        sql += " WHERE d.status = 'Open'"
    rows = get_db().execute(sql + " ORDER BY d.id DESC", args).fetchall()
    return render_template("damages.html", rows=rows, show=show)


@stock_bp.route("/damages/new", methods=["GET", "POST"])
def damage_new():
    db = get_db()
    if request.values.get("booking_id"):
        raw = request.values.get("booking_id", "")
        if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            abort(400, description=_("Select a valid item."))
        return redirect(url_for("changes.settlement", booking_id=int(raw)))
    items = db.execute("SELECT * FROM items ORDER BY name").fetchall()
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        db.execute("BEGIN IMMEDIATE")
        try:
            item_id = int(form["item_id"])
            qty = int(form["qty"])
            charge = float(form.get("charge") or 0)
            item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not item or qty <= 0 or not math.isfinite(charge) or charge < 0:
                raise ValueError
        except (KeyError, ValueError):
            flash(_("Select an item and enter a valid quantity and charge."), "error")
        else:
            out = db.execute(
                "SELECT COALESCE(SUM(dispatched_qty-returned_qty-lost_qty),0) FROM booking_items WHERE item_id=?",
                (item_id,)).fetchone()[0]
            if qty > item["total_qty"] - damaged_open(db, item_id) - out:
                flash(_("Quantity is more than the stock you have."), "error")
            else:
                kind = "Lost" if form.get("kind") == "Lost" else "Damaged"
                db.execute(
                    """INSERT INTO damages (item_id, booking_id, qty, kind, charge, note, reported_on)
                       VALUES (?,?,?,?,?,?,?)""",
                    (item_id, None, qty, kind, charge, form.get("note", "").strip(),
                     date.today().isoformat()),
                )
                commit_changes(db)
                flash(_("Damage recorded. Stock updated."), "ok")
                return redirect(url_for("stock.damages"))
    return render_template("damage_form.html", items=items, form=form)


@stock_bp.route("/damages/<int:damage_id>/resolve", methods=["POST"])
def damage_resolve(damage_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    d = db.execute("SELECT * FROM damages WHERE id = ? AND status = 'Open'", (damage_id,)).fetchone()
    if d:
        action = request.form.get("action")
        if action not in ("repaired", "writeoff") or (action == "repaired" and d["kind"] == "Lost"):
            abort(400, description=_("Lost items must be written off, not marked repaired."))
        if action == "repaired":
            db.execute("UPDATE damages SET status = 'Repaired' WHERE id = ?", (damage_id,))
            flash(_("Marked repaired. Units are back in stock."), "ok")
        else:
            total = db.execute("SELECT total_qty FROM items WHERE id=?", (d["item_id"],)).fetchone()[0]
            out = db.execute(
                "SELECT COALESCE(SUM(dispatched_qty-returned_qty-lost_qty),0) FROM booking_items WHERE item_id=?",
                (d["item_id"],)).fetchone()[0]
            if total - d["qty"] < out:
                abort(400, description=_("Write-off would reduce stock below the quantity still out. Reconcile stock first."))
            db.execute("UPDATE items SET total_qty = total_qty - ? WHERE id = ?",
                       (d["qty"], d["item_id"]))
            db.execute("UPDATE damages SET status = 'Written off' WHERE id = ?", (damage_id,))
            flash(_("Written off. Total stock reduced."), "ok")
        commit_changes(db)
    return redirect(url_for("stock.damages"))
