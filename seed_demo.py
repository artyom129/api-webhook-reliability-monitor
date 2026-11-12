"""Fill the database with demo data: endpoints, a day of traffic, failures and retries.

    python seed_demo.py            # refuses to touch a database that already has data
    python seed_demo.py --reset    # wipes the database first
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import random
import sys

from sqlalchemy import func, select

from app.config import Settings
from app.database import Base, build_engine, build_session_factory
from app.models import ApiRequestLog, DeliveryAttempt, WebhookEndpoint, WebhookEvent


def add_attempt(session, event, endpoint, status, duration_ms, error, when):
    session.add(
        DeliveryAttempt(
            event_id=event.id,
            target_url=endpoint.target_url or "https://api.example.com/none",
            status_code=status,
            duration_ms=duration_ms,
            succeeded=bool(status and status < 300),
            error=error,
            response_excerpt=event.response_body,
            created_at=when,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a demo database.")
    parser.add_argument("--reset", action="store_true", help="delete existing data first")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    SessionLocal = build_session_factory(engine)

    with SessionLocal() as probe:
        existing = probe.scalar(select(func.count(WebhookEndpoint.id))) or 0
    if existing and not args.reset:
        sys.exit("The database already contains data. Run `python seed_demo.py --reset` to replace it.")

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    rng = random.Random(7)  # the same demo data on every run
    now = datetime.now(timezone.utc)
    session = SessionLocal()
    try:
        stripe = WebhookEndpoint(
            name="Stripe Payments",
            slug="stripe-payments",
            target_url="https://api.example.com/billing/events",
            signing_secret="demo-stripe-secret",
            auto_forward=True,
            max_retries=4,
            retry_delay_seconds=30,
        )
        crm = WebhookEndpoint(
            name="CRM Lead Intake",
            slug="crm-leads",
            target_url="https://api.example.com/crm/webhook",
            auto_forward=True,
            max_retries=3,
            retry_delay_seconds=60,
        )
        audit = WebhookEndpoint(name="Audit Collector", slug="audit", auto_forward=False)
        session.add_all([stripe, crm, audit])
        session.flush()

        # The most recent events are hand-written so every status is on screen.
        samples = [
            (stripe, "POST", "valid", "delivered", 200, 1, {"type": "invoice.paid", "data": {"id": "in_9D2", "amount": 34900}}, None),
            (stripe, "POST", "valid", "delivered", 204, 1, {"type": "customer.updated", "data": {"id": "cus_81"}}, None),
            (crm, "POST", "valid", "retry_scheduled", 503, 2, {"lead_id": 8412, "source": "landing-page", "email": "alex@example.com"}, "Target returned HTTP 503"),
            (crm, "POST", "valid", "exhausted", 401, 4, {"lead_id": 8411, "source": "facebook"}, "Target returned HTTP 401"),
            (audit, "PUT", "invalid_json", "not_configured", None, 0, {"raw": "truncated payload"}, None),
            (stripe, "POST", "invalid_secret", "skipped", None, 0, {"type": "checkout.session.completed"}, None),
            (crm, "POST", "valid", "delivered", 201, 1, {"lead_id": 8409, "source": "referral"}, None),
            (audit, "GET", "valid", "not_configured", None, 0, {"ping": True}, None),
        ]
        for index, (endpoint, method, validation, delivery, status, attempts, payload, error) in enumerate(samples):
            event = WebhookEvent(
                endpoint_id=endpoint.id,
                method=method,
                content_type="application/json",
                headers_json=json.dumps({"content-type": "application/json", "user-agent": "Demo Client/1.0", "x-webhook-secret": "[redacted]"} if endpoint.signing_secret else {"content-type": "application/json", "user-agent": "Demo Client/1.0"}),
                query_json="{}",
                body_text=json.dumps(payload),
                body_json=json.dumps(payload),
                validation_status=validation,
                forwarding_status=delivery,
                response_status=status,
                response_body='{"accepted":true}' if status and status < 300 else None,
                attempt_count=attempts,
                last_error=error,
                next_retry_at=now + timedelta(minutes=5) if delivery == "retry_scheduled" else None,
                received_at=now - timedelta(minutes=index * 7),
            )
            session.add(event)
            session.flush()
            for number in range(attempts):
                failed = delivery != "delivered" or number < attempts - 1
                add_attempt(
                    session, event, endpoint,
                    status if not failed or number == attempts - 1 else 503,
                    180 + index * 31 + number * 40,
                    error if failed else None,
                    event.received_at + timedelta(seconds=1 + number * 30),
                )

        # Older traffic for the last 24 hours, busier in the middle of the day.
        for hours_ago in range(1, 24):
            busy = 2 + int(4 * max(0.0, 1 - abs(12 - ((now.hour - hours_ago) % 24)) / 12))
            for _ in range(rng.randint(max(0, busy - 2), busy + 2)):
                endpoint = rng.choice([stripe, stripe, crm, crm, audit])
                moment = now - timedelta(hours=hours_ago, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
                roll = rng.random()
                validation, delivery, status, error, attempts = "valid", "delivered", rng.choice([200, 200, 201, 204]), None, 1
                if not endpoint.target_url:
                    delivery, status = "not_configured", None
                    attempts = 0
                elif roll < 0.07:
                    validation, delivery, status, attempts = "invalid_secret", "skipped", None, 0
                elif roll < 0.17:
                    status = rng.choice([500, 502, 503])
                    error, delivery, attempts = f"Target returned HTTP {status}", "exhausted", endpoint.max_retries + 1
                payload = {"event": rng.choice(["order.created", "lead.created", "invoice.paid", "user.updated"]), "id": rng.randint(1000, 9999)}
                event = WebhookEvent(
                    endpoint_id=endpoint.id,
                    method="POST",
                    content_type="application/json",
                    headers_json=json.dumps({"content-type": "application/json", "user-agent": "Demo Client/1.0"}),
                    query_json="{}",
                    body_text=json.dumps(payload),
                    body_json=json.dumps(payload),
                    validation_status=validation,
                    forwarding_status=delivery,
                    response_status=status,
                    response_body='{"accepted":true}' if status and status < 300 else None,
                    attempt_count=attempts,
                    last_error=error,
                    received_at=moment,
                )
                session.add(event)
                session.flush()
                for number in range(attempts):
                    last = number == attempts - 1
                    add_attempt(
                        session, event, endpoint,
                        status if last else 503,
                        rng.randint(90, 420),
                        error if (not last or delivery != "delivered") else None,
                        moment + timedelta(seconds=1 + number * 30),
                    )

        session.add_all([
            ApiRequestLog(method="POST", url="https://api.example.com/orders", request_headers_json='{"Authorization":"Bearer ***"}', request_body='{"order_id":9012}', response_status=201, response_body='{"accepted":true}', duration_ms=246, created_at=now - timedelta(minutes=4)),
            ApiRequestLog(method="GET", url="https://api.example.com/health", request_headers_json='{}', request_body='', response_status=200, response_body='{"status":"ok"}', duration_ms=92, created_at=now - timedelta(minutes=11)),
            ApiRequestLog(method="POST", url="https://api.example.com/crm/leads", request_headers_json='{"Content-Type":"application/json"}', request_body='{"email":"alex@example.com"}', response_status=503, response_body='Service temporarily unavailable', duration_ms=1510, error="Target returned HTTP 503", created_at=now - timedelta(minutes=19)),
        ])
        session.commit()
        total = session.scalar(select(func.count(WebhookEvent.id)))
        print(f"Demo database created with 3 endpoints, {total} webhook events, and 3 API request logs.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
