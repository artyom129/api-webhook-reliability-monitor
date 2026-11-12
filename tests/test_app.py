import io

from openpyxl import load_workbook
from sqlalchemy import select

from app.models import DeliveryAttempt, WebhookEndpoint, WebhookEvent
from conftest import db, make_endpoint


# --------------------------------------------------------------------------
# Pages and health
# --------------------------------------------------------------------------

def test_health_reports_database_and_worker(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["retry_worker"] is True


def test_dashboard_renders_empty_and_with_data(client):
    empty = client.get("/")
    assert empty.status_code == 200
    assert "Reliability overview" in empty.text
    assert "No endpoints yet" in empty.text
    assert "activity-chart" in empty.text

    make_endpoint(client, slug="orders")
    client.post("/hooks/orders", json={"event": "order.created"})
    page = client.get("/")
    assert "/hooks/orders" in page.text
    assert "bar-other" in page.text


def test_every_page_renders(client, http):
    endpoint_id = make_endpoint(client, slug="orders")
    event_id = client.post("/hooks/orders", json={"a": 1}).json()["event_id"]

    for path in ["/", f"/endpoints/{endpoint_id}", f"/events/{event_id}", "/api-tester", "/docs"]:
        assert client.get(path).status_code == 200, path


def test_browser_gets_an_error_page_and_api_clients_get_json(client):
    html = client.get("/endpoints/999", headers={"Accept": "text/html"})
    assert html.status_code == 404
    assert "Endpoint not found." in html.text
    assert "Back to the dashboard" in html.text

    assert client.get("/endpoints/999").json() == {"detail": "Endpoint not found."}
    assert client.get("/events/999").status_code == 404


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

def test_slug_is_cleaned_and_duplicates_are_rejected(client):
    make_endpoint(client, name="My Hook!!")
    with db(client) as session:
        assert session.scalar(select(WebhookEndpoint.slug)) == "my-hook"

    again = client.post("/endpoints", data={"name": "My Hook!!"})
    assert again.status_code == 409


def test_endpoint_form_validation(client):
    assert client.post("/endpoints", data={"name": "   "}).status_code == 400
    assert client.post("/endpoints", data={"name": "X", "slug": "!!!", "target_url": ""}).status_code == 400
    bad_url = client.post("/endpoints", data={"name": "X", "target_url": "ftp://example.com"})
    assert bad_url.status_code == 400
    assert "http" in bad_url.json()["detail"]


def test_retry_settings_are_clamped(client):
    make_endpoint(client, name="Clamp", max_retries="99", retry_delay_seconds="1")
    with db(client) as session:
        endpoint = session.scalar(select(WebhookEndpoint))
        assert (endpoint.max_retries, endpoint.retry_delay_seconds) == (10, 5)


def test_pause_resume_and_rotate_secret(client):
    endpoint_id = make_endpoint(client, name="Secure", signing_secret="old-secret")

    client.post(f"/endpoints/{endpoint_id}/toggle")
    assert client.post("/hooks/secure", json={}).status_code == 404
    client.post(f"/endpoints/{endpoint_id}/toggle")
    ok = client.post("/hooks/secure", json={}, headers={"X-Webhook-Secret": "old-secret"})
    assert ok.json()["validation_status"] == "valid"

    client.post(f"/endpoints/{endpoint_id}/rotate-secret")
    stale = client.post("/hooks/secure", json={}, headers={"X-Webhook-Secret": "old-secret"})
    assert stale.json()["validation_status"] == "invalid_secret"
    assert client.post("/endpoints/999/toggle").status_code == 404
    assert client.post("/endpoints/999/rotate-secret").status_code == 404


# --------------------------------------------------------------------------
# Receiving webhooks
# --------------------------------------------------------------------------

def test_valid_webhook_is_captured_in_full(client):
    make_endpoint(client, slug="orders", signing_secret="s3cret")
    response = client.post(
        "/hooks/orders?source=test",
        json={"event": "order.created", "id": 123},
        headers={"X-Webhook-Secret": "s3cret"},
    )

    assert response.status_code == 202
    assert response.json()["validation_status"] == "valid"
    page = client.get(f"/events/{response.json()['event_id']}")
    assert "order.created" in page.text
    assert "source" in page.text


def test_invalid_json_and_secret_problems_are_flagged(client):
    make_endpoint(client, slug="secure", signing_secret="right")

    bad_json = client.post("/hooks/secure", content="{bad", headers={"Content-Type": "application/json", "X-Webhook-Secret": "right"})
    wrong = client.post("/hooks/secure", json={}, headers={"X-Webhook-Secret": "wrong"})
    missing = client.post("/hooks/secure", json={})

    assert bad_json.json()["validation_status"] == "invalid_json"
    assert wrong.json()["validation_status"] == "invalid_secret"
    assert missing.json()["validation_status"] == "invalid_secret"


def test_unknown_endpoint_returns_json_404(client):
    response = client.post("/hooks/nope", json={}, headers={"Accept": "text/html"})

    assert response.status_code == 404
    assert response.json()["detail"].startswith("Webhook endpoint not found")


def test_other_methods_are_accepted(client):
    make_endpoint(client, slug="orders")

    assert client.get("/hooks/orders?ping=1").status_code == 202
    assert client.put("/hooks/orders", json={}).status_code == 202
    assert client.delete("/hooks/orders").status_code == 202


def test_oversized_payload_is_rejected(client):
    make_endpoint(client, slug="orders")
    response = client.post("/hooks/orders", content="x" * 5000, headers={"Content-Type": "text/plain"})

    assert response.status_code == 413
    with db(client) as session:
        assert session.scalar(select(WebhookEvent)) is None


def test_shared_secret_is_never_stored_or_shown(client):
    make_endpoint(client, slug="secure", signing_secret="super-secret-value")
    event_id = client.post("/hooks/secure", json={}, headers={"X-Webhook-Secret": "super-secret-value"}).json()["event_id"]

    page = client.get(f"/events/{event_id}").text
    assert "super-secret-value" not in page
    assert "[redacted]" in page
    with db(client) as session:
        assert "super-secret-value" not in session.get(WebhookEvent, event_id).headers_json


# --------------------------------------------------------------------------
# Forwarding, replay, retries
# --------------------------------------------------------------------------

TARGET = "https://target.test/hook"


def test_auto_forward_delivers_and_hides_the_secret_from_the_target(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET, auto_forward="on", signing_secret="s3cret")
    http.reply(200)

    response = client.post("/hooks/orders?x=1", json={"id": 1}, headers={"X-Webhook-Secret": "s3cret", "X-Custom": "keep"})

    assert response.json()["forwarding_status"] == "queued"
    call = http.calls[0]
    headers = {key.lower(): value for key, value in call["headers"].items()}
    assert call["url"] == TARGET
    assert "x-webhook-secret" not in headers
    assert headers["x-custom"] == "keep"
    assert headers["x-webhook-monitor-event-id"] == str(response.json()["event_id"])
    assert call["params"] == {"x": "1"}

    with db(client) as session:
        event = session.get(WebhookEvent, response.json()["event_id"])
        assert event.forwarding_status == "delivered"
        assert event.attempt_count == 1
        assert session.scalar(select(DeliveryAttempt)).succeeded is True


def test_invalid_requests_are_stored_but_never_forwarded(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET, auto_forward="on", signing_secret="right")

    response = client.post("/hooks/orders", json={}, headers={"X-Webhook-Secret": "wrong"})

    assert response.json()["forwarding_status"] == "skipped"
    assert http.calls == []


def test_failed_delivery_schedules_a_retry(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET, auto_forward="on")
    http.reply(503)

    event_id = client.post("/hooks/orders", json={}).json()["event_id"]

    with db(client) as session:
        event = session.get(WebhookEvent, event_id)
        assert event.forwarding_status == "retry_scheduled"
        assert event.next_retry_at is not None
        assert event.last_error == "Target returned HTTP 503"
        assert event.response_status == 503


def test_network_errors_are_recorded_as_failed_attempts(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET, auto_forward="on")
    http.reply(RuntimeError("connection refused"))

    event_id = client.post("/hooks/orders", json={}).json()["event_id"]

    with db(client) as session:
        attempt = session.scalar(select(DeliveryAttempt))
        assert attempt.succeeded is False
        assert attempt.error == "connection refused"
        assert session.get(WebhookEvent, event_id).forwarding_status == "retry_scheduled"


def test_replay_forwards_again_and_can_recover(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET, auto_forward="on")
    http.reply(500, 200)
    event_id = client.post("/hooks/orders", json={}).json()["event_id"]

    response = client.post(f"/events/{event_id}/replay", follow_redirects=False)

    assert response.status_code == 303
    with db(client) as session:
        event = session.get(WebhookEvent, event_id)
        assert event.forwarding_status == "delivered"
        assert event.attempt_count == 2
        assert event.next_retry_at is None


def test_replay_and_retry_now_need_a_target_url(client):
    make_endpoint(client, slug="audit")
    event_id = client.post("/hooks/audit", json={}).json()["event_id"]

    assert client.post(f"/events/{event_id}/replay").status_code == 400
    assert client.post(f"/events/{event_id}/retry-now").status_code == 400
    assert client.post("/events/999/replay").status_code == 404
    assert client.post("/events/999/retry-now").status_code == 404


def test_retry_now_queues_the_event_immediately(client, http):
    make_endpoint(client, slug="orders", target_url=TARGET)
    event_id = client.post("/hooks/orders", json={}).json()["event_id"]

    client.post(f"/events/{event_id}/retry-now")

    with db(client) as session:
        event = session.get(WebhookEvent, event_id)
        assert event.forwarding_status == "retry_scheduled"
        assert event.next_retry_at is not None


# --------------------------------------------------------------------------
# API tester
# --------------------------------------------------------------------------

def test_api_tester_sends_the_request_and_logs_it(client, http):
    http.reply(201)
    response = client.post(
        "/api-tester",
        data={"method": "post", "url": "https://api.test/orders", "headers_json": '{"X-A": "1"}', "body": '{"a":1}'},
    )

    assert response.status_code == 200
    assert "response 201" in response.text
    assert http.calls[0]["method"] == "POST"
    assert http.calls[0]["headers"] == {"X-A": "1"}
    assert "https://api.test/orders" in client.get("/api-tester").text


def test_api_tester_reports_network_errors(client, http):
    http.reply(RuntimeError("dns failure"))
    response = client.post("/api-tester", data={"method": "GET", "url": "https://nope.test"})

    assert "dns failure" in response.text


def test_api_tester_rejects_bad_headers(client):
    not_json = client.post("/api-tester", data={"method": "GET", "url": "https://a.test", "headers_json": "{oops"})
    not_object = client.post("/api-tester", data={"method": "GET", "url": "https://a.test", "headers_json": "[1]"})

    assert not_json.status_code == 400
    assert not_object.status_code == 400


# --------------------------------------------------------------------------
# JSON API and exports
# --------------------------------------------------------------------------

def seed_events(client, http):
    make_endpoint(client, name="Orders", slug="orders", target_url=TARGET, auto_forward="on", signing_secret="k")
    make_endpoint(client, name="Audit", slug="audit")
    http.reply(200, 500)
    good = {"X-Webhook-Secret": "k"}
    client.post("/hooks/orders", json={"id": "alpha"}, headers=good)
    client.post("/hooks/orders", json={"id": "beta"}, headers=good)
    client.post("/hooks/orders", json={"id": "gamma"})  # missing secret
    client.post("/hooks/audit", json={"note": "100% done"})


def test_stats_summarize_delivery(client, http):
    seed_events(client, http)
    stats = client.get("/api/stats").json()

    assert stats["total_events"] == 4
    assert stats["delivered"] == 1
    assert stats["failed"] == 1
    assert stats["invalid"] == 1
    assert stats["active_endpoints"] == 2
    assert stats["delivery_rate"] == 50
    assert stats["avg_response_ms"] is not None


def test_stats_on_an_empty_database(client):
    stats = client.get("/api/stats").json()

    assert stats["total_events"] == 0
    assert stats["delivery_rate"] is None
    assert stats["avg_response_ms"] is None


def test_events_api_filters(client, http):
    seed_events(client, http)

    assert len(client.get("/api/events").json()) == 4
    assert {e["endpoint"] for e in client.get("/api/events?endpoint=audit").json()} == {"audit"}
    assert len(client.get("/api/events?validation_status=invalid_secret").json()) == 1
    assert len(client.get("/api/events?forwarding_status=delivered").json()) == 1
    assert len(client.get("/api/events?q=beta").json()) == 1
    assert len(client.get("/api/events?limit=2").json()) == 2
    assert client.get("/api/events?q=nothing-matches").json() == []


def test_search_treats_percent_as_a_literal_character(client, http):
    seed_events(client, http)

    assert len(client.get("/api/events?q=100%25").json()) == 1
    assert len(client.get("/api/events?q=%25").json()) == 1  # only the body that contains a real %


def test_csv_export(client, http):
    seed_events(client, http)
    response = client.get("/exports/events.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith("webhook-events.csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("event_id,endpoint,method")
    assert len(lines) == 5


def test_excel_export_has_a_styled_header(client, http):
    seed_events(client, http)
    workbook = load_workbook(io.BytesIO(client.get("/exports/events.xlsx").content))
    sheet = workbook["Webhook Events"]

    assert [cell.value for cell in sheet[1]][:3] == ["event_id", "endpoint", "method"]
    assert sheet.max_row == 5
    assert sheet.freeze_panes == "A2"
    assert sheet["A1"].font.bold is True


def test_exports_work_without_events(client):
    csv_text = client.get("/exports/events.csv").text
    sheet = load_workbook(io.BytesIO(client.get("/exports/events.xlsx").content)).active

    assert csv_text.startswith("event_id,endpoint")
    assert sheet.max_row == 1


def test_formulas_in_webhook_bodies_are_neutralized_in_exports(client):
    make_endpoint(client, slug="orders")
    payload = '=HYPERLINK("http://evil.test","click")'
    client.post("/hooks/orders", content=payload, headers={"Content-Type": "text/plain"})

    assert "'=HYPERLINK" in client.get("/exports/events.csv").text
    sheet = load_workbook(io.BytesIO(client.get("/exports/events.xlsx").content)).active
    body_cell = sheet.cell(row=2, column=10)
    assert body_cell.value.startswith("'=")
    assert body_cell.data_type == "s"  # stored as text, not as a formula
