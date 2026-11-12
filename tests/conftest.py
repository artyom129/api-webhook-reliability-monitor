"""Shared fixtures: an isolated database per test and a fake HTTP client.

No test touches the network. ``FakeHttp`` replaces ``httpx.AsyncClient`` inside
the services module, records every outgoing request, and answers from a queue.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import services
from app.config import Settings
from app.main import create_app


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class FakeHttp:
    """Stands in for httpx.AsyncClient. Queue answers with ``reply``."""

    def __init__(self):
        self.calls: list[dict] = []
        self.queue: list[int | Exception] = []

    def reply(self, *outcomes: int | Exception) -> None:
        self.queue.extend(outcomes)

    def client_class(self):
        outer = self

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def request(self, method, url, headers=None, params=None, content=None):
                outer.calls.append({"method": method, "url": url, "headers": headers or {}, "params": params, "content": content})
                outcome = outer.queue.pop(0) if outer.queue else 200
                if isinstance(outcome, Exception):
                    raise outcome
                return FakeResponse(outcome, f"response {outcome}")

        return Client


@pytest.fixture()
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(services.httpx, "AsyncClient", fake.client_class())
    return fake


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",  # in-memory: creating a file database is slow on some disks
        retry_poll_seconds=3600,  # the background worker never fires during a test
        request_timeout_seconds=2,
        max_body_bytes=2000,
    )


@pytest.fixture()
def client(settings, http):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def make_endpoint(client: TestClient, name: str = "Orders", **fields) -> int:
    """Create an endpoint through the form and return its id."""
    data = {"name": name, "max_retries": "3", "retry_delay_seconds": "30", **fields}
    response = client.post("/endpoints", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def db(client: TestClient):
    """A session on the same database the app uses."""
    return client.app.state.SessionLocal()
