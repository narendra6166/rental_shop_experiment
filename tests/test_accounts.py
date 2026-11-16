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
            for table in ("work_assignments", "bookings", "customers", "users", "login_attempts"):
                db.execute(f"DELETE FROM {table}")
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


if __name__ == "__main__":
    unittest.main()
