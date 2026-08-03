from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .models import ApiRequestLog, DeliveryAttempt, WebhookEndpoint, WebhookEvent


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def parse_json_text(value: str | None, fallback: Any):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def schedule_next_retry(event: WebhookEvent, endpoint: WebhookEndpoint) -> None:
    if event.attempt_count >= endpoint.max_retries:
        event.forwarding_status = "exhausted"
        event.next_retry_at = None
        return
    multiplier = max(1, 2 ** max(event.attempt_count - 1, 0))
    event.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=endpoint.retry_delay_seconds * multiplier)
    event.forwarding_status = "retry_scheduled"


async def forward_event(session: Session, event: WebhookEvent, timeout_seconds: int) -> DeliveryAttempt:
    endpoint = event.endpoint
    if not endpoint.target_url:
        event.forwarding_status = "not_configured"
        event.next_retry_at = None
        session.commit()
        raise ValueError("This endpoint does not have a target URL.")

    headers = parse_json_text(event.headers_json, {})
    for key in ["host", "content-length", "connection", "accept-encoding"]:
        headers.pop(key, None)
        headers.pop(key.title(), None)
    headers["X-Webhook-Monitor-Event-ID"] = str(event.id)

    event.attempt_count += 1
    started = time.perf_counter()
    status_code = None
    response_excerpt = None
    error = None
    succeeded = False

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.request(
                event.method,
                endpoint.target_url,
                headers=headers,
                params=parse_json_text(event.query_json, {}),
                content=event.body_text.encode("utf-8"),
            )
        status_code = response.status_code
        response_excerpt = response.text[:2000]
        succeeded = 200 <= response.status_code < 300
        if not succeeded:
            error = f"Target returned HTTP {response.status_code}"
    except Exception as exc:  # network and timeout failures are logged as delivery attempts
        error = str(exc)

    duration_ms = int((time.perf_counter() - started) * 1000)
    attempt = DeliveryAttempt(
        event_id=event.id,
        target_url=endpoint.target_url,
        status_code=status_code,
        duration_ms=duration_ms,
        succeeded=succeeded,
        error=error,
        response_excerpt=response_excerpt,
    )
    session.add(attempt)

    event.response_status = status_code
    event.response_body = response_excerpt
    event.last_error = error
    if succeeded:
        event.forwarding_status = "delivered"
        event.next_retry_at = None
    else:
        event.forwarding_status = "failed"
        schedule_next_retry(event, endpoint)

    session.commit()
    session.refresh(attempt)
    return attempt


async def execute_api_request(
    session: Session,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: str,
    timeout_seconds: int,
) -> ApiRequestLog:
    started = time.perf_counter()
    response_status = None
    response_body = None
    error = None
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.request(method, url, headers=headers, content=body.encode("utf-8") if body else None)
        response_status = response.status_code
        response_body = response.text[:10000]
    except Exception as exc:
        error = str(exc)

    log = ApiRequestLog(
        method=method,
        url=url,
        request_headers_json=json_dumps(headers),
        request_body=body,
        response_status=response_status,
        response_body=response_body,
        duration_ms=int((time.perf_counter() - started) * 1000),
        error=error,
    )
    session.add(log)
    session.commit()
    session.refresh(log)
    return log


def dashboard_stats(session: Session) -> dict[str, int]:
    total_events = session.scalar(select(func.count(WebhookEvent.id))) or 0
    delivered = session.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.forwarding_status == "delivered")) or 0
    failed = session.scalar(
        select(func.count(WebhookEvent.id)).where(WebhookEvent.forwarding_status.in_(["failed", "retry_scheduled", "exhausted"]))
    ) or 0
    invalid = session.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.validation_status != "valid")) or 0
    endpoints = session.scalar(select(func.count(WebhookEndpoint.id)).where(WebhookEndpoint.is_active.is_(True))) or 0
    return {
        "total_events": total_events,
        "delivered": delivered,
        "failed": failed,
        "invalid": invalid,
        "active_endpoints": endpoints,
    }


def due_event_ids(session: Session) -> list[int]:
    now = datetime.now(timezone.utc)
    rows = session.scalars(
        select(WebhookEvent.id)
        .join(WebhookEndpoint)
        .where(
            and_(
                WebhookEndpoint.is_active.is_(True),
                WebhookEndpoint.auto_forward.is_(True),
                WebhookEvent.next_retry_at.is_not(None),
                WebhookEvent.next_retry_at <= now,
                WebhookEvent.forwarding_status == "retry_scheduled",
            )
        )
        .order_by(WebhookEvent.next_retry_at.asc())
        .limit(20)
    ).all()
    return list(rows)


async def retry_worker(app) -> None:
    while True:
        await asyncio.sleep(app.state.settings.retry_poll_seconds)
        session = app.state.SessionLocal()
        try:
            ids = due_event_ids(session)
        finally:
            session.close()

        for event_id in ids:
            session = app.state.SessionLocal()
            try:
                event = session.get(WebhookEvent, event_id)
                if event is not None:
                    await forward_event(session, event, app.state.settings.request_timeout_seconds)
            except Exception:
                session.rollback()
            finally:
                session.close()
