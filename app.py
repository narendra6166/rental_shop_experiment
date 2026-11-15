import os
import sqlite3
from datetime import date, datetime

from flask import Flask, flash, g, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "rental.db")
SHOP_NAME = "Event Hire Shop"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    total_qty INTEGER NOT NULL CHECK (total_qty >= 0),
    rate_per_day REAL NOT NULL CHECK (rate_per_day >= 0),
    deposit REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    event_type TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Booked',
    discount REAL NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS booking_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    rate REAL NOT NULL,
    deposit REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    paid_on TEXT NOT NULL
);
"""

SAMPLE_ITEMS = [
    ("Tent 10x10 ft", "Tents", 20, 500, 1000),
    ("Shamiana (Large)", "Tents", 5, 3000, 5000),
    ("Gas Stove (2 burner)", "Cooking", 10, 150, 500),
    ("Big Cooking Vessel (Deg)", "Cooking", 15, 200, 500),
    ("Gas Cylinder Stand + Burner", "Cooking", 8, 250, 800),
    ("Plastic Chair", "Furniture", 300, 5, 0),
    ("Round Table", "Furniture", 40, 60, 200),
    ("Stage Decoration Set", "Decoration", 4, 2500, 3000),
]

EVENT_TYPES = ["Marriage", "Birthday", "Engagement", "Housewarming", "Party", "Other"]
STATUSES = ["Booked", "Delivered", "Returned", "Cancelled"]
PAYMENT_KINDS = ["Advance", "Payment", "Deposit refund"]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO items (name, category, total_qty, rate_per_day, deposit) VALUES (?,?,?,?,?)",
            SAMPLE_ITEMS,
        )
    db.commit()
    db.close()


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def rental_days(start, end):
    return (end - start).days + 1


def booked_qty(db, item_id, start, end, exclude_booking=None):
    """Max units of an item reserved on any single day within [start, end]."""
    rows = db.execute(
        """SELECT b.start_date, b.end_date, bi.qty FROM booking_items bi
           JOIN bookings b ON b.id = bi.booking_id
           WHERE bi.item_id = ? AND b.status IN ('Booked', 'Delivered')
             AND b.start_date <= ? AND b.end_date >= ? AND b.id IS NOT ?""",
        (item_id, end, start, exclude_booking),
    ).fetchall()
    peak = 0
    day = parse_date(start)
    last = parse_date(end)
    while day <= last:
        d = day.isoformat()
        used = sum(r["qty"] for r in rows if r["start_date"] <= d <= r["end_date"])
        peak = max(peak, used)
        day = date.fromordinal(day.toordinal() + 1)
    return peak


def available_qty(db, item, start, end):
    return item["total_qty"] - booked_qty(db, item["id"], start, end)


def booking_totals(db, booking_id):
    b = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    days = rental_days(parse_date(b["start_date"]), parse_date(b["end_date"]))
    lines = db.execute(
        """SELECT bi.*, i.name FROM booking_items bi
           JOIN items i ON i.id = bi.item_id WHERE bi.booking_id = ?""",
        (booking_id,),
    ).fetchall()
    rent = sum(l["qty"] * l["rate"] * days for l in lines)
    deposit = sum(l["qty"] * l["deposit"] for l in lines)
    payments = db.execute(
        "SELECT * FROM payments WHERE booking_id = ? ORDER BY id", (booking_id,)
    ).fetchall()
    rent_paid = sum(p["amount"] for p in payments if p["kind"] != "Deposit refund")
    refunded = sum(p["amount"] for p in payments if p["kind"] == "Deposit refund")
    rent_due = max(rent - b["discount"], 0)
    return {
        "days": days,
        "lines": lines,
        "rent": rent,
        "discount": b["discount"],
        "net_rent": rent_due,
        "deposit": deposit,
        "total": rent_due + deposit,
        "paid": rent_paid,
        "refunded": refunded,
        "balance": rent_due + deposit - rent_paid,
        "payments": payments,
    }


@app.route("/")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    stats = {
        "items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "customers": db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "active": db.execute(
            "SELECT COUNT(*) FROM bookings WHERE status IN ('Booked','Delivered')"
        ).fetchone()[0],
    }
    upcoming = db.execute(
        """SELECT b.*, c.name AS customer FROM bookings b
           JOIN customers c ON c.id = b.customer_id
           WHERE b.status IN ('Booked','Delivered') AND b.end_date >= ?
           ORDER BY b.start_date LIMIT 10""",
        (today,),
    ).fetchall()
    overdue = db.execute(
        """SELECT b.*, c.name AS customer FROM bookings b
           JOIN customers c ON c.id = b.customer_id
           WHERE b.status = 'Delivered' AND b.end_date < ? ORDER BY b.end_date""",
        (today,),
    ).fetchall()
    return render_template("dashboard.html", stats=stats, upcoming=upcoming, overdue=overdue)


# ---------- Items ----------
@app.route("/items")
def items():
    rows = get_db().execute("SELECT * FROM items ORDER BY category, name").fetchall()
    return render_template("items.html", items=rows)


@app.route("/items/new", methods=["GET", "POST"])
@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_form(item_id=None):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone() if item_id else None
    if item_id and not item:
        return redirect(url_for("items"))
    if request.method == "POST":
        try:
            data = (
                request.form["name"].strip(),
                request.form["category"].strip(),
                int(request.form["total_qty"]),
                float(request.form["rate_per_day"]),
                float(request.form.get("deposit") or 0),
            )
            if not data[0] or not data[1] or min(data[2:]) < 0:
                raise ValueError
        except (KeyError, ValueError):
            flash("Please enter valid item details.", "error")
            return render_template("item_form.html", item=request.form, item_id=item_id)
        if item_id:
            db.execute(
                "UPDATE items SET name=?, category=?, total_qty=?, rate_per_day=?, deposit=? WHERE id=?",
                data + (item_id,),
            )
        else:
            db.execute(
                "INSERT INTO items (name, category, total_qty, rate_per_day, deposit) VALUES (?,?,?,?,?)",
                data,
            )
        db.commit()
        flash("Item saved.", "ok")
        return redirect(url_for("items"))
    return render_template("item_form.html", item=item, item_id=item_id)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    db = get_db()
    try:
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        flash("Item deleted.", "ok")
    except sqlite3.IntegrityError:
        flash("Item is used in bookings and cannot be deleted.", "error")
    return redirect(url_for("items"))


# ---------- Customers ----------
@app.route("/customers")
def customers():
    q = request.args.get("q", "").strip()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY name",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    return render_template("customers.html", customers=rows, q=q)


@app.route("/customers/new", methods=["GET", "POST"])
def customer_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        if not name or not phone:
            flash("Name and phone are required.", "error")
            return render_template("customer_form.html", c=request.form)
        db = get_db()
        cur = db.execute(
            "INSERT INTO customers (name, phone, address) VALUES (?,?,?)",
            (name, phone, request.form.get("address", "").strip()),
        )
        db.commit()
        flash("Customer added.", "ok")
        nxt = request.args.get("next")
        if nxt == "booking":
            return redirect(url_for("booking_new", customer_id=cur.lastrowid))
        return redirect(url_for("customers"))
    return render_template("customer_form.html", c={})


# ---------- Bookings ----------
@app.route("/bookings")
def bookings():
    status = request.args.get("status", "")
    sql = """SELECT b.*, c.name AS customer, c.phone FROM bookings b
             JOIN customers c ON c.id = b.customer_id"""
    args = ()
    if status in STATUSES:
        sql += " WHERE b.status = ?"
        args = (status,)
    rows = get_db().execute(sql + " ORDER BY b.start_date DESC", args).fetchall()
    return render_template("bookings.html", bookings=rows, statuses=STATUSES, status=status)


@app.route("/bookings/new", methods=["GET", "POST"])
def booking_new():
    db = get_db()
    custs = db.execute("SELECT * FROM customers ORDER BY name").fetchall()
    all_items = db.execute("SELECT * FROM items ORDER BY category, name").fetchall()
    form = request.form if request.method == "POST" else request.args

    start_s, end_s = form.get("start_date", ""), form.get("end_date", "")
    start, end = parse_date(start_s), parse_date(end_s)
    avail = {}
    if start and end and end >= start:
        avail = {i["id"]: available_qty(db, i, start_s, end_s) for i in all_items}

    if request.method == "POST":
        errors = []
        if not form.get("customer_id", "").isdigit():
            errors.append("Select a customer.")
        if not (start and end) or end < start:
            errors.append("Enter valid start and end dates (end on/after start).")
        try:
            discount = float(form.get("discount") or 0)
            if discount < 0:
                raise ValueError
        except ValueError:
            errors.append("Invalid discount.")
            discount = 0
        chosen = []
        for i in all_items:
            raw = form.get(f"qty_{i['id']}", "").strip()
            if raw:
                if not raw.isdigit():
                    errors.append(f"Invalid quantity for {i['name']}.")
                elif int(raw) > 0:
                    chosen.append((i, int(raw)))
        if not chosen:
            errors.append("Select at least one item.")
        if avail:
            for i, q in chosen:
                if q > avail[i["id"]]:
                    errors.append(f"Only {avail[i['id']]} of {i['name']} available for those dates.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            cur = db.execute(
                """INSERT INTO bookings (customer_id, event_type, start_date, end_date, discount, notes, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (int(form["customer_id"]), form.get("event_type", "Other"), start_s, end_s,
                 discount, form.get("notes", "").strip(), datetime.now().isoformat(timespec="seconds")),
            )
            bid = cur.lastrowid
            db.executemany(
                "INSERT INTO booking_items (booking_id, item_id, qty, rate, deposit) VALUES (?,?,?,?,?)",
                [(bid, i["id"], q, i["rate_per_day"], i["deposit"]) for i, q in chosen],
            )
            try:
                advance = float(form.get("advance") or 0)
            except ValueError:
                advance = 0
            if advance > 0:
                db.execute(
                    "INSERT INTO payments (booking_id, amount, kind, paid_on) VALUES (?,?,?,?)",
                    (bid, advance, "Advance", date.today().isoformat()),
                )
            db.commit()
            flash("Booking created.", "ok")
            return redirect(url_for("booking_view", booking_id=bid))

    return render_template(
        "booking_form.html", customers=custs, items=all_items, avail=avail,
        form=form, event_types=EVENT_TYPES,
    )


