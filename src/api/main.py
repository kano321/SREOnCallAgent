"""
FastAPI server for the SRE On-Call Agent.

Run with: uvicorn src.api.main:app --reload --port 8000

Endpoints:
  GET  /                                — monitoring dashboard (static frontend)
  GET  /health                          — liveness check
  GET  /incidents/fixtures              — list available sample incident IDs
  POST /incidents/investigate           — run the full 3-agent pipeline (sync)
  GET  /incidents/investigate/stream    — run pipeline with SSE streaming (for frontend)
  GET  /incidents/{id}/report           — retrieve a report generated this session
  GET  /incidents/reports               — list historical reports (MySQL)
  GET  /incidents/reports/{rid}         — retrieve a historical report by run id
"""
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agents.pipeline import run_pipeline, list_incidents, load_incident
from src.api.auth import router as auth_router, get_current_user
from src.api import db

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
# Webhook alert feed (P0②) — Alertmanager → POST /webhook/alert →
# 自动触发调查 → 事件广播到 /webhook/events (SSE)，前端实时告警列表
# ---------------------------------------------------------------------------
WEBHOOK_TOKEN: str = os.getenv("WEBHOOK_TOKEN", "sre-webhook-dev")

_alert_subscribers: List[asyncio.Queue] = []  # SSE 订阅队列
_alert_running: Dict[str, bool] = {}          # fingerprint -> 是否已在调查（风暴去重，简化版）
_main_loop: asyncio.AbstractEventLoop = None


@app.on_event("startup")
async def _capture_loop() -> None:
    """记录主事件循环引用，供后台线程安全地投递事件。"""
    global _main_loop
    _main_loop = asyncio.get_running_loop()


def _broadcast(event: Dict[str, Any]) -> None:
    """主循环内调用：把事件推给所有订阅者。"""
    for q in list(_alert_subscribers):
        q.put_nowait(event)


def _broadcast_from_thread(event: Dict[str, Any]) -> None:
    """后台调查线程调用：跨线程安全投递。"""
    if _main_loop:
        _main_loop.call_soon_threadsafe(_broadcast, event)


def _alert_to_incident(
    alert: Dict[str, Any],
    common_labels: Dict[str, Any],
    common_annotations: Dict[str, Any],
) -> Dict[str, Any]:
    """Alertmanager 告警 → 本项目 incident 字典（Agent 流水线输入格式）。"""
    labels = {**common_labels, **alert.get("labels", {})}
    annotations = {**common_annotations, **alert.get("annotations", {})}
    fingerprint = alert.get("fingerprint") or ""
    alertname = labels.get("alertname", "unknown_alert")
    return {
        "incident_id": f"alert-{(fingerprint[:12] if fingerprint else alertname)}",
        "service": labels.get("service", "unknown"),
        "severity": labels.get("severity", "P2"),
        "alert": {
            "metric": alertname,
            "value": annotations.get("value") or labels.get("value", "N/A"),
            "threshold": annotations.get("threshold", "N/A"),
            "unit": annotations.get("unit", ""),
            "timestamp": alert.get("startsAt", ""),
            "description": annotations.get("description")
            or annotations.get("summary", ""),
        },
        # 真实数据链路：logs 待 P0③ 接 Loki，metrics 已由 metric_query 走 Prometheus
        "logs": [],
        "metrics": {},
    }


def _run_alert_investigation(incident: Dict[str, Any], fingerprint: str) -> None:
    """后台线程：跑完整三阶段流水线并广播进度事件。"""
    def emit(event: Dict[str, Any]) -> None:
        event = dict(event)
        event["incident_id"] = incident["incident_id"]
        _broadcast_from_thread(event)

    try:
        result = run_pipeline(
            incident["incident_id"], verbose=False, emit=emit, incident=incident
        )
        _report_store[incident["incident_id"]] = result
        _save_report(result)
        emit({"type": "alert_done", "root_cause": result["findings"]["root_cause"]})
    except Exception as e:
        emit({"type": "alert_error", "message": str(e)})
    finally:
        _alert_running.pop(fingerprint, None)


class AlertWebhookPayload(BaseModel):
    status: str = ""
    alerts: List[Dict[str, Any]] = []
    groupLabels: Dict[str, Any] = {}
    commonLabels: Dict[str, Any] = {}
    commonAnnotations: Dict[str, Any] = {}
    externalURL: str = ""


@app.post("/webhook/alert")
async def alert_webhook(payload: AlertWebhookPayload, request: Request):
    """
    Alertmanager webhook 接收端点：校验 token → 转换告警 → 自动触发调查（异步）。

    - 请求头需带 X-Webhook-Token（默认 sre-webhook-dev，可用 WEBHOOK_TOKEN 覆盖）
    - 返回 202，调查在后台线程运行，进度通过 GET /webhook/events 广播
    """
    token = request.headers.get("X-Webhook-Token", "") or request.query_params.get(
        "token", ""
    )
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    started: List[str] = []
    for alert in payload.alerts:
        if alert.get("status") == "resolved":
            continue  # 只对 firing 告警自动调查
        incident = _alert_to_incident(
            alert, payload.commonLabels, payload.commonAnnotations
        )
        fingerprint = alert.get("fingerprint") or incident["incident_id"]
        if _alert_running.get(fingerprint):
            continue  # 同源告警去重：已在调查中则跳过（P1 完整风暴合并前的最小版）
        _alert_running[fingerprint] = True
        _broadcast_from_thread(
            {
                "type": "alert",
                "incident": {
                    "incident_id": incident["incident_id"],
                    "service": incident["service"],
                    "severity": incident["severity"],
                    "metric": incident["alert"]["metric"],
                    "value": incident["alert"]["value"],
                    "description": incident["alert"]["description"],
                    "timestamp": incident["alert"]["timestamp"],
                },
            }
        )
        threading.Thread(
            target=_run_alert_investigation, args=(incident, fingerprint), daemon=True
        ).start()
        started.append(incident["incident_id"])

    return {"ok": True, "started": started, "status": "investigation_started"}


@app.get("/webhook/events")
async def alert_events(user: str = Depends(get_current_user)):
    """
    全局告警事件流（SSE）：webhook 接收的告警 + 自动调查的每一步进度。
    事件类型：connected / alert / stage / plan / step / root_cause /
              draft / critique / revise / report / alert_done / alert_error
    """
    queue: asyncio.Queue = asyncio.Queue()
    _alert_subscribers.append(queue)

    async def gen() -> AsyncIterator[str]:
        try:
            yield _sse({"type": "connected", "message": "alert feed ready"})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event)
        finally:
            if queue in _alert_subscribers:
                _alert_subscribers.remove(queue)

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
