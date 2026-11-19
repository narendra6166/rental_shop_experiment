import os
import tempfile
import unittest
from html import unescape
from unittest.mock import patch

from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash


class AuthenticatedClient(FlaskClient):
    def open(self, *args, **kwargs):
        if kwargs.get("method", "").upper() == "POST":
            kwargs["data"] = {**kwargs.get("data", {}), "csrf_token": "regression-token"}
        return super().open(*args, **kwargs)


class PhoneValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["RENTAL_DB"] = os.path.join(cls.tempdir.name, "test.db")
        os.environ["BACKUP_DIR"] = os.path.join(cls.tempdir.name, "backups")
        import app
        import db

        cls.app = app.app
        cls.db = db
        cls.app.config.update(TESTING=True)
        cls.path_patch = patch.object(db, "DB_PATH", os.path.join(cls.tempdir.name, "test.db"))
        cls.path_patch.start()
        db.init_db()
        cls.password_hash = generate_password_hash("test-password-long")

    @classmethod
    def tearDownClass(cls):
        cls.path_patch.stop()
        cls.tempdir.cleanup()

    def setUp(self):
        self.client = AuthenticatedClient(self.app, use_cookies=True)
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("DELETE FROM booking_change_requests")
            db.execute("DELETE FROM damages")
            db.execute("DELETE FROM work_assignments")
            db.execute("DELETE FROM bookings")
            db.execute("DELETE FROM customers")
            db.execute("DELETE FROM staff")
            db.execute("DELETE FROM users")
            owner_id = db.execute(
                "INSERT INTO users (username,name,password_hash,role) VALUES ('owner','Owner',?,'Owner')",
                (self.password_hash,),
            ).lastrowid
            db.commit()
        with self.client.session_transaction() as session:
            session.update(user_id=owner_id, session_version=1, _csrf="regression-token")

    def booking_data(self):
        return {
            "customer_name": "Customer", "phone": "0123456789", "address": "Shop road",
            "event_type": "Birthday", "start_date": "2027-01-01", "end_date": "2027-01-01",
            "qty_1": "1",
        }

    def test_category_migration_preserves_stock_and_custom_categories(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "migration.db")
            with patch.object(self.db, "DB_PATH", path):
                self.db.init_db()
                with sqlite3.connect(path) as db:
                    db.execute("UPDATE items SET category='Cooking' WHERE id=1")
                    db.execute("UPDATE items SET category='Furniture' WHERE id=2")
                    db.execute("UPDATE items SET category='Custom' WHERE id=3")
                    before = db.execute("SELECT id, name, total_qty, rate_per_day, deposit FROM items").fetchall()
                self.db.init_db()
                self.db.init_db()
                with sqlite3.connect(path) as db:
                    self.assertEqual(before, db.execute(
                        "SELECT id, name, total_qty, rate_per_day, deposit FROM items").fetchall())
                    categories = dict(db.execute("SELECT id,category FROM items"))
                    self.assertEqual(categories[1], "Cooking & Catering")
                    self.assertEqual(categories[2], "Tents & Event Setup")
                    self.assertEqual(categories[3], "Custom")

    def test_delivery_migration_is_applied_only_once(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "migration.db")
            with patch.object(self.db, "DB_PATH", path):
                self.db.init_db()
                with sqlite3.connect(path) as db:
                    db.execute("ALTER TABLE booking_items DROP COLUMN dispatched_qty")
                    db.execute("ALTER TABLE booking_items DROP COLUMN returned_qty")
                    db.execute("INSERT INTO customers (name,phone) VALUES ('Test','0123456789')")
                    for booking_id, status in enumerate(("Booked", "Delivered", "Returned"), 1):
                        db.execute(
                            """INSERT INTO bookings(id,customer_id,event_type,start_date,end_date,status,created_at)
                               VALUES (?,1,'Other','2027-01-01','2027-01-01',?,'2026-01-01')""",
                            (booking_id, status))
                        db.execute("INSERT INTO booking_items (booking_id,item_id,qty,rate) VALUES (?,1,5,10)",
                                   (booking_id,))
                self.db.init_db()
                with sqlite3.connect(path) as db:
                    rows = db.execute("SELECT dispatched_qty,returned_qty FROM booking_items ORDER BY booking_id").fetchall()
                    self.assertEqual(rows, [(0, 0), (5, 0), (5, 5)])
                    db.execute("UPDATE booking_items SET returned_qty=2 WHERE booking_id=2")
                self.db.init_db()
                with sqlite3.connect(path) as db:
                    self.assertEqual(db.execute(
                        "SELECT dispatched_qty,returned_qty FROM booking_items WHERE booking_id=2").fetchone(), (5, 2))

    def test_item_categories_are_validated_and_grouped_on_all_pages(self):
        data = {"name": "Homam equipment", "category": "Pooja & Rituals",
                "total_qty": "0", "rate_per_day": "100", "deposit": "0"}
        response = self.client.post("/items/new", data=data)
        self.assertEqual(response.status_code, 302)
        for path in ("/items", "/stock", "/bookings/new"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Pooja &amp; Rituals</h2>", response.data)
            self.assertIn(b"Cooking &amp; Catering</h2>", response.data)
            self.assertIn(b"Tents &amp; Event Setup</h2>", response.data)
        response = self.client.post("/items/new", data={**data, "category": "Unknown"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Please enter valid item details.", response.data)
        self.assertIn(b'value="Homam equipment"', response.data)

    def test_booking_validates_phone_before_saving_customer(self):
        for phone in ("666667778888888", "123", "123456789x", ""):
            response = self.client.post("/bookings/new", data={**self.booking_data(), "phone": phone})
            self.assertEqual(response.status_code, 200)
        response = self.client.post("/bookings/new", data={**self.booking_data(), "qty_1": ""})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            db = self.db.get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 0)

    def test_invoice_uses_selected_language_and_preserves_entered_details(self):
        response = self.client.post("/bookings/new", data=self.booking_data())
        booking_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        from i18n import _

        with self.app.app_context():
            db = self.db.get_db()
            db.execute("INSERT INTO payments (booking_id, amount, kind, paid_on) VALUES (?, 30, 'Advance', '2027-01-01')", (booking_id,))
            db.commit()
        for lang, month in (("en", "Jan"), ("te", "జన"), ("hi", "जन")):
            self.client.set_cookie("lang", lang)
            for status in ("Booked", "Returned", "Cancelled"):
                with self.subTest(lang=lang, status=status):
                    with self.app.app_context():
                        db = self.db.get_db()
                        db.execute("UPDATE bookings SET status=? WHERE id=?", (status, booking_id))
                        db.commit()
                    bill = self.client.get(f"/bookings/{booking_id}/invoice").get_data(as_text=True)
                    self.assertIn(f'<html lang="{lang}">', bill)
                    self.assertIn(f"01 {month} 2027", bill)
                    self.assertIn("Customer", bill)
                    self.assertIn("Shop road", bill)
                    with self.app.test_request_context(headers={"Cookie": f"lang={lang}"}):
                        from markupsafe import escape
                        for label in ("Not a GST tax invoice", "Payment history", "Advance",
                                      "Print / Save PDF", "Tent 10x10 ft"):
                            self.assertIn(str(escape(_(label))), bill)

    def test_shop_brand_is_consistent_across_dashboard_and_invoice_languages(self):
        response = self.client.post("/bookings/new", data=self.booking_data())
        booking_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        brands = {
            "en": "Amma Event's and Hyres",
            "te": "అమ్మ ఈవెంట్స్ అండ్ హైర్స్",
            "hi": "अम्मा इवेंट्स एंड हायर्स",
        }
        with patch.dict(self.app.jinja_env.globals, {"shop_name": brands["en"]}):
            for lang, brand in brands.items():
                with self.subTest(lang=lang):
                    self.client.set_cookie("lang", lang)
                    for path in ("/", f"/bookings/{booking_id}/invoice"):
                        self.assertIn(brand, unescape(self.client.get(path).get_data(as_text=True)))
        with patch.dict(self.app.jinja_env.globals, {"shop_name": "Custom shop"}):
            self.client.set_cookie("lang", "te")
            self.assertIn("Custom shop", self.client.get("/").get_data(as_text=True))

    def test_rental_bill_shows_consistent_totals_and_payment_history(self):
        response = self.client.post("/bookings/new", data=self.booking_data())
        booking_id = int(response.headers["Location"].rsplit("/", 1)[-1])
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE booking_items SET rate=100, deposit=50 WHERE booking_id=?", (booking_id,))
            db.execute("UPDATE bookings SET discount=150 WHERE id=?", (booking_id,))
            db.execute("INSERT INTO payments (booking_id, amount, kind, paid_on) VALUES (?, 30, 'Advance', '2027-01-01')", (booking_id,))
            db.commit()
        url = f"/bookings/{booking_id}/invoice"
        bill = self.client.get(url).get_data(as_text=True)
        self.assertIn("Not a GST tax invoice", bill)
        self.assertIn("<td>Discount applied</td><td class=\"num\">-₹100</td>", bill)
        self.assertIn("<td>Refundable deposit</td><td class=\"num\">₹50</td>", bill)
        self.assertIn("<td>Total due before payments</td><td class=\"num\">₹50</td>", bill)
        self.assertIn("<td>Balance due</td><td class=\"num\">₹20</td>", bill)
        self.assertIn("<td>01 Jan 2027</td><td>Advance</td><td class=\"num\">₹30</td>", bill)
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE bookings SET status='Returned' WHERE id=?", (booking_id,))
            db.execute("INSERT INTO payments (booking_id, amount, kind, paid_on) VALUES (?, 10, 'Deposit refund', '2027-01-02')", (booking_id,))
            db.commit()
        bill = self.client.get(url).get_data(as_text=True)
        self.assertIn("refundable deposit of ₹50 is excluded", bill)
        self.assertIn("<td>Total due before payments</td><td class=\"num\">₹0</td>", bill)
        self.assertIn("<td>Refund due</td><td class=\"num\">₹20</td>", bill)
        self.assertIn("<td>Refunded</td><td class=\"num\">+₹10</td>", bill)
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("UPDATE bookings SET status='Cancelled', cancel_fee=15 WHERE id=?", (booking_id,))
            db.commit()
        bill = self.client.get(url).get_data(as_text=True)
        self.assertIn("Items booked (not charged after cancellation)", bill)
        self.assertIn("<td>Total due before payments</td><td class=\"num\">₹15</td>", bill)
        self.assertIn("<td>Refund due</td><td class=\"num\">₹5</td>", bill)
        self.assertNotIn("<td>Refundable deposit</td>", bill)

    def test_inline_customer_reuse_and_edit_preserve_other_bookings(self):
        first = self.client.post("/bookings/new", data=self.booking_data())
        second = self.client.post("/bookings/new", data=self.booking_data())
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
        edit_url = first.headers["Location"] + "/edit"
        response = self.client.get(edit_url)
        self.assertIn(b'value="Customer"', response.data)
        response = self.client.get(edit_url, query_string=self.booking_data())
        self.assertIn(b'value="0123456789"', response.data)
        response = self.client.post(edit_url, data={**self.booking_data(), "customer_name": "Changed"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Changed", self.client.get(first.headers["Location"]).data)
        self.assertIn(b"Customer", self.client.get(second.headers["Location"]).data)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0], 2)

    def test_customer_delete_protects_bookings_and_is_post_only(self):
        self.client.post("/customers/new", data={"name": "Unused", "phone": "0123456789"})
        with self.app.app_context():
            customer_id = self.db.get_db().execute("SELECT id FROM customers").fetchone()[0]
        url = f"/customers/{customer_id}/delete"
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertIn(b'value="Unused"', self.client.get(
            "/bookings/new", query_string={"customer_id": customer_id}).data)
        self.assertEqual(self.client.post(url).status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0)
        self.client.post("/bookings/new", data=self.booking_data())
        with self.app.app_context():
            customer_id = self.db.get_db().execute("SELECT id FROM customers").fetchone()[0]
        response = self.client.post(f"/customers/{customer_id}/delete", follow_redirects=True)
        self.assertIn(b"Customer has bookings and cannot be deleted.", response.data)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
            self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)

    def test_customer_requires_exactly_ten_ascii_digits(self):
        for phone in ("", "123", "12345678901", "123456789a", "12345 6789", "１２３４５６７８９０"):
            with self.subTest(phone=phone):
                response = self.client.post("/customers/new", data={"name": "Customer", "phone": phone})
                self.assertEqual(response.status_code, 200)
                with self.app.app_context():
                    self.assertEqual(self.db.get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0)
        response = self.client.post("/customers/new", data={"name": "Customer", "phone": "0123456789"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute("SELECT phone FROM customers").fetchone()[0], "0123456789")

    def test_staff_phone_is_optional_and_validated_on_create_and_edit(self):
        data = {"name": "Worker", "role": "Helper", "monthly_salary": "100", "salary_type": "Monthly"}
        response = self.client.post("/staff/new", data={**data, "phone": ""})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            staff_id = self.db.get_db().execute("SELECT id FROM staff").fetchone()[0]

        for url in ("/staff/new", f"/staff/{staff_id}/edit"):
            for phone in ("123456789", "12345678901", "123456789x"):
                with self.subTest(url=url, phone=phone):
                    response = self.client.post(url, data={**data, "phone": phone})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"Phone number must contain exactly 10 digits.", response.data)
            response = self.client.post(url, data={**data, "phone": "0123456789"})
            self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.db.get_db().execute(
                "SELECT phone FROM staff WHERE id=?", (staff_id,)).fetchone()[0], "0123456789")


if __name__ == "__main__":
    unittest.main()
