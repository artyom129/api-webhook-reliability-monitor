from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from app.config import Settings
from app.database import Base, build_engine, build_session_factory
from app.models import ApiRequestLog, DeliveryAttempt, WebhookEndpoint, WebhookEvent


def main() -> None:
    settings = Settings.from_env()
    engine = build_engine(settings.database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    SessionLocal = build_session_factory(engine)
    session = SessionLocal()
    now = datetime.now(timezone.utc)
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

        samples = [
            (stripe, "POST", "valid", "delivered", 200, 1, {"type":"invoice.paid","data":{"id":"in_9D2","amount":34900}}, None),
            (stripe, "POST", "valid", "delivered", 204, 1, {"type":"customer.updated","data":{"id":"cus_81"}}, None),
            (crm, "POST", "valid", "retry_scheduled", 503, 2, {"lead_id":8412,"source":"landing-page","email":"alex@example.com"}, "Target returned HTTP 503"),
            (crm, "POST", "valid", "failed", 401, 1, {"lead_id":8411,"source":"facebook"}, "Target returned HTTP 401"),
            (audit, "PUT", "invalid_json", "not_configured", None, 0, {"raw":"truncated payload"}, None),
            (stripe, "POST", "invalid_secret", "not_configured", None, 0, {"type":"checkout.session.completed"}, None),
            (crm, "POST", "valid", "delivered", 201, 1, {"lead_id":8409,"source":"referral"}, None),
            (audit, "GET", "valid", "not_configured", None, 0, {"ping":True}, None),
        ]
        for index, (endpoint, method, validation, delivery, status, attempts, payload, error) in enumerate(samples):
            event = WebhookEvent(
                endpoint_id=endpoint.id,
                method=method,
                content_type="application/json",
                headers_json=json.dumps({"content-type":"application/json","user-agent":"Demo Client/1.0"}),
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
            if attempts:
                session.add(
                    DeliveryAttempt(
                        event_id=event.id,
                        target_url=endpoint.target_url or "https://api.example.com/none",
                        status_code=status,
                        duration_ms=180 + index * 31,
                        succeeded=bool(status and status < 300),
                        error=error,
                        response_excerpt=event.response_body,
                        created_at=event.received_at + timedelta(seconds=1),
                    )
                )
        session.add_all([
            ApiRequestLog(method="POST", url="https://api.example.com/orders", request_headers_json='{"Authorization":"Bearer ***"}', request_body='{"order_id":9012}', response_status=201, response_body='{"accepted":true}', duration_ms=246, created_at=now - timedelta(minutes=4)),
            ApiRequestLog(method="GET", url="https://api.example.com/health", request_headers_json='{}', request_body='', response_status=200, response_body='{"status":"ok"}', duration_ms=92, created_at=now - timedelta(minutes=11)),
            ApiRequestLog(method="POST", url="https://api.example.com/crm/leads", request_headers_json='{"Content-Type":"application/json"}', request_body='{"email":"alex@example.com"}', response_status=503, response_body='Service temporarily unavailable', duration_ms=1510, error="Target returned HTTP 503", created_at=now - timedelta(minutes=19)),
        ])
        session.commit()
        print("Demo database created with 3 endpoints, 8 webhook events, and 3 API request logs.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
