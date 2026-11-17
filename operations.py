import math
from datetime import date, datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from accounts import allowed
from db import PAYMENT_KINDS, available_qty, booking_totals, commit_changes, get_db, parse_date
from filters import date_window
from i18n import _

operations_bp = Blueprint("operations", __name__)


@operations_bp.route("/bookings/<int:booking_id>/movement", methods=["POST"])
def movement(booking_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking:
        abort(404)
    if booking["status"] in ("Cancelled", "Returned"):
        db.rollback()
        flash(_("This booking is closed; deliveries and returns cannot be changed."), "error")
        return redirect(url_for("booking_view", booking_id=booking_id))
    kind = request.form.get("kind", "")
    if kind not in ("dispatch", "return"):
        abort(400, description=_("Select delivery or return."))
    lines = db.execute("SELECT * FROM booking_items WHERE booking_id=?", (booking_id,)).fetchall()
    chosen = []
    for line in lines:
        raw = request.form.get(f"move_{line['id']}", "").strip()
        if not raw:
            continue
        if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            abort(400, description=_("Enter valid item quantities."))
        qty = int(raw)
        limit = (line["qty"] - line["dispatched_qty"] if kind == "dispatch"
                 else line["dispatched_qty"] - line["returned_qty"])
        if qty > limit:
            abort(400, description=_("Quantity exceeds the items pending delivery or return."))
        if qty:
            if kind == "dispatch":
                item = db.execute("SELECT * FROM items WHERE id=?", (line["item_id"],)).fetchone()
                if qty > available_qty(db, item, booking["start_date"], booking["end_date"], booking_id):
                    abort(400, description=_("Not enough stock available for delivery."))
                out = db.execute(
                    "SELECT COALESCE(SUM(dispatched_qty-returned_qty),0) FROM booking_items WHERE item_id=?",
                    (line["item_id"],)).fetchone()[0]
                damaged = db.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM damages WHERE item_id=? AND status='Open'",
                    (line["item_id"],)).fetchone()[0]
                selected = sum(n for selected_line, n in chosen if selected_line["item_id"] == line["item_id"])
                if qty + selected + out + damaged > item["total_qty"]:
                    abort(400, description=_("Not enough stock available for delivery."))
            chosen.append((line, qty))
    if not chosen:
        db.rollback()
        flash(_("Enter at least one quantity."), "error")
        return redirect(url_for("booking_view", booking_id=booking_id))
    column = "dispatched_qty" if kind == "dispatch" else "returned_qty"
    for line, qty in chosen:
        db.execute(f"UPDATE booking_items SET {column}={column}+? WHERE id=?", (qty, line["id"]))
    remaining = db.execute(
        "SELECT SUM(qty-returned_qty) FROM booking_items WHERE booking_id=?", (booking_id,)
    ).fetchone()[0]
    status = "Returned" if remaining == 0 else ("Delivered" if kind == "dispatch" else booking["status"])
    db.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
    g.activity_resource = {"booking_id": booking_id, "kind": kind,
                           "items": {line["item_id"]: qty for line, qty in chosen}}
    commit_changes(db)
    flash(_("Delivery quantities saved.") if kind == "dispatch" else _("Return quantities saved."), "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@operations_bp.route("/activity")
def activity():
    window = date_window(request.args)
    actor = request.args.get("actor", "").strip()
    clauses, params = ["instr(lower(actor_name), lower(?))>0"], [actor]
    if window["from_date"]:
        clauses.append("substr(created_at,1,10) BETWEEN ? AND ?")
        params.extend((window["from_date"], window["to_date"]))
    raw_page = request.args.get("page", "1")
    if not raw_page.isascii() or not raw_page.isdigit() or len(raw_page) > 9 or int(raw_page) < 1:
        abort(400, description=_("Select a valid page."))
    db = get_db()
    where = " AND ".join(clauses)
    count = db.execute(f"SELECT COUNT(*) FROM activity_log WHERE {where}", params).fetchone()[0]
    pages = max(1, (count + 49) // 50)
    page = min(int(raw_page), pages)
    rows = db.execute(f"SELECT * FROM activity_log WHERE {where} ORDER BY id DESC LIMIT 50 OFFSET ?",
                      (*params, (page - 1) * 50)).fetchall()
    return render_template("activity.html", events=rows, window=window, actor=actor,
                           page=page, pages=pages)


def payment_details():
    try:
        amount = float(request.form.get("amount", ""))
    except ValueError:
        abort(400, description=_("Enter a valid amount."))
    if not math.isfinite(amount) or not 0 < amount <= 1_000_000_000:
        abort(400, description=_("Enter a valid amount."))
    method = request.form.get("method", "Cash")
    reference = request.form.get("reference", "").strip()
    if method not in ("Cash", "UPI", "Bank") or len(reference) > 120 or (method != "Cash" and not reference):
        abort(400, description=_("Choose a payment method and enter a reference for UPI or Bank."))
    if abs(amount * 100 - round(amount * 100)) > 0.0001:
        abort(400, description=_("Use no more than two decimal places."))
    return amount, method, reference


def record_payment(booking_id):
    amount, method, reference = payment_details()
    kind = request.form.get("kind", "Payment")
    if kind not in PAYMENT_KINDS:
        abort(400, description=_("Select a valid payment type."))
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT id FROM bookings WHERE id=?", (booking_id,)).fetchone():
        abort(404)
    if kind == "Deposit refund":
        if amount > round(max(-booking_totals(db, booking_id)["balance"], 0), 2):
            abort(400, description=_("Refund exceeds the amount currently owed to the customer."))
        refund_id = db.execute(
            """INSERT INTO refund_requests (booking_id,amount,method,reference,requested_by,created_at)
               VALUES (?,?,?,?,?,?)""",
            (booking_id, amount, method, reference, g.user["id"], datetime.now().isoformat(timespec="seconds")),
        ).lastrowid
        g.activity_resource = {"booking_id": booking_id, "refund_id": refund_id}
        commit_changes(db)
        flash(_("Refund requested. An Owner must approve it before it affects the balance."), "ok")
    else:
        payment_id = db.execute(
            "INSERT INTO payments (booking_id,amount,kind,paid_on,method,reference) VALUES (?,?,?,?,?,?)",
            (booking_id, amount, kind, date.today().isoformat(), method, reference),
        ).lastrowid
        g.activity_resource = {"booking_id": booking_id, "payment_id": payment_id}
        commit_changes(db)
        flash(_("Payment recorded."), "ok")
    return redirect(url_for("booking_view", booking_id=booking_id))


@operations_bp.route("/refunds/<int:refund_id>/review", methods=["POST"])
def review_refund(refund_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    refund = db.execute("SELECT * FROM refund_requests WHERE id=?", (refund_id,)).fetchone()
    if not refund:
        abort(404)
    if refund["status"] != "Pending":
        abort(400, description=_("This refund request has already been reviewed."))
    decision = request.form.get("decision", "")
    if decision not in ("approve", "reject"):
        abort(400, description=_("Choose approve or reject."))
    if decision == "approve":
        if refund["amount"] > round(max(-booking_totals(db, refund["booking_id"])["balance"], 0), 2):
            abort(400, description=_("Refund exceeds the amount currently owed to the customer."))
        db.execute(
            "INSERT INTO payments (booking_id,amount,kind,paid_on,method,reference) VALUES (?,?,'Deposit refund',?,?,?)",
            (refund["booking_id"], refund["amount"], date.today().isoformat(), refund["method"], refund["reference"]),
        )
    db.execute("UPDATE refund_requests SET status=?,reviewed_by=? WHERE id=?",
               ("Approved" if decision == "approve" else "Rejected", g.user["id"], refund_id))
    commit_changes(db)
    flash(_("Refund request reviewed."), "ok")
    return redirect(url_for("booking_view", booking_id=refund["booking_id"]))


@operations_bp.route("/payments/<int:payment_id>/receipt")
def receipt(payment_id):
    payment = get_db().execute(
        """SELECT p.*,c.name,c.phone,c.address FROM payments p
           JOIN bookings b ON b.id=p.booking_id JOIN customers c ON c.id=b.customer_id
           WHERE p.id=?""", (payment_id,)).fetchone()
    if not payment:
        abort(404)
    return render_template("receipt.html", payment=payment)


@operations_bp.route("/customers/<int:customer_id>/ledger")
def ledger(customer_id):
    db = get_db()
    customer = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not customer:
        abort(404)
    bookings = db.execute("SELECT * FROM bookings WHERE customer_id=? ORDER BY start_date DESC,id DESC",
                          (customer_id,)).fetchall()
    entries = [(booking, booking_totals(db, booking["id"])) for booking in bookings]
    return render_template("ledger.html", customer=customer, entries=entries,
                           due=sum(max(t["balance"], 0) for _, t in entries),
                           credit=sum(max(-t["balance"], 0) for _, t in entries))


@operations_bp.route("/operations")
def daily():
    day = request.args.get("day", date.today().isoformat())
    if not parse_date(day):
        abort(400, description=_("Select a valid date."))
    db = get_db()
    bookings = db.execute(
        """SELECT b.*,c.name AS customer,c.phone,
                  SUM(bi.qty-bi.dispatched_qty) AS to_send,
                  SUM(bi.dispatched_qty-bi.returned_qty) AS outstanding
           FROM bookings b JOIN customers c ON c.id=b.customer_id
           JOIN booking_items bi ON bi.booking_id=b.id WHERE b.start_date<=?
           GROUP BY b.id ORDER BY b.start_date,b.id""", (day,)).fetchall()
    deliveries = [b for b in bookings if b["status"] not in ("Returned", "Cancelled") and b["to_send"] > 0]
    collections = [b for b in bookings if b["end_date"] <= day and b["outstanding"] > 0]
    tasks = db.execute(
        """SELECT a.*,u.name,b.end_date FROM work_assignments a JOIN users u ON u.id=a.user_id
           JOIN bookings b ON b.id=a.booking_id WHERE b.start_date<=? AND a.status!='Done'
           AND b.status NOT IN ('Returned','Cancelled') ORDER BY b.end_date,a.id""", (day,)).fetchall()
    payments = []
    if allowed("finance"):
        for booking in bookings:
            balance = booking_totals(db, booking["id"])["balance"]
            if balance > 0.005:
                payments.append((booking, balance))
    return render_template("daily_operations.html", day=day, deliveries=deliveries,
                           collections=collections, tasks=tasks, payments=payments)
