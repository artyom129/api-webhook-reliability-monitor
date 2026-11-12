# Upwork Portfolio Entry

## Title

API & Webhook Monitoring Dashboard with Automated Retry System

## Short description

Built a FastAPI service that receives webhooks, validates them, stores the complete request history, forwards payloads to external APIs, retries failed deliveries with exponential backoff, and exports incident data to CSV and Excel.

The dashboard shows delivery rate, events that need attention, and an hourly chart of the last 24 hours. Any stored event can be replayed with one click, and a REST tester helps reproduce failing calls.

## Key capabilities

- Dynamic webhook receiver endpoints with JSON and shared-secret validation
- Shared secret is redacted before storage and never forwarded to the target
- Complete headers, body, query, and response logging
- Automatic retries with doubling delay, manual replay, and a visible retry worker status
- Search and filter API, statistics, and a health check that verifies the database
- CSV and Excel exports protected against spreadsheet formula injection
- Request size limit, per-endpoint pause and secret rotation
- SQLite persistence, Swagger documentation, Docker support
- 44 automated tests with the HTTP client replaced by a fake, CI on Python 3.11 and 3.12

## Suggested screenshots

The README includes the dashboard, an endpoint page, an event page with delivery attempts, and the API tester.

## Suggested skills

Python, FastAPI, API Integration, Webhooks, REST API, SQLAlchemy, SQLite, HTTPX, Pytest, Data Export, Excel Automation, Docker, GitHub Actions
