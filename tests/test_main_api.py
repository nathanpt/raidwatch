"""Tests for main.py routes via an in-process ASGI client (httpx).

The app's lifespan starts a real collector; to keep these tests hermetic we use
``create_app()`` (registers routes + static mount) but drive it through httpx's
ASGITransport, which does NOT run the lifespan. We populate ``app.state``
manually with a real Config/DB/Broker/Collector (collector unstarted) so the
routes read the same state shape the lifespan would produce.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.templating import Jinja2Templates

from raidwatch.auth import COOKIE_NAME
from raidwatch.broker import Broker
from raidwatch.collector import Collector
from raidwatch.config import AppConfig, AuthConfig, GateConfig
from raidwatch.database import Database, now_ms
from raidwatch.main import create_app
from raidwatch.models import MetricsSnapshot, SystemMetrics

TOKEN = "a" * 64
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _auth_cookie() -> dict[str, str]:
    """A raw Cookie header — avoids httpx's deprecated per-request ``cookies=``."""
    return {"Cookie": f"{COOKIE_NAME}={TOKEN}"}


@pytest.fixture
async def app(tmp_path):
    """A real app with hermetic state (no lifespan / no running collector)."""
    config = AppConfig(
        auth=AuthConfig(token=TOKEN),
        gates={
            "ram_pressure": GateConfig(
                enabled=True,
                threshold=95.0,
                metric="system.ram_percent",
                severity="high",
            )
        },
    )
    application = create_app()

    db = Database(str(tmp_path / "rw.db"))
    await db.connect()
    broker = Broker()
    collector = Collector(config=config, db=db, broker=broker)
    # Pretend the collector has already ticked once.
    collector.last_tick_ts = now_ms()
    collector.latest = MetricsSnapshot(
        ts=now_ms(),
        system=SystemMetrics(cpu_total_percent=10.0, ram_percent=40.0),
    )

    application.state.config = config
    application.state.db = db
    application.state.broker = broker
    application.state.collector = collector
    application.state.started_at = 1000
    application.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    yield application

    await broker.close()
    await db.close()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_health_shape(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"operational", "degraded", "critical"}
        for key in (
            "status",
            "version",
            "started_at",
            "collector",
            "modules",
            "sse_subscribers",
            "db_size_mb",
        ):
            assert key in body, f"missing {key} in /health"
        assert "last_tick_ts" in body["collector"]


class TestMetricsCurrent:
    @pytest.mark.asyncio
    async def test_unauthenticated_is_rejected(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/metrics/current")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_authenticated_returns_snapshot(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/metrics/current", headers=_auth_cookie())
        assert resp.status_code == 200
        body = resp.json()
        assert "ts" in body
        assert body["system"]["cpu_total_percent"] == 10.0


class TestGates:
    @pytest.mark.asyncio
    async def test_authenticated_returns_gate_payload(
        self, client: httpx.AsyncClient
    ) -> None:
        resp = await client.get("/api/gates", headers=_auth_cookie())
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["active"], list)
        assert isinstance(body["all_gates"], list)
        assert isinstance(body["history"], list)
        # The configured gate shows up in all_gates.
        assert any(g["gate_id"] == "ram_pressure" for g in body["all_gates"])

    @pytest.mark.asyncio
    async def test_unauthenticated_is_rejected(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/gates")
        assert resp.status_code == 401


class TestSSEStream:
    """GET /api/stream: auth-gated Server-Sent Events.

    The generator blocks indefinitely after the resync event, and streaming it
    through httpx's ASGI transport deadlocks against sse_starlette's task group
    (response.start never returns to the client). So we assert auth is enforced
    on the live endpoint (unauth → 401, proving it responds) and that the route
    is registered. Full SSE parsing is out of scope (per the assignment).
    """

    @pytest.mark.asyncio
    async def test_unauthenticated_is_rejected(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/stream")
        assert resp.status_code == 401

    def test_stream_route_registered(self, app) -> None:
        """The SSE endpoint exists and is wired into the app."""
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/api/stream" in paths
