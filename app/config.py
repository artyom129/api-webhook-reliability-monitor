from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_APP_NAME = "API & Webhook Reliability Monitor"
DEFAULT_DATABASE_URL = f"sqlite:///{BASE_DIR / 'data' / 'monitor.db'}"


@dataclass(slots=True)
class Settings:
    app_name: str = DEFAULT_APP_NAME
    database_url: str = DEFAULT_DATABASE_URL
    retry_poll_seconds: int = 5
    request_timeout_seconds: int = 15
    demo_mode: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("APP_NAME", DEFAULT_APP_NAME),
            database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
            retry_poll_seconds=int(os.getenv("RETRY_POLL_SECONDS", "5")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "15")),
            demo_mode=os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes"},
        )
