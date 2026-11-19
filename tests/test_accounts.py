import os
import re
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        os.environ["RENTAL_DB"] = os.path.join(cls.folder.name, "accounts.db")
        os.environ["BACKUP_DIR"] = os.path.join(cls.folder.name, "backups")
        import app
        import db

        cls.app, cls.db = app.app, db
        cls.app.config.update(TESTING=True)
        cls.db_patch = patch.object(db, "DB_PATH", os.environ["RENTAL_DB"])
        cls.db_patch.start()
        cls.backup_patch = patch.object(app, "BACKUP_DIR", os.environ["BACKUP_DIR"])
        cls.backup_patch.start()
        cls.hashed = generate_password_hash("owner-password-123")
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.backup_patch.stop()
        cls.folder.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        with self.app.app_context():
            db = self.db.get_db()
            for table in ("activity_log", "booking_change_requests", "refund_requests", "damages",
                          "work_assignments", "bookings", "customers", "staff", "users", "login_attempts"):
                db.execute(f"DELETE FROM {table}")
            db.execute("UPDATE items SET total_qty=20 WHERE id=1")
            self.ids = {}
            for role in ("Owner", "Partner", "Agent", "Accounts", "Staff", "Worker"):
                self.ids[role] = db.execute(
                    "INSERT INTO users (username,name,password_hash,role) VALUES (?,?,?,?)",
                    (role.lower(), role + " name", self.hashed, role),
                ).lastrowid
            customer = db.execute(
                "INSERT INTO customers (name,phone,address) VALUES ('Assigned customer','0123456789','Event address')"
            ).lastrowid
            self.booking_id = db.execute(
                """INSERT INTO bookings(customer_id,event_type,start_date,end_date,created_at)
                   VALUES (?,'Birthday','2027-01-01','2027-01-01','2026-11-16')""",
                (customer,),
            ).lastrowid
            db.execute(
                "INSERT INTO booking_items (booking_id,item_id,qty,rate,deposit) VALUES (?,1,1,100,0)",
                (self.booking_id,),
            )
            db.commit()

    def as_role(self, role, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            session.clear()
            session.update(user_id=self.ids[role], session_version=1, _csrf="account-test-token")
        return client

    def post(self, url, data=None, client=None, **kwargs):
        client = client or self.client
        with client.session_transaction() as session:
            token = session.get("_csrf", "account-test-token")
            session["_csrf"] = token
        return client.post(url, data={**(data or {}), "csrf_token": token}, **kwargs)

    def test_anonymous_login_csrf_logout_and_legacy_session(self):
        for path in ("/", "/accounts", "/bookings", "/backup", "/work"):
            self.assertEqual(self.client.get(path).status_code, 302)
        with self.client.session_transaction() as session:
            session["auth"] = True
        self.assertIn("/login", self.client.get("/backup").headers["Location"])
        self.assertEqual(self.client.post("/login", data={"user": "owner", "password": "owner-password-123"}).status_code, 400)
        self.assertEqual(self.client.post("/login", data={"csrf_token": "అసాధువు"}).status_code, 400)
        response = self.client.get("/login")
        self.assertIn(b'name="csrf_token"', response.data)
        response = self.post("/login?next=//external.example", {"user": "owner", "password": "owner-password-123"})
        self.assertEqual(response.headers["Location"], "/")
        with self.client.session_transaction() as session:
            self.assertNotIn("auth", session)
            self.assertEqual(session["user_id"], self.ids["Owner"])
        self.assertEqual(self.client.get("/logout").status_code, 405)
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.assertEqual(self.post("/logout").status_code, 302)
        self.assertIn("/login", self.client.get("/accounts").headers["Location"])

    def test_login_help_and_password_control_keep_account_management_private(self):
        for language, message in (
            ("en", "Currently disabled. Please contact your administrator."),
            ("te", "ప్రస్తుతం అందుబాటులో లేదు. దయచేసి మీ అడ్మినిస్ట్రేటర్‌ను సంప్రదించండి."),
            ("hi", "यह सुविधा अभी बंद है। कृपया अपने व्यवस्थापक से संपर्क करें।"),
        ):
            with self.subTest(language=language):
                self.client.set_cookie("lang", language)
                response = self.client.get("/login")
                body = response.get_data(as_text=True)
                self.assertEqual(body.count(f"<p>{message}</p>"), 2)
                self.assertIn('data-password-toggle', body)
                self.assertIn('type="password" name="password"', body)
                for internal_detail in ("reset-password", "Team accounts", "Owner", "<code>"):
                    self.assertNotIn(internal_detail, body)
        self.assertIn("/login", self.client.get("/accounts/new").headers["Location"])
        self.assertIn("/login", self.post("/accounts/new", {}).headers["Location"])

    def test_dashboard_uses_original_role_gated_booking_button(self):
        self.as_role("Owner")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="fab noprint" href="/bookings/new"', body)
        self.assertNotIn(" dashboard-booking\"", body)
        self.as_role("Partner")
        self.assertNotIn('class="fab noprint"', self.client.get("/").get_data(as_text=True))

    def test_role_permissions_on_direct_gets_and_posts(self):
        matrix = {
            "Partner": (["/", "/bookings", "/finance", "/items", "/attendance"], ["/accounts", "/backup", "/bookings/new", "/expenses/new"]),
            "Agent": (["/bookings", "/bookings/new", "/customers", "/stock"], ["/accounts", "/backup", "/finance", "/expenses", "/staff"]),
            "Accounts": (["/finance", "/expenses/new", "/bookings", "/staff"], ["/accounts", "/backup", "/bookings/new", "/attendance"]),
            "Staff": (["/work", "/more", "/account/password"], ["/bookings", "/customers", "/staff", "/finance", "/accounts", "/backup"]),
            "Worker": (["/work", "/more", "/account/password"], ["/bookings", "/customers", "/stock", "/finance", "/accounts", "/backup"]),
        }
        for role, (allowed, forbidden) in matrix.items():
            self.as_role(role)
            for path in allowed:
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
            for path in forbidden:
                with self.subTest(role=role, path=path):
                    self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.post("/accounts/new", {}).status_code, 403)
            self.assertEqual(self.post(f"/bookings/{self.booking_id}/delete").status_code, 403)
        self.as_role("Partner")
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/status", {"status": "Cancelled"}).status_code, 403)
        self.as_role("Agent")
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/payment", {"amount": "30"}).status_code, 403)
        response = self.post("/bookings/new", {
            "customer_name": "Agent customer", "phone": "0123456789", "start_date": "2027-02-01",
            "end_date": "2027-02-01", "qty_1": "1", "advance": "100",
        })
        self.assertIn(b"Only Owner or Accounts can record payments.", response.data)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/status", {"status": "Delivered"}).status_code, 302)
        self.as_role("Accounts")
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/status", {"status": "Cancelled"}).status_code, 403)
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/payment", {"amount": "30", "kind": "Payment"}).status_code, 302)

    def test_owner_account_creation_last_owner_and_deactivation(self):
        self.as_role("Owner")
        data = {"username": "new.worker", "name": "New worker", "role": "Worker", "password": "new-password-123"}
        response = self.post("/accounts/new", data)
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = self.db.get_db().execute("SELECT * FROM users WHERE username='new.worker'").fetchone()
            self.assertTrue(check_password_hash(user["password_hash"], data["password"]))
            self.assertNotEqual(user["password_hash"], data["password"])
        response = self.post("/accounts/new", data)
        self.assertIn(b"Username already exists.", response.data)
        response = self.post("/accounts/new", {**data, "username": "bad", "role": "Superuser"})
        self.assertIn(b"Select a valid role.", response.data)
        response = self.post("/accounts/new", {**data, "username": "bad", "active": "invalid"})
        self.assertIn(b"Select a valid account status.", response.data)
        response = self.post(f"/accounts/{self.ids['Owner']}/edit",
                             {**data, "username": "owner", "role": "Owner", "active": "0", "password": ""})
        self.assertIn(b"Keep at least one active owner.", response.data)
        other = self.as_role("Worker", self.app.test_client())
        response = self.post(f"/accounts/{self.ids['Worker']}/edit",
                             {**data, "username": "worker", "role": "Worker", "active": "0", "password": ""})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", other.get("/work").headers["Location"])

    def test_password_change_invalidates_other_sessions(self):
        self.as_role("Owner")
        other = self.as_role("Owner", self.app.test_client())
        response = self.post("/account/password",
                             {"current_password": "wrong", "new_password": "changed-password-123"})
        self.assertIn(b"Current password is incorrect.", response.data)
        response = self.post("/account/password",
                             {"current_password": "owner-password-123", "new_password": "changed-password-123"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/accounts").status_code, 200)
        self.assertIn("/login", other.get("/accounts").headers["Location"])
        result = self.app.test_cli_runner().invoke(args=[
            "reset-password", "--username", "owner", "--password", "console-recovery-password",
        ])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("/login", self.client.get("/accounts").headers["Location"])
        response = self.post("/login", {"user": "OWNER", "password": "console-recovery-password"})
        self.assertEqual(response.headers["Location"], "/")

    def test_owner_role_changes_revoke_old_access_and_bootstrap_does_not_overwrite(self):
        from accounts import bootstrap_owner

        self.as_role("Owner")
        partner = self.as_role("Partner", self.app.test_client())
        response = self.post(f"/accounts/{self.ids['Partner']}/edit",
                             {"username": "partner", "name": "Partner name", "role": "Worker", "password": ""})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", partner.get("/finance").headers["Location"])
        bootstrap_owner(self.app, "another.owner", "not-a-real-password")
        with self.app.app_context():
            self.assertIsNone(self.db.get_db().execute(
                "SELECT id FROM users WHERE username='another.owner'").fetchone())
        response = self.post("/login", {"user": "partner", "password": "owner-password-123"}, client=partner)
        self.assertEqual(response.headers["Location"], "/work")
        self.assertEqual(partner.get("/finance").status_code, 403)

    def test_worker_only_sees_and_updates_own_assignments(self):
        self.as_role("Owner")
        self.assertEqual(self.post(f"/bookings/{self.booking_id}/assign",
            {"user_id": self.ids["Worker"], "task": "Deliver chairs"}).status_code, 302)
        with self.app.app_context():
            assignment = self.db.get_db().execute("SELECT id FROM work_assignments").fetchone()[0]
        self.as_role("Worker")
        page = self.client.get("/work")
        self.assertIn(b"Deliver chairs", page.data)
        self.assertIn(b"Assigned customer", page.data)
        self.assertNotIn(b"Finance report", page.data)
        self.assertNotIn(b"Monthly salary", page.data)
        self.assertEqual(self.post(f"/work/{assignment}/status", {"status": "Done"}).status_code, 302)
        self.assertEqual(self.post(f"/work/{assignment}/status", {"status": "Cancelled"}).status_code, 400)
        self.as_role("Staff")
        self.assertNotIn(b"Deliver chairs", self.client.get("/work").data)
        self.assertEqual(self.post(f"/work/{assignment}/status", {"status": "Assigned"}).status_code, 404)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(db.execute("SELECT status FROM work_assignments").fetchone()[0], "Done")
            self.assertEqual(db.execute("SELECT status FROM bookings").fetchone()[0], "Booked")
        self.as_role("Owner")
        self.post(f"/bookings/{self.booking_id}/assign",
                  {"user_id": self.ids["Worker"], "task": "Collect chairs"})
        with self.app.app_context():
            row = self.db.get_db().execute("SELECT status,task FROM work_assignments").fetchone()
            self.assertEqual((row["status"], row["task"]), ("Assigned", "Collect chairs"))

    def test_read_only_pages_hide_mutations_and_post_forms_have_tokens(self):
        with self.app.app_context():
            db = self.db.get_db()
            person = db.execute(
                "INSERT INTO staff (name,role,monthly_salary) VALUES ('Payroll worker','Helper',100)"
            ).lastrowid
            db.execute(
                "INSERT INTO salary_payments (staff_id,month,amount,kind,paid_on) VALUES (?,'2027-01',10,'Salary','2027-01-01')",
                (person,),
            )
            db.commit()
        self.as_role("Partner")
        page = self.client.get(f"/bookings/{self.booking_id}").get_data(as_text=True)
        self.assertNotIn('action="/bookings/', page)
        self.assertNotIn("Delete booking", page)
        self.as_role("Owner")
        for path in ("/bookings/new", f"/bookings/{self.booking_id}", "/accounts/new",
                     "/accounts", "/customers/new", "/items/new", "/expenses/new",
                     "/staff/new", "/damages/new", "/more", "/account/password",
                     "/attendance", f"/staff/{person}?month=2027-01"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            for form in re.findall(r"<form\b.*?</form>", page.get_data(as_text=True), re.S):
                if 'method="post"' in form:
                    self.assertIn('name="csrf_token"', form, path)
        self.assertEqual(self.client.post("/accounts/new", data={
            "username": "no.token", "name": "No token", "role": "Owner", "password": "no-token-password",
        }).status_code, 400)
        with patch.object(tempfile, "tempdir", self.folder.name):
            with self.client.get("/backup") as backup:
                self.assertEqual(backup.status_code, 200)
                self.assertTrue(backup.data.startswith(b"SQLite format 3"))

    def test_login_rate_limit_and_setup_without_public_registration(self):
        for _ in range(10):
            self.assertEqual(self.post("/login", {"user": "owner", "password": "wrong"}).status_code, 200)
        self.assertEqual(self.post("/login", {"user": "owner", "password": "owner-password-123"}).status_code, 429)
        with self.app.app_context():
            self.db.get_db().execute("DELETE FROM users")
            self.db.get_db().commit()
        self.assertEqual(self.client.get("/login").status_code, 503)
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=["create-owner", "--username", "fresh.owner", "--name", "Owner",
                                     "--password", "fresh-owner-password"])
        self.assertEqual(result.exit_code, 0, result.output)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT role FROM users").fetchone()[0], "Owner")

    def test_booking_period_filters_preserve_tabs_and_overlap(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE bookings SET start_date='2026-12-30',end_date='2027-01-03' WHERE id=?",
                       (self.booking_id,))
            db.commit()
        for filters, expected in (
            ({"period": "month", "month": "2027-01"}, True),
            ({"period": "month", "month": "2027-02"}, False),
            ({"period": "range", "from_date": "2027-01-03", "to_date": "2027-01-03"}, True),
            ({"period": "all", "q": "no match"}, False),
        ):
            response = self.client.get("/bookings", query_string=filters)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(f'href="/bookings/{self.booking_id}"' in response.get_data(as_text=True), expected)
        response = self.client.get("/bookings?period=month&month=2027-01&q=Assigned")
        self.assertIn(b"month=2027-01", response.data)
        self.assertIn(b"q=assigned", response.data)
        self.assertEqual(self.client.get("/bookings?period=month&month=2027-13").status_code, 400)
        self.assertEqual(self.client.get("/bookings?period=range&from_date=2027-02-01&to_date=2027-01-01").status_code, 400)

    def test_attendance_filters_history_totals_and_staff_search(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            first = db.execute("INSERT INTO staff (name,role) VALUES ('Alice Helper','Helper')").lastrowid
            second = db.execute("INSERT INTO staff (name,role,active) VALUES ('Bob Driver','Driver',0)").lastrowid
            db.executemany("INSERT INTO attendance (staff_id,day,status) VALUES (?,?,?)", [
                (first, "2027-01-01", "Present"), (first, "2027-01-02", "Half"),
                (second, "2027-01-01", "Absent"), (second, "2027-02-01", "Leave"),
            ])
            db.commit()
        response = self.client.get("/staff?q=alice&active=1")
        self.assertIn(b"Alice Helper", response.data)
        self.assertNotIn(b"Bob Driver", response.data)
        response = self.client.get("/attendance/history", query_string={
            "staff_id": first, "period": "month", "month": "2027-01",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 records", response.data)
        response = self.client.get("/attendance/history?period=all&q=alice&status=Half")
        self.assertIn(b"1 records", response.data)
        self.assertIn(b"02 Jan 2027", response.data)
        response = self.client.get("/attendance/history?period=range&from_date=2027-02-01&to_date=2027-02-01")
        self.assertIn(b"1 records", response.data)
        self.assertIn(b"01 Feb 2027", response.data)
        self.assertIn(b"4 records", self.client.get("/attendance/history?period=all").data)
        response = self.client.get("/attendance", query_string={"day": "2027-01-01", "staff_id": first})
        self.assertIn(b"Alice Helper", response.data)
        self.assertNotIn(b"Mark everyone present", response.data)
        for path in ("/attendance?day=invalid", "/attendance/history?staff_id=999999",
                     "/attendance/history?status=wrong", "/attendance/history?page=-1"):
            self.assertEqual(self.client.get(path).status_code, 400)
        self.as_role("Partner")
        self.assertEqual(self.client.get("/attendance/history").status_code, 200)
        self.as_role("Worker")
        self.assertEqual(self.client.get("/attendance/history").status_code, 403)

    def test_language_page_only_contains_language_choices(self):
        self.as_role("Owner")
        self.assertIn(b'href="/language"', self.client.get("/").data)
        for lang in ("en", "te", "hi"):
            response = self.client.get(f"/lang/{lang}?next=/language", follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'<html lang="{lang}">'.encode(), response.data)
            for label in ("English", "తెలుగు", "हिन्दी"):
                self.assertIn(label.encode(), response.data)
            self.assertNotIn(b"tabbar", response.data)
            self.assertNotIn(b"/accounts", response.data)
            self.assertNotIn(b"/backup", response.data)
        self.as_role("Worker")
        self.assertEqual(self.client.get("/language").status_code, 200)

    def test_partial_deliveries_returns_and_audit_preserve_outstanding_stock(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET qty=10 WHERE booking_id=?", (self.booking_id,))
            line_id = db.execute("SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
            db.commit()
        url = f"/bookings/{self.booking_id}/movement"
        self.assertEqual(self.post(url, {"kind": "dispatch", f"move_{line_id}": "10"}).status_code, 302)
        self.assertEqual(self.post(url, {"kind": "return", f"move_{line_id}": "8"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            line = db.execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual((line["dispatched_qty"], line["returned_qty"]), (10, 8))
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0], "Delivered")
            reserved = self.db.booked_qty(db, 1, "2027-01-01", "2027-01-01")
            self.assertEqual(reserved, 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM activity_log WHERE action='operations.movement' AND resource LIKE ?", (f'%\"booking_id\": {self.booking_id},%',)).fetchone()[0], 2)
        self.assertEqual(self.post(url, {"kind": "return", f"move_{line_id}": "3"}).status_code, 400)
        self.assertIn(b"Use delivery and return tracking", self.post(
            f"/bookings/{self.booking_id}/status", {"status": "Cancelled"}, follow_redirects=True).data)
        self.assertEqual(self.post(url, {"kind": "return", f"move_{line_id}": "2"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0], "Returned")
        self.as_role("Worker")
        self.assertEqual(self.post(url, {"kind": "dispatch", f"move_{line_id}": "1"}).status_code, 403)
        self.assertEqual(self.client.get("/activity").status_code, 403)

    def test_payments_refunds_receipts_and_customer_ledger(self):
        self.as_role("Accounts")
        url = f"/bookings/{self.booking_id}/payment"
        for data in ({"amount": "nan"}, {"amount": "inf"}, {"amount": "-1"},
                     {"amount": "1.001"}, {"amount": "5", "method": "UPI"}):
            self.assertEqual(self.post(url, data).status_code, 400)
        self.assertEqual(self.post(url, {"amount": "150", "kind": "Payment",
                                        "method": "UPI", "reference": "TEST-REF"}).status_code, 302)
        self.assertEqual(self.post(url, {"amount": "51", "kind": "Deposit refund"}).status_code, 400)
        self.assertEqual(self.post(url, {"amount": "50", "kind": "Deposit refund"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            refund_id = db.execute("SELECT id FROM refund_requests").fetchone()[0]
            payment_id = db.execute("SELECT id FROM payments").fetchone()[0]
            customer_id = db.execute("SELECT customer_id FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0]
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["refunded"], 0)
        review = f"/refunds/{refund_id}/review"
        self.assertEqual(self.post(review, {"decision": "approve"}).status_code, 403)
        self.as_role("Owner")
        self.assertEqual(self.post(review, {"decision": "approve"}).status_code, 302)
        self.assertEqual(self.post(review, {"decision": "approve"}).status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.db.booking_totals(self.db.get_db(), self.booking_id)["refunded"], 50)
        self.assertIn(b"TEST-REF", self.client.get(f"/payments/{payment_id}/receipt").data)
        self.assertIn(b"Customer ledger", self.client.get(f"/customers/{customer_id}/ledger").data)
        self.as_role("Agent")
        self.assertEqual(self.client.get(f"/customers/{customer_id}/ledger").status_code, 403)
        self.assertEqual(self.client.get(f"/payments/{payment_id}/receipt").status_code, 403)
        self.assertEqual(self.client.get(f"/bookings/{self.booking_id}/invoice").status_code, 403)
        self.assertNotIn(b"TEST-REF", self.client.get(f"/bookings/{self.booking_id}").data)
        self.as_role("Worker")
        self.assertEqual(self.client.get(f"/payments/{payment_id}/receipt").status_code, 403)

    def test_pending_refunds_recheck_balance_and_daily_role_visibility(self):
        self.as_role("Accounts")
        url = f"/bookings/{self.booking_id}/payment"
        self.post(url, {"amount": "150"})
        for _ in range(2):
            self.assertEqual(self.post(url, {"amount": "50", "kind": "Deposit refund"}).status_code, 302)
        self.as_role("Owner")
        with self.app.app_context():
            ids = [r[0] for r in self.db.get_db().execute("SELECT id FROM refund_requests ORDER BY id")]
        self.assertEqual(self.post(f"/refunds/{ids[0]}/review", {"decision": "approve"}).status_code, 302)
        self.assertEqual(self.post(f"/refunds/{ids[1]}/review", {"decision": "approve"}).status_code, 400)
        response = self.client.get("/operations?day=2027-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assigned customer", response.data)
        self.assertIn(b"Pending payments", response.data)
        self.as_role("Agent")
        response = self.client.get("/operations?day=2027-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Pending payments", response.data)
        self.as_role("Staff")
        self.assertEqual(self.client.get("/operations").status_code, 403)

    def test_movement_guards_preserve_history_and_full_return_deposit(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET qty=2,deposit=10 WHERE booking_id=?", (self.booking_id,))
            line_id = db.execute("SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
            db.commit()
        url = f"/bookings/{self.booking_id}/movement"
        self.assertEqual(self.post(url, {"kind": "dispatch", f"move_{line_id}": "3"}).status_code, 400)
        self.post(url, {"kind": "dispatch", f"move_{line_id}": "2"})
        self.assertIn(b"Items have been dispatched", self.client.get(
            f"/bookings/{self.booking_id}/edit", follow_redirects=True).data)
        self.assertIn(b"Dispatched bookings cannot be deleted", self.post(
            f"/bookings/{self.booking_id}/delete", follow_redirects=True).data)
        self.post(url, {"kind": "return", f"move_{line_id}": "1"})
        with self.app.app_context():
            self.assertEqual(self.db.booking_totals(self.db.get_db(), self.booking_id)["total"], 220)
        self.post(url, {"kind": "return", f"move_{line_id}": "1"})
        with self.app.app_context():
            self.assertEqual(self.db.booking_totals(self.db.get_db(), self.booking_id)["total"], 200)

    def test_activity_filters_and_failed_mutations_do_not_create_events(self):
        self.as_role("Owner")
        url = f"/bookings/{self.booking_id}/payment"
        self.assertEqual(self.post(url, {"amount": "bad"}).status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM activity_log").fetchone()[0], 0)
        self.post(url, {"amount": "10"})
        response = self.client.get("/activity?actor=owner&period=all")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"booking_payment", response.data)
        self.assertNotIn(b"booking_payment", self.client.get("/activity?actor=nobody").data)

    def change_form(self, path, **fields):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        base = re.search(rb'name="base" value="([^"]+)"', response.data).group(1).decode()
        return {"base": base, "reason": "Customer agreed to change", **fields}

    def latest_change(self):
        with self.app.app_context():
            return self.db.get_db().execute("SELECT id FROM booking_change_requests ORDER BY id DESC").fetchone()[0]

    def test_agent_amendment_requires_accounts_approval_and_preserves_delivery_history(self):
        self.as_role("Owner")
        with self.app.app_context():
            line_id = self.db.get_db().execute(
                "SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
        self.post(f"/bookings/{self.booking_id}/movement", {"kind": "dispatch", f"move_{line_id}": "1"})
        self.as_role("Agent")
        path = f"/bookings/{self.booking_id}/amend"
        data = self.change_form(path, start_date="2027-01-02", end_date="2027-01-03", qty_1="3")
        self.assertEqual(self.post(path, data).status_code, 302)
        change_id = self.latest_change()
        with self.app.app_context():
            line = self.db.get_db().execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual(line["qty"], 1)
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 403)
        self.as_role("Accounts")
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            line = db.execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual((line["qty"], line["dispatched_qty"], line["rate"]), (3, 1, 100))
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["total"], 600)
            change = db.execute("SELECT * FROM booking_change_requests WHERE id=?", (change_id,)).fetchone()
            self.assertEqual((change["status"], change["requester_name"], change["reviewer_name"]),
                             ("Approved", "Agent name", "Accounts name"))
            self.assertIn('"qty": 1', change["before_snapshot"])
            self.assertIn('"qty": 3', change["after_snapshot"])
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 400)
        self.assertIn(b"After approval", self.client.get(f"/bookings/{self.booking_id}/changes").data)

    def test_amendment_preserves_original_quotes_and_records_approved_price_changes(self):
        self.as_role("Owner")
        path = f"/bookings/{self.booking_id}/amend"
        data = self.change_form(path, start_date="2027-01-01", end_date="2027-01-01",
                                qty_1="2", rate_1="120", deposit_1="25", travel_charge="10")
        self.assertEqual(self.post(path, data).status_code, 302)
        change_id = self.latest_change()
        self.as_role("Accounts")
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["total"], 300)
            change = db.execute("SELECT * FROM booking_change_requests WHERE id=?", (change_id,)).fetchone()
            self.assertIn('"rate": 100.0', change["before_snapshot"])
            self.assertIn('"rate": 120.0', change["after_snapshot"])
        self.as_role("Owner")
        self.assertEqual(self.client.get(f"/bookings/{self.booking_id}/edit").status_code, 302)
        self.assertIn(b"Bookings with change history", self.post(
            f"/bookings/{self.booking_id}/delete", follow_redirects=True).data)

    def test_stale_amendment_and_overbooking_approval_are_rejected(self):
        self.as_role("Owner")
        path = f"/bookings/{self.booking_id}/amend"
        data = self.change_form(path, start_date="2027-01-01", end_date="2027-01-01", qty_1="10")
        self.post(path, data)
        change_id = self.latest_change()
        with self.app.app_context():
            db = self.db.get_db()
            customer_id = db.execute("SELECT customer_id FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0]
            other = db.execute(
                """INSERT INTO bookings(customer_id,event_type,start_date,end_date,created_at)
                   VALUES (?,'Other','2027-01-01','2027-01-01','2026-01-01')""", (customer_id,)).lastrowid
            db.execute("INSERT INTO booking_items (booking_id,item_id,qty,rate) VALUES (?,1,15,100)", (other,))
            db.commit()
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 400)
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE bookings SET notes='Another edit' WHERE id=?", (self.booking_id,))
            db.commit()
        self.assertEqual(self.post(path, data).status_code, 409)
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "approve"}).status_code, 409)
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review", {"decision": "reject"}).status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute(
                "SELECT qty FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0], 1)

    def test_losses_close_outstanding_items_without_faking_returns_or_restocking(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET qty=5,deposit=10 WHERE booking_id=?", (self.booking_id,))
            line_id = db.execute("SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
            db.commit()
        movement = f"/bookings/{self.booking_id}/movement"
        self.post(movement, {"kind": "dispatch", f"move_{line_id}": "5"})
        self.as_role("Agent")
        path = f"/bookings/{self.booking_id}/settlement"
        self.post(path, self.change_form(path, line_id=str(line_id), qty="2", kind="Lost", charge="150"))
        change_id = self.latest_change()
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM damages").fetchone()[0], 0)
        self.as_role("Accounts")
        self.assertEqual(self.post(f"/booking-changes/{change_id}/review",
                                   {"decision": "approve", "charge": "200"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            line = db.execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual((line["returned_qty"], line["lost_qty"]), (0, 2))
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["total"], 750)
            self.assertEqual(self.db.available_qty(db, db.execute("SELECT * FROM items WHERE id=1").fetchone(),
                                                   "2027-01-01", "2027-01-01"), 15)
            damage_id = db.execute("SELECT id FROM damages").fetchone()[0]
        self.assertEqual(self.post(f"/damages/{damage_id}/resolve", {"action": "repaired"}).status_code, 400)
        self.assertEqual(self.post(f"/damages/{damage_id}/resolve", {"action": "writeoff"}).status_code, 302)
        self.as_role("Owner")
        self.assertEqual(self.post(movement, {"kind": "return", f"move_{line_id}": "4"}).status_code, 400)
        self.post(movement, {"kind": "return", f"move_{line_id}": "3"})
        with self.app.app_context():
            db = self.db.get_db()
            line = db.execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual((line["returned_qty"], line["lost_qty"]), (3, 2))
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0], "Returned")
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["total"], 700)
            self.assertEqual(db.execute("SELECT total_qty FROM items WHERE id=1").fetchone()[0], 18)
            self.assertEqual(self.db.available_qty(db, db.execute("SELECT * FROM items WHERE id=1").fetchone(),
                                                   "2027-01-01", "2027-01-01"), 18)
        self.assertIn(b"not physically returned", self.client.get(f"/bookings/{self.booking_id}/invoice").data)

    def test_damaged_return_and_later_inspection_do_not_double_receive_items(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET qty=2 WHERE booking_id=?", (self.booking_id,))
            line_id = db.execute("SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
            db.commit()
        movement = f"/bookings/{self.booking_id}/movement"
        self.post(movement, {"kind": "dispatch", f"move_{line_id}": "2"})
        path = f"/bookings/{self.booking_id}/settlement"
        for kind in ("Damaged return", "Already returned damaged"):
            if kind == "Already returned damaged":
                self.post(movement, {"kind": "return", f"move_{line_id}": "1"})
            data = self.change_form(path, line_id=str(line_id), qty="1", kind=kind, charge="10")
            self.assertEqual(self.post(path, data).status_code, 302)
            self.assertEqual(self.post(f"/booking-changes/{self.latest_change()}/review",
                                       {"decision": "approve"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            line = db.execute("SELECT * FROM booking_items WHERE id=?", (line_id,)).fetchone()
            self.assertEqual((line["returned_qty"], line["lost_qty"]), (2, 0))
            self.assertEqual(self.db.damaged_open(db, 1), 2)
            damage_id = db.execute("SELECT id FROM damages ORDER BY id LIMIT 1").fetchone()[0]
        self.assertEqual(self.post(path, self.change_form(path, line_id=str(line_id), qty="1",
                              kind="Already returned damaged", charge="0")).status_code, 400)
        self.post(f"/damages/{damage_id}/resolve", {"action": "repaired"})
        with self.app.app_context():
            self.assertEqual(self.db.damaged_open(self.db.get_db(), 1), 1)
        self.assertEqual(self.post(path, self.change_form(path, line_id=str(line_id), qty="1",
                              kind="Already returned damaged", charge="0")).status_code, 400)

    def test_amendment_permissions_validation_and_price_tampering(self):
        path = f"/bookings/{self.booking_id}/amend"
        for role in ("Worker", "Staff", "Partner"):
            self.as_role(role)
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.post(f"/booking-changes/999/review", {"decision": "approve"}).status_code, 403)
        self.as_role("Agent")
        data = self.change_form(path, start_date="2027-01-01", end_date="2027-01-01", qty_1="1")
        self.assertEqual(self.post(path, {**data, "rate_1": "1"}).status_code, 403)
        self.assertEqual(self.post(path, {**data, "discount": "1"}).status_code, 403)
        self.assertEqual(self.post(path, {**data, "qty_1": "-1"}).status_code, 400)
        self.assertEqual(self.post(path, {**data, "reason": ""}).status_code, 400)
        self.assertEqual(self.post(path, {**data, "end_date": "2026-01-01"}).status_code, 400)
        self.as_role("Owner")
        self.assertEqual(self.post(path, {**data, "rate_1": "nan"}).status_code, 400)
        self.assertEqual(self.post(path, {**data, "deposit_1": "inf"}).status_code, 400)

    def test_live_updates_are_scoped_and_do_not_extend_inactive_sessions(self):
        self.as_role("Worker")
        first = self.client.get("/live")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(set(first.json), {"revision"})
        self.assertNotIn("Set-Cookie", first.headers)
        worker_revision = first.json["revision"]
        self.as_role("Owner")
        owner_revision = self.client.get("/live").json["revision"]
        self.post(f"/bookings/{self.booking_id}/payment", {"amount": "5"})
        self.assertNotEqual(self.client.get("/live").json["revision"], owner_revision)
        self.as_role("Worker")
        self.assertEqual(self.client.get("/live").json["revision"], worker_revision)
        self.as_role("Owner")
        self.post(f"/bookings/{self.booking_id}/assign", {"user_id": self.ids["Worker"], "task": "Collect chairs"})
        self.as_role("Worker")
        assigned_revision = self.client.get("/live").json["revision"]
        self.assertNotEqual(assigned_revision, worker_revision)
        self.as_role("Owner")
        with self.app.app_context():
            assignment_id = self.db.get_db().execute("SELECT id FROM work_assignments").fetchone()[0]
        self.post(f"/work/{assignment_id}/remove")
        self.as_role("Worker")
        self.assertNotEqual(self.client.get("/live").json["revision"], assigned_revision)
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.ids["Worker"],))
            db.commit()
        self.assertEqual(self.client.get("/live").status_code, 302)

    def test_amendment_cannot_reduce_dispatched_quantities_but_can_remove_unsent_remainder(self):
        self.as_role("Owner")
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET qty=3,deposit=10 WHERE booking_id=?", (self.booking_id,))
            line_id = db.execute("SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
            db.commit()
        movement = f"/bookings/{self.booking_id}/movement"
        self.post(movement, {"kind": "dispatch", f"move_{line_id}": "2"})
        self.post(movement, {"kind": "return", f"move_{line_id}": "2"})
        self.assertIn(b"Use delivery and return tracking", self.post(
            f"/bookings/{self.booking_id}/status", {"status": "Cancelled"}, follow_redirects=True).data)
        path = f"/bookings/{self.booking_id}/amend"
        data = self.change_form(path, start_date="2027-01-01", end_date="2027-01-01", qty_1="1")
        self.assertEqual(self.post(path, data).status_code, 400)
        self.assertEqual(self.post(path, {**data, "qty_1": "2"}).status_code, 302)
        self.assertEqual(self.post(f"/booking-changes/{self.latest_change()}/review",
                                   {"decision": "approve"}).status_code, 302)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(db.execute("SELECT status FROM bookings WHERE id=?", (self.booking_id,)).fetchone()[0], "Returned")
            self.assertEqual(self.db.booking_totals(db, self.booking_id)["total"], 200)

    def test_competing_loss_requests_cannot_settle_the_same_items_twice(self):
        self.as_role("Owner")
        with self.app.app_context():
            line_id = self.db.get_db().execute(
                "SELECT id FROM booking_items WHERE booking_id=?", (self.booking_id,)).fetchone()[0]
        self.post(f"/bookings/{self.booking_id}/movement", {"kind": "dispatch", f"move_{line_id}": "1"})
        path = f"/bookings/{self.booking_id}/settlement"
        data = self.change_form(path, line_id=str(line_id), qty="1", kind="Lost", charge="20")
        self.post(path, data)
        first = self.latest_change()
        self.post(path, data)
        second = self.latest_change()
        self.assertEqual(self.post(f"/booking-changes/{first}/review", {"decision": "approve"}).status_code, 302)
        self.assertEqual(self.post(f"/booking-changes/{second}/review", {"decision": "approve"}).status_code, 409)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM damages").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT lost_qty FROM booking_items WHERE id=?", (line_id,)).fetchone()[0], 1)

    def test_agent_history_hides_financial_snapshots_and_payment_live_events(self):
        self.as_role("Owner")
        path = f"/bookings/{self.booking_id}/amend"
        data = self.change_form(path, start_date="2027-01-01", end_date="2027-01-01",
                                qty_1="2", rate_1="314159.26")
        self.post(path, data)
        self.as_role("Agent")
        self.assertNotIn(b"314159", self.client.get(f"/bookings/{self.booking_id}/changes").data)
        revision = self.client.get("/live").json["revision"]
        self.as_role("Owner")
        self.post(f"/bookings/{self.booking_id}/payment", {"amount": "5"})
        self.as_role("Agent")
        self.assertEqual(self.client.get("/live").json["revision"], revision)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/live").status_code, 302)


if __name__ == "__main__":
    unittest.main()
