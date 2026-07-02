"""Pydantic models for the wire/data contract (§3.4), gate config, and API responses.

The snapshot shape defined here is the single source of truth for what the
collector produces, the DB persists (scalar subset), and the UI/SSE consumes.
All nullable fields are nullable so a degraded/missing module yields ``None``
rather than a schema violation (D8 contract).

Timestamps are UTC epoch milliseconds (D19).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Shared config: allow extra keys to be ignored (forward-compat)."""

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------- #
# System metrics (§3.4)                                                       #
# --------------------------------------------------------------------------- #
class DiskVolume(_Model):
    mount: str
    total_bytes: int
    free_bytes: int


class NetNicStats(_Model):
    sent_bps: int
    recv_bps: int
    errin: int = 0
    errout: int = 0
    dropout: int = 0


class SystemMetrics(_Model):
    cpu_total_percent: float | None = None
    cpu_per_core_percent: list[float] = Field(default_factory=list)
    ram_total_bytes: int | None = None
    ram_used_bytes: int | None = None
    ram_available_bytes: int | None = None
    ram_percent: float | None = None
    swap_total_bytes: int | None = None
    swap_used_bytes: int | None = None
    swap_percent: float | None = None
    pages_per_sec: float | None = None
    disk_read_bps: int | None = None
    disk_write_bps: int | None = None
    disk_queue_length: float | None = None
    disk_avg_sec_per_transfer: float | None = None
    disk_volumes: list[DiskVolume] = Field(default_factory=list)
    net_by_nic: dict[str, NetNicStats] = Field(default_factory=dict)
    temp_cpu_celsius: float | None = None
    whea_count_2h: int | None = None


# --------------------------------------------------------------------------- #
# Fika metrics (§3.4)                                                         #
# --------------------------------------------------------------------------- #
class ProcessInfo(_Model):
    """A single tracked process (SPT.Server or one headless instance)."""

    pid: int | None = None
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    uptime_seconds: int | None = None
    handle_count: int | None = None


class FikaConfigSummary(_Model):
    """Display-only config snapshot (read-only; D3)."""

    max_players: int | None = None
    bot_limits: str | None = None
    send_rate: str | None = None


class FikaEvent(_Model):
    """A parsed log event — decorative, never gate-feeding (D3)."""

    ts: int
    source: str
    severity: str = "info"  # info | warn | error
    message: str


class FikaMetrics(_Model):
    spt_server: ProcessInfo = Field(default_factory=ProcessInfo)
    headless: list[ProcessInfo] = Field(default_factory=list)
    headless_count: int = 0
    headless_cpu_total: float = 0.0
    headless_rss_total: int = 0
    config_summary: FikaConfigSummary = Field(default_factory=FikaConfigSummary)
    events_recent: list[FikaEvent] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Process table (top-others; D20)                                             #
# --------------------------------------------------------------------------- #
class TopProcess(_Model):
    pid: int
    name: str
    cpu_percent: float
    rss_bytes: int


class ProcessMetrics(_Model):
    top: list[TopProcess] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Self-metrics                                                                #
# --------------------------------------------------------------------------- #
class SelfMetrics(_Model):
    """Dashboard self-monitoring (in ``self.*`` of the snapshot)."""

    cpu_percent: float = 0.0
    rss_bytes: int = 0
    cycle_ms: float = 0.0
    subscribers: int = 0


# --------------------------------------------------------------------------- #
# Full snapshot (§3.4)                                                        #
# --------------------------------------------------------------------------- #
class MetricsSnapshot(_Model):
    """The canonical full snapshot pushed over SSE and returned by /current.

    This is the wire format — variable-cardinality lists are live-only (not all
    persisted as wide-table columns; see database.py for the scalar subset).
    """

    ts: int
    system: SystemMetrics = Field(default_factory=SystemMetrics)
    fika: FikaMetrics = Field(default_factory=FikaMetrics)
    process: ProcessMetrics = Field(default_factory=ProcessMetrics)
    self: SelfMetrics = Field(default_factory=SelfMetrics)


# --------------------------------------------------------------------------- #
# Module health states (for /health, D35)                                     #
# --------------------------------------------------------------------------- #
class ModuleState(_Model):
    """Per-module health: ok | degraded | backoff | error (D8/D35)."""

    state: str = "ok"
    consecutive_failures: int = 0
    last_error: str | None = None


class CollectorHealth(_Model):
    last_tick_ts: int | None = None
    last_tick_age_seconds: float | None = None
    last_cycle_ms: float = 0.0
    consecutive_failures: int = 0


class HealthResponse(_Model):
    """The /health machine-readable contract (D35)."""

    status: str  # operational | degraded | critical
    version: str
    started_at: int
    collector: CollectorHealth = Field(default_factory=CollectorHealth)
    modules: dict[str, ModuleState] = Field(default_factory=dict)
    sse_subscribers: int = 0
    db_size_mb: float = 0.0


# --------------------------------------------------------------------------- #
# Gate status (for /api/gates)                                                #
# --------------------------------------------------------------------------- #
class GateStatus(_Model):
    gate_id: str
    enabled: bool
    triggered: bool
    severity: str = "medium"
    metric: str = ""
    current_value: float | None = None
    threshold: float | None = None
    operator: str = ">"
    recommendation: str = ""
    last_triggered_ts: int | None = None
    trigger_count: int = 0


class GateHistoryEntry(_Model):
    ts: int
    gate_id: str
    action: str
    value: float | None = None
    severity: str | None = None


class GatesResponse(_Model):
    active: list[GateStatus] = Field(default_factory=list)
    all_gates: list[GateStatus] = Field(default_factory=list)
    history: list[GateHistoryEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# API response envelopes                                                      #
# --------------------------------------------------------------------------- #
class ApiResponse(BaseModel):
    """Generic success envelope."""

    ok: bool = True
    data: Any = None


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
