from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator


class EndpointCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]*$")
    target_url: HttpUrl | None = None
    signing_secret: str | None = Field(default=None, max_length=255)
    auto_forward: bool = False
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay_seconds: int = Field(default=30, ge=5, le=3600)


class ApiTesterPayload(BaseModel):
    method: str
    url: HttpUrl
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        method = value.upper().strip()
        allowed = {"GET", "POST", "PUT", "PATCH", "DELETE"}
        if method not in allowed:
            raise ValueError(f"method must be one of: {', '.join(sorted(allowed))}")
        return method