@app.route("/bookings/<int:booking_id>")
def booking_view(booking_id):
    db = get_db()
    b = db.execute(
        """SELECT b.*, c.name AS customer, c.phone, c.address FROM bookings b
           JOIN customers c ON c.id = b.customer_id WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()
    if not b:
        return redirect(url_for("bookings"))
    return render_template(
        "booking_view.html", b=b, t=booking_totals(db, booking_id),
        statuses=STATUSES, kinds=PAYMENT_KINDS,
    )


@app.route("/bookings/<int:booking_id>/invoice")
def invoice(booking_id):
    db = get_db()
    b = db.execute(
        """SELECT b.*, c.name AS customer, c.phone, c.address FROM bookings b
           JOIN customers c ON c.id = b.customer_id WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()
    if not b:
        return redirect(url_for("bookings"))
    return render_template("invoice.html", b=b, t=booking_totals(db, booking_id), shop=SHOP_NAME)


@app.route("/bookings/<int:booking_id>/status", methods=["POST"])
def booking_status(booking_id):
    status = request.form.get("status")
    if status in STATUSES:
        db = get_db()
        db.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        db.commit()
        flash(f"Status changed to {status}.", "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/payment", methods=["POST"])
def booking_payment(booking_id):
    try:
        amount = float(request.form["amount"])
        if amount <= 0:
            raise ValueError
    except (KeyError, ValueError):
        flash("Enter a valid amount.", "error")
        return redirect(url_for("booking_view", booking_id=booking_id))
    kind = request.form.get("kind")
    if kind not in PAYMENT_KINDS:
        kind = "Payment"
    db = get_db()
    db.execute(
        "INSERT INTO payments (booking_id, amount, kind, paid_on) VALUES (?,?,?,?)",
        (booking_id, amount, kind, date.today().isoformat()),
    )
    db.commit()
    flash("Payment recorded.", "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/delete", methods=["POST"])
def booking_delete(booking_id):
    db = get_db()
    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    flash("Booking deleted.", "ok")
    return redirect(url_for("bookings"))


@app.template_filter("money")
def money(v):
    return f"₹{v:,.2f}"


init_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
