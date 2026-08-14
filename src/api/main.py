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
  GET  /incidents/reports               — list historical reports (SQLite)
  GET  /incidents/reports/{rid}         — retrieve a historical report by run id
"""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.agents.pipeline import run_pipeline, list_incidents, load_incident

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "reports.db"

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

# In-memory store for this session's reports (SQLite keeps the full history)
_report_store: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT    NOT NULL,
                service     TEXT,
                severity    TEXT,
                created_at  TEXT    NOT NULL,
                payload     TEXT    NOT NULL
            )
            """
        )


def _save_report(result: Dict[str, Any]) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO reports (incident_id, service, severity, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                result["incident_id"],
                result.get("service", ""),
                result.get("severity", ""),
                time.strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(result, ensure_ascii=False),
            ),
        )


_init_db()


class InvestigateRequest(BaseModel):
    incident_id: str


# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    """Serve the monitoring dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/incidents/fixtures")
def get_fixtures():
    """List all available sample incident IDs."""
    return {"incidents": list_incidents()}


# ---------------------------------------------------------------------------
# Sync pipeline (kept for CLI/curl compatibility)
# ---------------------------------------------------------------------------
@app.post("/incidents/investigate")
def investigate(req: InvestigateRequest):
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
async def investigate_stream(incident_id: str = Query(...)):
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
def get_report(incident_id: str):
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
def list_reports():
    """List all historical reports (newest first)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, incident_id, service, severity, created_at "
            "FROM reports ORDER BY id DESC"
        ).fetchall()
    return {"reports": [dict(r) for r in rows]}


@app.get("/incidents/reports/{rid}")
def get_report_by_id(rid: int):
    """Retrieve one historical report by its run id."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id, incident_id, service, severity, created_at, payload "
            "FROM reports WHERE id = ?",
            (rid,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No report with id {rid}")
    return {
        "id": row["id"],
        "incident_id": row["incident_id"],
        "service": row["service"],
        "severity": row["severity"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload"]),
    }


# Mount static assets last so /static/* never shadows API routes
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
