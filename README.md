# BillFlow

BillFlow is a Streamlit bill and subscription manager with user accounts, recurring bill lifecycle tracking, reminder scheduling, payment history, and email reminders.

## Deployment Target

Required web deployment path:

```text
GitHub repository under dvdr998
        -> Streamlit Community Cloud
        -> app.py
        -> cloud-accessible persistent database
```

`app.py` is the Streamlit Community Cloud entry point.

`reminder_worker.py` is intentionally separate from the Streamlit UI. Streamlit Community Cloud does not run this worker every 5 minutes. The repository includes `.github/workflows/reminder-worker.yml` as an independent scheduled execution option through GitHub Actions.

## Configuration

BillFlow uses local SQLite (`bills.db`) by default. This is suitable for local or single-instance use.

Optional SMTP secrets for sending test emails from the app:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`

Do not commit `.streamlit/secrets.toml`, `.env`, Gmail App Passwords, or database credentials.

Set `BILLFLOW_DB` to use a different local SQLite file.

## Streamlit Community Cloud Setup

1. Push this repository to GitHub under `dvdr998`.
2. In Streamlit Community Cloud, create a new app from the GitHub repository.
3. Set the main file path to `app.py`.
4. Add the production secrets listed above in the Streamlit app secrets manager.
5. Use a cloud database that accepts connections from Streamlit Community Cloud and GitHub Actions.

## Reminder Worker Setup

Automated cloud reminders are disabled in the SQLite-only configuration because a separate worker cannot access the app's local database file.

This repository includes a GitHub Actions workflow:

```text
.github/workflows/reminder-worker.yml
```

The workflow is retained for manual development use only and is not scheduled.

Configure SMTP secrets in the GitHub repository only if you use the worker against a database explicitly configured for that environment:

`Settings -> Secrets and variables -> Actions -> Repository secrets`

The worker uses database-backed duplicate protection in `reminder_occurrences`, so repeated scheduler runs do not send the same reminder occurrence more than once.

## Local Development

```bash
pip install -r requirements.txt
streamlit run app.py
```

Run the worker locally:

```bash
python3 reminder_worker.py
```

Run tests:

```bash
python3 -m py_compile app.py reminder_worker.py test_app_reminders.py
python3 -m unittest test_app_reminders.py
```
