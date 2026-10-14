CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bills_subscriptions (
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
  reminder_email TEXT,
  current_cycle_paid_at TEXT,
  last_reminder_sent_for_date TEXT,
  last_reminder_sent_at TEXT,
  last_reminder_failed_for_date TEXT,
  last_reminder_failed_at TEXT,
  last_reminder_error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_bills_subscriptions_user_date
ON bills_subscriptions(user_id, next_payment_date);

CREATE TABLE payment_occurrences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bill_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  service_name TEXT NOT NULL,
  amount REAL NOT NULL,
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(bill_id) REFERENCES bills_subscriptions(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_payment_occurrences_bill_date
ON payment_occurrences(bill_id, payment_date);

CREATE TABLE reminder_occurrences (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(bill_id) REFERENCES bills_subscriptions(id) ON DELETE CASCADE,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE(bill_id, payment_date)
);

CREATE INDEX idx_reminder_occurrences_status
ON reminder_occurrences(status, scheduled_datetime);
