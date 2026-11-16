import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta

from flask import (Flask, flash, make_response, redirect, render_template, request, send_file,
                   url_for)

import db as core
from i18n import LANGS, _, fmt_date, get_lang
from db import (EVENT_TYPES, EXPENSE_CATEGORIES, PAYMENT_KINDS, STATUS_TABS, STATUSES,
                STOCK_PURCHASE, available_qty, booking_totals, get_db, month_finance, parse_date,
                parse_month, shift_month, status_info)
from staff import staff_bp
from stock import stock_bp

SHOP_NAME = os.environ.get("SHOP_NAME", "Ganesh Hyres")
SHOP_ADDRESS = os.environ.get(
    "SHOP_ADDRESS",
    "Near Sri Potti Sreeramulu Bomma, Chennur Road, Buchireddypalem, Nellore, Andhra Pradesh")
SHOP_PHONE = os.environ.get("SHOP_PHONE", "")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.teardown_appcontext(core.close_db)
app.register_blueprint(staff_bp)
app.register_blueprint(stock_bp)
app.jinja_env.globals.update(salary_types=core.SALARY_TYPES, id_types=core.ID_TYPES,
                           status_info=status_info, shop_name=SHOP_NAME, shop_address=SHOP_ADDRESS,
                           shop_phone=SHOP_PHONE, today=date.today,
                           timedelta=timedelta, _=_, get_lang=get_lang, langs=LANGS)


@app.route("/lang/<code>")
def set_language(code):
    target = request.args.get("next") or request.referrer or url_for("dashboard")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("dashboard")
    resp = make_response(redirect(target))
    if code in LANGS:
        resp.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.template_filter("money")
def money(v):
    v = v or 0
    return f"₹{v:,.0f}" if float(v).is_integer() else f"₹{v:,.2f}"


@app.template_filter("dmy")
def dmy(v):
    d = parse_date(v)
    return fmt_date(d, "%d %b") if d else v


@app.template_filter("ldate")
def ldate(d, pattern):
    return fmt_date(d, pattern)


