"""Tests for the collector: one cycle pipeline, D27 loop wrapping, D8 isolation.

Mirrors the async + mock style of ``tests/test_gate_timing.py``. The collector's
per-module sources (system/temps/fika) are mocked so the tests run on Linux dev
without pywin32/pythonnet. psutil-backed helpers (``_sample_top_processes``,
``_gather_self``) are stubbed where determinism matters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from raidwatch.collector import Collector
from raidwatch.config import AppConfig
from raidwatch.models import GateStatus

# Base shape of raidwatch.modules.system.gather() output. Handed out as a fresh
# copy per call — the collector mutates the dict it receives (it injects
# ``temp_cpu_celsius``), so a shared instance would leak state across cycles.
_SYSTEM_DICT = {
    "cpu_total_percent": 10.0,
    "ram_percent": 40.0,
    "ram_used_bytes": 1000,
}


@pytest.fixture
def mock_db() -> MagicMock:
    """A mock Database exposing only the methods the collector calls per cycle."""
    db = MagicMock()
    db.insert_metrics_row = AsyncMock()
    db.maybe_prune = AsyncMock()
    return db


@pytest.fixture
def broker() -> MagicMock:
    """A mock broker with a non-async publish() (D28 contract)."""
    b = MagicMock()
    b.publish = MagicMock()
    b.subscriber_count = 0
    return b


@pytest.fixture
def config() -> AppConfig:
    """Default config: no SPT path (Fika disabled), no gates, 5s interval."""
    return AppConfig()


def _new_collector(config: AppConfig, db: MagicMock, broker: MagicMock) -> Collector:
    return Collector(config=config, db=db, broker=broker)


def _stub_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    system: dict | None = None,
    temps: float | None = 55.0,
    top: list | None = None,
) -> None:
    """Patch the per-module gather functions the collector calls lazily.

    ``system`` is returned as a *copy* each call (see _SYSTEM_DICT note above).
    """
    sys_dict = system if system is not None else _SYSTEM_DICT
    monkeypatch.setattr("raidwatch.modules.system.gather", lambda **kw: {**sys_dict})
    monkeypatch.setattr("raidwatch.modules.temps.gather_temps", lambda *a, **kw: temps)
    monkeypatch.setattr("raidwatch.collector._sample_top_processes", lambda n=5: top or [])


class TestCycle:
    """One collection cycle: gather → persist → gate-eval → publish."""

    @pytest.mark.asyncio
    async def test_cycle_full_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: AppConfig,
        mock_db: MagicMock,
        broker: MagicMock,
    ) -> None:
        _stub_modules(monkeypatch)
        collector = _new_collector(config, mock_db, broker)

        canned_gates = [GateStatus(gate_id="ram", enabled=True, triggered=True)]
        collector._gate_eval = MagicMock()
        collector._gate_eval.evaluate = AsyncMock(return_value=canned_gates)

        await collector._cycle()

        # latest snapshot is set and carries the canned system metrics.
        assert collector.latest is not None
        assert collector.latest.system.cpu_total_percent == 10.0
        assert collector.latest.system.temp_cpu_celsius == 55.0  # temps merged in

        # live_buffer received exactly this snapshot.
        assert len(collector.live_buffer) == 1
        assert collector.live_buffer[0] is collector.latest

        # last_tick_ts tracks the snapshot timestamp.
        assert collector.last_tick_ts == collector.latest.ts

        # Persisted to SQLite (scalar subset) + maintenance prune ran.
        mock_db.insert_metrics_row.assert_awaited_once()
        mock_db.maybe_prune.assert_awaited_once()
        row = mock_db.insert_metrics_row.await_args.args[0]
        assert row["cpu_total_percent"] == 10.0
        assert row["temp_cpu_celsius"] == 55.0

        # Gate results populated from the evaluator.
        collector._gate_eval.evaluate.assert_awaited_once()
        assert collector.triggered_gates == canned_gates

        # Published to broker (non-blocking, not awaited).
        broker.publish.assert_called_once_with(collector.latest)

    @pytest.mark.asyncio
    async def test_live_buffer_appends_each_cycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: AppConfig,
        mock_db: MagicMock,
        broker: MagicMock,
    ) -> None:
        _stub_modules(monkeypatch)
        collector = _new_collector(config, mock_db, broker)
        collector._gate_eval = MagicMock()
        collector._gate_eval.evaluate = AsyncMock(return_value=[])

        for _ in range(3):
            await collector._cycle()

        assert len(collector.live_buffer) == 3
        assert all(s.system.cpu_total_percent == 10.0 for s in collector.live_buffer)


class TestModuleIsolation:
    """D8: one module raising does not blank the others."""

    @pytest.mark.asyncio
    async def test_temps_failure_keeps_system_metrics(
        self,
        monkeypatch: pytest.MonkeyPatch,
        config: AppConfig,
        mock_db: MagicMock,
        broker: MagicMock,
    ) -> None:
        def temps_boom(*a: object, **kw: object) -> None:
            raise RuntimeError("temps unavailable (no LHM)")

        _stub_modules(
            monkeypatch,
            system={**_SYSTEM_DICT, "cpu_total_percent": 77.0},
            temps=None,
        )
        monkeypatch.setattr("raidwatch.modules.temps.gather_temps", temps_boom)

        collector = _new_collector(config, mock_db, broker)
        snapshot = await collector._gather_snapshot()

        # system module survived — its metric is intact.
        assert snapshot.system.cpu_total_percent == 77.0

        # temps failure recorded but did not propagate.
        assert collector._module_failures.get("temps") == 1
        assert collector._module_failures.get("system", 0) == 0

        # temps produced nothing — temp sensor stays absent (no crash, no None-mask).
        assert snapshot.system.temp_cpu_celsius is None


class TestLoopWrapping:
    """D27: an exception inside _cycle is caught; the loop task does NOT die."""

    @pytest.mark.asyncio
    async def test_loop_survives_cycle_exception(
        self,
        config: AppConfig,
        mock_db: MagicMock,
        broker: MagicMock,
    ) -> None:
        config.collection.interval_seconds = 0.01  # tight scheduling for the test
        collector = _new_collector(config, mock_db, broker)

        calls: list[int] = []

        async def flaky_cycle() -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            # Second cycle succeeds — then request a clean stop.
            collector._stop_event.set()

        collector._cycle = flaky_cycle  # type: ignore[method-assign]
        collector.start()
        await collector._task  # returns when the loop exits via stop_event

        # The first cycle raised, yet a second cycle ran — the task did not die.
        assert len(calls) == 2
        assert collector._task.exception() is None
        # consecutive_failures incremented after the failure, then reset on success.
        assert collector.consecutive_failures == 0
