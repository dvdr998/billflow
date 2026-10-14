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

Production must use a persistent cloud database. Set `DATABASE_URL` in Streamlit Community Cloud secrets and in the independent worker scheduler secrets.

Required secrets:

- `DATABASE_URL`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM_EMAIL`

Do not commit `.streamlit/secrets.toml`, `.env`, Gmail App Passwords, or database credentials.

For local development only, BillFlow falls back to `bills.db` if `DATABASE_URL` is not configured.

## Streamlit Community Cloud Setup

1. Push this repository to GitHub under `dvdr998`.
2. In Streamlit Community Cloud, create a new app from the GitHub repository.
3. Set the main file path to `app.py`.
4. Add the production secrets listed above in the Streamlit app secrets manager.
5. Use a cloud database that accepts connections from Streamlit Community Cloud and GitHub Actions.

## Reminder Worker Setup

The web app does not need to stay open for reminders, but the worker must be scheduled separately.

This repository includes a GitHub Actions workflow:

```text
.github/workflows/reminder-worker.yml
```

It runs `python reminder_worker.py` every 5 minutes and also supports manual `workflow_dispatch`.

Configure the same production secrets in the GitHub repository:

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