def booking_rows(where="", args=()):
    db = get_db()
    rows = db.execute(
        f"""SELECT b.*, c.name AS customer, c.phone FROM bookings b
            JOIN customers c ON c.id = b.customer_id {where}
            ORDER BY b.start_date""",
        args,
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["balance"] = booking_totals(db, r["id"])["balance"]
        result.append(d)
    return result


@app.route("/")
def dashboard():
    db = get_db()
    month = date.today().strftime("%Y-%m")
    active = [b for b in booking_rows()
              if b["status"] != "Cancelled" and status_info(b)[2] != "done"]
    by_key = {}
    for b in active:
        by_key.setdefault(status_info(b)[2], []).append(b)
    needs_action = (by_key.get("pending", []) + by_key.get("today", []) + by_key.get("overdue", [])
                    + by_key.get("payment", []))
    soon = by_key.get("soon", [])
    on_work = by_key.get("onwork", []) + by_key.get("workdone", [])
    totals = [(b, booking_totals(db, b["id"])) for b in booking_rows() if b["status"] != "Cancelled"]
    balance_due = sum(max(t["balance"], 0) for b, t in totals)
    refunds_due = sum(-t["balance"] for b, t in totals if b["status"] == "Returned" and t["balance"] < 0)
    fin = month_finance(db, month)
    stats = {
        "soon": len(soon) + len(by_key.get("today", [])) + len(by_key.get("pending", [])),
        "on_work": len(on_work) + len(by_key.get("overdue", [])),
        "balance": balance_due,
        "refunds_due": refunds_due,
        "collected": fin["received"],
        "spent": fin["expenses"],
        "net_cash": fin["net_cash"],
        "open_damages": db.execute(
            "SELECT COALESCE(SUM(qty),0) FROM damages WHERE status='Open'"
        ).fetchone()[0],
        "staff_total": db.execute("SELECT COUNT(*) FROM staff WHERE active=1").fetchone()[0],
        "staff_present": db.execute(
            """SELECT COUNT(*) FROM attendance a JOIN staff s ON s.id=a.staff_id
               WHERE a.day=? AND a.status IN ('Present','Half') AND s.active=1""",
            (date.today().isoformat(),),
        ).fetchone()[0],
    }
    return render_template("dashboard.html", stats=stats, needs_action=needs_action,
                           soon=soon, on_work=on_work)


@app.route("/more")
def more():
    return render_template("more.html")


@app.route("/finance")
def finance():
    db = get_db()
    month = parse_month(request.args.get("month"))
    totals = [(b, booking_totals(db, b["id"])) for b in booking_rows() if b["status"] != "Cancelled"]
    by_category = db.execute(
        "SELECT category, SUM(amount) AS total FROM expenses WHERE paid_on LIKE ? "
        "GROUP BY category ORDER BY total DESC", (month + "%",)).fetchall()
    return render_template(
        "finance.html", month=month, f=month_finance(db, month), by_category=by_category,
        prev_month=shift_month(month, -1), next_month=shift_month(month, 1),
        to_collect=sum(max(t["balance"], 0) for b, t in totals),
        to_refund=sum(-t["balance"] for b, t in totals if b["status"] == "Returned" and t["balance"] < 0),
    )


@app.route("/expenses")
def expenses():
    db = get_db()
    month = parse_month(request.args.get("month"))
    rows = db.execute(
        """SELECT e.*, i.name AS item FROM expenses e LEFT JOIN items i ON i.id = e.item_id
           WHERE e.paid_on LIKE ? ORDER BY e.paid_on DESC, e.id DESC""", (month + "%",)).fetchall()
    return render_template("expenses.html", rows=rows, month=month, total=sum(r["amount"] for r in rows),
                           prev_month=shift_month(month, -1), next_month=shift_month(month, 1))


@app.route("/expenses/new", methods=["GET", "POST"])
def expense_new():
    db = get_db()
    items = db.execute("SELECT * FROM items ORDER BY name").fetchall()
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        category = form.get("category")
        item_id = int(form["item_id"]) if form.get("item_id", "").isdigit() else None
        qty = int(form["qty"]) if form.get("qty", "").isdigit() else 0
        paid_on = (parse_date(form.get("paid_on")) or date.today()).isoformat()
        try:
            amount = float(form.get("amount", ""))
        except ValueError:
            amount = 0
        stock_buy = category == STOCK_PURCHASE
        if category not in EXPENSE_CATEGORIES or amount <= 0 or (stock_buy and (not item_id or qty <= 0)):
            flash(_("Enter a valid amount and category (and item and quantity for stock purchases)."), "error")
        else:
            db.execute(
                "INSERT INTO expenses (category, amount, paid_on, note, item_id, qty) VALUES (?,?,?,?,?,?)",
                (category, amount, paid_on, form.get("note", "").strip(),
                 item_id if stock_buy else None, qty if stock_buy else None),
            )
            if stock_buy:
                db.execute("UPDATE items SET total_qty = total_qty + ? WHERE id = ?", (qty, item_id))
            db.commit()
            flash(_("Expense saved."), "ok")
            return redirect(url_for("expenses", month=paid_on[:7]))
    return render_template("expense_form.html", items=items, form=form, categories=EXPENSE_CATEGORIES,
                           stock_purchase=STOCK_PURCHASE)


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def expense_delete(expense_id):
    db = get_db()
    e = db.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if e:
        if e["item_id"] and e["qty"]:
            db.execute("UPDATE items SET total_qty = MAX(total_qty - ?, 0) WHERE id = ?",
                       (e["qty"], e["item_id"]))
        db.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        db.commit()
        flash(_("Expense deleted."), "ok")
    return redirect(url_for("expenses", month=e["paid_on"][:7] if e else None))


@app.route("/backup")
def backup():
    src = get_db()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dest = sqlite3.connect(tmp.name)
    src.backup(dest)
    dest.close()
    name = f"rental-backup-{datetime.now():%Y%m%d-%H%M}.db"
    return send_file(tmp.name, as_attachment=True, download_name=name)


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
            flash(_("Please enter valid item details."), "error")
            return render_template("item_form.html", item=request.form, item_id=item_id)
        if item_id:
            out_now = db.execute(
                """SELECT COALESCE(SUM(bi.qty), 0) FROM booking_items bi JOIN bookings b ON b.id = bi.booking_id
                   WHERE bi.item_id = ? AND b.status IN ('Delivered', 'WorkDone')""", (item_id,)).fetchone()[0]
            if data[2] < out_now:
                flash(_("{n} units are out on rent, total cannot be less than that.", n=out_now), "error")
                return render_template("item_form.html", item=request.form, item_id=item_id)
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
        flash(_("Item saved."), "ok")
        return redirect(url_for("items"))
    return render_template("item_form.html", item=item, item_id=item_id)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
def item_delete(item_id):
    db = get_db()
    try:
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        flash(_("Item deleted."), "ok")
    except sqlite3.IntegrityError:
        flash(_("Item is used in bookings or damage records and cannot be deleted."), "error")
    return redirect(url_for("items"))


# ---------- Customers ----------
@app.route("/customers")
def customers():
    q = request.args.get("q", "").strip()
    rows = get_db().execute(
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
            flash(_("Name and phone are required."), "error")
            return render_template("customer_form.html", c=request.form)
        db = get_db()
        cur = db.execute(
            "INSERT INTO customers (name, phone, address) VALUES (?,?,?)",
            (name, phone, request.form.get("address", "").strip()),
        )
        db.commit()
        flash(_("Customer added."), "ok")
        if request.args.get("next") == "booking":
            return redirect(url_for("booking_new", customer_id=cur.lastrowid))
        return redirect(url_for("customers"))
    return render_template("customer_form.html", c={})


# ---------- Bookings ----------
@app.route("/bookings")
def bookings():
    tab = request.args.get("tab", "all")
    q = request.args.get("q", "").strip().lower()
    keys = {t[0]: t[2] for t in STATUS_TABS}.get(tab)
    rows = booking_rows()
    counts = {t[0]: 0 for t in STATUS_TABS}
    shown = []
    for b in rows:
        key = status_info(b)[2]
        counts["all"] += 1
        for t in STATUS_TABS[1:]:
            if key in t[2]:
                counts[t[0]] += 1
        if keys and key not in keys:
            continue
        if q and q not in f"{b['customer']} {b['phone']} {b['id']} {b['event_type']}".lower():
            continue
        shown.append(b)
    # Active work first, finished ones last
    shown.sort(key=lambda b: (status_info(b)[2] in ("done", "cancelled"), b["start_date"]))
    return render_template("bookings.html", bookings=shown, tabs=STATUS_TABS,
                           tab=tab, counts=counts, q=q)


def money_field(form, name, errors, label):
    try:
        v = float(form.get(name) or 0)
        if v < 0:
            raise ValueError
        return v
    except ValueError:
        errors.append(_("Invalid {label}.", label=_(label)))
        return 0


@app.route("/bookings/new", methods=["GET", "POST"])
@app.route("/bookings/<int:booking_id>/edit", methods=["GET", "POST"])
def booking_new(booking_id=None):
    db = get_db()
    existing = None
    if booking_id:
        existing = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not existing:
            return redirect(url_for("bookings"))
    custs = db.execute("SELECT * FROM customers ORDER BY name").fetchall()
    all_items = db.execute("SELECT * FROM items ORDER BY category, name").fetchall()
    if request.method == "POST":
        form = request.form
    elif existing and not request.args:
        form = {k: existing[k] for k in existing.keys()}
        form["customer_id"] = existing["customer_id"]
        for l in db.execute("SELECT item_id, qty FROM booking_items WHERE booking_id = ?", (booking_id,)):
            form[f"qty_{l['item_id']}"] = l["qty"]
    else:
        form = request.args

    start_s, end_s = form.get("start_date", ""), form.get("end_date", "")
    start, end = parse_date(start_s), parse_date(end_s)
    avail = {}
    if start and end and end >= start:
        avail = {i["id"]: available_qty(db, i, start_s, end_s, booking_id) for i in all_items}

    if request.method == "POST":
        errors = []
        if not form.get("customer_id", "").isdigit():
            errors.append(_("Select a customer."))
        if not (start and end) or end < start:
            errors.append(_("Enter valid start and end dates (end on/after start)."))
        discount = money_field(form, "discount", errors, "Discount")
        travel = money_field(form, "travel_charge", errors, "Travel charges")
        labour = money_field(form, "labour_charge", errors, "Labour charges")
        damage = money_field(form, "damage_charge", errors, "Damage charges")
        chosen = []
        for i in all_items:
            raw = form.get(f"qty_{i['id']}", "").strip()
            if raw:
                if not raw.isdigit():
                    errors.append(_("Invalid quantity for {name}.", name=i["name"]))
                elif int(raw) > 0:
                    chosen.append((i, int(raw)))
        if not chosen:
            errors.append(_("Select at least one item."))
        if avail and not (existing and existing["status"] in ("Returned", "Cancelled")):
            for i, q in chosen:
                if q > avail[i["id"]]:
                    errors.append(_("Only {n} of {name} available for those dates.",
                                    n=avail[i["id"]], name=i["name"]))
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            fields = (int(form["customer_id"]), form.get("event_type", "Other"), start_s, end_s,
                      discount, travel, labour, damage, form.get("notes", "").strip())
            if existing:
                bid = booking_id
                old = {r["item_id"]: r for r in db.execute(
                    "SELECT * FROM booking_items WHERE booking_id = ?", (bid,))}
                db.execute(
                    """UPDATE bookings SET customer_id=?, event_type=?, start_date=?, end_date=?, discount=?,
                       travel_charge=?, labour_charge=?, damage_charge=?, notes=? WHERE id=?""",
                    fields + (bid,))
                db.execute("DELETE FROM booking_items WHERE booking_id = ?", (bid,))
            else:
                cur = db.execute(
                    """INSERT INTO bookings (customer_id, event_type, start_date, end_date, discount,
                       travel_charge, labour_charge, damage_charge, notes, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    fields + (datetime.now().isoformat(timespec="seconds"),))
                bid, old = cur.lastrowid, {}
            # Keep the rate the customer was quoted for items that were already on the booking.
            db.executemany(
                "INSERT INTO booking_items (booking_id, item_id, qty, rate, deposit) VALUES (?,?,?,?,?)",
                [(bid, i["id"], q, old[i["id"]]["rate"] if i["id"] in old else i["rate_per_day"],
                  old[i["id"]]["deposit"] if i["id"] in old else i["deposit"]) for i, q in chosen],
            )
            if not existing:
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
            flash(_("Booking updated.") if existing else _("Booking created."), "ok")
            return redirect(url_for("booking_view", booking_id=bid))

    return render_template("booking_form.html", customers=custs, items=all_items, avail=avail,
                           form=form, event_types=EVENT_TYPES, editing=existing)


def load_booking(booking_id):
    return get_db().execute(
        """SELECT b.*, c.name AS customer, c.phone, c.address FROM bookings b
           JOIN customers c ON c.id = b.customer_id WHERE b.id = ?""",
        (booking_id,),
    ).fetchone()


@app.route("/bookings/<int:booking_id>")
def booking_view(booking_id):
    b = load_booking(booking_id)
    if not b:
        return redirect(url_for("bookings"))
    t = booking_totals(get_db(), booking_id)
    return render_template("booking_view.html", b=dict(b, balance=t["balance"]), t=t,
                           kinds=PAYMENT_KINDS)


@app.route("/bookings/<int:booking_id>/invoice")
def invoice(booking_id):
    b = load_booking(booking_id)
    if not b:
        return redirect(url_for("bookings"))
    return render_template("invoice.html", b=b, t=booking_totals(get_db(), booking_id))


@app.route("/bookings/<int:booking_id>/status", methods=["POST"])
def booking_status(booking_id):
    status = request.form.get("status")
    if status in STATUSES:
        db = get_db()
        db.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        db.commit()
        flash(_("Booking updated."), "ok")
        if status == "Returned":
            flash(_("Items back? Report any damaged or lost items below."), "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/payment", methods=["POST"])
def booking_payment(booking_id):
    try:
        amount = float(request.form["amount"])
        if amount <= 0:
            raise ValueError
    except (KeyError, ValueError):
        flash(_("Enter a valid amount."), "error")
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
    flash(_("Payment recorded."), "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@app.route("/bookings/<int:booking_id>/delete", methods=["POST"])
def booking_delete(booking_id):
    db = get_db()
    if db.execute("SELECT 1 FROM payments WHERE booking_id = ?", (booking_id,)).fetchone():
        flash(_("This booking has payments, so it cannot be deleted. Cancel it instead."), "error")
        return redirect(url_for("booking_view", booking_id=booking_id))
    db.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    db.commit()
    flash(_("Booking deleted."), "ok")
    return redirect(url_for("bookings"))


core.init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), debug=os.environ.get("FLASK_DEBUG") == "1")
