import hashlib
import json
import math
from datetime import date, datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from db import available_qty, commit_changes, damaged_open, get_db, parse_date
from i18n import _

changes_bp = Blueprint("changes", __name__)
MONEY_FIELDS = ("discount", "travel_charge", "labour_charge", "damage_charge")
BOOKING_FIELDS = ("id", "customer_id", "event_type", "start_date", "end_date", "status",
                  *MONEY_FIELDS, "notes")
LINE_FIELDS = ("id", "item_id", "name", "qty", "rate", "deposit", "dispatched_qty", "returned_qty", "lost_qty")
SETTLEMENT_KINDS = ("Damaged return", "Lost", "Already returned damaged")


def version(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def snapshot(db, booking_id):
    booking = db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    if not booking:
        abort(404)
    lines = db.execute(
        """SELECT bi.*,i.name FROM booking_items bi JOIN items i ON i.id=bi.item_id
           WHERE bi.booking_id=? ORDER BY bi.id""", (booking_id,)).fetchall()
    damages = db.execute(
        """SELECT d.id,d.item_id,d.qty,d.charge,d.kind,d.status,i.name FROM damages d
           JOIN items i ON i.id=d.item_id WHERE d.booking_id=? ORDER BY d.id""",
        (booking_id,)).fetchall()
    return {"booking": {key: booking[key] for key in BOOKING_FIELDS},
            "lines": [{key: line[key] for key in LINE_FIELDS} for line in lines],
            "damages": [dict(row) for row in damages]}


def money(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        abort(400, description=_("Enter a valid amount."))
    if not math.isfinite(value) or not 0 <= value <= 1_000_000_000:
        abort(400, description=_("Enter a valid amount."))
    if abs(value * 100 - round(value * 100)) > 0.0001:
        abort(400, description=_("Use no more than two decimal places."))
    return value


def quantity(raw):
    if not raw or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        abort(400, description=_("Enter valid item quantities."))
    return int(raw)


def open_booking(state):
    if state["booking"]["status"] in ("Cancelled", "Returned"):
        abort(400, description=_("Closed bookings cannot be amended."))


def validate_amendment(db, state, proposal):
    open_booking(state)
    start, end = parse_date(proposal["start_date"]), parse_date(proposal["end_date"])
    if not start or not end or end < start:
        abort(400, description=_("End date must be on or after start date."))
    if start.isoformat() != proposal["start_date"] or end.isoformat() != proposal["end_date"]:
        abort(400, description=_("Select a valid date."))
    for field in MONEY_FIELDS:
        money(proposal[field])
    old = {line["item_id"]: line for line in state["lines"]}
    new = {line["item_id"]: line for line in proposal["lines"]}
    if not new:
        abort(400, description=_("Select at least one item."))
    for item_id, previous in old.items():
        if new.get(item_id, {}).get("qty", 0) < previous["dispatched_qty"]:
            abort(400, description=_("Quantity cannot be less than the quantity already dispatched."))
    for line in proposal["lines"]:
        money(line["rate"])
        money(line["deposit"])
        item = db.execute("SELECT * FROM items WHERE id=?", (line["item_id"],)).fetchone()
        if not item:
            abort(400, description=_("Select a valid item."))
        previous = old.get(line["item_id"], {})
        reserved = line["qty"] - previous.get("returned_qty", 0) - previous.get("lost_qty", 0)
        first, last = proposal["start_date"], proposal["end_date"]
        if state["booking"]["status"] in ("Delivered", "WorkDone"):
            today = date.today().isoformat()
            first, last = min(first, today), max(last, today)
        if reserved > available_qty(db, item, first, last, state["booking"]["id"]):
            abort(400, description=_("Not enough stock for the proposed booking dates and quantities."))


def save_request(db, booking_id, kind, before, proposal, reason):
    if not reason or len(reason) > 1000:
        abort(400, description=_("Enter a reason of 1 to 1000 characters."))
    request_id = db.execute(
        """INSERT INTO booking_change_requests
           (booking_id,kind,before_snapshot,proposal,reason,requested_by,requester_name,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (booking_id, kind, json.dumps(before, sort_keys=True), json.dumps(proposal, sort_keys=True),
         reason, g.user["id"], g.user["name"], datetime.now().isoformat(timespec="seconds"))).lastrowid
    g.activity_resource = {"booking_id": booking_id, "change_request_id": request_id, "kind": kind}
    commit_changes(db)
    flash(_("Request submitted. Stock and charges change only after approval."), "ok")
    return redirect(url_for("changes.history", booking_id=booking_id))


@changes_bp.route("/bookings/<int:booking_id>/amend", methods=["GET", "POST"])
def amend(booking_id):
    db = get_db()
    if request.method == "POST":
        db.execute("BEGIN IMMEDIATE")
    state = snapshot(db, booking_id)
    open_booking(state)
    items = db.execute("SELECT * FROM items ORDER BY category,name,id").fetchall()
    old = {line["item_id"]: line for line in state["lines"]}
    can_price = g.user["role"] in ("Owner", "Accounts")
    if request.method == "POST":
        if request.form.get("base") != version(state):
            abort(409, description=_("Booking changed. Reload and submit a new request."))
        proposal = {"start_date": request.form.get("start_date", ""),
                    "end_date": request.form.get("end_date", ""), "lines": []}
        for field in MONEY_FIELDS:
            if not can_price and field in request.form:
                abort(403)
            proposal[field] = money(request.form.get(field, state["booking"][field])) if can_price else state["booking"][field]
        for item in items:
            item_id = item["id"]
            qty = quantity(request.form.get(f"qty_{item_id}", "0") or "0")
            if not can_price and (f"rate_{item_id}" in request.form or f"deposit_{item_id}" in request.form):
                abort(403)
            if qty:
                previous = old.get(item_id, {})
                rate = previous.get("rate", item["rate_per_day"])
                deposit = previous.get("deposit", item["deposit"])
                if can_price:
                    rate = money(request.form.get(f"rate_{item_id}", rate))
                    deposit = money(request.form.get(f"deposit_{item_id}", deposit))
                proposal["lines"].append({"item_id": item_id, "name": item["name"], "qty": qty,
                                          "rate": rate, "deposit": deposit})
        validate_amendment(db, state, proposal)
        return save_request(db, booking_id, "Amendment", state, proposal, request.form.get("reason", "").strip())
    return render_template("booking_amend.html", state=state, items=items, old=old,
                           base=version(state), can_price=can_price)


def validate_settlement(db, state, proposal):
    if state["booking"]["status"] == "Cancelled":
        abort(400, description=_("This booking is closed; deliveries and returns cannot be changed."))
    line = next((line for line in state["lines"] if line["id"] == proposal["line_id"]), None)
    if not line or proposal["kind"] not in SETTLEMENT_KINDS or proposal["qty"] <= 0:
        abort(400, description=_("Select a valid item and settlement quantity."))
    if proposal["kind"] == "Already returned damaged":
        recorded = db.execute(
            "SELECT COALESCE(SUM(qty),0) FROM damages WHERE booking_item_id=? AND kind='Damaged'",
            (line["id"],)).fetchone()[0]
        limit = line["returned_qty"] - recorded
    else:
        limit = line["dispatched_qty"] - line["returned_qty"] - line["lost_qty"]
    if proposal["qty"] > limit:
        abort(400, description=_("Settlement exceeds the eligible dispatched or returned quantity."))
    item = db.execute("SELECT * FROM items WHERE id=?", (line["item_id"],)).fetchone()
    if proposal["qty"] > item["total_qty"] - damaged_open(db, item["id"]):
        abort(400, description=_("Quantity is more than the stock you have."))
    return line


@changes_bp.route("/bookings/<int:booking_id>/settlement", methods=["GET", "POST"])
def settlement(booking_id):
    db = get_db()
    if request.method == "POST":
        db.execute("BEGIN IMMEDIATE")
    state = snapshot(db, booking_id)
    if state["booking"]["status"] == "Cancelled":
        abort(400, description=_("This booking is closed; deliveries and returns cannot be changed."))
    if request.method == "POST":
        if request.form.get("base") != version(state):
            abort(409, description=_("Booking changed. Reload and submit a new request."))
        proposal = {"line_id": quantity(request.form.get("line_id", "")),
                    "kind": request.form.get("kind"), "qty": quantity(request.form.get("qty", "")),
                    "charge": money(request.form.get("charge", "0"))}
        validate_settlement(db, state, proposal)
        return save_request(db, booking_id, proposal["kind"], state, proposal, request.form.get("reason", "").strip())
    return render_template("booking_settlement.html", state=state, kinds=SETTLEMENT_KINDS,
                           base=version(state))


def finish_if_settled(db, booking_id):
    pending = db.execute(
        "SELECT COALESCE(SUM(qty-returned_qty-lost_qty),0) FROM booking_items WHERE booking_id=?",
        (booking_id,)).fetchone()[0]
    if pending == 0:
        db.execute("UPDATE bookings SET status='Returned' WHERE id=? AND status!='Cancelled'", (booking_id,))


def apply_amendment(db, state, proposal):
    validate_amendment(db, state, proposal)
    booking_id = state["booking"]["id"]
    db.execute(
        """UPDATE bookings SET start_date=?,end_date=?,discount=?,travel_charge=?,labour_charge=?,
           damage_charge=? WHERE id=?""",
        (proposal["start_date"], proposal["end_date"], *(proposal[key] for key in MONEY_FIELDS), booking_id))
    old = {line["item_id"]: line for line in state["lines"]}
    new_ids = {line["item_id"] for line in proposal["lines"]}
    for line in state["lines"]:
        if line["item_id"] not in new_ids:
            db.execute("DELETE FROM booking_items WHERE id=?", (line["id"],))
    for line in proposal["lines"]:
        values = (line["qty"], line["rate"], line["deposit"])
        if line["item_id"] in old:
            db.execute("UPDATE booking_items SET qty=?,rate=?,deposit=? WHERE id=?",
                       (*values, old[line["item_id"]]["id"]))
        else:
            db.execute("INSERT INTO booking_items (qty,rate,deposit,booking_id,item_id) VALUES (?,?,?,?,?)",
                       (*values, booking_id, line["item_id"]))
    finish_if_settled(db, booking_id)


@changes_bp.route("/booking-changes/<int:change_id>/review", methods=["POST"])
def review(change_id):
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    change = db.execute("SELECT * FROM booking_change_requests WHERE id=?", (change_id,)).fetchone()
    if not change:
        abort(404)
    if change["status"] != "Pending":
        abort(400, description=_("This request has already been reviewed."))
    decision = request.form.get("decision", "")
    if decision not in ("approve", "reject"):
        abort(400, description=_("Choose approve or reject."))
    before, proposal = json.loads(change["before_snapshot"]), json.loads(change["proposal"])
    after = None
    if decision == "approve":
        if before != snapshot(db, change["booking_id"]):
            abort(409, description=_("Booking changed. Reject this stale request and submit a new one."))
        if change["kind"] == "Amendment":
            apply_amendment(db, before, proposal)
        else:
            line = validate_settlement(db, before, proposal)
            charge = money(request.form.get("charge", proposal["charge"]))
            db.execute(
                """INSERT INTO damages (item_id,booking_id,qty,kind,charge,note,reported_on,booking_item_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (line["item_id"], change["booking_id"], proposal["qty"],
                 "Lost" if change["kind"] == "Lost" else "Damaged", charge, change["reason"],
                 datetime.now().date().isoformat(), line["id"]))
            if change["kind"] != "Already returned damaged":
                column = "lost_qty" if change["kind"] == "Lost" else "returned_qty"
                db.execute(f"UPDATE booking_items SET {column}={column}+? WHERE id=?",
                           (proposal["qty"], line["id"]))
            finish_if_settled(db, change["booking_id"])
        after = json.dumps(snapshot(db, change["booking_id"]), sort_keys=True)
    db.execute(
        """UPDATE booking_change_requests SET status=?,reviewed_by=?,reviewer_name=?,reviewed_at=?,
           after_snapshot=? WHERE id=?""",
        ("Approved" if decision == "approve" else "Rejected", g.user["id"], g.user["name"],
         datetime.now().isoformat(timespec="seconds"), after, change_id))
    g.activity_resource = {"booking_id": change["booking_id"], "change_request_id": change_id,
                           "decision": decision}
    commit_changes(db)
    flash(_("Change request reviewed."), "ok")
    return redirect(url_for("changes.history", booking_id=change["booking_id"]))


@changes_bp.route("/bookings/<int:booking_id>/changes")
def history(booking_id):
    db = get_db()
    state = snapshot(db, booking_id)
    rows = db.execute(
        "SELECT * FROM booking_change_requests WHERE booking_id=? ORDER BY id DESC", (booking_id,)).fetchall()
    changes = [dict(row, before=json.loads(row["before_snapshot"]), proposed=json.loads(row["proposal"]),
                    after=json.loads(row["after_snapshot"]) if row["after_snapshot"] else None) for row in rows]
    return render_template("booking_changes.html", state=state, changes=changes)


@changes_bp.route("/booking-changes")
def queue():
    show = request.args.get("show", "pending")
    if show not in ("pending", "all"):
        abort(400, description=_("Select status"))
    page = quantity(request.args.get("page", "1"))
    if page < 1:
        abort(400, description=_("Select a valid page."))
    rows = get_db().execute(
        """SELECT r.*,c.name AS customer FROM booking_change_requests r
           JOIN bookings b ON b.id=r.booking_id JOIN customers c ON c.id=b.customer_id
           WHERE (?='all' OR r.status='Pending') ORDER BY r.id DESC LIMIT 51 OFFSET ?""",
        (show, (page - 1) * 50)).fetchall()
    return render_template("change_queue.html", changes=rows[:50], page=page, has_next=len(rows) > 50, show=show)
