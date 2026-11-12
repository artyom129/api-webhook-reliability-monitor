from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.database import Base, build_engine, build_session_factory
from app.models import WebhookEndpoint, WebhookEvent
from app.services import (
    due_event_ids,
    hourly_activity,
    redact_headers,
    schedule_next_retry,
    search_events,
)


@pytest.fixture()
def session():
    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as db_session:
        yield db_session


def endpoint(session, **fields):
    row = WebhookEndpoint(name="E", slug=fields.pop("slug", "e"), **fields)
    session.add(row)
    session.flush()
    return row


def event(session, ep, **fields):
    row = WebhookEvent(endpoint_id=ep.id, method="POST", body_text=fields.pop("body_text", "{}"), **fields)
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------
# Retry schedule
# --------------------------------------------------------------------------

def plan(max_retries, attempt_count, delay=30):
    ep = SimpleNamespace(max_retries=max_retries, retry_delay_seconds=delay)
    ev = SimpleNamespace(attempt_count=attempt_count, forwarding_status="failed", next_retry_at=None)
    schedule_next_retry(ev, ep)
    return ev


@pytest.mark.parametrize("attempt, expected_delay", [(1, 30), (2, 60), (3, 120)])
def test_backoff_doubles_after_every_failed_attempt(attempt, expected_delay):
    ev = plan(max_retries=3, attempt_count=attempt)

    assert ev.forwarding_status == "retry_scheduled"
    wait = (ev.next_retry_at - datetime.now(timezone.utc)).total_seconds()
    assert expected_delay - 2 <= wait <= expected_delay


def test_max_retries_counts_retries_after_the_first_attempt():
    # max_retries=3 means 1 first attempt + 3 retries = 4 attempts in total.
    assert plan(3, 3).forwarding_status == "retry_scheduled"
    last = plan(3, 4)
    assert last.forwarding_status == "exhausted"
    assert last.next_retry_at is None


def test_one_retry_really_retries_once():
    assert plan(1, 1).forwarding_status == "retry_scheduled"
    assert plan(1, 2).forwarding_status == "exhausted"


def test_zero_retries_means_a_single_attempt():
    assert plan(0, 1).forwarding_status == "exhausted"


# --------------------------------------------------------------------------
# Header redaction
# --------------------------------------------------------------------------

def test_redaction_is_case_insensitive_and_keeps_other_headers():
    result = redact_headers({"X-Webhook-Secret": "a", "x-webhook-secret": "b", "Content-Type": "json"})

    assert result == {"X-Webhook-Secret": "[redacted]", "x-webhook-secret": "[redacted]", "Content-Type": "json"}


# --------------------------------------------------------------------------
# Hourly activity
# --------------------------------------------------------------------------

def test_hourly_activity_buckets_events_by_outcome(session):
    ep = endpoint(session)
    now = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
    event(session, ep, forwarding_status="delivered", received_at=now)
    event(session, ep, forwarding_status="failed", received_at=now - timedelta(minutes=5))
    event(session, ep, validation_status="invalid_json", forwarding_status="skipped", received_at=now)
    event(session, ep, forwarding_status="not_configured", received_at=now - timedelta(hours=1))
    event(session, ep, forwarding_status="delivered", received_at=now - timedelta(hours=30))  # too old
    session.commit()

    buckets = hourly_activity(session, hours=24, now=now)

    assert len(buckets) == 24
    assert buckets[-1]["start"] == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert (buckets[-1]["delivered"], buckets[-1]["problems"], buckets[-1]["other"]) == (1, 2, 0)
    assert (buckets[-2]["delivered"], buckets[-2]["problems"], buckets[-2]["other"]) == (0, 0, 1)
    assert sum(b["delivered"] + b["problems"] + b["other"] for b in buckets) == 4


def test_hourly_activity_is_empty_without_events(session):
    buckets = hourly_activity(session, hours=24)

    assert len(buckets) == 24
    assert all(b["delivered"] == b["problems"] == b["other"] == 0 for b in buckets)


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def test_search_matches_body_and_error_text_and_escapes_wildcards(session):
    ep = endpoint(session)
    event(session, ep, body_text="order 100% paid")
    event(session, ep, body_text="order_7 paid")
    event(session, ep, body_text="plain", last_error="Target returned HTTP 503")
    session.commit()

    assert len(search_events(session, q="100%")) == 1
    assert len(search_events(session, q="_7")) == 1
    assert len(search_events(session, q="HTTP 503")) == 1
    assert len(search_events(session)) == 3


def test_search_limit_is_clamped(session):
    ep = endpoint(session)
    for _ in range(3):
        event(session, ep)
    session.commit()

    assert len(search_events(session, limit=0)) == 1
    assert len(search_events(session, limit=10_000)) == 3


# --------------------------------------------------------------------------
# Retry worker selection
# --------------------------------------------------------------------------

def test_only_due_events_of_active_forwarding_endpoints_are_selected(session):
    now = datetime.now(timezone.utc)
    active = endpoint(session, slug="a", auto_forward=True)
    paused = endpoint(session, slug="b", auto_forward=True, is_active=False)
    manual = endpoint(session, slug="c", auto_forward=False)

    due = event(session, active, forwarding_status="retry_scheduled", next_retry_at=now - timedelta(seconds=1))
    event(session, active, forwarding_status="retry_scheduled", next_retry_at=now + timedelta(minutes=5))
    event(session, active, forwarding_status="failed", next_retry_at=now - timedelta(seconds=1))
    event(session, paused, forwarding_status="retry_scheduled", next_retry_at=now - timedelta(seconds=1))
    event(session, manual, forwarding_status="retry_scheduled", next_retry_at=now - timedelta(seconds=1))
    session.commit()

    assert due_event_ids(session) == [due.id]
