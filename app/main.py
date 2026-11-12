from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import secrets
from typing import Annotated

import pandas as pd
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from openpyxl.styles import Font, PatternFill
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import BASE_DIR, Settings
from .database import Base, build_engine, build_session_factory
from .models import ApiRequestLog, WebhookEndpoint, WebhookEvent
from .services import (
    dashboard_stats,
    execute_api_request,
    forward_event,
    hourly_activity,
    json_dumps,
    parse_json_text,
    redact_headers,
    retry_worker,
    search_events,
)


EXPORT_COLUMNS = [
    "event_id", "endpoint", "method", "validation_status", "forwarding_status",
    "response_status", "attempt_count", "last_error", "received_at", "body",
]
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_session(request: Request):
    session = request.app.state.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def as_bool(value: str | None) -> bool:
    return value in {"on", "true", "1", "yes"}


def clean_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower().strip()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def template_context(request: Request, **kwargs):
    worker = getattr(request.app.state, "retry_worker", None)
    return {
        "request": request,
        "app_name": request.app.state.settings.app_name,
        "now": datetime.now(timezone.utc),
        "worker_running": worker is not None and not worker.done(),
        **kwargs,
    }


def spreadsheet_safe(value):
    """Stop text that starts like a formula from running when opened in Excel.

    Webhook bodies come from outside, so a body like ``=HYPERLINK(...)`` would
    otherwise become a live formula in the exported file.
    """
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


