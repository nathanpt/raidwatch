"""Gate evaluator — stateful sustained-duration state machines (D8/D10/D16/D19).

Gates turn observed metrics into concrete upgrade recommendations. Each gate is
configurable: ``metric``, ``operator``, ``threshold``, ``duration_seconds``,
``severity``, ``recommendation_text``, ``enabled``.

Key properties:
- **Monotonic durations** (D19): sustained-duration logic uses ``time.monotonic()``
  deltas exclusively — wall-clock only labels, never measures elapsed time.
- **Conservative v1 defaults** (D10): all hardware gates ship enabled with
  conservative thresholds; ``cpu_thermal`` ships disabled (D9).
- **WHEA windowed** (D16): the stability gate uses the sliding 2h count.
- **State persisted** in SQLite (survives restarts).
- **Cooldown/hysteresis**: re-alert only after 30 min or value drops 10% below.
- **Layered status pill** (D22): stale-core > High gate > Medium gate > Operational.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from raidwatch.config import AppConfig, GateConfig
from raidwatch.database import Database, now_ms
from raidwatch.models import GateStatus

logger = logging.getLogger(__name__)

# Cooldown: don't re-alert within this window (D10).
COOLDOWN_SECONDS = 1800  # 30 min
# Hysteresis: value must drop this fraction below threshold to clear (D10).
HYSTERESIS_FACTOR = 0.9

# Map gate IDs to their metric extraction path in the snapshot (§3.4 contract).
# Each gate's ``metric`` config field is matched against these known paths.
GATE_METRIC_MAP: dict[str, str] = {
    "ram_high": "system.ram_percent",
    "cpu_sustained": "system.cpu_total_percent",
    "storage_io": "system.disk_queue_length",
    "storage_space": "storage.free_percent",
    "stability_whea": "system.whea_count_2h",
    "cpu_thermal": "system.temp_cpu_celsius",
}

# Default operator per gate (if not specified in config).
GATE_DEFAULT_OPERATOR: dict[str, str] = {
    "storage_space": "<",  # fires when free space is BELOW threshold
}


def _extract_metric(snapshot: dict[str, Any], metric_path: str) -> float | None:
    """Extract a scalar metric value from the snapshot by dotted path (e.g. 'system.ram_percent')."""
    parts = metric_path.split(".")
    current: Any = snapshot
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    if current is None:
        return None
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def _extract_free_percent(snapshot: dict[str, Any]) -> float | None:
    """Special case: extract game-drive free space percentage (D10)."""
    volumes = snapshot.get("system", {}).get("disk_volumes", [])
    if not volumes:
        return None
    v = volumes[0]
    if v.get("total_bytes", 0) <= 0:
        return None
    return (v["free_bytes"] / v["total_bytes"]) * 100


class GateEvaluator:
    """Evaluates all configured gates against the latest snapshot.

    State (last_crossed via time.monotonic(), currently_triggered) is persisted
    in SQLite so it survives restarts (D19).
    """

    def __init__(self, config: AppConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._gates: dict[str, GateConfig] = {
            gid: g for gid, g in config.gates.items() if g.enabled
        }
        # In-memory cache of restored state (loaded lazily on first evaluate).
        self._state_loaded: dict[str, bool] = {}

    async def evaluate(self, snapshot: dict[str, Any]) -> list[GateStatus]:
        """Evaluate all enabled gates; return list of triggered gates.

        Side effects: persists state changes to SQLite, logs gate_events on
        transitions.
        """
        triggered: list[GateStatus] = []

        for gate_id, gate_cfg in self._gates.items():
            status = await self._evaluate_gate(gate_id, gate_cfg, snapshot)
            if status.triggered:
                triggered.append(status)

        return triggered

    async def _evaluate_gate(
        self,
        gate_id: str,
        gate_cfg: GateConfig,
        snapshot: dict[str, Any],
    ) -> GateStatus:
        """Evaluate a single gate with sustained-duration state machine (D10/D19)."""
        # Get the metric value.
        metric_path = gate_cfg.metric or GATE_METRIC_MAP.get(gate_id, "")
        value: float | None = None

        if metric_path == "storage.free_percent":
            value = _extract_free_percent(snapshot)
        else:
            value = _extract_metric(snapshot, metric_path)

        operator = gate_cfg.operator or GATE_DEFAULT_OPERATOR.get(gate_id, ">")

        # Load persisted state (or defaults for first run).
        state = await self._db.get_gate_state(gate_id)
        if state is None:
            state = {
                "last_crossed_monotonic": None,
                "currently_triggered": False,
                "last_triggered_ts": None,
                "trigger_count": 0,
            }
        last_crossed = state["last_crossed_monotonic"]
        currently_triggered = state["currently_triggered"]
        last_triggered_ts = state["last_triggered_ts"]
        trigger_count = state["trigger_count"]

        now_mono = time.monotonic()

        # Determine if the metric is currently crossing the threshold.
        crossed = False
        if value is not None:
            crossed = _check_operator(value, operator, gate_cfg.threshold)

        if crossed:
            # First crossing: record the monotonic time.
            if last_crossed is None:
                last_crossed = now_mono
            # Check sustained duration.
            elapsed = now_mono - last_crossed
            if elapsed >= gate_cfg.duration_seconds and not currently_triggered:
                # Transition: not-triggered → triggered.
                currently_triggered = True
                last_triggered_ts = now_ms()
                trigger_count += 1
                await self._db.insert_gate_event(
                    last_triggered_ts, gate_id, "triggered", value, gate_cfg.severity
                )
                logger.warning(
                    "Gate '%s' TRIGGERED: %s %.1f %s %.1f (sustained %.0fs)",
                    gate_id,
                    metric_path,
                    value,
                    operator,
                    gate_cfg.threshold,
                    gate_cfg.duration_seconds,
                )
        else:
            # Not crossing. Check hysteresis for clearing (D10).
            # For '>' gates: clear when value drops below threshold * 0.9
            # For '<' gates: clear when value rises above threshold / 0.9
            clear_threshold_gt = gate_cfg.threshold * HYSTERESIS_FACTOR
            clear_threshold_lt = gate_cfg.threshold / HYSTERESIS_FACTOR
            should_clear = False
            if value is not None:
                if (operator in (">", ">=") and value < clear_threshold_gt) or (
                    operator in ("<", "<=") and value > clear_threshold_lt
                ):
                    should_clear = True
                # If in the hysteresis zone (between clear_threshold and
                # threshold), do NOT clear — prevents oscillation (D10).
            elif currently_triggered:
                # Metric became unavailable — clear to avoid stuck gate.
                should_clear = True

            if should_clear and currently_triggered:
                currently_triggered = False
                last_crossed = None
                await self._db.insert_gate_event(
                    now_ms(), gate_id, "cleared", value, gate_cfg.severity
                )
                logger.info("Gate '%s' CLEARED (value=%.1f)", gate_id, value or -1)

        # Persist updated state.
        await self._db.upsert_gate_state(
            gate_id, last_crossed, currently_triggered, last_triggered_ts, trigger_count
        )

        return GateStatus(
            gate_id=gate_id,
            enabled=True,
            triggered=currently_triggered,
            severity=gate_cfg.severity,
            metric=metric_path,
            current_value=value,
            threshold=gate_cfg.threshold,
            operator=operator,
            recommendation=gate_cfg.recommendation,
            last_triggered_ts=last_triggered_ts,
            trigger_count=trigger_count,
        )

    async def all_statuses(self, snapshot: dict[str, Any] | None = None) -> list[GateStatus]:
        """Return status of all gates (enabled + disabled) for the /api/gates endpoint."""
        statuses: list[GateStatus] = []
        for gate_id, gate_cfg in self._config.gates.items():
            if not gate_cfg.enabled:
                statuses.append(
                    GateStatus(
                        gate_id=gate_id,
                        enabled=False,
                        triggered=False,
                        severity=gate_cfg.severity,
                        recommendation=gate_cfg.recommendation,
                        threshold=gate_cfg.threshold,
                    )
                )
                continue
            # Use last evaluated status if available.
            state = await self._db.get_gate_state(gate_id)
            statuses.append(
                GateStatus(
                    gate_id=gate_id,
                    enabled=True,
                    triggered=bool(state["currently_triggered"]) if state else False,
                    severity=gate_cfg.severity,
                    recommendation=gate_cfg.recommendation,
                    threshold=gate_cfg.threshold,
                    last_triggered_ts=state["last_triggered_ts"] if state else None,
                    trigger_count=state["trigger_count"] if state else 0,
                )
            )
        return statuses


def _check_operator(value: float, operator: str, threshold: float) -> bool:
    """Check if value satisfies the operator against threshold."""
    match operator:
        case ">":
            return value > threshold
        case "<":
            return value < threshold
        case ">=":
            return value >= threshold
        case "<=":
            return value <= threshold
        case "==":
            return value == threshold
        case "!=":
            return value != threshold
        case _:
            return False


def compute_status_pill(
    stale: bool,
    triggered_gates: list[GateStatus],
    all_gates: list[GateStatus],
) -> tuple[str, str]:
    """Compute the layered status pill (D22).

    Precedence: stale-core > High gate > Medium gate > Operational.
    Returns (status_class, label).
    """
    if stale:
        return "critical", "Monitoring Degraded — Stale Data"

    # Check active triggered gates by severity.
    for g in triggered_gates:
        if g.severity == "high":
            return "critical", f"Critical: {g.gate_id}"
    for g in triggered_gates:
        if g.severity == "medium":
            return "degraded", f"Degraded: {g.gate_id}"

    return "operational", "Operational"
