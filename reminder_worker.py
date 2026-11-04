import app


def main():
    if not app.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is required for the reminder worker; "
            "refusing to use local SQLite in GitHub Actions."
        )
    app.initialize_database()
    result = app.process_due_reminders()

    print(f"Records checked: {result['checked']}")
    print(f"Reminders due: {result['eligible']}")
    print(f"Emails successfully sent: {result['sent']}")
    print(f"Failed: {len(result['errors'])}")


if __name__ == "__main__":
    main()
