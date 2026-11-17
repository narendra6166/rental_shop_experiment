import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import g, has_request_context, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("RENTAL_DB", os.path.join(BASE_DIR, "rental.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER,
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Owner', 'Partner', 'Agent', 'Accounts', 'Staff', 'Worker')),
    active INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS login_attempts (
    address TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    started_at REAL NOT NULL
);
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
CREATE TABLE IF NOT EXISTS work_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Assigned' CHECK (status IN ('Assigned', 'In progress', 'Done')),
    UNIQUE (booking_id, user_id)
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    paid_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refund_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    amount REAL NOT NULL CHECK (amount>0),
    method TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    requested_by INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'Pending',
    reviewed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
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
    active INTEGER NOT NULL DEFAULT 1,
    salary_type TEXT NOT NULL DEFAULT 'Monthly',
    address TEXT,
    id_type TEXT,
    id_number TEXT
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    amount REAL NOT NULL CHECK (amount > 0),
    paid_on TEXT NOT NULL,
    note TEXT,
    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    qty INTEGER
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

ITEM_CATEGORIES = ["Tents & Event Setup", "Cooking & Catering", "Pooja & Rituals"]
LEGACY_ITEM_CATEGORIES = {
    "Tents": ITEM_CATEGORIES[0],
    "Furniture": ITEM_CATEGORIES[0],
    "Decoration": ITEM_CATEGORIES[0],
    "Lighting": ITEM_CATEGORIES[0],
    "Cooking": ITEM_CATEGORIES[1],
}

SAMPLE_ITEMS = [
    ("Tent 10x10 ft", ITEM_CATEGORIES[0], 20, 500, 1000),
    ("Shamiana (Large)", ITEM_CATEGORIES[0], 5, 3000, 5000),
    ("Gas Stove (2 burner)", ITEM_CATEGORIES[1], 10, 150, 500),
    ("Big Cooking Vessel (Deg)", ITEM_CATEGORIES[1], 15, 200, 500),
    ("Gas Cylinder Stand + Burner", ITEM_CATEGORIES[1], 8, 250, 800),
    ("Plastic Chair", ITEM_CATEGORIES[0], 300, 5, 0),
    ("Round Table", ITEM_CATEGORIES[0], 40, 60, 200),
    ("Stage Decoration Set", ITEM_CATEGORIES[0], 4, 2500, 3000),
]

EVENT_TYPES = ["Marriage", "Birthday", "Engagement", "Housewarming", "Party", "Other"]
# Delivered = work started; WorkDone = work finished, items not yet back; Returned = items back.
STATUSES = ["Booked", "Delivered", "WorkDone", "Returned", "Cancelled"]
OUT_STATUSES = ("Delivered", "WorkDone")
PAYMENT_KINDS = ["Advance", "Payment", "Deposit refund"]
ATTENDANCE = ["Present", "Half", "Absent", "Leave"]
SALARY_KINDS = ["Salary", "Advance", "Bonus"]
SALARY_TYPES = ["Monthly", "Daily"]
ID_TYPES = ["Aadhaar", "PAN", "Voter ID", "Driving Licence", "Other"]
EXPENSE_CATEGORIES = ["Shop rent", "New stock purchase", "Transport / Fuel", "Repairs",
                      "Electricity & water", "Other"]
STOCK_PURCHASE = "New stock purchase"

# Columns added after the first release; applied to existing databases on start-up.
MIGRATIONS = {
    "payments": [
        ("method", "TEXT NOT NULL DEFAULT 'Not recorded'"),
        ("reference", "TEXT NOT NULL DEFAULT ''"),
    ],
    "booking_items": [
        ("dispatched_qty", "INTEGER NOT NULL DEFAULT 0"),
        ("returned_qty", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "staff": [
        ("salary_type", "TEXT NOT NULL DEFAULT 'Monthly'"),
        ("address", "TEXT"),
        ("id_type", "TEXT"),
        ("id_number", "TEXT"),
    ],
    "bookings": [
        ("travel_charge", "REAL NOT NULL DEFAULT 0"),
        ("labour_charge", "REAL NOT NULL DEFAULT 0"),
        ("damage_charge", "REAL NOT NULL DEFAULT 0"),
        ("cancel_fee", "REAL NOT NULL DEFAULT 0"),
    ],
}

# Filter tabs on the bookings page -> derived status keys
STATUS_TABS = [
    ("all", "All", None),
    ("soon", "Starts soon", ("pending", "today", "soon")),
    ("upcoming", "Upcoming", ("upcoming",)),
    ("onwork", "On work", ("onwork", "overdue", "workdone")),
    ("payment", "Payment due", ("payment",)),
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


def commit_changes(db):
    """Keep the actor record in the same transaction as the business change."""
    if db.in_transaction and has_request_context() and g.get("user"):
        db.execute(
            "INSERT INTO activity_log (actor_id,actor_name,action,resource,created_at) VALUES (?,?,?,?,?)",
            (g.user["id"], g.user["username"], request.endpoint,
             json.dumps(g.get("activity_resource", request.view_args or {}), sort_keys=True),
             datetime.now().isoformat(timespec="seconds")),
        )
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("BEGIN IMMEDIATE")
    migrate_deliveries = "dispatched_qty" not in {
        r[1] for r in db.execute("PRAGMA table_info(booking_items)")}
    for table, columns in MIGRATIONS.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    if migrate_deliveries:
        db.execute("""UPDATE booking_items SET dispatched_qty=qty WHERE booking_id IN
                      (SELECT id FROM bookings WHERE status IN ('Delivered','WorkDone','Returned'))""")
        db.execute("""UPDATE booking_items SET returned_qty=qty WHERE booking_id IN
                      (SELECT id FROM bookings WHERE status='Returned')""")
    if db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO items (name, category, total_qty, rate_per_day, deposit) VALUES (?,?,?,?,?)",
            SAMPLE_ITEMS,
        )
    db.executemany("UPDATE items SET category=? WHERE category=?",
                   [(new, old) for old, new in LEGACY_ITEM_CATEGORIES.items()])
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
def booked_qty(db, item_id, start, end, exclude_booking=None, statuses=("Booked", "Delivered", "WorkDone"),
               quantity="reserved"):
    """Max units of an item unavailable on any single day within [start, end].

    A delivered booking keeps its items out until it is marked returned, so an overdue
    one blocks stock up to today even though its end date has passed.
    """
    expression = {"reserved": "bi.qty-bi.returned_qty",
                  "out": "bi.dispatched_qty-bi.returned_qty",
                  "pending": "bi.qty-bi.dispatched_qty"}[quantity]
    rows = db.execute(
        """SELECT b.start_date, b.end_date, b.status,
                  {} AS qty FROM booking_items bi
           JOIN bookings b ON b.id = bi.booking_id
           WHERE bi.item_id = ? AND b.status IN ({})
             AND b.id IS NOT ?""".format(expression, ",".join("?" * len(statuses))),
        (item_id, *statuses, exclude_booking),
    ).fetchall()
    today = date.today().isoformat()
    spans = []
    for r in rows:
        first, last = r["start_date"], r["end_date"]
        if r["status"] in OUT_STATUSES:
            # Items are physically out from the day work started until they come back.
            first, last = min(first, today), max(last, today)
        if first <= end and last >= start:
            spans.append((first, last, r["qty"]))
    peak = 0
    day, last_day = parse_date(start), parse_date(end)
    while day <= last_day:
        d = day.isoformat()
        peak = max(peak, sum(q for s, e, q in spans if s <= d <= e))
        day += timedelta(days=1)
    return peak


def damaged_open(db, item_id):
    return db.execute(
        "SELECT COALESCE(SUM(qty), 0) FROM damages WHERE item_id = ? AND status = 'Open'",
        (item_id,),
    ).fetchone()[0]


def available_qty(db, item, start, end, exclude_booking=None):
    return (item["total_qty"] - damaged_open(db, item["id"])
            - booked_qty(db, item["id"], start, end, exclude_booking))


# ---------- Bookings ----------
def status_info(b, today=None):
    """Return (label, css class, key) describing where a booking is in its life."""
    today = today or date.today()
    st = b["status"]
    if st == "Cancelled":
        return "Cancelled", "grey", "cancelled"
    if st == "Returned":
        # "balance" is attached by booking_rows; Completed means fully paid.
        if b["balance"] > 0 if "balance" in b.keys() else False:
            return "Payment pending", "orange", "payment"
        return "Completed", "green", "done"
    start, end = parse_date(b["start_date"]), parse_date(b["end_date"])
    if st == "WorkDone":
        if end < today:
            return "Overdue return", "red", "overdue"
        return "Work done, return pending", "blue", "workdone"
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
        return "Starts in {d}d", "orange", "soon", days
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
    damage = sum(d["charge"] for d in damages) + b["damage_charge"]
    travel, labour = b["travel_charge"], b["labour_charge"]
    payments = db.execute(
        "SELECT * FROM payments WHERE booking_id = ? ORDER BY id", (booking_id,)
    ).fetchall()
    paid = sum(p["amount"] for p in payments if p["kind"] != "Deposit refund")
    refunded = sum(p["amount"] for p in payments if p["kind"] == "Deposit refund")
    net_rent = max(rent - b["discount"], 0)
    charges = net_rent + damage + travel + labour
    # Deposit is no longer owed once the items are back; it then only appears as a refund.
    returned = b["status"] == "Returned"
    cancelled = b["status"] == "Cancelled"
    if cancelled:
        # Only the cancellation fee the shop keeps is owed; any other money paid is refundable.
        charges = b["cancel_fee"]
    total = charges if cancelled else charges + (0 if returned else deposit)
    net_paid = paid - refunded
    return {
        "days": days, "lines": lines, "damages": damages, "rent": rent,
        "discount": b["discount"], "net_rent": net_rent, "deposit": deposit,
        "damage": damage, "travel": travel, "labour": labour, "charges": charges, "total": total, "paid": paid,
        "refunded": refunded, "net_paid": net_paid, "deposit_returned": returned, "cancelled": cancelled, "cancel_fee": b["cancel_fee"],
        # > 0: customer still owes this; < 0: shop owes the customer a refund
        "balance": total - net_paid, "payments": payments,
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
    daily = staff["salary_type"] == "Daily"
    # Daily-wage workers are paid only for days worked; monthly staff also get paid leave.
    paid_days = counts["Present"] + 0.5 * counts["Half"] + (0 if daily else counts["Leave"])
    rate = staff["monthly_salary"]
    earned = rate * paid_days if daily else rate / days_in_month * paid_days
    payments = db.execute(
        "SELECT * FROM salary_payments WHERE staff_id = ? AND month = ? ORDER BY id",
        (staff["id"], month),
    ).fetchall()
    paid = sum(p["amount"] for p in payments if p["kind"] in ("Salary", "Advance"))
    bonus = sum(p["amount"] for p in payments if p["kind"] == "Bonus")
    return {
        "counts": counts, "marked": sum(counts.values()), "days_in_month": days_in_month,
        "paid_days": paid_days, "earned": earned, "paid": paid, "bonus": bonus,
        "due": earned - paid, "payments": payments, "daily": daily, "rate": rate,
    }


# ---------- Finance ----------
def month_finance(db, month):
    """Money in/out for a calendar month (YYYY-MM)."""
    like = month + "%"
    received = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='Deposit refund' THEN -amount ELSE amount END), 0) "
        "FROM payments WHERE paid_on LIKE ?", (like,)).fetchone()[0]
    # Rental income is booked in the month the event starts; deposits are not income.
    revenue = 0
    for b in db.execute("SELECT id FROM bookings WHERE start_date LIKE ?",
                        (like,)).fetchall():
        revenue += booking_totals(db, b["id"])["charges"]
    shop_expenses = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE paid_on LIKE ?", (like,)).fetchone()[0]
    salaries = db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM salary_payments WHERE paid_on LIKE ?", (like,)).fetchone()[0]
    expenses = shop_expenses + salaries
    return {"received": received, "revenue": revenue, "shop_expenses": shop_expenses,
            "salaries": salaries, "expenses": expenses, "profit": revenue - expenses,
            "net_cash": received - expenses}
