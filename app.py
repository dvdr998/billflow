import hashlib
import hmac
import html
import os
import re
import secrets
import smtplib
import sqlite3
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo, available_timezones

import pandas as pd
import streamlit as st

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


def initial_config_value(key):
    value = os.environ.get(key)
    if value:
        return value

    try:
        value = st.secrets.get(key)
    except Exception:
        value = None
    return value


DATABASE_URL = initial_config_value("DATABASE_URL")
DB = initial_config_value("BILLFLOW_DB") or "bills.db"
BILLS_TABLE = "bills_subscriptions"
PAYMENT_OCCURRENCES_TABLE = "payment_occurrences"
REMINDER_OCCURRENCES_TABLE = "reminder_occurrences"

CATEGORIES = [
    "Entertainment",
    "Utilities",
    "Software",
    "Internet",
    "Mobile",
    "Insurance",
    "Membership",
    "Education",
    "Other",
]

FREQUENCIES = ["Monthly", "Yearly", "Weekly"]
STATUSES = ["Active", "Cancelled"]
REMINDER_OPTIONS = {
    "On payment day": 0,
    "1 day before": 1,
    "3 days before": 3,
    "7 days before": 7,
}
DEFAULT_REMINDER_TIME = "08:00"
DEFAULT_TIMEZONE = "America/Los_Angeles"
COMMON_TIMEZONES = [
    ("America/Los_Angeles", "Pacific Time — Los Angeles"),
    ("America/Denver", "Mountain Time — Denver"),
    ("America/Chicago", "Central Time — Chicago"),
    ("America/New_York", "Eastern Time — New York"),
    ("America/Phoenix", "Arizona — Phoenix"),
    ("Asia/Kolkata", "India — Kolkata"),
    ("Europe/London", "United Kingdom — London"),
    ("Europe/Berlin", "Central Europe — Berlin"),
    ("Asia/Tokyo", "Japan — Tokyo"),
    ("Asia/Singapore", "Singapore"),
    ("Australia/Sydney", "Australia — Sydney"),
    ("UTC", "UTC"),
]
COMMON_TIMEZONE_LABELS = dict(COMMON_TIMEZONES)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_ITERATIONS = 260_000


def using_postgres():
    return bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))


def translate_sql(sql):
    if using_postgres():
        return sql.replace("?", "%s")
    return sql


class DatabaseSession:
    def __init__(self):
        self.dialect = "postgres" if using_postgres() else "sqlite"
        self.connection = None

    def __enter__(self):
        if self.dialect == "postgres":
            if psycopg is None:
                raise RuntimeError("DATABASE_URL requires psycopg. Install dependencies from requirements.txt.")
            self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            self.connection = sqlite3.connect(DB, check_same_thread=False)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()

    def execute(self, sql, params=()):
        return self.connection.execute(translate_sql(sql), params)

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


def integrity_errors():
    errors = [sqlite3.IntegrityError]
    if psycopg is not None:
        errors.append(psycopg.IntegrityError)
    return tuple(errors)


@contextmanager
def conn():
    session = DatabaseSession()
    c = session.__enter__()
    try:
        yield c
        session.__exit__(None, None, None)
    except Exception:
        session.__exit__(*__import__("sys").exc_info())
        raise


def table_exists(c, table_name):
    if c.dialect == "postgres":
        row = c.execute("SELECT to_regclass(?) AS name", (table_name,)).fetchone()
        return row["name"] is not None
    row = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def create_users_table(c):
    if c.dialect == "postgres":
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
    else:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def create_bills_table(c):
    if c.dialect == "postgres":
        id_column = "id SERIAL PRIMARY KEY"
        amount_type = "DOUBLE PRECISION"
        created_at_type = "TIMESTAMPTZ"
    else:
        id_column = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        amount_type = "REAL"
        created_at_type = "TEXT"

    c.execute(
        f"""CREATE TABLE IF NOT EXISTS {BILLS_TABLE} (
            {id_column},
            user_id INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            amount {amount_type} NOT NULL,
            category TEXT NOT NULL,
            billing_frequency TEXT NOT NULL,
            next_payment_date TEXT NOT NULL,
            reminder_days INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Active',
            reminder_time TEXT NOT NULL DEFAULT '08:00',
            timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
            reminder_email TEXT,
            current_cycle_paid_at TEXT,
            last_reminder_sent_for_date TEXT,
            last_reminder_sent_at TEXT,
            last_reminder_failed_for_date TEXT,
            last_reminder_failed_at TEXT,
            last_reminder_error TEXT,
            created_at {created_at_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {created_at_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )"""
    )
    c.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{BILLS_TABLE}_user_date "
        f"ON {BILLS_TABLE}(user_id, next_payment_date)"
    )


def create_payment_occurrences_table(c):
    if c.dialect == "postgres":
        id_column = "id SERIAL PRIMARY KEY"
        amount_type = "DOUBLE PRECISION"
        created_at_type = "TIMESTAMPTZ"
    else:
        id_column = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        amount_type = "REAL"
        created_at_type = "TEXT"

    c.execute(
        f"""CREATE TABLE IF NOT EXISTS {PAYMENT_OCCURRENCES_TABLE} (
            {id_column},
            bill_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            service_name TEXT NOT NULL,
            amount {amount_type} NOT NULL,
            category TEXT NOT NULL,
            billing_frequency TEXT NOT NULL,
            payment_date TEXT NOT NULL,
            reminder_days INTEGER NOT NULL,
            reminder_time TEXT NOT NULL,
            timezone TEXT NOT NULL,
            reminder_email TEXT,
            payment_status TEXT NOT NULL,
            paid_at TEXT,
            reminder_status TEXT NOT NULL,
            reminder_sent_at TEXT,
            reminder_failed_at TEXT,
            reminder_error TEXT,
            created_at {created_at_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(bill_id) REFERENCES {BILLS_TABLE}(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )"""
    )
    c.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{PAYMENT_OCCURRENCES_TABLE}_bill_date "
        f"ON {PAYMENT_OCCURRENCES_TABLE}(bill_id, payment_date)"
    )


def create_reminder_occurrences_table(c):
    if c.dialect == "postgres":
        id_column = "id SERIAL PRIMARY KEY"
        created_at_type = "TIMESTAMPTZ"
    else:
        id_column = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        created_at_type = "TEXT"

    c.execute(
        f"""CREATE TABLE IF NOT EXISTS {REMINDER_OCCURRENCES_TABLE} (
            {id_column},
            bill_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            scheduled_datetime TEXT NOT NULL,
            timezone TEXT NOT NULL,
            recipient_email TEXT NOT NULL,
            status TEXT NOT NULL,
            sent_at TEXT,
            failed_at TEXT,
            failure_error TEXT,
            created_at {created_at_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at {created_at_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(bill_id) REFERENCES {BILLS_TABLE}(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(bill_id, payment_date)
        )"""
    )
    c.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{REMINDER_OCCURRENCES_TABLE}_status "
        f"ON {REMINDER_OCCURRENCES_TABLE}(status, scheduled_datetime)"
    )


def ensure_column(c, table_name, column_name, column_definition):
    if c.dialect == "postgres":
        c.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_name} {column_definition}"
        )
        return

    columns = {
        row["name"]
        for row in c.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password, stored_hash):
    try:
        algorithm, iterations, salt, expected = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (AttributeError, ValueError):
        return False


