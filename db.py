import calendar
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RENTAL_DB", os.path.join(BASE_DIR, "rental.db"))

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
CREATE TABLE IF NOT EXISTS damages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    kind TEXT NOT NULL,
    charge REAL NOT NULL DEFAULT 0,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    reported_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    role TEXT NOT NULL,
    monthly_salary REAL NOT NULL DEFAULT 0,
    join_date TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (staff_id, day)
);
CREATE TABLE IF NOT EXISTS salary_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    month TEXT NOT NULL,
    amount REAL NOT NULL CHECK (amount > 0),
    kind TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    note TEXT
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
ATTENDANCE = ["Present", "Half", "Absent", "Leave"]
SALARY_KINDS = ["Salary", "Advance", "Bonus"]

# Filter tabs on the bookings page -> derived status keys
STATUS_TABS = [
    ("all", "All", None),
    ("soon", "Starts soon", ("pending", "today", "soon")),
    ("upcoming", "Upcoming", ("upcoming",)),
    ("onwork", "On work", ("onwork", "overdue")),
    ("done", "Completed", ("done",)),
    ("cancelled", "Cancelled", ("cancelled",)),
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
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


def parse_month(value):
    try:
        datetime.strptime(value, "%Y-%m")
        return value
    except (TypeError, ValueError):
        return date.today().strftime("%Y-%m")


def shift_month(month, delta):
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def rental_days(start, end):
    return (end - start).days + 1


# ---------- Stock ----------
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
    day, last = parse_date(start), parse_date(end)
    while day <= last:
        d = day.isoformat()
        peak = max(peak, sum(r["qty"] for r in rows if r["start_date"] <= d <= r["end_date"]))
        day += timedelta(days=1)
    return peak


def damaged_open(db, item_id):
    return db.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM damages WHERE item_id = ? AND status = 'Open'",
        (item_id,),
    ).fetchone()[0]


def available_qty(db, item, start, end):
    return item["total_qty"] - damaged_open(db, item["id"]) - booked_qty(db, item["id"], start, end)


# ---------- Bookings ----------
def status_info(b, today=None):
    """Return (label, css class, key) describing where a booking is in its life."""
    today = today or date.today()
    st = b["status"]
    if st == "Cancelled":
        return "Cancelled", "grey", "cancelled"
    if st == "Returned":
        return "Completed", "green", "done"
    start, end = parse_date(b["start_date"]), parse_date(b["end_date"])
    if st == "Delivered":
        if end < today:
            return "Overdue return", "red", "overdue"
        return "On work", "blue", "onwork"
    days = (start - today).days
    if days < 0:
        return "Delivery pending", "red", "pending"
    if days == 0:
        return "Starts today", "orange", "today"
    if days <= 3:
        return f"Starts in {days}d", "orange", "soon"
    return "Upcoming", "purple", "upcoming"


def booking_totals(db, booking_id):
    b = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    days = rental_days(parse_date(b["start_date"]), parse_date(b["end_date"]))
    lines = db.execute(
        """SELECT bi.*, i.name FROM booking_items bi
           JOIN items i ON i.id = bi.item_id WHERE bi.booking_id = ?""",
        (booking_id,),
    ).fetchall()
    damages = db.execute(
        """SELECT d.*, i.name FROM damages d JOIN items i ON i.id = d.item_id
           WHERE d.booking_id = ? ORDER BY d.id""",
        (booking_id,),
    ).fetchall()
    rent = sum(l["qty"] * l["rate"] * days for l in lines)
    deposit = sum(l["qty"] * l["deposit"] for l in lines)
    damage = sum(d["charge"] for d in damages)
    payments = db.execute(
        "SELECT * FROM payments WHERE booking_id = ? ORDER BY id", (booking_id,)
    ).fetchall()
    paid = sum(p["amount"] for p in payments if p["kind"] != "Deposit refund")
    refunded = sum(p["amount"] for p in payments if p["kind"] == "Deposit refund")
    net_rent = max(rent - b["discount"], 0)
    total = net_rent + deposit + damage
    return {
        "days": days, "lines": lines, "damages": damages, "rent": rent,
        "discount": b["discount"], "net_rent": net_rent, "deposit": deposit,
        "damage": damage, "total": total, "paid": paid, "refunded": refunded,
        "balance": total - paid + refunded, "payments": payments,
    }


# ---------- Staff ----------
def salary_summary(db, staff, month):
    y, m = map(int, month.split("-"))
    days_in_month = calendar.monthrange(y, m)[1]
    counts = {s: 0 for s in ATTENDANCE}
    for r in db.execute(
        "SELECT status, COUNT(*) AS c FROM attendance WHERE staff_id = ? AND substr(day,1,7) = ? GROUP BY status",
        (staff["id"], month),
    ):
        counts[r["status"]] = r["c"]
    paid_days = counts["Present"] + counts["Leave"] + 0.5 * counts["Half"]
    earned = staff["monthly_salary"] / days_in_month * paid_days
    payments = db.execute(
        "SELECT * FROM salary_payments WHERE staff_id = ? AND month = ? ORDER BY id",
        (staff["id"], month),
    ).fetchall()
    paid = sum(p["amount"] for p in payments if p["kind"] in ("Salary", "Advance"))
    bonus = sum(p["amount"] for p in payments if p["kind"] == "Bonus")
    return {
        "counts": counts, "marked": sum(counts.values()), "days_in_month": days_in_month,
        "paid_days": paid_days, "earned": earned, "paid": paid, "bonus": bonus,
        "due": earned - paid, "payments": payments,
    }
