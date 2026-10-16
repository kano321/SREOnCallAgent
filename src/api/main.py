"""
FastAPI server for the SRE On-Call Agent.

Run with: uvicorn src.api.main:app --reload --port 8000

Endpoints:
  GET  /                                — monitoring dashboard (static frontend)
  GET  /health                          — liveness check
  GET  /incidents/fixtures              — list available sample incident IDs
  POST /incidents/investigate           — run the full 3-agent pipeline (sync)
  GET  /incidents/investigate/stream    — run pipeline with SSE streaming (for frontend)
  POST /webhook/alert                   — receive Alertmanager webhook alerts
  GET  /webhook/events                  — stream alert and investigation events (SSE)
  GET  /incidents/{id}/report           — retrieve a report generated this session
  GET  /incidents/reports               — list historical reports (MySQL)
  GET  /incidents/reports/{rid}         — retrieve a historical report by run id
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agents.pipeline import run_pipeline, list_incidents, load_incident
from src.api.auth import router as auth_router, get_current_user
from src.api import db
from src.core.llm_client import HelloAgentsLLM

# 兜底：无论控制台/重定向使用何种编码（如 Windows GBK），
# 强制 stdout/stderr 按 UTF-8 输出，避免 emoji/中文打印触发 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

app = FastAPI(
    title="SRE On-Call Agent",
    description="AI-powered incident triage and post-mortem generation",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """静态资源与页面响应禁用缓存，确保前端更新后浏览器立即拿到最新版本
    （避免用户看到旧页面，误以为功能没上线）。"""
    response = await call_next(request)
    if request.url.path.startswith(("/static/", "/login.html", "/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

app.include_router(auth_router)

# In-memory store for this session's reports (SQLite keeps the full history)
_report_store: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# MySQL persistence
# ---------------------------------------------------------------------------
def _save_report(result: Dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO reports (incident_id, service, severity, created_at, payload) "
        "VALUES (%s, %s, %s, %s, %s)",
        (
            result["incident_id"],
            result.get("service", ""),
            result.get("severity", ""),
            time.strftime("%Y-%m-%d %H:%M:%S"),
            json.dumps(result, ensure_ascii=False),
        ),
    )


db.init_db()


class InvestigateRequest(BaseModel):
    incident_id: str


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    """Serve the monitoring dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login.html")
def login_page():
    """Serve the login / register / password-recovery page."""
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/llm/check")
def check_llm(user: str = Depends(get_current_user)):
    """Verify the configured AI model is reachable with a minimal prompt."""
    try:
        llm = HelloAgentsLLM(verbose=False)
    except ValueError as e:
        return {
            "ok": False,
            "configured": False,
            "model": None,
            "base_url": None,
            "latency_ms": None,
            "reply": "",
            "error": f"LLM 配置不完整: {e}",
            "category": "config_error",
            "message": "LLM 配置缺失",
            "hint": "在 .env 中配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID（可参考 .env.example）",
        }

    result = llm.check()
    result["configured"] = True
    result["model"] = llm.model
    result["base_url"] = llm.base_url
    return result


@app.get("/incidents/fixtures")
def get_fixtures(user: str = Depends(get_current_user)):
    """List all available sample incident IDs."""
    return {"incidents": list_incidents()}


