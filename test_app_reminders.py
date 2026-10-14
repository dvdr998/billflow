import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta

import app


class ReminderFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db = app.DB
        app.DB = os.path.join(self.tempdir.name, "test_bills.db")
        app.initialize_database()
        success, message = app.register_user(
            "Vamshidhar Reddy",
            "vamshi@example.com",
            "password",
            "password",
        )
        self.assertTrue(success, message)
        user = app.authenticate_user("vamshi@example.com", "password")
        self.user_id = user["id"]

    def tearDown(self):
        app.DB = self.original_db
        self.tempdir.cleanup()

    def rows(self):
        return app.get_bills(self.user_id)

    def add_sample_bill(
        self,
        *,
        service_name="Netflix",
        next_payment_date=date(2026, 10, 20),
        reminder_days=0,
        reminder_time="08:00",
        reminder_email="vamshi@example.com",
        frequency="Monthly",
        status="Active",
    ):
        app.add_bill(
            self.user_id,
            service_name,
            15.99,
            "Entertainment",
            frequency,
            next_payment_date,
            reminder_days,
            status,
            reminder_time,
            "America/Los_Angeles",
            reminder_email,
        )
        with app.conn() as c:
            bill_id = c.execute(
                "SELECT MAX(id) AS id FROM bills_subscriptions WHERE user_id=?",
                (self.user_id,),
            ).fetchone()["id"]
        return app.get_bill(self.user_id, bill_id)

    def test_ampm_conversion_and_display(self):
        morning = app.reminder_time_from_parts(8, 0, "AM")
        evening = app.reminder_time_from_parts(8, 0, "PM")

        self.assertEqual(morning.strftime("%H:%M"), "08:00")
        self.assertEqual(evening.strftime("%H:%M"), "20:00")
        self.assertEqual(app.format_reminder_time("08:00"), "8:00 AM")
        self.assertEqual(app.format_reminder_time("18:30"), "6:30 PM")
        self.assertEqual(app.format_reminder_time("23:45"), "11:45 PM")

    def test_add_bill_stores_registered_and_custom_reminder_email(self):
        registered = self.add_sample_bill(reminder_email="vamshi@example.com")
        custom = self.add_sample_bill(
            service_name="Hulu",
            reminder_time="20:00",
            reminder_email="billing@example.net",
        )

        self.assertEqual(registered["reminder_email"], "vamshi@example.com")
        self.assertEqual(custom["reminder_email"], "billing@example.net")
        self.assertEqual(custom["reminder_time"], "20:00")

    def test_invalid_email_validation_helper(self):
        self.assertTrue(app.is_valid_email("billing@example.net"))
        self.assertFalse(app.is_valid_email("not-an-email"))

    def test_existing_bill_without_reminder_email_falls_back_to_user_email(self):
        row = self.add_sample_bill(reminder_email=None)
        self.assertIsNone(row["reminder_email"])
        self.assertEqual(app.resolved_reminder_email(row, "vamshi@example.com"), "vamshi@example.com")

    def test_scheduled_dates_for_on_payment_day_and_three_days_before(self):
        on_payment_day = self.add_sample_bill(reminder_days=0, reminder_time="08:00")
        three_days_before = self.add_sample_bill(
            service_name="Adobe",
            reminder_days=3,
            reminder_time="08:00",
        )

        self.assertEqual(
            app.scheduled_reminder_datetime(on_payment_day).strftime("%Y-%m-%d %H:%M"),
            "2026-10-20 08:00",
        )
        self.assertEqual(
            app.scheduled_reminder_datetime(three_days_before).strftime("%Y-%m-%d %H:%M"),
            "2026-10-17 08:00",
        )

    def test_worker_sends_to_bill_specific_email_and_prevents_duplicates(self):
        self.add_sample_bill(reminder_email="billing@example.net")
        sent = []

        def fake_send(*args):
            sent.append(args)

        now = datetime(2026, 10, 20, 8, 0)
        first_result = app.process_due_reminders(now=now, send_email_func=fake_send)
        second_result = app.process_due_reminders(now=now, send_email_func=fake_send)

        self.assertEqual(first_result["eligible"], 1)
        self.assertEqual(first_result["sent"], 1)
        self.assertEqual(sent[0][0], "billing@example.net")
        self.assertEqual(second_result["eligible"], 0)
        self.assertEqual(second_result["sent"], 0)

    def test_worker_falls_back_to_user_email_for_old_bills(self):
        self.add_sample_bill(reminder_email=None)
        sent = []

        def fake_send(*args):
            sent.append(args)

        result = app.process_due_reminders(
            now=datetime(2026, 10, 20, 8, 0),
            send_email_func=fake_send,
        )

        self.assertEqual(result["sent"], 1)
        self.assertEqual(sent[0][0], "vamshi@example.com")

    def test_smtp_failure_does_not_mark_sent(self):
        row = self.add_sample_bill(reminder_email="billing@example.net")

        def failing_send(*args):
            raise RuntimeError("smtp unavailable")

        result = app.process_due_reminders(
            now=datetime(2026, 10, 20, 8, 0),
            send_email_func=failing_send,
        )
        updated = app.get_bill(self.user_id, row["id"])

        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(updated["last_reminder_sent_for_date"], None)
        self.assertEqual(app.reminder_status(updated), "Failed")

    def test_payment_statuses_for_future_today_past_paid_and_cancelled(self):
        today = date.today()
        future = self.add_sample_bill(service_name="Future", next_payment_date=today + timedelta(days=1))
        due_today = self.add_sample_bill(service_name="Today", next_payment_date=today)
        overdue = self.add_sample_bill(service_name="Past", next_payment_date=today - timedelta(days=1))
        cancelled = self.add_sample_bill(
            service_name="Cancelled",
            next_payment_date=today,
            status="Cancelled",
        )

        self.assertEqual(app.payment_status(future, today=today), "Upcoming")
        self.assertEqual(app.payment_status(due_today, today=today), "Due Today")
        self.assertEqual(app.payment_status(overdue, today=today), "Overdue")
        self.assertEqual(app.payment_status(cancelled, today=today), "Cancelled")

        result = app.mark_current_payment_paid(
            self.user_id,
            overdue["id"],
            now=datetime.combine(today, datetime.min.time()),
        )
        updated = app.get_bill(self.user_id, overdue["id"])
        history = app.get_payment_occurrences(self.user_id, overdue["id"])

        self.assertIsNotNone(result)
        self.assertEqual(history[0]["payment_status"], "Completed")
        self.assertEqual(app.payment_status(updated, today=today), "Upcoming")

    def test_monthly_weekly_and_yearly_paid_cycles_advance_calendar_dates(self):
        today = date.today()
        monthly = self.add_sample_bill(
            service_name="Monthly",
            next_payment_date=date(2026, 1, 31),
            frequency="Monthly",
        )
        weekly = self.add_sample_bill(
            service_name="Weekly",
            next_payment_date=today,
            frequency="Weekly",
        )
        yearly = self.add_sample_bill(
            service_name="Yearly",
            next_payment_date=date(2024, 2, 29),
            frequency="Yearly",
        )

        self.assertEqual(app.next_occurrence_date(date(2026, 1, 31), "Monthly"), date(2026, 2, 28))
        self.assertEqual(app.next_occurrence_date(date(2024, 2, 29), "Yearly"), date(2025, 2, 28))

        app.mark_current_payment_paid(self.user_id, weekly["id"], now=datetime.combine(today, datetime.min.time()))
        updated_weekly = app.get_bill(self.user_id, weekly["id"])

        self.assertEqual(app.next_occurrence_date(app.parse_due_date(monthly), "Monthly"), date(2026, 2, 28))
        self.assertEqual(app.parse_due_date(updated_weekly), today + timedelta(days=7))
        self.assertEqual(app.next_occurrence_date(app.parse_due_date(yearly), "Yearly"), date(2025, 2, 28))

    def test_paid_recurring_cycle_preserves_sent_history_and_resets_next_reminder(self):
        today = date.today()
        row = self.add_sample_bill(
            next_payment_date=today,
            reminder_days=3,
            reminder_time="08:00",
            frequency="Monthly",
            reminder_email="billing@example.net",
        )
        sent = []

        def fake_send(*args):
            sent.append(args)

        send_result = app.process_due_reminders(
            now=datetime.combine(today, datetime.min.time()).replace(hour=9),
            send_email_func=fake_send,
        )
        self.assertEqual(send_result["sent"], 1)

        paid_result = app.mark_current_payment_paid(
            self.user_id,
            row["id"],
            now=datetime.combine(today, datetime.min.time()).replace(hour=10),
        )
        updated = app.get_bill(self.user_id, row["id"])
        history = app.get_payment_occurrences(self.user_id, row["id"])

        self.assertEqual(history[0]["payment_date"], today.isoformat())
        self.assertEqual(history[0]["payment_status"], "Completed")
        self.assertEqual(history[0]["reminder_status"], "Sent")
        self.assertIsNotNone(history[0]["reminder_sent_at"])
        self.assertEqual(app.parse_due_date(updated), app.next_occurrence_date(today, "Monthly"))
        self.assertIsNone(updated["last_reminder_sent_for_date"])
        self.assertEqual(app.reminder_status(updated), "Scheduled")
        self.assertEqual(
            app.scheduled_reminder_datetime(updated).date(),
            paid_result["next_date"] - timedelta(days=3),
        )

    def test_duplicate_prevention_is_per_recurring_payment_cycle(self):
        today = date.today()
        row = self.add_sample_bill(next_payment_date=today, reminder_days=0, frequency="Weekly")
        sent = []

        def fake_send(*args):
            sent.append(args)

        now = datetime.combine(today, datetime.min.time()).replace(hour=9)
        first = app.process_due_reminders(now=now, send_email_func=fake_send)
        duplicate = app.process_due_reminders(now=now, send_email_func=fake_send)
        app.mark_current_payment_paid(self.user_id, row["id"], now=now.replace(hour=10))
        updated = app.get_bill(self.user_id, row["id"])
        next_now = datetime.combine(app.parse_due_date(updated), datetime.min.time()).replace(hour=9)
        next_cycle = app.process_due_reminders(now=next_now, send_email_func=fake_send)

        self.assertEqual(first["sent"], 1)
        self.assertEqual(duplicate["sent"], 0)
        self.assertEqual(next_cycle["sent"], 1)
        self.assertEqual(len(sent), 2)
        with app.conn() as c:
            occurrences = c.execute(
                "SELECT * FROM reminder_occurrences WHERE bill_id=? ORDER BY payment_date",
                (row["id"],),
            ).fetchall()
        self.assertEqual(len(occurrences), 2)
        self.assertEqual([item["status"] for item in occurrences], ["Sent", "Sent"])

    def test_cancelled_bill_and_paid_one_time_do_not_create_future_reminders(self):
        today = date.today()
        cancelled = self.add_sample_bill(
            service_name="Cancelled",
            next_payment_date=today,
            status="Cancelled",
        )
        one_time = self.add_sample_bill(
            service_name="One Time",
            next_payment_date=today,
            frequency="One-time",
        )

        self.assertFalse(app.is_reminder_due(cancelled, now=datetime.combine(today, datetime.min.time()).replace(hour=9)))
        paid_result = app.mark_current_payment_paid(
            self.user_id,
            one_time["id"],
            now=datetime.combine(today, datetime.min.time()).replace(hour=9),
        )
        updated = app.get_bill(self.user_id, one_time["id"])

        self.assertIsNone(paid_result["next_date"])
        self.assertEqual(app.payment_status(updated, today=today), "Completed")
        self.assertFalse(app.is_reminder_due(updated, now=datetime.combine(today, datetime.min.time()).replace(hour=10)))

    def test_dashboard_edit_cancel_reactivate_delete_and_auth_still_work(self):
        today = date.today()
        row = self.add_sample_bill(next_payment_date=today, frequency="Monthly")
        app.mark_current_payment_paid(
            self.user_id,
            row["id"],
            now=datetime.combine(today, datetime.min.time()).replace(hour=9),
        )
        updated = app.get_bill(self.user_id, row["id"])
        metrics = app.calculate_expenses(app.get_bills(self.user_id))

        self.assertEqual(app.parse_due_date(updated), app.next_occurrence_date(today, "Monthly"))
        self.assertEqual(metrics["upcoming_count"], 1)

        app.update_bill(
            self.user_id,
            updated["id"],
            "Updated Service",
            20.0,
            "Utilities",
            "Monthly",
            app.parse_due_date(updated),
            updated["reminder_days"],
            "Cancelled",
            updated["reminder_time"],
            updated["timezone"],
            updated["reminder_email"],
        )
        cancelled = app.get_bill(self.user_id, updated["id"])
        self.assertEqual(cancelled["service_name"], "Updated Service")
        self.assertEqual(cancelled["status"], "Cancelled")

        app.update_bill(
            self.user_id,
            cancelled["id"],
            cancelled["service_name"],
            cancelled["amount"],
            cancelled["category"],
            cancelled["billing_frequency"],
            app.parse_due_date(cancelled),
            cancelled["reminder_days"],
            "Active",
            cancelled["reminder_time"],
            cancelled["timezone"],
            cancelled["reminder_email"],
        )
        self.assertEqual(app.get_bill(self.user_id, cancelled["id"])["status"], "Active")

        self.assertIsNotNone(app.authenticate_user("vamshi@example.com", "password"))
        app.delete_bill(self.user_id, cancelled["id"])
        self.assertIsNone(app.get_bill(self.user_id, cancelled["id"]))

    def test_two_accounts_are_isolated(self):
        success, message = app.register_user(
            "Second User",
            "second@example.com",
            "password",
            "password",
        )
        self.assertTrue(success, message)
        second_user = app.authenticate_user("second@example.com", "password")

        first_bill = self.add_sample_bill(service_name="First Account")
        app.add_bill(
            second_user["id"],
            "Second Account",
            9.99,
            "Other",
            "Monthly",
            date.today(),
            0,
            "Active",
            "08:00",
            "America/Los_Angeles",
            "second@example.com",
        )

        first_rows = app.get_bills(self.user_id)
        second_rows = app.get_bills(second_user["id"])

        self.assertEqual([row["service_name"] for row in first_rows], ["First Account"])
        self.assertEqual([row["service_name"] for row in second_rows], ["Second Account"])
        self.assertIsNone(app.get_bill(second_user["id"], first_bill["id"]))

    def test_migration_adds_reminder_email_without_deleting_existing_records(self):
        other_db = os.path.join(self.tempdir.name, "legacy.db")
        with sqlite3.connect(other_db) as c:
            c.execute(
                """CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            c.execute(
                """CREATE TABLE bills_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    service_name TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    billing_frequency TEXT NOT NULL,
                    next_payment_date TEXT NOT NULL,
                    reminder_days INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Active',
                    reminder_time TEXT NOT NULL DEFAULT '08:00',
                    timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
                    last_reminder_sent_for_date TEXT,
                    last_reminder_sent_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            c.execute(
                "INSERT INTO users(full_name, email, password_hash) VALUES(?,?,?)",
                ("Legacy User", "legacy@example.com", app.hash_password("password")),
            )
            c.execute(
                """INSERT INTO bills_subscriptions(
                    user_id, service_name, amount, category, billing_frequency,
                    next_payment_date, reminder_days, status
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (1, "Legacy Bill", 10.0, "Other", "Monthly", "2026-10-20", 0, "Active"),
            )

        app.DB = other_db
        app.initialize_database()
        with app.conn() as c:
            columns = [row["name"] for row in c.execute("PRAGMA table_info(bills_subscriptions)")]
            history_table = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='payment_occurrences'"
            ).fetchone()
            reminder_history_table = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reminder_occurrences'"
            ).fetchone()
            records = c.execute("SELECT * FROM bills_subscriptions").fetchall()

        self.assertIn("reminder_email", columns)
        self.assertIn("current_cycle_paid_at", columns)
        self.assertIn("last_reminder_failed_at", columns)
        self.assertIsNotNone(history_table)
        self.assertIsNotNone(reminder_history_table)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["service_name"], "Legacy Bill")


if __name__ == "__main__":
    unittest.main()