def is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = build_engine(settings.database_url)
    SessionLocal = build_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(engine)
        worker = asyncio.create_task(retry_worker(app))
        app.state.retry_worker = worker
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title=settings.app_name,
        description="Receive, inspect, validate, forward, retry, replay, and export webhook events.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.SessionLocal = SessionLocal
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """Browsers get a readable page, API clients and webhook senders get JSON."""
        wants_html = "text/html" in request.headers.get("accept", "")
        machine_path = request.url.path.startswith(("/hooks/", "/api/", "/health"))
        if wants_html and not machine_path:
            return templates.TemplateResponse(
                request=request,
                name="error.html",
                context=template_context(request, status_code=exc.status_code, message=exc.detail),
                status_code=exc.status_code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.get("/health", tags=["System"])
    def health(request: Request, session: Session = Depends(get_session)):
        worker = getattr(request.app.state, "retry_worker", None)
        worker_running = worker is not None and not worker.done()
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                {"status": "error", "service": settings.app_name, "database": "unreachable", "retry_worker": worker_running},
                status_code=503,
            )
        return {"status": "ok", "service": settings.app_name, "database": "ok", "retry_worker": worker_running}

    @app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
    def dashboard(request: Request, session: Session = Depends(get_session)):
        endpoints = session.scalars(select(WebhookEndpoint).order_by(desc(WebhookEndpoint.created_at))).all()
        events = session.scalars(select(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(20)).all()
        activity = hourly_activity(session)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=template_context(
                request,
                endpoints=endpoints,
                events=events,
                stats=dashboard_stats(session),
                activity=activity,
                activity_max=max([hour["delivered"] + hour["problems"] + hour["other"] for hour in activity] + [1]),
            ),
        )

    @app.post("/endpoints", tags=["Endpoints"])
    def create_endpoint(
        request: Request,
        name: Annotated[str, Form()],
        slug: Annotated[str, Form()] = "",
        target_url: Annotated[str, Form()] = "",
        signing_secret: Annotated[str, Form()] = "",
        auto_forward: Annotated[str | None, Form()] = None,
        max_retries: Annotated[int, Form()] = 3,
        retry_delay_seconds: Annotated[int, Form()] = 30,
        session: Session = Depends(get_session),
    ):
        slug = clean_slug(slug or name)
        if not name.strip() or not slug:
            raise HTTPException(400, "A valid endpoint name or slug is required.")
        target_url = target_url.strip()
        if target_url and not is_http_url(target_url):
            raise HTTPException(400, "The target URL must start with http:// or https://.")
        endpoint = WebhookEndpoint(
            name=name.strip(),
            slug=slug,
            target_url=target_url or None,
            signing_secret=signing_secret.strip() or None,
            auto_forward=as_bool(auto_forward),
            max_retries=max(0, min(max_retries, 10)),
            retry_delay_seconds=max(5, min(retry_delay_seconds, 3600)),
        )
        session.add(endpoint)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, "That endpoint slug already exists.") from exc
        return RedirectResponse(url=f"/endpoints/{endpoint.id}", status_code=303)

    @app.get("/endpoints/{endpoint_id}", response_class=HTMLResponse, tags=["Endpoints"])
    def endpoint_detail(endpoint_id: int, request: Request, session: Session = Depends(get_session)):
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "Endpoint not found.")
        events = session.scalars(
            select(WebhookEvent).where(WebhookEvent.endpoint_id == endpoint_id).order_by(desc(WebhookEvent.received_at)).limit(100)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="endpoint.html",
            context=template_context(request, endpoint=endpoint, events=events),
        )

    @app.post("/endpoints/{endpoint_id}/toggle", tags=["Endpoints"])
    def toggle_endpoint(endpoint_id: int, session: Session = Depends(get_session)):
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "Endpoint not found.")
        endpoint.is_active = not endpoint.is_active
        session.commit()
        return RedirectResponse(url=f"/endpoints/{endpoint_id}", status_code=303)

    @app.post("/endpoints/{endpoint_id}/rotate-secret", tags=["Endpoints"])
    def rotate_secret(endpoint_id: int, session: Session = Depends(get_session)):
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None:
            raise HTTPException(404, "Endpoint not found.")
        endpoint.signing_secret = secrets.token_urlsafe(24)
        session.commit()
        return RedirectResponse(url=f"/endpoints/{endpoint_id}", status_code=303)

    @app.api_route("/hooks/{slug}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], tags=["Webhook Receiver"])
    async def receive_webhook(slug: str, request: Request, background_tasks: BackgroundTasks, session: Session = Depends(get_session)):
        endpoint = session.scalar(select(WebhookEndpoint).where(WebhookEndpoint.slug == slug))
        if endpoint is None or not endpoint.is_active:
            raise HTTPException(404, "Webhook endpoint not found or disabled.")

        declared_length = request.headers.get("content-length")
        if declared_length and declared_length.isdigit() and int(declared_length) > settings.max_body_bytes:
            raise HTTPException(413, f"Payload is larger than {settings.max_body_bytes} bytes.")
        body_bytes = await request.body()
        if len(body_bytes) > settings.max_body_bytes:
            raise HTTPException(413, f"Payload is larger than {settings.max_body_bytes} bytes.")
        body_text = body_bytes.decode("utf-8", errors="replace")
        body_json = None
        validation_status = "valid"
        if "json" in (request.headers.get("content-type") or "").lower() and body_text:
            try:
                body_json = json_dumps(json.loads(body_text))
            except json.JSONDecodeError:
                validation_status = "invalid_json"

        if endpoint.signing_secret:
            supplied = request.headers.get("x-webhook-secret")
            if not supplied or not secrets.compare_digest(supplied, endpoint.signing_secret):
                validation_status = "invalid_secret"

        will_forward = bool(endpoint.target_url and endpoint.auto_forward)
        if not will_forward:
            forwarding_status = "not_configured"
        elif validation_status != "valid":
            forwarding_status = "skipped"  # invalid requests are stored but never forwarded
        else:
            forwarding_status = "queued"

        event = WebhookEvent(
            endpoint_id=endpoint.id,
            method=request.method,
            content_type=request.headers.get("content-type"),
            # The shared secret is checked above and must not be stored or shown.
            headers_json=json_dumps(redact_headers(dict(request.headers))),
            query_json=json_dumps(dict(request.query_params)),
            body_text=body_text,
            body_json=body_json,
            validation_status=validation_status,
            forwarding_status=forwarding_status,
        )
        session.add(event)
        session.commit()
        session.refresh(event)

        if forwarding_status == "queued":
            async def run_forward(event_id: int):
                bg_session = request.app.state.SessionLocal()
                try:
                    bg_event = bg_session.get(WebhookEvent, event_id)
                    if bg_event is not None:
                        await forward_event(bg_session, bg_event, settings.request_timeout_seconds)
                finally:
                    bg_session.close()

            background_tasks.add_task(run_forward, event.id)

        return JSONResponse(
            {
                "accepted": True,
                "event_id": event.id,
                "validation_status": validation_status,
                "forwarding_status": event.forwarding_status,
            },
            status_code=202,
        )

    @app.get("/events/{event_id}", response_class=HTMLResponse, tags=["Events"])
    def event_detail(event_id: int, request: Request, session: Session = Depends(get_session)):
        event = session.get(WebhookEvent, event_id)
        if event is None:
            raise HTTPException(404, "Event not found.")
        return templates.TemplateResponse(
            request=request,
            name="event_detail.html",
            context=template_context(
                request,
                event=event,
                headers_pretty=json.dumps(parse_json_text(event.headers_json, {}), indent=2, ensure_ascii=False),
                query_pretty=json.dumps(parse_json_text(event.query_json, {}), indent=2, ensure_ascii=False),
                body_pretty=json.dumps(parse_json_text(event.body_json, {}), indent=2, ensure_ascii=False)
                if event.body_json
                else event.body_text,
            ),
        )

    @app.post("/events/{event_id}/replay", tags=["Events"])
    async def replay_event(event_id: int, session: Session = Depends(get_session)):
        event = session.get(WebhookEvent, event_id)
        if event is None:
            raise HTTPException(404, "Event not found.")
        try:
            await forward_event(session, event, settings.request_timeout_seconds)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(url=f"/events/{event_id}", status_code=303)

    @app.post("/events/{event_id}/retry-now", tags=["Events"])
    def retry_now(event_id: int, session: Session = Depends(get_session)):
        event = session.get(WebhookEvent, event_id)
        if event is None:
            raise HTTPException(404, "Event not found.")
        if not event.endpoint.target_url:
            raise HTTPException(400, "This endpoint does not have a target URL.")
        event.forwarding_status = "retry_scheduled"
        event.next_retry_at = datetime.now(timezone.utc)
        session.commit()
        return RedirectResponse(url=f"/events/{event_id}", status_code=303)

    @app.get("/api-tester", response_class=HTMLResponse, tags=["API Tester"])
    def api_tester(request: Request, session: Session = Depends(get_session)):
        logs = session.scalars(select(ApiRequestLog).order_by(desc(ApiRequestLog.created_at)).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="api_tester.html",
            context=template_context(request, logs=logs, result=None),
        )

    @app.post("/api-tester", response_class=HTMLResponse, tags=["API Tester"])
    async def run_api_test(
        request: Request,
        method: Annotated[str, Form()],
        url: Annotated[str, Form()],
        headers_json: Annotated[str, Form()] = "{}",
        body: Annotated[str, Form()] = "",
        session: Session = Depends(get_session),
    ):
        try:
            headers = json.loads(headers_json or "{}")
            if not isinstance(headers, dict):
                raise ValueError("Headers must be a JSON object.")
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(400, f"Invalid headers JSON: {exc}") from exc
        log = await execute_api_request(
            session,
            method=method.upper(),
            url=url,
            headers={str(k): str(v) for k, v in headers.items()},
            body=body,
            timeout_seconds=settings.request_timeout_seconds,
        )
        logs = session.scalars(select(ApiRequestLog).order_by(desc(ApiRequestLog.created_at)).limit(20)).all()
        return templates.TemplateResponse(
            request=request,
            name="api_tester.html",
            context=template_context(request, logs=logs, result=log),
        )

    @app.get("/exports/events.csv", tags=["Exports"])
    def export_events_csv(session: Session = Depends(get_session)):
        rows = build_event_export_rows(session)
        buffer = io.StringIO()
        pd.DataFrame(rows, columns=EXPORT_COLUMNS).to_csv(buffer, index=False)
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=webhook-events.csv"},
        )

    @app.get("/exports/events.xlsx", tags=["Exports"])
    def export_events_excel(session: Session = Depends(get_session)):
        rows = build_event_export_rows(session)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            pd.DataFrame(rows, columns=EXPORT_COLUMNS).to_excel(writer, index=False, sheet_name="Webhook Events")
            worksheet = writer.sheets["Webhook Events"]
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cell in worksheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="1F2937")
            for column in worksheet.columns:
                width = min(max(len(str(cell.value or "")) for cell in column) + 2, 48)
                worksheet.column_dimensions[column[0].column_letter].width = width
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=webhook-events.xlsx"},
        )

    @app.get("/api/stats", tags=["JSON API"])
    def api_stats(session: Session = Depends(get_session)):
        return dashboard_stats(session)

    @app.get("/api/events", tags=["JSON API"])
    def api_events(
        limit: int = 50,
        endpoint: str | None = None,
        validation_status: str | None = None,
        forwarding_status: str | None = None,
        q: str | None = None,
        session: Session = Depends(get_session),
    ):
        """Recent events, newest first. ``q`` searches the body and the last error."""
        events = search_events(
            session,
            limit=limit,
            endpoint=endpoint,
            validation_status=validation_status,
            forwarding_status=forwarding_status,
            q=q,
        )
        return [
            {
                "id": event.id,
                "endpoint": event.endpoint.slug,
                "method": event.method,
                "validation_status": event.validation_status,
                "forwarding_status": event.forwarding_status,
                "response_status": event.response_status,
                "attempt_count": event.attempt_count,
                "received_at": event.received_at.isoformat(),
            }
            for event in events
        ]

    return app


def build_event_export_rows(session: Session) -> list[dict]:
    events = session.scalars(select(WebhookEvent).order_by(desc(WebhookEvent.received_at))).all()
    return [
        {
            "event_id": event.id,
            "endpoint": spreadsheet_safe(event.endpoint.slug),
            "method": event.method,
            "validation_status": event.validation_status,
            "forwarding_status": event.forwarding_status,
            "response_status": event.response_status,
            "attempt_count": event.attempt_count,
            "last_error": spreadsheet_safe(event.last_error),
            "received_at": event.received_at.isoformat(),
            "body": spreadsheet_safe(event.body_text),
        }
        for event in events
    ]


app = create_app()