# ---------------------------------------------------------------------------
# Sync pipeline (kept for CLI/curl compatibility)
# ---------------------------------------------------------------------------
@app.post("/incidents/investigate")
def investigate(req: InvestigateRequest, user: str = Depends(get_current_user)):
    """
    Run the full triage → investigation → post-mortem pipeline synchronously.
    """
    try:
        load_incident(req.incident_id)  # Validate early
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    start = time.time()
    try:
        result = run_pipeline(req.incident_id, verbose=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pipeline error: {e}")

    result["elapsed_seconds"] = round(time.time() - start, 1)
    _report_store[req.incident_id] = result
    _save_report(result)
    return result


# ---------------------------------------------------------------------------
# SSE streaming pipeline (for the frontend)
# ---------------------------------------------------------------------------
def _sse(event: Dict[str, Any]) -> str:
    """Format one dict as a Server-Sent-Events message."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Alertmanager webhook ingestion
# ---------------------------------------------------------------------------
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "sre-webhook-dev")
WEBHOOK_DEDUP_SECONDS = int(os.getenv("WEBHOOK_DEDUP_SECONDS", "600"))

_alert_subscribers: Set[asyncio.Queue] = set()
_alert_running: Set[str] = set()
_alert_seen: Dict[str, float] = {}
_main_loop: Optional[asyncio.AbstractEventLoop] = None


class AlertmanagerAlert(BaseModel):
    status: str = ""
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""


class AlertmanagerPayload(BaseModel):
    version: str = ""
    groupKey: str = ""
    truncatedAlerts: int = 0
    status: str = ""
    receiver: str = ""
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)


@app.on_event("startup")
async def _capture_main_loop() -> None:
    """Keep the API event loop so worker threads can deliver SSE events."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()


def _broadcast(event: Dict[str, Any]) -> None:
    """Deliver one event to every connected SSE subscriber."""
    for queue in tuple(_alert_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # A slow browser must not block investigations or other clients.
            pass


def _broadcast_from_thread(event: Dict[str, Any]) -> None:
    """Schedule an event broadcast on the API event loop."""
    if _main_loop is not None and not _main_loop.is_closed():
        _main_loop.call_soon_threadsafe(_broadcast, event)


def _fingerprint_alert(alert: AlertmanagerAlert, labels: Dict[str, str]) -> str:
    if alert.fingerprint:
        return alert.fingerprint
    raw = json.dumps(
        {"labels": labels, "startsAt": alert.startsAt},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _alert_to_incident(
    alert: AlertmanagerAlert,
    common_labels: Dict[str, str],
    common_annotations: Dict[str, str],
) -> Dict[str, Any]:
    """Convert one Alertmanager alert into the pipeline's incident schema."""
    labels = {**common_labels, **alert.labels}
    annotations = {**common_annotations, **alert.annotations}
    fingerprint = _fingerprint_alert(alert, labels)
    alert_name = labels.get("alertname", "unknown_alert")
    service = labels.get("service", "unknown")
    severity = labels.get("severity", "P2")

    return {
        "incident_id": f"alert-{fingerprint[:12]}",
        "service": service,
        "severity": severity,
        "alert": {
            "metric": labels.get("metric", alert_name),
            "value": annotations.get("value", labels.get("value", "N/A")),
            "threshold": annotations.get("threshold", labels.get("threshold", "N/A")),
            "unit": annotations.get("unit", labels.get("unit", "")),
            "timestamp": alert.startsAt,
            "description": annotations.get("description")
            or annotations.get("summary")
            or alert_name,
        },
        "logs": [],
        "metrics": {},
        "labels": labels,
        "annotations": annotations,
        "source": "alertmanager",
        "affected_users": 0,
        "alertmanager": {
            "fingerprint": fingerprint,
            "status": alert.status,
            "generator_url": alert.generatorURL,
            "ends_at": alert.endsAt,
        },
    }


def _run_alert_investigation(incident: Dict[str, Any], fingerprint: str) -> None:
    """Run a full AI investigation in a worker thread."""
    incident_id = incident["incident_id"]

    def emit(event: Dict[str, Any]) -> None:
        payload = dict(event)
        payload["incident_id"] = incident_id
        _broadcast_from_thread(payload)

    try:
        result = run_pipeline(
            incident_id,
            verbose=False,
            emit=emit,
            incident=incident,
        )
        _report_store[incident_id] = result
        try:
            _save_report(result)
        except Exception as exc:
            emit({"type": "alert_warning", "message": f"Report persistence failed: {exc}"})

        emit(
            {
                "type": "alert_done",
                "service": incident["service"],
                "severity": incident["severity"],
                "root_cause": result.get("findings", {}).get("root_cause", ""),
            }
        )
    except Exception as exc:
        emit(
            {
                "type": "alert_error",
                "service": incident.get("service", "unknown"),
                "severity": incident.get("severity", "unknown"),
                "message": str(exc),
            }
        )
    finally:
        _alert_running.discard(fingerprint)


def _prune_seen_alerts(now: float) -> None:
    expired = [
        key
        for key, seen_at in _alert_seen.items()
        if now - seen_at > WEBHOOK_DEDUP_SECONDS
    ]
    for key in expired:
        _alert_seen.pop(key, None)


@app.post("/webhook/alert", status_code=202)
async def alert_webhook(payload: AlertmanagerPayload, request: Request):
    """Receive an Alertmanager webhook and start investigations in background."""
    token = request.headers.get("X-Webhook-Token", "") or request.query_params.get(
        "token", ""
    )
    if not WEBHOOK_TOKEN or not hmac.compare_digest(token, WEBHOOK_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    now = time.time()
    _prune_seen_alerts(now)
    started: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for alert in payload.alerts:
        if alert.status and alert.status != "firing":
            continue

        incident = _alert_to_incident(
            alert,
            payload.commonLabels,
            payload.commonAnnotations,
        )
        fingerprint = incident["alertmanager"]["fingerprint"]
        dedup_key = f"{fingerprint}|{alert.startsAt or payload.groupKey}"

        if fingerprint in _alert_running or dedup_key in _alert_seen:
            skipped.append(
                {
                    "incident_id": incident["incident_id"],
                    "reason": "already_running_or_duplicate",
                }
            )
            continue

        _alert_seen[dedup_key] = now
        _alert_running.add(fingerprint)
        _broadcast(
            {
                "type": "alert",
                "incident_id": incident["incident_id"],
                "service": incident["service"],
                "severity": incident["severity"],
                "metric": incident["alert"]["metric"],
                "value": incident["alert"]["value"],
                "threshold": incident["alert"]["threshold"],
                "description": incident["alert"]["description"],
                "timestamp": incident["alert"]["timestamp"],
            }
        )
        threading.Thread(
            target=_run_alert_investigation,
            args=(incident, fingerprint),
            name=f"alert-investigation-{fingerprint[:8]}",
            daemon=True,
        ).start()
        started.append(
            {
                "incident_id": incident["incident_id"],
                "service": incident["service"],
                "severity": incident["severity"],
            }
        )

    return {
        "ok": True,
        "status": "accepted",
        "started": started,
        "skipped": skipped,
    }


@app.get("/webhook/events")
async def alert_events(
    request: Request,
    user: str = Depends(get_current_user),
):
    """Stream Alertmanager events and AI investigation progress as SSE."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _alert_subscribers.add(queue)

    async def gen() -> AsyncIterator[str]:
        try:
            yield _sse(
                {
                    "type": "connected",
                    "message": "alert event stream ready",
                }
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
        finally:
            _alert_subscribers.discard(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/incidents/investigate/stream")
async def investigate_stream(
    incident_id: str = Query(...),
    user: str = Depends(get_current_user),
):
    """
    Run the pipeline in a background thread and stream every agent step
    to the client as Server-Sent Events.

    Event types: start, stage, plan, step, root_cause, draft,
                 critique, revise, report, error, done
    """
    try:
        load_incident(incident_id)  # Validate early
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: Dict[str, Any]) -> None:
        # Pipeline runs in a worker thread; hop back onto the event loop
        # before putting into the asyncio queue (thread-safe handoff).
        loop.call_soon_threadsafe(queue.put_nowait, event)

    async def runner() -> None:
        try:
            result = await asyncio.to_thread(run_pipeline, incident_id, False, emit)
            _report_store[incident_id] = result
            _save_report(result)
        except Exception as e:
            emit({"type": "error", "message": str(e)})
        finally:
            emit({"type": "done"})

    task = asyncio.create_task(runner())

    async def gen() -> AsyncIterator[str]:
        try:
            yield _sse({"type": "start", "incident_id": incident_id})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # SSE comment: keeps proxies from timing out
                    continue
                yield _sse(event)
                if event.get("type") == "done":
                    break
        finally:
            task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Report retrieval
# ---------------------------------------------------------------------------
@app.get("/incidents/{incident_id}/report")
def get_report(incident_id: str, user: str = Depends(get_current_user)):
    """Retrieve a report generated earlier in this session."""
    if incident_id not in _report_store:
        raise HTTPException(
            status_code=404,
            detail=f"No report found for '{incident_id}'. Call POST /incidents/investigate first.",
        )
    return {
        "incident_id": incident_id,
        "report": _report_store[incident_id]["report"],
    }


@app.get("/incidents/reports")
def list_reports(
    keyword: str = Query("", max_length=64),
    user: str = Depends(get_current_user),
):
    """List historical reports (newest first); optional keyword filters
    incident_id / service / severity."""
    select = (
        "SELECT id, incident_id, service, severity, "
        "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created_at "
        "FROM reports"
    )
    kw = keyword.strip()
    if kw:
        like = f"%{kw}%"
        rows = db.query(
            select + " WHERE incident_id LIKE %s OR service LIKE %s OR severity LIKE %s "
            "ORDER BY id DESC",
            (like, like, like),
        )
    else:
        rows = db.query(select + " ORDER BY id DESC")
    return {"reports": rows}


@app.delete("/incidents/reports")
def delete_all_reports(user: str = Depends(get_current_user)):
    """Delete ALL historical reports (permanent)."""
    affected = db.execute("DELETE FROM reports")
    return {"ok": True, "deleted": affected}


@app.get("/incidents/reports/{rid}")
def get_report_by_id(rid: int, user: str = Depends(get_current_user)):
    """Retrieve one historical report by its run id."""
    rows = db.query(
        "SELECT id, incident_id, service, severity, payload, "
        "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') AS created_at "
        "FROM reports WHERE id = %s",
        (rid,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No report with id {rid}")
    row = rows[0]
    return {
        "id": row["id"],
        "incident_id": row["incident_id"],
        "service": row["service"],
        "severity": row["severity"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload"]),
    }


@app.delete("/incidents/reports/{rid}")
def delete_report_by_id(rid: int, user: str = Depends(get_current_user)):
    """Delete one historical report by its run id (permanent)."""
    affected = db.execute("DELETE FROM reports WHERE id = %s", (rid,))
    if not affected:
        raise HTTPException(status_code=404, detail=f"No report with id {rid}")
    return {"ok": True, "deleted_id": rid}


# Mount static assets last so /static/* never shadows API routes
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
