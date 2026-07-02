"""FastAPI app: lifespan, routers, templates, static files, REST endpoints.

Lifespan (D27): open the shared DB connection, run migrations, start the
collector + supervisor tasks; on shutdown cancel the collector, close SSE
subscribers, and close the DB.

REST endpoints (M1 subset): login, ``/api/metrics/current``, ``/api/metrics/history``,
``/api/metrics/export.csv``. SSE (``/api/stream``), ``/health``, broker, and
supervisor are wired in M2.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from raidwatch import __version__
from raidwatch.auth import COOKIE_NAME, handle_login_post, require_auth
from raidwatch.broker import Broker
from raidwatch.collector import Collector
from raidwatch.config import AppConfig, load_config
from raidwatch.database import Database
from raidwatch.health import build_health
from raidwatch.supervisor import Supervisor

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_DIR = REPO_ROOT / "static"
DB_PATH = REPO_ROOT / "data" / "raidwatch.db"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup + shutdown (D27)."""
    # --- Startup ---
    config = load_config()
    db = Database(str(DB_PATH))
    await db.connect()

    broker = Broker()
    collector = Collector(config=config, db=db, broker=broker)
    supervisor = Supervisor(collector)

    app.state.config = config
    app.state.db = db
    app.state.broker = broker
    app.state.collector = collector
    app.state.supervisor = supervisor
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.started_at = int(time.time() * 1000)

    # Start the collector + supervisor (D27).
    collector.start()
    supervisor.start()
    logger.info(
        "RaidWatch v%s started (host=%s, port=%s)",
        __version__,
        config.server.bind_host,
        config.server.port,
    )

    yield

    # --- Shutdown (D27): stop supervisor + collector, close subscribers, close DB ---
    logger.info("RaidWatch shutting down...")
    await supervisor.stop()
    await broker.close()
    await db.close()
    logger.info("RaidWatch stopped.")


# --------------------------------------------------------------------------- #
# App factory                                                                 #
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    app = FastAPI(
        title="RaidWatch",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
    )

    # Static files (vendored CSS/JS + app.js; D29).
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/health", response_class=JSONResponse)
    async def health() -> dict[str, Any]:
        """Machine-readable liveness (D35). Drives D22 pill + external watchdog."""
        return await build_health(app.state.config, app.state)

    # --- Login (D24) ---
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        templates = app.state.templates
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    async def login_post(request: Request):
        return await handle_login_post(request)

    @app.get("/logout")
    async def logout():
        """Clear the auth cookie and redirect to login."""
        from fastapi.responses import RedirectResponse

        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # --- Dashboard ---
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    async def dashboard(request: Request) -> HTMLResponse:
        templates = app.state.templates
        config: AppConfig = request.app.state.config
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"server_name": config.server.name},
        )

    # --- REST API (auth-protected) ---
    @app.get("/api/metrics/current", dependencies=[Depends(require_auth)])
    async def metrics_current() -> dict[str, Any]:
        collector: Collector = app.state.collector
        if collector.latest is None:
            return {"ok": False, "error": "No data yet — collector starting"}
        return collector.latest.model_dump()

    @app.get("/api/metrics/history", dependencies=[Depends(require_auth)])
    async def metrics_history(minutes: int = 60, metrics: str | None = None) -> dict[str, Any]:
        db: Database = app.state.db
        cols = metrics.split(",") if metrics else None
        rows = await db.query_history(minutes=minutes, metrics=cols)
        return {"ok": True, "data": rows}

    @app.get("/api/metrics/export.csv", dependencies=[Depends(require_auth)])
    async def metrics_export(minutes: int = 1440) -> StreamingResponse:
        db: Database = app.state.db
        rows = await db.query_history_csv(minutes=minutes)

        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=raidwatch_history.csv"},
        )

    # --- SSE live stream (D5/D12/D25/D28) ---
    @app.get("/api/stream")
    async def api_stream(_token: str = Depends(require_auth)):
        """Server-Sent Events: full snapshot first, then every cycle (D25/D28).

        Auth via HttpOnly cookie (D12) — EventSource sends it automatically.
        """
        from sse_starlette.sse import EventSourceResponse

        broker: Broker = app.state.broker
        collector: Collector = app.state.collector

        async def event_generator():
            # Subscribe — get the latest snapshot first for resync (D25).
            queue = await broker.subscribe(latest=collector.latest)
            try:
                while True:
                    snapshot = await queue.get()
                    yield {
                        "event": "snapshot",
                        "data": snapshot.model_dump_json(),
                    }
            finally:
                await broker.unsubscribe(queue)

        return EventSourceResponse(event_generator())

    # --- Fika endpoints (D3/D4) ---
    @app.get("/api/fika/status", dependencies=[Depends(require_auth)])
    async def fika_status() -> dict[str, Any]:
        """Process/config/events summary for the Fika module (D3)."""
        collector: Collector = app.state.collector
        snap = collector.latest
        if snap is None:
            return {"ok": False, "error": "No data yet"}
        return {"ok": True, "data": snap.fika.model_dump()}

    @app.get("/api/logs/tail", dependencies=[Depends(require_auth)])
    async def logs_tail(source: str = "fika", lines: int = 100) -> dict[str, Any]:
        """Recent log lines + metadata from fika_events (D17)."""
        db: Database = app.state.db
        events = await db.log_tail(source=source, lines=lines)
        return {"ok": True, "data": events}

    # --- Gates (D10/D19/D22) ---
    @app.get("/api/gates", dependencies=[Depends(require_auth)])
    async def gates_status() -> dict[str, Any]:
        """Current gate status + history (D10/D22)."""
        from raidwatch.gates import GateEvaluator

        config: AppConfig = app.state.config
        db: Database = app.state.db

        evaluator = GateEvaluator(config, db)
        all_statuses = await evaluator.all_statuses()
        history = await db.recent_gate_events(limit=50)

        active = [g for g in all_statuses if g.triggered]
        return {
            "ok": True,
            "active": [g.model_dump() for g in active],
            "all_gates": [g.model_dump() for g in all_statuses],
            "history": history,
        }


# Module-level app instance for ``uvicorn raidwatch.main:app``.
app = create_app()


def main() -> None:
    """Entry point for NSSM / ``python main.py``.

    Reads bind_host/port from config, defaults to 0.0.0.0:8080 (D11).
    """
    import uvicorn

    config = load_config()
    uvicorn.run(
        app,
        host=config.server.bind_host,
        port=config.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
