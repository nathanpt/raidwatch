"""Tests for health: staleness precedence (D22/D35) and full response assembly.

Pure-Python — no Windows-only deps. ``build_health`` is driven by a fake
``app.state`` (SimpleNamespace) so we assert the contract, not the collector.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from raidwatch import __version__
from raidwatch.config import AppConfig
from raidwatch.health import STALE_THRESHOLD_CYCLES, build_health, compute_status


class TestComputeStatus:
    """Top-level status from collector liveness (D22 server-side precedence)."""

    def test_never_ticked_is_critical(self) -> None:
        """No tick at all → critical (None age)."""
        assert compute_status(None, interval_seconds=5.0, consecutive_failures=0) == "critical"

    def test_stale_is_critical(self) -> None:
        """Age just past interval*(cycles+1) → critical."""
        stale_limit = 5.0 * (STALE_THRESHOLD_CYCLES + 1)
        assert compute_status(stale_limit + 0.1, 5.0, 0) == "critical"

    def test_exactly_at_limit_is_operational(self) -> None:
        """The boundary itself is NOT stale (strict >)."""
        stale_limit = 5.0 * (STALE_THRESHOLD_CYCLES + 1)
        assert compute_status(stale_limit, 5.0, 0) == "operational"

    def test_fresh_is_operational(self) -> None:
        assert compute_status(1.0, 5.0, 0) == "operational"

    def test_sustained_failures_degrade(self) -> None:
        """>threshold consecutive failures (but not stale) → degraded."""
        assert compute_status(1.0, 5.0, STALE_THRESHOLD_CYCLES + 1) == "degraded"

    def test_stale_wins_over_failures(self) -> None:
        """Staleness dominates the failure counter."""
        age = 5.0 * (STALE_THRESHOLD_CYCLES + 1) + 1
        assert compute_status(age, 5.0, 99) == "critical"


class TestBuildHealth:
    """build_health assembles the full HealthResponse (D35)."""

    @pytest.mark.asyncio
    async def test_assembles_full_response(self) -> None:
        config = AppConfig()
        collector = SimpleNamespace(
            last_tick_age_seconds=2.0,
            consecutive_failures=0,
            last_tick_ts=123_456,
            last_cycle_ms=12.5,
            # Per-module tracking: 0 failures → ok, 3 → degraded, >=5 → backoff.
            _module_failures={"system": 0, "temps": 3, "fika": 7},
        )
        db = SimpleNamespace(db_size_mb=AsyncMock(return_value=4.2))
        broker = SimpleNamespace(subscriber_count=2)
        app_state = SimpleNamespace(
            collector=collector, db=db, broker=broker, started_at=999
        )

        result = await build_health(config, app_state)

        assert result["status"] == "operational"
        assert result["version"] == __version__
        assert result["started_at"] == 999

        assert result["collector"] == {
            "last_tick_ts": 123_456,
            "last_tick_age_seconds": 2.0,
            "last_cycle_ms": 12.5,
            "consecutive_failures": 0,
        }

        assert result["modules"]["system"]["state"] == "ok"
        assert result["modules"]["temps"]["state"] == "degraded"
        assert result["modules"]["fika"]["state"] == "backoff"
        assert result["modules"]["temps"]["consecutive_failures"] == 3

        assert result["sse_subscribers"] == 2
        assert result["db_size_mb"] == 4.2
