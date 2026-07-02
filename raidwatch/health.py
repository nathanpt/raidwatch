"""Health builder — the ``/health`` machine-readable contract (D35) + staleness logic.

The ``/health`` endpoint is load-bearing: it's what makes collector staleness
*machine-detectable* server-side (the D22 pill is human-facing only) and what
the external watchdog Scheduled Task curls to decide whether to restart the
service (D27).

Contract (D35):
    {
      "status": "operational" | "degraded" | "critical",
      "version": <str>,
      "started_at": <int epoch ms>,
      "collector": {
        "last_tick_ts": <int>,
        "last_tick_age_seconds": <float>,
        "last_cycle_ms": <float>,
        "consecutive_failures": <int>
      },
      "modules": {
        "system": {"state": "ok|degraded|backoff|error", ...},
        "fika": {...},
        "temps": {...}
      },
      "sse_subscribers": <int>,
      "db_size_mb": <float>
    }

Status precedence (D22, server-side): stale-core (>3 cycles) → critical; else
operational. Gate severity enriches the status in M5 but doesn't change the
core staleness precedence.
"""

from __future__ import annotations

import logging
from typing import Any

from raidwatch import __version__
from raidwatch.config import AppConfig
from raidwatch.models import HealthResponse

logger = logging.getLogger(__name__)

# A cycle is "stale" if the collector hasn't ticked in this many seconds.
# >3 missed cycles at 5s = ~15s (D22).
STALE_THRESHOLD_CYCLES = 3


def compute_status(
    last_tick_age_seconds: float | None,
    interval_seconds: float,
    consecutive_failures: int,
) -> str:
    """Compute the top-level health status from collector liveness (D22/D35).

    Precedence: stale-core (>3 cycles) → critical; else operational.
    Gate severity is layered on top in M5 (gates can degrade to "critical" or
    "degraded" but stale-core still wins).
    """
    if last_tick_age_seconds is None:
        return "critical"  # no data yet and never ticked

    stale_limit = interval_seconds * (STALE_THRESHOLD_CYCLES + 1)
    if last_tick_age_seconds > stale_limit:
        return "critical"

    if consecutive_failures > STALE_THRESHOLD_CYCLES:
        return "degraded"

    return "operational"


async def build_health(
    config: AppConfig,
    app_state: Any,
) -> dict[str, Any]:
    """Build the full /health response dict (D35).

    Args:
        config: The application config.
        app_state: ``FastAPI.app.state`` with ``collector``, ``db``, ``broker``,
            ``started_at`` attributes.
    """
    collector = app_state.collector
    db = app_state.db
    broker = app_state.broker

    age = collector.last_tick_age_seconds
    status = compute_status(
        last_tick_age_seconds=age,
        interval_seconds=config.collection.interval_seconds,
        consecutive_failures=collector.consecutive_failures,
    )

    # Build module health states from the collector's per-module tracking (D8).
    modules: dict[str, dict[str, Any]] = {}
    for name, failures in collector._module_failures.items():
        if failures == 0:
            state = "ok"
        elif failures >= 5:  # _MODULE_BACKOFF_THRESHOLD
            state = "backoff"
        else:
            state = "degraded"
        modules[name] = {"state": state, "consecutive_failures": failures}

    response = HealthResponse(
        status=status,
        version=__version__,
        started_at=app_state.started_at,
        collector={
            "last_tick_ts": collector.last_tick_ts,
            "last_tick_age_seconds": age,
            "last_cycle_ms": collector.last_cycle_ms,
            "consecutive_failures": collector.consecutive_failures,
        },
        modules=modules,
        sse_subscribers=broker.subscriber_count,
        db_size_mb=await db.db_size_mb(),
    )
    return response.model_dump()
