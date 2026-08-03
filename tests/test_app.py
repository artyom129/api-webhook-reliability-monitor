from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        retry_poll_seconds=60,
        request_timeout_seconds=2,
    )
    return TestClient(create_app(settings))


def test_health_and_dashboard(tmp_path):
    with build_client(tmp_path) as client:
        assert client.get("/health").json()["status"] == "ok"
        response = client.get("/")
        assert response.status_code == 200
        assert "Reliability overview" in response.text


def test_create_endpoint_and_receive_webhook(tmp_path):
    with build_client(tmp_path) as client:
        response = client.post(
            "/endpoints",
            data={
                "name": "Orders",
                "slug": "orders",
                "signing_secret": "demo-secret",
                "max_retries": "3",
                "retry_delay_seconds": "30",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        accepted = client.post(
            "/hooks/orders?source=test",
            headers={"X-Webhook-Secret": "demo-secret"},
            json={"event": "order.created", "id": 123},
        )
        assert accepted.status_code == 202
        payload = accepted.json()
        assert payload["validation_status"] == "valid"

        event_page = client.get(f"/events/{payload['event_id']}")
        assert event_page.status_code == 200
        assert "order.created" in event_page.text

        stats = client.get("/api/stats").json()
        assert stats["total_events"] == 1
        assert stats["active_endpoints"] == 1


def test_invalid_secret_and_exports(tmp_path):
    with build_client(tmp_path) as client:
        client.post(
            "/endpoints",
            data={"name": "Secure", "slug": "secure", "signing_secret": "correct", "max_retries": "1", "retry_delay_seconds": "5"},
        )
        response = client.post("/hooks/secure", headers={"Content-Type": "application/json", "X-Webhook-Secret": "wrong"}, content="{bad json")
        assert response.status_code == 202
        assert response.json()["validation_status"] == "invalid_secret"

        csv_response = client.get("/exports/events.csv")
        assert csv_response.status_code == 200
        assert "event_id,endpoint" in csv_response.text

        excel_response = client.get("/exports/events.xlsx")
        assert excel_response.status_code == 200
        assert excel_response.headers["content-type"].startswith("application/vnd.openxmlformats")
