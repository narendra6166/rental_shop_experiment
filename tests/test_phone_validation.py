import os
import tempfile
import unittest


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

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        with self.app.app_context():
            db = self.db.get_db()
            db.execute("DELETE FROM customers")
            db.execute("DELETE FROM staff")
            db.commit()

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