def ensure_legacy_user(c):
    email = "development@example.local"
    row = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        return row["id"]

    c.execute(
        "INSERT INTO users(full_name, email, password_hash) VALUES(?,?,?)",
        ("Development User", email, hash_password(secrets.token_urlsafe(24))),
    )
    return c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def migrate_legacy_bills(c):
    if table_exists(c, BILLS_TABLE):
        c.execute(
            f"UPDATE {BILLS_TABLE} SET status='Cancelled' WHERE status='Inactive'"
        )
        ensure_column(c, BILLS_TABLE, "last_reminder_sent_for_date", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_sent_at", "TEXT")
        ensure_column(
            c,
            BILLS_TABLE,
            "reminder_time",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_REMINDER_TIME}'",
        )
        ensure_column(
            c,
            BILLS_TABLE,
            "timezone",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'",
        )
        ensure_column(c, BILLS_TABLE, "reminder_email", "TEXT")
        ensure_column(c, BILLS_TABLE, "current_cycle_paid_at", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_failed_for_date", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_failed_at", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_error", "TEXT")
        ensure_column(c, BILLS_TABLE, "updated_at", "TEXT")
        c.execute(
            f"UPDATE {BILLS_TABLE} SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
        )
        create_payment_occurrences_table(c)
        create_reminder_occurrences_table(c)
        c.execute(
            f"""UPDATE {BILLS_TABLE}
            SET reminder_time=COALESCE(reminder_time, ?),
                timezone=COALESCE(timezone, ?)
            """,
            (DEFAULT_REMINDER_TIME, DEFAULT_TIMEZONE),
        )
        return

    create_bills_table(c)
    create_payment_occurrences_table(c)
    create_reminder_occurrences_table(c)
    if not table_exists(c, "bills"):
        return

    legacy_rows = c.execute("SELECT * FROM bills ORDER BY id").fetchall()
    if not legacy_rows:
        return

    user_id = ensure_legacy_user(c)
    for row in legacy_rows:
        status = "Cancelled" if row["status"] == "Inactive" else row["status"]
        c.execute(
            f"""INSERT INTO {BILLS_TABLE}(
                id,
                user_id,
                service_name,
                amount,
                category,
                billing_frequency,
                next_payment_date,
                reminder_days,
                status
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                row["id"],
                user_id,
                row["service_name"],
                row["amount"],
                row["category"],
                row["billing_frequency"],
                row["next_payment_date"],
                row["reminder_days"],
                status,
            ),
        )


def initialize_database():
    with conn() as c:
        create_users_table(c)
        migrate_legacy_bills(c)
        create_bills_table(c)
        ensure_column(c, BILLS_TABLE, "last_reminder_sent_for_date", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_sent_at", "TEXT")
        ensure_column(
            c,
            BILLS_TABLE,
            "reminder_time",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_REMINDER_TIME}'",
        )
        ensure_column(
            c,
            BILLS_TABLE,
            "timezone",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_TIMEZONE}'",
        )
        ensure_column(c, BILLS_TABLE, "reminder_email", "TEXT")
        ensure_column(c, BILLS_TABLE, "current_cycle_paid_at", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_failed_for_date", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_failed_at", "TEXT")
        ensure_column(c, BILLS_TABLE, "last_reminder_error", "TEXT")
        ensure_column(c, BILLS_TABLE, "updated_at", "TEXT")
        c.execute(
            f"UPDATE {BILLS_TABLE} SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
        )
        create_payment_occurrences_table(c)
        create_reminder_occurrences_table(c)


def normalize_email(email):
    return email.strip().lower()


def is_valid_email(email):
    return bool(EMAIL_PATTERN.match(normalize_email(email or "")))


def register_user(full_name, email, password, confirm_password):
    full_name = full_name.strip()
    email = normalize_email(email)

    if not full_name or not email or not password or not confirm_password:
        return False, "All registration fields are required."
    if not EMAIL_PATTERN.match(email):
        return False, "Enter a valid email address."
    if password != confirm_password:
        return False, "Passwords must match."

    try:
        with conn() as c:
            c.execute(
                "INSERT INTO users(full_name, email, password_hash) VALUES(?,?,?)",
                (full_name, email, hash_password(password)),
            )
        return True, "Registration successful. Please log in."
    except integrity_errors():
        return False, "An account with that email already exists."


def authenticate_user(email, password):
    email = normalize_email(email)
    if not email or not password:
        return None

    with conn() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

    if user and verify_password(password, user["password_hash"]):
        return user
    return None


def get_user(user_id):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def add_bill(
    user_id,
    name,
    amount,
    category,
    frequency,
    next_date,
    reminder,
    status,
    reminder_time=DEFAULT_REMINDER_TIME,
    timezone=DEFAULT_TIMEZONE,
    reminder_email=None,
):
    with conn() as c:
        c.execute(
            f"""INSERT INTO {BILLS_TABLE}(
                user_id,
                service_name,
                amount,
                category,
                billing_frequency,
                next_payment_date,
                reminder_days,
                status,
                reminder_time,
                timezone,
                reminder_email
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                user_id,
                name,
                amount,
                category,
                frequency,
                next_date.isoformat(),
                reminder,
                status,
                reminder_time,
                timezone,
                normalize_email(reminder_email) if reminder_email else None,
            ),
        )


def update_bill(
    user_id,
    bill_id,
    name,
    amount,
    category,
    frequency,
    next_date,
    reminder,
    status,
    reminder_time=DEFAULT_REMINDER_TIME,
    timezone=DEFAULT_TIMEZONE,
    reminder_email=None,
):
    with conn() as c:
        c.execute(
            f"""UPDATE {BILLS_TABLE}
            SET service_name=?,
                amount=?,
                category=?,
                billing_frequency=?,
                next_payment_date=?,
                reminder_days=?,
                status=?,
                reminder_time=?,
                timezone=?,
                reminder_email=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND user_id=?""",
            (
                name,
                amount,
                category,
                frequency,
                next_date.isoformat(),
                reminder,
                status,
                reminder_time,
                timezone,
                normalize_email(reminder_email) if reminder_email else None,
                bill_id,
                user_id,
            ),
        )


def delete_bill(user_id, bill_id):
    with conn() as c:
        c.execute(f"DELETE FROM {BILLS_TABLE} WHERE id=? AND user_id=?", (bill_id, user_id))


def get_bills(user_id):
    with conn() as c:
        return c.execute(
            f"SELECT * FROM {BILLS_TABLE} WHERE user_id=? ORDER BY next_payment_date",
            (user_id,),
        ).fetchall()


def get_bill(user_id, bill_id):
    with conn() as c:
        return c.execute(
            f"SELECT * FROM {BILLS_TABLE} WHERE id=? AND user_id=?",
            (bill_id, user_id),
        ).fetchone()


def get_payment_occurrences(user_id, bill_id=None):
    with conn() as c:
        if bill_id is None:
            return c.execute(
                f"""SELECT * FROM {PAYMENT_OCCURRENCES_TABLE}
                WHERE user_id=?
                ORDER BY payment_date DESC, id DESC""",
                (user_id,),
            ).fetchall()
        return c.execute(
            f"""SELECT * FROM {PAYMENT_OCCURRENCES_TABLE}
            WHERE user_id=? AND bill_id=?
            ORDER BY payment_date DESC, id DESC""",
            (user_id, bill_id),
        ).fetchall()


def get_reminder_occurrence(c, bill_id, payment_date):
    return c.execute(
        f"""SELECT * FROM {REMINDER_OCCURRENCES_TABLE}
        WHERE bill_id=? AND payment_date=?""",
        (bill_id, payment_date.isoformat()),
    ).fetchone()


def claim_reminder_occurrence(row, scheduled):
    payment_date = parse_due_date(row)
    now_text = datetime.now(tz=scheduled.tzinfo).isoformat(timespec="seconds")
    scheduled_text = scheduled.isoformat(timespec="seconds")

    with conn() as c:
        if c.dialect == "postgres":
            cursor = c.execute(
                f"""INSERT INTO {REMINDER_OCCURRENCES_TABLE}(
                    bill_id,
                    user_id,
                    payment_date,
                    scheduled_datetime,
                    timezone,
                    recipient_email,
                    status,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT (bill_id, payment_date) DO NOTHING""",
                (
                    row["id"],
                    row["user_id"],
                    payment_date.isoformat(),
                    scheduled_text,
                    row["timezone"],
                    row["effective_reminder_email"],
                    "Processing",
                    now_text,
                ),
            )
        else:
            cursor = c.execute(
                f"""INSERT OR IGNORE INTO {REMINDER_OCCURRENCES_TABLE}(
                    bill_id,
                    user_id,
                    payment_date,
                    scheduled_datetime,
                    timezone,
                    recipient_email,
                    status,
                    updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    row["id"],
                    row["user_id"],
                    payment_date.isoformat(),
                    scheduled_text,
                    row["timezone"],
                    row["effective_reminder_email"],
                    "Processing",
                    now_text,
                ),
            )
        if cursor.rowcount == 1:
            return True

        cursor = c.execute(
            f"""UPDATE {REMINDER_OCCURRENCES_TABLE}
            SET status='Processing',
                scheduled_datetime=?,
                timezone=?,
                recipient_email=?,
                failure_error=NULL,
                updated_at=?
            WHERE bill_id=?
              AND payment_date=?
              AND status IN ('Scheduled', 'Due', 'Failed')""",
            (
                scheduled_text,
                row["timezone"],
                row["effective_reminder_email"],
                now_text,
                row["id"],
                payment_date.isoformat(),
            ),
        )
        return cursor.rowcount == 1


def update_reminder_occurrence_sent(bill_id, payment_date, sent_at):
    with conn() as c:
        c.execute(
            f"""UPDATE {REMINDER_OCCURRENCES_TABLE}
            SET status='Sent',
                sent_at=?,
                failed_at=NULL,
                failure_error=NULL,
                updated_at=?
            WHERE bill_id=? AND payment_date=?""",
            (
                sent_at.isoformat(timespec="seconds"),
                sent_at.isoformat(timespec="seconds"),
                bill_id,
                payment_date.isoformat(),
            ),
        )


def update_reminder_occurrence_failed(bill_id, payment_date, failed_at, error):
    with conn() as c:
        c.execute(
            f"""UPDATE {REMINDER_OCCURRENCES_TABLE}
            SET status='Failed',
                failed_at=?,
                failure_error=?,
                updated_at=?
            WHERE bill_id=? AND payment_date=?""",
            (
                failed_at.isoformat(timespec="seconds"),
                str(error),
                failed_at.isoformat(timespec="seconds"),
                bill_id,
                payment_date.isoformat(),
            ),
        )


def add_months(due_date, months):
    month_index = due_date.month - 1 + months
    year = due_date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(due_date.day, monthrange(year, month)[1])
    return due_date.replace(year=year, month=month, day=day)


def next_occurrence_date(due_date, frequency):
    if frequency == "Weekly":
        return due_date + timedelta(days=7)
    if frequency == "Monthly":
        return add_months(due_date, 1)
    if frequency == "Yearly":
        return add_months(due_date, 12)
    return None


def is_recurring_frequency(frequency):
    return next_occurrence_date(date.today(), frequency) is not None


def monthly_equivalent(amount, frequency):
    yearly_amount = yearly_equivalent(amount, frequency)
    return yearly_amount / 12


def yearly_equivalent(amount, frequency):
    if frequency == "Monthly":
        return amount * 12
    if frequency == "Yearly":
        return amount
    if frequency == "Weekly":
        return amount * 52
    return amount * 12


def parse_due_date(row):
    return date.fromisoformat(row["next_payment_date"])


def row_paid_at(row):
    if "current_cycle_paid_at" in row.keys():
        return row["current_cycle_paid_at"]
    return None


def payment_status(row, today=None):
    if row["status"] == "Cancelled":
        return "Cancelled"
    if row_paid_at(row):
        return "Completed"

    today = today or date.today()
    due = parse_due_date(row)
    if due > today:
        return "Upcoming"
    if due == today:
        return "Due Today"
    return "Overdue"


def can_mark_current_cycle_paid(row, today=None):
    return payment_status(row, today=today) in ("Due Today", "Overdue")


def money(value):
    return f"${value:,.2f}"


def h(value):
    return html.escape(str(value))


def first_name(full_name):
    name = (full_name or "there").strip()
    return name.split()[0] if name else "there"


def greeting():
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def due_label(due_date):
    days = (due_date - date.today()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"
    if days > 1:
        return f"{days} days"
    return due_date.strftime("%b %d, %Y")


def payment_date_label(due_date):
    return due_date.strftime("%b %d, %Y")


def parse_reminder_time(value):
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if not value:
        return time(8, 0)
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt).time().replace(second=0, microsecond=0)
        except ValueError:
            continue
    return time(8, 0)


def format_reminder_time(value):
    return parse_reminder_time(value).strftime("%I:%M %p").lstrip("0")


def reminder_time_parts(value):
    parsed = parse_reminder_time(value)
    meridiem = "AM" if parsed.hour < 12 else "PM"
    hour = parsed.hour % 12
    if hour == 0:
        hour = 12
    return hour, parsed.minute, meridiem


def reminder_time_from_parts(hour, minute, meridiem):
    hour = int(hour)
    minute = int(minute)
    if meridiem == "AM":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return time(hour, minute)


def reminder_time_ampm_input(label, value, key_prefix):
    hour, minute, meridiem = reminder_time_parts(value)
    st.write(label)
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        selected_hour = st.selectbox(
            "Hour",
            list(range(1, 13)),
            index=hour - 1,
            format_func=lambda item: f"{item:02d}",
            key=f"{key_prefix}_hour",
        )
    with c2:
        selected_minute = st.selectbox(
            "Minute",
            list(range(60)),
            index=minute,
            format_func=lambda item: f"{item:02d}",
            key=f"{key_prefix}_minute",
        )
    with c3:
        selected_meridiem = st.selectbox(
            "AM/PM",
            ["AM", "PM"],
            index=0 if meridiem == "AM" else 1,
            key=f"{key_prefix}_meridiem",
        )
    st.caption("BillFlow will send your reminder at approximately this time in your selected timezone.")
    return reminder_time_from_parts(selected_hour, selected_minute, selected_meridiem)


def reminder_choice_from_days(days):
    days = int(days or 0)
    for label, option_days in REMINDER_OPTIONS.items():
        if days == option_days:
            return label
    return "Custom"


def resolved_reminder_days(choice, custom_days):
    if choice == "Custom":
        return int(custom_days)
    return REMINDER_OPTIONS[choice]


def timezone_options():
    common_values = [value for value, _ in COMMON_TIMEZONES]
    remaining = sorted(tz for tz in available_timezones() if tz not in common_values)
    return common_values + remaining


def timezone_display_name(timezone_name):
    return COMMON_TIMEZONE_LABELS.get(timezone_name, timezone_name.replace("_", " "))


def email_timezone_label(timezone_name):
    return timezone_display_name(timezone_name).split(" — ", 1)[0]


def timezone_index(timezone_name):
    options = timezone_options()
    return options.index(timezone_name) if timezone_name in options else options.index(DEFAULT_TIMEZONE)


def reminder_label(days):
    days = int(days or 0)
    if days == 0:
        return "On payment day"
    return f"{days} day before" if days == 1 else f"{days} days before"


def get_row_timezone(row):
    timezone_name = row["timezone"] if "timezone" in row.keys() else DEFAULT_TIMEZONE
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def scheduled_reminder_datetime(row):
    payment_date = parse_due_date(row)
    reminder_days = int(row["reminder_days"])
    reminder_date = payment_date - timedelta(days=reminder_days)
    reminder_time = parse_reminder_time(
        row["reminder_time"] if "reminder_time" in row.keys() else DEFAULT_REMINDER_TIME
    )
    return datetime.combine(reminder_date, reminder_time, tzinfo=get_row_timezone(row))


def scheduled_reminder_label(row):
    scheduled = scheduled_reminder_datetime(row)
    return f"{scheduled.strftime('%b %d, %Y')} at {scheduled.strftime('%I:%M %p').lstrip('0')}"


def resolved_reminder_email(row, user_email):
    if "reminder_email" in row.keys() and row["reminder_email"]:
        return row["reminder_email"]
    return user_email


def scheduled_preview(next_date, reminder_days, reminder_time_value, timezone_name):
    row = {
        "next_payment_date": next_date.isoformat(),
        "reminder_days": int(reminder_days),
        "reminder_time": reminder_time_value.strftime("%H:%M")
        if isinstance(reminder_time_value, time)
        else str(reminder_time_value),
        "timezone": timezone_name,
    }
    return scheduled_reminder_datetime(row)


def render_reminder_preview(next_date, reminder_days, reminder_time_value, timezone_name):
    scheduled = scheduled_preview(next_date, reminder_days, reminder_time_value, timezone_name)
    st.markdown(
        f"""
        <div class="reminder-preview">
            <div class="row-label">Reminder scheduled for</div>
            <div class="row-value service-name">
                {h(scheduled.strftime('%B %d, %Y'))} at {h(scheduled.strftime('%I:%M %p').lstrip('0'))}
            </div>
            <div class="section-note">{h(timezone_display_name(timezone_name))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def due_wording(service_name, amount, payment_date, today=None):
    today = today or date.today()
    days = (payment_date - today).days
    if days == 0:
        return f"Your {service_name} payment of {money(amount)} is due today."
    if days == 1:
        return f"Your {service_name} payment of {money(amount)} is due tomorrow on {payment_date.strftime('%B %d, %Y')}."
    if days > 1:
        return f"Your {service_name} payment of {money(amount)} is due in {days} days on {payment_date.strftime('%B %d, %Y')}."
    return f"Your {service_name} payment of {money(amount)} was due on {payment_date.strftime('%B %d, %Y')}."


def get_config_value(key):
    value = os.environ.get(key)
    if value:
        return value

    try:
        value = st.secrets.get(key)
    except Exception:
        value = None
    return value


def get_smtp_config():
    config = {
        "host": get_config_value("SMTP_HOST"),
        "port": get_config_value("SMTP_PORT"),
        "username": get_config_value("SMTP_USERNAME"),
        "password": get_config_value("SMTP_PASSWORD"),
        "from_email": get_config_value("SMTP_FROM_EMAIL"),
    }
    missing = [
        name
        for name, value in {
            "SMTP_HOST": config["host"],
            "SMTP_PORT": config["port"],
            "SMTP_USERNAME": config["username"],
            "SMTP_PASSWORD": config["password"],
            "SMTP_FROM_EMAIL": config["from_email"],
        }.items()
        if value in (None, "")
    ]
    if missing:
        raise ValueError(f"Missing email configuration: {', '.join(missing)}")

    try:
        config["port"] = int(config["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP_PORT must be a number.") from exc

    return config


def build_email_message(to_email, subject, body):
    config = get_smtp_config()
    message = EmailMessage()
    message["From"] = config["from_email"]
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    return message, config


def send_email_message(to_email, subject, body, smtp_factory=smtplib.SMTP):
    message, config = build_email_message(to_email, subject, body)
    with smtp_factory(config["host"], config["port"]) as smtp:
        smtp.starttls()
        smtp.login(config["username"], config["password"])
        smtp.send_message(message)


def send_reminder_email(
    to_email,
    user_name,
    service_name,
    amount,
    next_payment_date,
    billing_frequency,
    category,
    scheduled_datetime,
    smtp_factory=smtplib.SMTP,
):
    payment_date = date.fromisoformat(str(next_payment_date))
    scheduled_local = scheduled_datetime.astimezone(scheduled_datetime.tzinfo)
    days_until_due = (payment_date - scheduled_local.date()).days

    if days_until_due == 0:
        subject = f"Payment Due Today: {service_name} - {money(amount)}"
    elif days_until_due == 1:
        subject = f"Payment Due Tomorrow: {service_name} - {money(amount)}"
    else:
        subject = f"Payment Due in {days_until_due} Days: {service_name} - {money(amount)}"
    body = (
        f"Hello {user_name},\n\n"
        f"{due_wording(service_name, amount, payment_date, today=scheduled_local.date())}\n\n"
        "Payment Details\n\n"
        f"Service: {service_name}\n"
        f"Amount Due: {money(amount)}\n"
        f"Payment Date: {payment_date.strftime('%B %d, %Y')}\n"
        f"Billing Frequency: {billing_frequency}\n"
        f"Category: {category}\n\n"
        "This reminder was scheduled for "
        f"{scheduled_local.strftime('%B %d, %Y')} at "
        f"{scheduled_local.strftime('%I:%M %p').lstrip('0')} "
        f"{email_timezone_label(scheduled_local.tzinfo.key if hasattr(scheduled_local.tzinfo, 'key') else str(scheduled_local.tzinfo))}.\n\n"
        "BillFlow\n"
        "Your recurring payments, simplified.\n"
    )
    send_email_message(to_email, subject, body, smtp_factory=smtp_factory)


def send_test_email(user_id, smtp_factory=smtplib.SMTP):
    user = get_user(user_id)
    if not user:
        raise ValueError("User account was not found.")

    subject = "Bill & Subscription Manager Test Email"
    body = (
        f"Hello {user['full_name']},\n\n"
        "This is a test email from your Bill & Subscription Manager.\n\n"
        "If you received this message, your local SMTP configuration is working.\n"
    )
    send_email_message(user["email"], subject, body, smtp_factory=smtp_factory)


def send_bill_test_email(user_id, bill_id, smtp_factory=smtplib.SMTP):
    user = get_user(user_id)
    row = get_bill(user_id, bill_id)
    if not user:
        raise ValueError("User account was not found.")
    if not row:
        raise ValueError("Bill was not found.")

    send_reminder_email(
        resolved_reminder_email(row, user["email"]),
        user["full_name"],
        row["service_name"],
        row["amount"],
        row["next_payment_date"],
        row["billing_frequency"],
        row["category"],
        scheduled_reminder_datetime(row),
        smtp_factory=smtp_factory,
    )


def mark_reminder_sent(bill_id, payment_date, sent_at):
    update_reminder_occurrence_sent(bill_id, payment_date, sent_at)
    with conn() as c:
        c.execute(
            f"""UPDATE {BILLS_TABLE}
            SET last_reminder_sent_for_date=?,
                last_reminder_sent_at=?,
                last_reminder_failed_for_date=NULL,
                last_reminder_failed_at=NULL,
                last_reminder_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            (payment_date.isoformat(), sent_at.isoformat(timespec="seconds"), bill_id),
        )


def mark_reminder_failed(bill_id, payment_date, failed_at, error):
    update_reminder_occurrence_failed(bill_id, payment_date, failed_at, error)
    with conn() as c:
        c.execute(
            f"""UPDATE {BILLS_TABLE}
            SET last_reminder_failed_for_date=?,
                last_reminder_failed_at=?,
                last_reminder_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
            (
                payment_date.isoformat(),
                failed_at.isoformat(timespec="seconds"),
                str(error),
                bill_id,
            ),
        )


def get_active_reminder_candidates():
    with conn() as c:
        return c.execute(
            f"""SELECT
                b.*,
                u.full_name,
                u.email AS user_email,
                COALESCE(NULLIF(TRIM(b.reminder_email), ''), u.email) AS effective_reminder_email
            FROM {BILLS_TABLE} b
            JOIN users u ON u.id = b.user_id
            WHERE b.status='Active'
            ORDER BY b.next_payment_date"""
        ).fetchall()


def is_reminder_eligible(row, today=None):
    today = today or date.today()
    payment_date = date.fromisoformat(row["next_payment_date"])

    if row["status"] != "Active":
        return False
    if row_paid_at(row):
        return False
    if today < scheduled_reminder_datetime(row).date():
        return False
    if row["last_reminder_sent_for_date"] == payment_date.isoformat():
        return False
    return True


def is_reminder_due(row, now=None):
    scheduled = scheduled_reminder_datetime(row)
    if now is None:
        now = datetime.now(tz=scheduled.tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=scheduled.tzinfo)
    else:
        now = now.astimezone(scheduled.tzinfo)

    payment_date = parse_due_date(row)
    if row["status"] != "Active":
        return False
    if row_paid_at(row):
        return False
    if now < scheduled:
        return False
    if row["last_reminder_sent_for_date"] == payment_date.isoformat():
        return False
    return True


def process_due_reminders(now=None, send_email_func=send_reminder_email):
    rows = get_active_reminder_candidates()
    result = {"checked": len(rows), "eligible": 0, "sent": 0, "errors": []}

    for row in rows:
        scheduled = scheduled_reminder_datetime(row)
        effective_now = now
        if effective_now is None:
            effective_now = datetime.now(tz=scheduled.tzinfo)
        elif effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=scheduled.tzinfo)

        if not is_reminder_due(row, now=effective_now):
            continue

        payment_date = date.fromisoformat(row["next_payment_date"])
        if not claim_reminder_occurrence(row, scheduled):
            continue

        result["eligible"] += 1
        try:
            send_email_func(
                row["effective_reminder_email"],
                row["full_name"],
                row["service_name"],
                row["amount"],
                row["next_payment_date"],
                row["billing_frequency"],
                row["category"],
                scheduled,
            )
        except Exception as exc:
            mark_reminder_failed(
                row["id"],
                payment_date,
                datetime.now(tz=scheduled.tzinfo),
                f"{type(exc).__name__}: {exc}",
            )
            result["errors"].append(
                f"{row['service_name']}: {type(exc).__name__}: {exc}"
            )
            continue

        mark_reminder_sent(row["id"], payment_date, datetime.now(tz=scheduled.tzinfo))
        result["sent"] += 1

    return result


def check_due_reminders(now=None):
    return process_due_reminders(now=now)


def current_reminder_status(row, now=None):
    if row["status"] == "Cancelled":
        return "Cancelled"

    payment_date = parse_due_date(row)
    payment_date_value = payment_date.isoformat()
    if row["last_reminder_sent_for_date"] == payment_date_value:
        return "Sent"
    if (
        "last_reminder_failed_for_date" in row.keys()
        and row["last_reminder_failed_for_date"] == payment_date_value
    ):
        return "Failed"

    scheduled = scheduled_reminder_datetime(row)
    if now is None:
        now = datetime.now(tz=scheduled.tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=scheduled.tzinfo)
    else:
        now = now.astimezone(scheduled.tzinfo)

    if row_paid_at(row):
        return "Completed"
    if now >= scheduled:
        return "Due"
    return "Scheduled"


def insert_payment_occurrence(c, row, user_email, paid_at):
    payment_date = parse_due_date(row)
    c.execute(
        f"""INSERT INTO {PAYMENT_OCCURRENCES_TABLE}(
            bill_id,
            user_id,
            service_name,
            amount,
            category,
            billing_frequency,
            payment_date,
            reminder_days,
            reminder_time,
            timezone,
            reminder_email,
            payment_status,
            paid_at,
            reminder_status,
            reminder_sent_at,
            reminder_failed_at,
            reminder_error
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            row["id"],
            row["user_id"],
            row["service_name"],
            row["amount"],
            row["category"],
            row["billing_frequency"],
            payment_date.isoformat(),
            row["reminder_days"],
            row["reminder_time"] if "reminder_time" in row.keys() else DEFAULT_REMINDER_TIME,
            row["timezone"] if "timezone" in row.keys() else DEFAULT_TIMEZONE,
            resolved_reminder_email(row, user_email),
            "Completed",
            paid_at.isoformat(timespec="seconds"),
            current_reminder_status(row, now=paid_at),
            row["last_reminder_sent_at"],
            row["last_reminder_failed_at"] if "last_reminder_failed_at" in row.keys() else None,
            row["last_reminder_error"] if "last_reminder_error" in row.keys() else None,
        ),
    )


def mark_current_payment_paid(user_id, bill_id, now=None):
    with conn() as c:
        row = c.execute(
            f"""SELECT
                b.*,
                u.email AS user_email
            FROM {BILLS_TABLE} b
            JOIN users u ON u.id = b.user_id
            WHERE b.id=? AND b.user_id=?""",
            (bill_id, user_id),
        ).fetchone()
        if not row or not can_mark_current_cycle_paid(row):
            return None

        scheduled = scheduled_reminder_datetime(row)
        paid_at = now
        if paid_at is None:
            paid_at = datetime.now(tz=scheduled.tzinfo)
        elif paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=scheduled.tzinfo)
        else:
            paid_at = paid_at.astimezone(scheduled.tzinfo)

        insert_payment_occurrence(c, row, row["user_email"], paid_at)

        current_due = parse_due_date(row)
        next_due = next_occurrence_date(current_due, row["billing_frequency"])
        if next_due is None:
            c.execute(
                f"""UPDATE {BILLS_TABLE}
                SET current_cycle_paid_at=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND user_id=?""",
                (paid_at.isoformat(timespec="seconds"), bill_id, user_id),
            )
            return {"completed_date": current_due, "next_date": None}

        c.execute(
            f"""UPDATE {BILLS_TABLE}
            SET next_payment_date=?,
                current_cycle_paid_at=NULL,
                last_reminder_sent_for_date=NULL,
                last_reminder_sent_at=NULL,
                last_reminder_failed_for_date=NULL,
                last_reminder_failed_at=NULL,
                last_reminder_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND user_id=?""",
            (next_due.isoformat(), bill_id, user_id),
        )
        return {"completed_date": current_due, "next_date": next_due}


def apply_theme():
    st.markdown(
        """
        <style>
            :root {
                --bg: #ffffff;
                --text: #0a0a0a;
                --muted: #5f5f5f;
                --soft: #f7f7f7;
                --softer: #fafafa;
                --border: #e8e8e8;
                --border-strong: #cfcfcf;
                --panel: rgba(255, 255, 255, 0.82);
                --shadow: 0 18px 48px rgba(10, 10, 10, 0.08);
                --shadow-soft: 0 8px 24px rgba(10, 10, 10, 0.05);
            }

            .stApp {
                background:
                    radial-gradient(circle at 8% 12%, rgba(10, 10, 10, 0.055), transparent 30%),
                    radial-gradient(circle at 92% 4%, rgba(10, 10, 10, 0.045), transparent 28%),
                    linear-gradient(180deg, #ffffff 0%, #fbfbfb 100%);
                color: var(--text);
                font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }

            .stApp::before,
            .stApp::after {
                content: "";
                position: fixed;
                z-index: 0;
                pointer-events: none;
                border-radius: 38% 62% 62% 38% / 42% 34% 66% 58%;
                background: rgba(10, 10, 10, 0.035);
                filter: blur(24px);
            }

            .stApp::before {
                width: 360px;
                height: 240px;
                top: 84px;
                right: -120px;
            }

            .stApp::after {
                width: 300px;
                height: 220px;
                left: -110px;
                bottom: 60px;
            }

            section[data-testid="stSidebar"] {
                display: none;
            }

            .block-container {
                position: relative;
                z-index: 1;
                padding-top: 1.35rem;
                padding-bottom: 3rem;
                max-width: 1220px;
            }

            h1, h2, h3 {
                color: var(--text);
                letter-spacing: 0;
            }

            header[data-testid="stHeader"],
            div[data-testid="stToolbar"],
            div[data-testid="stDecoration"] {
                visibility: hidden;
                height: 0;
            }

            .app-shell {
                border: 1px solid rgba(232, 232, 232, 0.92);
                border-radius: 28px;
                background: rgba(255, 255, 255, 0.72);
                backdrop-filter: blur(22px);
                box-shadow: var(--shadow);
                padding: 1.1rem;
                margin-bottom: 1.25rem;
            }

            .topbar {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 1rem;
                border: 1px solid var(--border);
                border-radius: 22px;
                background: rgba(255, 255, 255, 0.86);
                padding: 0.75rem 0.9rem;
            }

            .brand {
                font-size: 1.2rem;
                font-weight: 700;
                letter-spacing: -0.01em;
            }

            .account-chip {
                text-align: right;
                color: var(--muted);
                font-size: 0.82rem;
                line-height: 1.35;
            }

            .account-chip strong {
                color: var(--text);
                font-weight: 650;
            }

            .auth-card,
            .form-card,
            .glass-panel {
                border: 1px solid var(--border);
                border-radius: 24px;
                background: var(--panel);
                backdrop-filter: blur(18px);
                box-shadow: var(--shadow-soft);
                padding: 1.4rem;
            }

            .auth-card {
                max-width: 520px;
                margin: 2.5rem auto 0;
            }

            .page-hero {
                margin: 1.2rem 0 1.2rem;
                padding: 1.2rem 0.2rem;
            }

            .page-kicker {
                color: var(--muted);
                font-size: 0.82rem;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                margin-bottom: 0.5rem;
            }

            .page-title {
                color: var(--text);
                font-size: clamp(2.0rem, 4vw, 3.6rem);
                line-height: 1.02;
                font-weight: 720;
                letter-spacing: 0;
                margin: 0;
            }

            .page-subtitle {
                color: var(--muted);
                font-size: 1.05rem;
                margin-top: 0.75rem;
                max-width: 620px;
            }

            .summary-card {
                position: relative;
                overflow: hidden;
                background: rgba(255, 255, 255, 0.82);
                border: 1px solid var(--border);
                border-radius: 22px;
                padding: 1.05rem 1.1rem;
                min-height: 126px;
                box-shadow: var(--shadow-soft);
                transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
            }

            .summary-card:hover,
            .bill-row:hover,
            .payment-row:hover,
            .reminder-row:hover,
            .category-row:hover {
                transform: translateY(-2px);
                border-color: var(--border-strong);
                box-shadow: var(--shadow);
            }

            .summary-card::after {
                content: "";
                position: absolute;
                width: 116px;
                height: 72px;
                right: -28px;
                bottom: -24px;
                border-radius: 48% 52% 64% 36% / 38% 48% 52% 62%;
                background: rgba(10, 10, 10, 0.045);
                filter: blur(4px);
            }

            .summary-label {
                color: var(--muted);
                font-size: 0.78rem;
                font-weight: 560;
                margin-bottom: 0.7rem;
                text-transform: uppercase;
                letter-spacing: 0.07em;
            }

            .summary-value {
                color: var(--text);
                font-size: 1.85rem;
                font-weight: 720;
                line-height: 1.2;
            }

            .section-heading {
                color: var(--text);
                font-size: 1.08rem;
                font-weight: 650;
                margin: 0;
            }

            .section-header {
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 1rem;
                margin: 1.65rem 0 0.8rem;
            }

            .section-note {
                color: var(--muted);
                font-size: 0.92rem;
                margin-top: 0.22rem;
            }

            .payment-row,
            .bill-row,
            .reminder-row,
            .category-row {
                display: grid;
                gap: 0.75rem;
                align-items: center;
                border: 1px solid var(--border);
                border-radius: 18px;
                background: rgba(255, 255, 255, 0.82);
                padding: 0.95rem 1rem;
                margin-bottom: 0.65rem;
                box-shadow: 0 4px 14px rgba(10, 10, 10, 0.035);
                transition: transform 180ms ease, box-shadow 180ms ease, border-color 180ms ease;
            }

            .payment-row {
                grid-template-columns: minmax(160px, 1.35fr) 0.8fr 1fr 1fr 0.8fr 0.8fr;
            }

            .bill-row {
                grid-template-columns: minmax(180px, 1.4fr) 0.7fr 0.9fr 0.95fr 0.95fr 0.95fr 0.75fr;
            }

            .reminder-row {
                grid-template-columns: minmax(170px, 1.2fr) repeat(3, minmax(145px, 1fr));
            }

            .category-row {
                grid-template-columns: minmax(150px, 1fr) 2fr 0.8fr;
            }

            .row-label {
                color: var(--muted);
                font-size: 0.72rem;
                text-transform: uppercase;
                letter-spacing: 0.06em;
                margin-bottom: 0.2rem;
            }

            .row-value {
                color: var(--text);
                font-size: 0.95rem;
                font-weight: 520;
                overflow-wrap: normal;
                word-break: normal;
            }

            .service-name {
                font-size: 1.0rem;
                font-weight: 650;
            }

            .status-pill,
            .mini-pill {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border: 1px solid var(--border-strong);
                border-radius: 999px;
                padding: 0.22rem 0.62rem;
                color: var(--text);
                background: rgba(255, 255, 255, 0.72);
                font-size: 0.78rem;
                font-weight: 560;
                white-space: nowrap;
            }

            .status-pill.cancelled {
                color: #5f5f5f;
                background: #f3f3f3;
                border-color: #e1e1e1;
            }

            .empty-state {
                border: 1px dashed var(--border-strong);
                border-radius: 22px;
                background: rgba(255, 255, 255, 0.68);
                padding: 1.4rem;
                color: var(--muted);
                text-align: center;
            }

            .category-track {
                height: 10px;
                border-radius: 999px;
                background: #eeeeee;
                overflow: hidden;
            }

            .category-fill {
                height: 100%;
                border-radius: 999px;
                background: #0a0a0a;
            }

            .dev-panel {
                border: 1px solid var(--border);
                border-radius: 22px;
                background: #fafafa;
                padding: 1.2rem;
                margin-top: 1rem;
            }

            .reminder-preview,
            .edit-panel {
                border: 1px solid var(--border);
                border-radius: 20px;
                background: rgba(255, 255, 255, 0.78);
                padding: 1rem;
                margin: 0.8rem 0 1rem;
                box-shadow: 0 4px 14px rgba(10, 10, 10, 0.035);
            }

            .stButton > button,
            .stFormSubmitButton > button {
                border-radius: 999px;
                border: 1px solid #0a0a0a;
                background: #0a0a0a;
                color: #ffffff;
                font-weight: 620;
                transition: background 160ms ease, color 160ms ease, border-color 160ms ease, transform 160ms ease;
            }

            .stButton > button:hover,
            .stFormSubmitButton > button:hover {
                background: #2a2a2a;
                border-color: #2a2a2a;
                color: #ffffff;
                transform: translateY(-1px);
            }

            .stButton > button:disabled {
                background: #f1f1f1;
                color: #9a9a9a;
                border-color: #dedede;
            }

            div[data-testid="stForm"] {
                border: 1px solid var(--border);
                border-radius: 22px;
                background: rgba(255, 255, 255, 0.74);
                padding: 1rem;
                width: 100%;
            }

            div[data-testid="stDataFrame"] {
                border: 1px solid var(--border);
                border-radius: 18px;
                overflow: hidden;
            }

            div[data-testid="stExpander"] {
                border: 1px solid var(--border);
                border-radius: 18px;
                background: rgba(255, 255, 255, 0.7);
            }

            .stTextInput input,
            .stNumberInput input,
            .stDateInput input,
            div[data-baseweb="select"] > div {
                border-radius: 14px;
                border-color: var(--border);
            }

            label,
            div[data-testid="stWidgetLabel"] p {
                white-space: normal;
                word-break: normal;
                overflow-wrap: normal;
            }

            .stButton > button,
            .stFormSubmitButton > button {
                min-height: 2.4rem;
                white-space: nowrap;
            }

            @media (max-width: 900px) {
                .topbar {
                    align-items: flex-start;
                    flex-direction: column;
                }

                .account-chip {
                    text-align: left;
                }

                .payment-row,
                .bill-row,
                .reminder-row,
                .category-row {
                    grid-template-columns: 1fr;
                }

                .page-title {
                    font-size: 2.1rem;
                }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header():
    st.markdown(
        """
        <div class="app-shell">
            <div class="topbar">
                <div class="brand">BillFlow</div>
                <div class="account-chip">Recurring payments, reminders, and subscriptions</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_app_shell(user):
    st.markdown(
        f"""
        <div class="app-shell">
            <div class="topbar">
                <div class="brand">BillFlow</div>
                <div class="account-chip">
                    <strong>{h(user['full_name'])}</strong><br>
                    {h(user['email'])}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_hero(title, subtitle, kicker=None):
    kicker_html = f'<div class="page-kicker">{h(kicker)}</div>' if kicker else ""
    st.markdown(
        f"""
        <div class="page-hero">
            {kicker_html}
            <h1 class="page-title">{h(title)}</h1>
            <div class="page-subtitle">{h(subtitle)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_header(title, note=None):
    note_html = f'<div class="section-note">{h(note)}</div>' if note else ""
    st.markdown(
        f"""
        <div class="section-header">
            <div>
                <div class="section-heading">{h(title)}</div>
                {note_html}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_empty_state(message):
    st.markdown(f'<div class="empty-state">{h(message)}</div>', unsafe_allow_html=True)


def set_flash(message):
    st.session_state.flash_message = message


def show_flash():
    message = st.session_state.pop("flash_message", None)
    if message:
        st.success(message)


def render_summary_card(label, value):
    st.markdown(
        f"""
        <div class="summary-card">
            <div class="summary-label">{label}</div>
            <div class="summary-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_login():
    st.markdown('<div class="auth-card">', unsafe_allow_html=True)
    render_page_hero("Welcome back", "Sign in to manage your recurring payments.", "Account")
    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
    st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        user = authenticate_user(email, password)
        if user:
            st.session_state.user_id = user["id"]
            st.session_state.user_name = user["full_name"]
            st.rerun()
        st.error("Incorrect email or password.")


def show_register():
    st.markdown('<div class="auth-card">', unsafe_allow_html=True)
    render_page_hero("Create account", "Start with a private workspace for your own bills.", "Account")
    with st.form("register_form"):
        full_name = st.text_input("Full Name")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        confirm_password = st.text_input("Confirm Password", type="password")
        submitted = st.form_submit_button("Create Account")
    st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        success, message = register_user(full_name, email, password, confirm_password)
        if success:
            st.success(message)
        else:
            st.error(message)


def show_auth():
    auth_screen = st.radio(
        "Account",
        ["Login", "Register"],
        horizontal=True,
        key="auth_screen",
    )
    if auth_screen == "Register":
        show_register()
    else:
        show_login()


def logout():
    for key in ("user_id", "user_name"):
        st.session_state.pop(key, None)
    st.session_state.auth_screen = "Login"
    st.rerun()


def active_rows(rows):
    return [r for r in rows if r["status"] == "Active"]


def calculate_expenses(rows):
    active = active_rows(rows)
    monthly_total = sum(
        monthly_equivalent(r["amount"], r["billing_frequency"]) for r in active
    )
    yearly_total = sum(
        yearly_equivalent(r["amount"], r["billing_frequency"]) for r in active
    )
    upcoming_count = sum(1 for r in active if parse_due_date(r) >= date.today())

    return {
        "active_count": len(active),
        "monthly_total": monthly_total,
        "yearly_total": yearly_total,
        "upcoming_count": upcoming_count,
    }


def upcoming_payments_table(rows):
    today = date.today()
    data = []

    for r in active_rows(rows):
        due = parse_due_date(r)
        if due >= today:
            data.append(
                {
                    "Service": r["service_name"],
                    "Amount": money(r["amount"]),
                    "Category": r["category"],
                    "Next Payment": due.strftime("%b %d, %Y"),
                    "Days Left": (due - today).days,
                }
            )

    return data


def category_chart_data(rows):
    totals = {}

    for r in active_rows(rows):
        category = r["category"]
        amount = monthly_equivalent(r["amount"], r["billing_frequency"])
        totals[category] = totals.get(category, 0) + amount

    return pd.DataFrame(
        [
            {"Category": category, "Estimated Monthly Cost": round(amount, 2)}
            for category, amount in sorted(totals.items())
        ]
    )


def status_pill(status):
    css_class = "cancelled" if status == "Cancelled" else "active"
    return f'<span class="status-pill {css_class}">{h(status)}</span>'


def render_payment_row(row):
    due = parse_due_date(row)
    st.markdown(
        f"""
        <div class="payment-row">
            <div>
                <div class="row-label">Service</div>
                <div class="row-value service-name">{h(row['service_name'])}</div>
            </div>
            <div>
                <div class="row-label">Amount</div>
                <div class="row-value">{money(row['amount'])}</div>
            </div>
            <div>
                <div class="row-label">Category</div>
                <div class="row-value">{h(row['category'])}</div>
            </div>
            <div>
                <div class="row-label">Next Payment</div>
                <div class="row-value">{h(payment_date_label(due))}</div>
            </div>
            <div>
                <div class="row-label">Days Left</div>
                <div class="row-value">{h(due_label(due))}</div>
            </div>
            <div>
                <div class="row-label">Status</div>
                <div class="row-value">{status_pill(row['status'])}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_category_row(category, amount, max_amount):
    width = 0 if max_amount <= 0 else max(4, min(100, amount / max_amount * 100))
    st.markdown(
        f"""
        <div class="category-row">
            <div>
                <div class="row-label">Category</div>
                <div class="row-value">{h(category)}</div>
            </div>
            <div class="category-track">
                <div class="category-fill" style="width: {width:.1f}%"></div>
            </div>
            <div>
                <div class="row-label">Monthly</div>
                <div class="row-value">{money(amount)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_dashboard(rows, user):
    metrics = calculate_expenses(rows)
    render_page_hero(
        f"{greeting()}, {first_name(user['full_name'])}",
        "Your recurring payments, simplified.",
        "Dashboard",
    )

    cols = st.columns(4)
    with cols[0]:
        render_summary_card("Monthly Spend", money(metrics["monthly_total"]))
    with cols[1]:
        render_summary_card("Yearly Spend", money(metrics["yearly_total"]))
    with cols[2]:
        render_summary_card("Active Bills", f"{metrics['active_count']:,}")
    with cols[3]:
        render_summary_card("Upcoming Payments", f"{metrics['upcoming_count']:,}")

    header_cols = st.columns([1, 0.2])
    with header_cols[0]:
        render_section_header(
            "Upcoming Payments",
            "Active payments that are due today or later.",
        )
    with header_cols[1]:
        if st.button("+ Add Bill", key="dashboard_add_bill", use_container_width=True):
            st.session_state.current_page = "Bills"
            st.session_state.bill_mode = "Add"
            st.rerun()

    upcoming_rows = [r for r in active_rows(rows) if parse_due_date(r) >= date.today()]
    if upcoming_rows:
        for row in upcoming_rows:
            render_payment_row(row)
    else:
        render_empty_state("No upcoming active bills or subscriptions are stored yet.")

    render_section_header(
        "Spending Overview",
        "Estimated monthly spending grouped by category.",
    )
    chart_df = category_chart_data(rows)
    if not chart_df.empty:
        max_amount = chart_df["Estimated Monthly Cost"].max()
        for item in chart_df.to_dict("records"):
            render_category_row(
                item["Category"],
                item["Estimated Monthly Cost"],
                max_amount,
            )
    else:
        render_empty_state("Add active bills or subscriptions to build a spending overview.")


def show_add_bill(user_id):
    user = get_user(user_id)
    default_reminder_email = user["email"] if user else ""
    render_page_hero(
        "Add a Bill or Subscription",
        "Create a recurring payment record with reminder timing and status.",
        "Bills",
    )

    st.markdown('<div class="form-card">', unsafe_allow_html=True)
    name = st.text_input("Service Name", placeholder="Electricity bill", key="add_service_name")

    c1, c2 = st.columns(2)
    with c1:
        amount = st.number_input(
            "Amount",
            min_value=0.01,
            step=1.0,
            format="%.2f",
            key="add_amount",
        )
        category = st.selectbox("Category", CATEGORIES, key="add_category")
        frequency = st.selectbox("Billing Frequency", FREQUENCIES, key="add_frequency")
    with c2:
        next_date = st.date_input("Next Payment Date", min_value=date.today(), key="add_next_date")
        reminder_choice = st.selectbox(
            "Reminder",
            list(REMINDER_OPTIONS.keys()) + ["Custom"],
            index=2,
            key="add_reminder_choice",
        )
        custom_days = 0
        if reminder_choice == "Custom":
            custom_days = st.number_input(
                "Custom Days Before",
                min_value=0,
                max_value=365,
                step=1,
                value=2,
                key="add_custom_days",
            )
        selected_reminder_time = reminder_time_ampm_input(
            "Reminder Time",
            value=parse_reminder_time(DEFAULT_REMINDER_TIME),
            key_prefix="add_reminder_time",
        )
        selected_timezone = st.selectbox(
            "Timezone",
            timezone_options(),
            index=timezone_index(DEFAULT_TIMEZONE),
            format_func=timezone_display_name,
            key="add_timezone",
        )
        reminder_email = st.text_input(
            "Reminder Email",
            value=default_reminder_email,
            help="BillFlow will send this bill's reminder to this email address.",
            key="add_reminder_email",
        )
        status = st.selectbox("Status", STATUSES, key="add_status")

    reminder_days = resolved_reminder_days(reminder_choice, custom_days)
    render_reminder_preview(next_date, reminder_days, selected_reminder_time, selected_timezone)

    if st.button("Add Bill", key="add_bill_submit", use_container_width=False):
        normalized_reminder_email = normalize_email(reminder_email)
        if not name.strip():
            st.error("Service Name is required.")
        elif not is_valid_email(normalized_reminder_email):
            st.error("Enter a valid reminder email address.")
        else:
            add_bill(
                user_id,
                name.strip(),
                amount,
                category,
                frequency,
                next_date,
                reminder_days,
                status,
                selected_reminder_time.strftime("%H:%M"),
                selected_timezone,
                normalized_reminder_email,
            )
            set_flash(f"{name.strip()} saved successfully.")
            st.session_state.bill_mode = "View"
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def option_index(options, value, default=0):
    return options.index(value) if value in options else default


def toggle_bill_status(user_id, row):
    new_status = "Active" if row["status"] == "Cancelled" else "Cancelled"
    update_bill(
        user_id,
        row["id"],
        row["service_name"],
        row["amount"],
        row["category"],
        row["billing_frequency"],
        parse_due_date(row),
        row["reminder_days"],
        new_status,
        row["reminder_time"] if "reminder_time" in row.keys() else DEFAULT_REMINDER_TIME,
        row["timezone"] if "timezone" in row.keys() else DEFAULT_TIMEZONE,
        row["reminder_email"] if "reminder_email" in row.keys() else None,
    )


def show_edit_form(user_id, row):
    user = get_user(user_id)
    current_reminder_email = resolved_reminder_email(row, user["email"] if user else "")
    st.markdown('<div class="edit-panel">', unsafe_allow_html=True)
    current_choice = reminder_choice_from_days(row["reminder_days"])
    with st.form(f"edit_bill_form_{row['id']}", clear_on_submit=False):
        name = st.text_input(
            "Service Name",
            value=row["service_name"],
            key=f"edit_name_{row['id']}",
        )

        r1c1, r1c2 = st.columns(2)
        with r1c1:
            amount = st.number_input(
                "Amount",
                min_value=0.01,
                step=1.0,
                value=float(row["amount"]),
                format="%.2f",
                key=f"edit_amount_{row['id']}",
            )
            category = st.selectbox(
                "Category",
                CATEGORIES,
                index=option_index(CATEGORIES, row["category"], len(CATEGORIES) - 1),
                key=f"edit_category_{row['id']}",
            )
            frequency = st.selectbox(
                "Billing Frequency",
                FREQUENCIES,
                index=option_index(FREQUENCIES, row["billing_frequency"]),
                key=f"edit_frequency_{row['id']}",
            )
        with r1c2:
            next_date = st.date_input(
                "Next Payment Date",
                value=parse_due_date(row),
                key=f"edit_next_date_{row['id']}",
            )
            selected_reminder_time = reminder_time_ampm_input(
                "Reminder Time",
                value=parse_reminder_time(
                    row["reminder_time"]
                    if "reminder_time" in row.keys()
                    else DEFAULT_REMINDER_TIME
                ),
                key_prefix=f"edit_reminder_time_{row['id']}",
            )
            status = st.selectbox(
                "Status",
                STATUSES,
                index=option_index(STATUSES, row["status"]),
                key=f"edit_status_{row['id']}",
            )

        reminder_choice = st.selectbox(
            "Reminder",
            list(REMINDER_OPTIONS.keys()) + ["Custom"],
            index=option_index(
                list(REMINDER_OPTIONS.keys()) + ["Custom"], current_choice, 2
            ),
            key=f"edit_reminder_choice_{row['id']}",
        )
        custom_days = int(row["reminder_days"])
        if reminder_choice == "Custom":
            custom_days = st.number_input(
                "Custom Days Before",
                min_value=0,
                max_value=365,
                step=1,
                value=int(row["reminder_days"]),
                key=f"edit_custom_days_{row['id']}",
            )

        selected_timezone = st.selectbox(
            "Timezone",
            timezone_options(),
            index=timezone_index(row["timezone"] if "timezone" in row.keys() else DEFAULT_TIMEZONE),
            format_func=timezone_display_name,
            key=f"edit_timezone_{row['id']}",
        )
        reminder_email = st.text_input(
            "Reminder Email",
            value=current_reminder_email,
            help="BillFlow will send this bill's reminder to this email address.",
            key=f"edit_reminder_email_{row['id']}",
        )

        reminder_days = resolved_reminder_days(reminder_choice, custom_days)
        render_reminder_preview(next_date, reminder_days, selected_reminder_time, selected_timezone)

        c1, c2, _ = st.columns([1.2, 1, 4])
        with c1:
            save = st.form_submit_button("Save Changes", use_container_width=True)
        with c2:
            close = st.form_submit_button("Close", use_container_width=True)

    if close:
        st.session_state.edit_bill_id = None
        st.rerun()
    if save:
        normalized_reminder_email = normalize_email(reminder_email)
        if not name.strip():
            st.error("Service Name is required.")
        elif not is_valid_email(normalized_reminder_email):
            st.error("Enter a valid reminder email address.")
        else:
            update_bill(
                user_id,
                row["id"],
                name.strip(),
                amount,
                category,
                frequency,
                next_date,
                reminder_days,
                status,
                selected_reminder_time.strftime("%H:%M"),
                selected_timezone,
                normalized_reminder_email,
            )
            set_flash(f"{name.strip()} updated.")
            st.session_state.edit_bill_id = None
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def show_manage_bills(user_id, rows):
    render_page_hero(
        "Bills",
        "Review, edit, cancel, reactivate, or delete your recurring records.",
        "Workspace",
    )
    show_flash()

    view_cols = st.columns([1, 6])
    with view_cols[0]:
        if st.button("+ Add Bill", key="bills_mode_add", use_container_width=True):
            st.session_state.bill_mode = "Add"
            st.session_state.edit_bill_id = None
            st.rerun()

    if st.session_state.get("bill_mode") == "Add":
        show_add_bill(user_id)
        return

    render_section_header("Current Bills", "All records belong only to your account.")

    if not rows:
        render_empty_state("No stored bills or subscriptions are available to manage.")
        return

    for r in rows:
        due = parse_due_date(r)
        st.markdown(
            f"""
            <div class="bill-row">
                <div>
                    <div class="row-label">Service</div>
                    <div class="row-value service-name">{h(r['service_name'])}</div>
                </div>
                <div>
                    <div class="row-label">Amount</div>
                    <div class="row-value">{money(r['amount'])}</div>
                </div>
                <div>
                    <div class="row-label">Frequency</div>
                    <div class="row-value">{h(r['billing_frequency'])}</div>
                </div>
                <div>
                    <div class="row-label">Category</div>
                    <div class="row-value">{h(r['category'])}</div>
                </div>
                <div>
                    <div class="row-label">Next payment</div>
                    <div class="row-value">{h(payment_date_label(due))}</div>
                </div>
                <div>
                    <div class="row-label">Reminder</div>
                    <div class="row-value">{h(reminder_label(r['reminder_days']))}</div>
                </div>
                <div>
                    <div class="row-label">Status</div>
                    <div class="row-value">{status_pill(r['status'])}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(f"Payment Status: {payment_status(r)}")

        show_paid_action = can_mark_current_cycle_paid(r)
        action_cols = st.columns([0.9, 1.2, 1.1, 1.3, 4.0])
        with action_cols[0]:
            if st.button("Edit", key=f"edit_{r['id']}", use_container_width=True):
                st.session_state.edit_bill_id = r["id"]
                st.rerun()
        with action_cols[1]:
            if show_paid_action:
                if st.button("Mark as Paid", key=f"paid_{r['id']}", use_container_width=True):
                    result = mark_current_payment_paid(user_id, r["id"])
                    if result and result["next_date"]:
                        set_flash(
                            f"{r['service_name']} marked paid. Next payment: {payment_date_label(result['next_date'])}."
                        )
                    elif result:
                        set_flash(f"{r['service_name']} marked paid.")
                    else:
                        set_flash(f"{r['service_name']} was not eligible to mark paid.")
                    st.rerun()
        with action_cols[2]:
            toggle_label = "Reactivate" if r["status"] == "Cancelled" else "Cancel"
            if st.button(toggle_label, key=f"toggle_{r['id']}", use_container_width=True):
                toggle_bill_status(user_id, r)
                set_flash(f"{r['service_name']} marked as {'Active' if r['status'] == 'Cancelled' else 'Cancelled'}.")
                st.rerun()
        with action_cols[3]:
            confirm_delete = st.checkbox("Confirm delete", key=f"confirm_delete_{r['id']}")
            if st.button(
                "Delete",
                key=f"del_{r['id']}",
                use_container_width=True,
                disabled=not confirm_delete,
            ):
                delete_bill(user_id, r["id"])
                set_flash(f"{r['service_name']} deleted.")
                st.rerun()
        if st.session_state.get("edit_bill_id") == r["id"]:
            show_edit_form(user_id, r)
        st.divider()


def reminder_status(row):
    return current_reminder_status(row)


def render_reminder_row(row, user_email):
    payment_date = parse_due_date(row)
    st.markdown(
        f"""
        <div class="reminder-row">
            <div>
                <div class="row-label">Service</div>
                <div class="row-value service-name">{h(row['service_name'])}</div>
            </div>
            <div>
                <div class="row-label">Amount Due</div>
                <div class="row-value">{money(row['amount'])}</div>
            </div>
            <div>
                <div class="row-label">Payment Date</div>
                <div class="row-value">{h(payment_date_label(payment_date))}</div>
            </div>
            <div>
                <div class="row-label">Reminder</div>
                <div class="row-value">{h(reminder_label(row['reminder_days']))}</div>
            </div>
            <div>
                <div class="row-label">Reminder Time</div>
                <div class="row-value">{h(format_reminder_time(row['reminder_time'] if 'reminder_time' in row.keys() else DEFAULT_REMINDER_TIME))}</div>
            </div>
            <div>
                <div class="row-label">Timezone</div>
                <div class="row-value">{h(timezone_display_name(row['timezone'] if 'timezone' in row.keys() else DEFAULT_TIMEZONE))}</div>
            </div>
            <div>
                <div class="row-label">Scheduled For</div>
                <div class="row-value">{h(scheduled_reminder_label(row))}</div>
            </div>
            <div>
                <div class="row-label">Reminder Email</div>
                <div class="row-value">{h(resolved_reminder_email(row, user_email))}</div>
            </div>
            <div>
                <div class="row-label">Reminder Status</div>
                <div class="row-value"><span class="mini-pill">{h(reminder_status(row))}</span></div>
            </div>
            <div>
                <div class="row-label">Last Sent</div>
                <div class="row-value">{h(row['last_reminder_sent_at'] or '—')}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def show_email_reminders(user_id, rows):
    user = get_user(user_id)
    user_email = user["email"] if user else ""
    render_page_hero(
        "Reminders",
        "Review upcoming reminder timing and delivery details.",
        "Notifications",
    )

    render_section_header(
        "Upcoming Reminders",
        "Reminder dates are calculated from each bill's next payment date and preference.",
    )
    reminder_rows = sorted(rows, key=lambda r: parse_due_date(r))
    if reminder_rows:
        for row in reminder_rows:
            render_reminder_row(row, user_email)
    else:
        render_empty_state("No reminders are available until you add a bill or subscription.")


def render_navigation():
    if "current_page" not in st.session_state:
        st.session_state.current_page = "Dashboard"

    nav_items = ["Dashboard", "Bills", "Reminders", "Account"]
    cols = st.columns(len(nav_items))
    for col, item in zip(cols, nav_items):
        label = item if st.session_state.current_page != item else f"{item}"
        with col:
            if st.button(label, key=f"nav_{item}", use_container_width=True):
                st.session_state.current_page = item
                if item == "Bills" and "bill_mode" not in st.session_state:
                    st.session_state.bill_mode = "View"
                st.rerun()


def show_account(user):
    render_page_hero(
        "Account",
        "Your profile for this local BillFlow workspace.",
        "Profile",
    )
    st.markdown(
        f"""
        <div class="glass-panel">
            <div class="row-label">User name</div>
            <div class="row-value service-name">{h(user['full_name'])}</div>
            <br>
            <div class="row-label">Registered email</div>
            <div class="row-value">{h(user['email'])}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button("Logout", use_container_width=False):
        logout()


def show_authenticated_app():
    user_id = st.session_state.user_id
    user = get_user(user_id)
    if not user:
        logout()
        return

    render_app_shell(user)
    render_navigation()
    rows = get_bills(user_id)
    page = st.session_state.current_page

    if page == "Dashboard":
        show_dashboard(rows, user)
    elif page == "Bills":
        show_manage_bills(user_id, rows)
    elif page == "Reminders":
        show_email_reminders(user_id, rows)
    else:
        show_account(user)


def main():
    st.set_page_config(page_title="Bill & Subscription Manager", layout="wide")
    initialize_database()
    apply_theme()

    if "user_id" not in st.session_state:
        render_header()
        show_auth()
    else:
        show_authenticated_app()


if __name__ == "__main__":
    main()
