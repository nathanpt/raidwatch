"""Tests for gate timing: sustained duration, monotonic deltas, cooldown, restart restore (D10/D19)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from raidwatch.config import AppConfig, GateConfig
from raidwatch.gates import GateEvaluator, _check_operator, compute_status_pill
from raidwatch.models import GateStatus


@pytest.fixture
def mock_db():
    """A mock Database that simulates gate_state persistence."""
    db = MagicMock()
    db.get_gate_state = AsyncMock(return_value=None)
    db.upsert_gate_state = AsyncMock()
    db.insert_gate_event = AsyncMock()
    return db


@pytest.fixture
def evaluator(mock_db):
    """A GateEvaluator with a simple RAM gate enabled."""
    config = AppConfig(
        gates={
            "ram_high": GateConfig(
                enabled=True,
                threshold=85,
                operator=">",
                duration_seconds=10,
                severity="high",
                recommendation="Add RAM",
            ),
            "cpu_thermal": GateConfig(
                enabled=False,
                threshold=90,
                operator=">",
                duration_seconds=180,
                severity="high",
                recommendation="Check cooling",
            ),
        }
    )
    return GateEvaluator(config, mock_db)


def _ram_snapshot(percent: float) -> dict:
    return {"system": {"ram_percent": percent}}


class TestOperatorCheck:
    """Operator evaluation logic."""

    def test_greater_than(self) -> None:
        assert _check_operator(91, ">", 90) is True
        assert _check_operator(90, ">", 90) is False
        assert _check_operator(89, ">", 90) is False

    def test_less_than(self) -> None:
        assert _check_operator(14, "<", 15) is True
        assert _check_operator(15, "<", 15) is False

    def test_greater_equal(self) -> None:
        assert _check_operator(90, ">=", 90) is True

    def test_less_equal(self) -> None:
        assert _check_operator(15, "<=", 15) is True

    def test_equal(self) -> None:
        assert _check_operator(50, "==", 50) is True
        assert _check_operator(51, "==", 50) is False


class TestSustainedDuration:
    """Gates require sustained crossing before triggering (D10/D19)."""

    @pytest.mark.asyncio
    async def test_not_triggered_below_threshold(self, evaluator, mock_db) -> None:
        """Below threshold → not triggered."""
        result = await evaluator.evaluate(_ram_snapshot(50.0))
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_not_triggered_immediately_above(self, evaluator, mock_db) -> None:
        """Above threshold but not yet sustained → not triggered (records cross time)."""
        # First cycle above threshold — should record last_crossed but NOT trigger.
        result = await evaluator.evaluate(_ram_snapshot(90.0))
        assert len(result) == 0
        # State should have been persisted with last_crossed set.
        mock_db.upsert_gate_state.assert_called()
        call_args = mock_db.upsert_gate_state.call_args
        assert call_args[0][1] is not None  # last_crossed_monotonic

    @pytest.mark.asyncio
    async def test_triggered_after_sustained(self, evaluator, mock_db) -> None:
        """After sustained duration → triggered (D10)."""
        # Simulate state where crossing started 15s ago (duration is 10s).
        mock_db.get_gate_state = AsyncMock(
            return_value={
                "last_crossed_monotonic": time.monotonic() - 15,
                "currently_triggered": False,
                "last_triggered_ts": None,
                "trigger_count": 0,
            }
        )

        result = await evaluator.evaluate(_ram_snapshot(90.0))
        assert len(result) == 1
        assert result[0].gate_id == "ram_high"
        assert result[0].triggered is True
        # Should log a gate_event.
        mock_db.insert_gate_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_triggered_stays_triggered(self, evaluator, mock_db) -> None:
        """Once triggered, stays triggered while above threshold."""
        mock_db.get_gate_state = AsyncMock(
            return_value={
                "last_crossed_monotonic": time.monotonic() - 60,
                "currently_triggered": True,
                "last_triggered_ts": int(time.time() * 1000) - 60000,
                "trigger_count": 1,
            }
        )
        result = await evaluator.evaluate(_ram_snapshot(90.0))
        assert len(result) == 1
        assert result[0].trigger_count == 1  # no new trigger


class TestHysteresisClear:
    """Gates clear with hysteresis (D10: value must drop 10% below threshold)."""

    @pytest.mark.asyncio
    async def test_clears_when_drops_below_hysteresis(self, evaluator, mock_db) -> None:
        """Value drops below 90% of threshold → gate clears."""
        mock_db.get_gate_state = AsyncMock(
            return_value={
                "last_crossed_monotonic": time.monotonic() - 60,
                "currently_triggered": True,
                "last_triggered_ts": int(time.time() * 1000) - 60000,
                "trigger_count": 1,
            }
        )
        # 85 * 0.9 = 76.5; value 70 < 76.5 → clear
        result = await evaluator.evaluate(_ram_snapshot(70.0))
        assert len(result) == 0  # cleared

    @pytest.mark.asyncio
    async def test_does_not_clear_within_hysteresis(self, evaluator, mock_db) -> None:
        """Value between hysteresis and threshold → stays triggered."""
        mock_db.get_gate_state = AsyncMock(
            return_value={
                "last_crossed_monotonic": time.monotonic() - 60,
                "currently_triggered": True,
                "last_triggered_ts": int(time.time() * 1000) - 60000,
                "trigger_count": 1,
            }
        )
        # 85 * 0.9 = 76.5; value 80 > 76.5 but < 85 → stays triggered
        result = await evaluator.evaluate(_ram_snapshot(80.0))
        assert len(result) == 1


class TestStatusPill:
    """Layered status pill precedence (D22). stale > High > Medium > Operational."""

    def test_stale_wins(self) -> None:
        status, label = compute_status_pill(stale=True, triggered_gates=[], all_gates=[])
        assert status == "critical"
        assert "Stale" in label

    def test_high_gate(self) -> None:
        gate = GateStatus(gate_id="ram_high", enabled=True, triggered=True, severity="high")
        status, label = compute_status_pill(stale=False, triggered_gates=[gate], all_gates=[gate])
        assert status == "critical"
        assert "ram_high" in label

    def test_medium_gate(self) -> None:
        gate = GateStatus(gate_id="storage_io", enabled=True, triggered=True, severity="medium")
        status, label = compute_status_pill(stale=False, triggered_gates=[gate], all_gates=[gate])
        assert status == "degraded"
        assert "storage_io" in label

    def test_operational(self) -> None:
        status, label = compute_status_pill(stale=False, triggered_gates=[], all_gates=[])
        assert status == "operational"
        assert label == "Operational"

    def test_high_beats_medium(self) -> None:
        """High severity gate should take precedence over medium."""
        high = GateStatus(gate_id="ram_high", enabled=True, triggered=True, severity="high")
        medium = GateStatus(
            gate_id="cpu_sustained", enabled=True, triggered=True, severity="medium"
        )
        status, _ = compute_status_pill(stale=False, triggered_gates=[medium, high], all_gates=[])
        assert status == "critical"  # high wins


class TestDisabledGate:
    """Disabled gates never trigger (D9: cpu_thermal disabled by default)."""

    @pytest.mark.asyncio
    async def test_disabled_gate_not_evaluated(self, evaluator) -> None:
        """cpu_thermal is disabled and should not appear in triggered list."""
        snapshot = {"system": {"temp_cpu_celsius": 95.0, "ram_percent": 50.0}}
        result = await evaluator.evaluate(snapshot)
        # Only ram_high is enabled, and it's not triggered at 50%.
        assert len(result) == 0
