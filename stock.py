from datetime import date

from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import booked_qty, damaged_open, get_db, parse_date

stock_bp = Blueprint("stock", __name__)


@stock_bp.route("/stock")
def stock():
    db = get_db()
    today = date.today().isoformat()
    start_s = request.args.get("start_date") or today
    end_s = request.args.get("end_date") or start_s
    start, end = parse_date(start_s), parse_date(end_s)
    if not start or not end or end < start:
        flash("End date must be on or after start date.", "error")
        start_s = end_s = today
    rows = []
    for i in db.execute("SELECT * FROM items ORDER BY category, name").fetchall():
        reserved = booked_qty(db, i["id"], start_s, end_s)
        damaged = damaged_open(db, i["id"])
        out_now = db.execute(
            """SELECT COALESCE(SUM(bi.qty), 0) FROM booking_items bi
               JOIN bookings b ON b.id = bi.booking_id
               WHERE bi.item_id = ? AND b.status = 'Delivered'""",
            (i["id"],),
        ).fetchone()[0]
        free = i["total_qty"] - damaged - reserved
        rows.append({"i": i, "reserved": reserved, "damaged": damaged, "out_now": out_now,
                     "free": free})
    return render_template("stock.html", rows=rows, start=start_s, end=end_s)


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
    items = db.execute("SELECT * FROM items ORDER BY name").fetchall()
    bookings = db.execute(
        """SELECT b.id, b.event_type, b.start_date, c.name FROM bookings b
           JOIN customers c ON c.id = b.customer_id
           WHERE b.status != 'Cancelled' ORDER BY b.id DESC LIMIT 50"""
    ).fetchall()
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        try:
            item_id = int(form["item_id"])
            qty = int(form["qty"])
            charge = float(form.get("charge") or 0)
            item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if not item or qty <= 0 or charge < 0:
                raise ValueError
        except (KeyError, ValueError):
            flash("Select an item and enter a valid quantity and charge.", "error")
        else:
            if qty > item["total_qty"] - damaged_open(db, item_id):
                flash("Quantity is more than the stock you have.", "error")
            else:
                kind = "Lost" if form.get("kind") == "Lost" else "Damaged"
                booking_id = int(form["booking_id"]) if form.get("booking_id", "").isdigit() else None
                db.execute(
                    """INSERT INTO damages (item_id, booking_id, qty, kind, charge, note, reported_on)
                       VALUES (?,?,?,?,?,?,?)""",
                    (item_id, booking_id, qty, kind, charge, form.get("note", "").strip(),
                     date.today().isoformat()),
                )
                db.commit()
                flash("Damage recorded. Stock updated.", "ok")
                if booking_id:
                    return redirect(url_for("booking_view", booking_id=booking_id))
                return redirect(url_for("stock.damages"))
    return render_template("damage_form.html", items=items, bookings=bookings, form=form)


@stock_bp.route("/damages/<int:damage_id>/resolve", methods=["POST"])
def damage_resolve(damage_id):
    db = get_db()
    d = db.execute("SELECT * FROM damages WHERE id = ? AND status = 'Open'", (damage_id,)).fetchone()
    if d:
        if request.form.get("action") == "repaired":
            db.execute("UPDATE damages SET status = 'Repaired' WHERE id = ?", (damage_id,))
            flash("Marked repaired. Units are back in stock.", "ok")
        else:
            db.execute("UPDATE items SET total_qty = MAX(total_qty - ?, 0) WHERE id = ?",
                       (d["qty"], d["item_id"]))
            db.execute("UPDATE damages SET status = 'Written off' WHERE id = ?", (damage_id,))
            flash("Written off. Total stock reduced.", "ok")
        db.commit()
    return redirect(url_for("stock.damages"))
