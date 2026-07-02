"""The collector — async 5s loop, the heartbeat of a 24/7 service (D8/D27).

Key reliability properties:
- **Loop body fully wrapped** (D27): the entire per-cycle body (gather + persist
  + gates + publish) is inside try/except-log-continue, so the task cannot die
  from any raised exception.
- **Per-module isolation** (D8): each metric source in its own try/except;
  failure returns ``None`` for that key + a per-module error counter. After N
  consecutive failures a module backs off (~60s).
- **No-overlap scheduling**: the next cycle is scheduled 5s after *completion*
  (cycles never overlap; slight drift acceptable).
- **Non-blocking publish** (D28): ``publish()`` drops to the broker and never
  awaits a client.

Modules return dicts merged under namespaced keys (``system.*``, ``fika.*``) (D6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

import psutil

from raidwatch.config import AppConfig
from raidwatch.database import Database, now_ms
from raidwatch.models import MetricsSnapshot, ProcessMetrics, TopProcess

logger = logging.getLogger(__name__)

# Backoff: after this many consecutive failures, a module polls every ~60s (D8).
_MODULE_BACKOFF_THRESHOLD = 5
_MODULE_BACKOFF_SECONDS = 60.0


class Collector:
    """Runs the periodic metrics collection loop.

    The collector is started in the FastAPI lifespan and supervised by
    :class:`~raidwatch.supervisor.Supervisor` (D27).
    """

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        broker: Any,  # broker.Broker; typed as Any to avoid circular import
    ) -> None:
        self._config = config
        self._db = db
        self._broker = broker

        # Scheduling
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._interval = config.collection.interval_seconds

        # State exposed to /health and the supervisor (D27/D35)
        self.last_tick_ts: int | None = None
        self.last_cycle_ms: float = 0.0
        self.consecutive_failures: int = 0

        # In-memory live buffer for charts (1h @ 5s = 720 points)
        self.live_buffer: deque[MetricsSnapshot] = deque(maxlen=720)

        # Fika module (None if not configured; D23)
        from raidwatch.modules.fika import FikaModule

        self._fika_module = FikaModule(config) if config.server.spt_path else None
        if self._fika_module is None:
            logger.info("Fika module disabled — set server.spt_path in config.yaml (D23)")

        # Gate evaluator (D10/D19) — lazy import to avoid circular dep
        from raidwatch.gates import GateEvaluator

        self._gate_eval = GateEvaluator(config, db)
        # Latest gate results for the status pill (D22)
        self.triggered_gates: list = []

        # Per-module failure tracking (D8)
        self._module_failures: dict[str, int] = {}
        self._module_last_poll: dict[str, float] = {}

        # Top-others process sampling state (D20)
        self._top_others: list[TopProcess] = []
        self._last_top_others_mono = 0.0

        # Latest snapshot (for /api/metrics/current without hitting the DB)
        self.latest: MetricsSnapshot | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Start the collector task (called from lifespan)."""
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_forever(), name="collector")

    async def stop(self) -> None:
        """Signal the collector to stop and wait for it."""
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    @property
    def last_tick_age_seconds(self) -> float | None:
        if self.last_tick_ts is None:
            return None
        return (now_ms() - self.last_tick_ts) / 1000.0

    # ------------------------------------------------------------------ #
    # Main loop (D27)                                                    #
    # ------------------------------------------------------------------ #
    async def _run_forever(self) -> None:
        logger.info("Collector loop started (interval=%.1fs)", self._interval)
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                await self._cycle()
                self.consecutive_failures = 0
            except Exception:
                # D27: the loop body cannot die from any raised exception.
                self.consecutive_failures += 1
                logger.exception(
                    "Collector cycle failed (consecutive_failures=%d)",
                    self.consecutive_failures,
                )
            finally:
                self.last_cycle_ms = (time.monotonic() - cycle_start) * 1000.0

            # Schedule next cycle 5s after completion (no overlap; D8).
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.1, self._interval - elapsed)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)

        logger.info("Collector loop stopped.")

    async def _cycle(self) -> None:
        """One collection cycle: gather → persist → gates → publish (D27)."""
        # --- Gather metrics from each module (D6 namespaced merge, D8 isolation) ---
        snapshot = await self._gather_snapshot()
        self.latest = snapshot
        self.last_tick_ts = snapshot.ts
        self.live_buffer.append(snapshot)

        # --- Persist to SQLite (scalar subset only; D14) ---
        await self._persist(snapshot)

        # --- Evaluate gates (D10/D19) ---
        try:
            self.triggered_gates = await self._gate_eval.evaluate(snapshot.model_dump())
        except Exception:
            logger.exception("Gate evaluation failed")
            self.triggered_gates = []

        # --- Publish to broker (non-blocking; D28) ---
        self._broker.publish(snapshot)

        # --- Periodic maintenance ---
        await self._db.maybe_prune(self._config.collection.history_retention_hours)

    # ------------------------------------------------------------------ #
    # Gathering                                                          #
    # ------------------------------------------------------------------ #
    async def _gather_snapshot(self) -> MetricsSnapshot:
        """Gather metrics from all modules and assemble the snapshot (D6/D8)."""
        from raidwatch.modules import system as system_mod
        from raidwatch.modules import temps as temps_mod

        ts = now_ms()

        # System metrics (psutil + pywin32) — isolated (D8).
        system_metrics: dict[str, Any] = {}
        system_metrics = await self._call_module(
            "system",
            lambda: system_mod.gather(
                whea_interval_seconds=self._config.collection.whea_poll_seconds
            ),
            system_metrics,
        )

        # Temps (LHM via pythonnet; D9) — isolated (D8). Display ON, gate disabled.
        temps_metrics = await self._call_module(
            "temps",
            lambda: temps_mod.gather_temps(
                self._config.temps.lhm_dll_path,
                self._config.temps.cpu_sensor_name,
                self._config.temps.tctl_offset,
            ),
            None,
        )
        if isinstance(temps_metrics, float | int):
            system_metrics["temp_cpu_celsius"] = float(temps_metrics)

        # Fika metrics — isolated (D8). Disabled if no SPT path configured (D23).
        fika_metrics: dict[str, Any] = {}
        if self._fika_module is not None:
            fika_result = await self._call_module(
                "fika",
                lambda: self._fika_module.gather(db=self._db).model_dump(),
                {},
            )
            if isinstance(fika_result, dict):
                fika_metrics = fika_result

        # Top-others process table (D20) — sampled on its own timer.
        top = await self._maybe_sample_top_others()

        # Self-metrics.
        self_metrics = await self._gather_self(cycle_start_mono=ts)

        return MetricsSnapshot(
            ts=ts,
            system=system_metrics,  # type: ignore[arg-type]
            fika=fika_metrics,  # type: ignore[arg-type]
            process=ProcessMetrics(top=top),
            self=self_metrics,  # type: ignore[arg-type]
        )

    async def _call_module(
        self,
        name: str,
        fn: Any,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """Call a module function with D8 isolation + backoff.

        On failure, returns ``fallback`` and increments the module's failure
        counter. After :data:`_MODULE_BACKOFF_THRESHOLD` consecutive failures,
        the module is polled at most every :data:`_MODULE_BACKOFF_SECONDS`.
        """
        # Backoff check: skip if in backoff and not yet time to retry.
        failures = self._module_failures.get(name, 0)
        last_poll = self._module_last_poll.get(name, 0.0)
        if (
            failures >= _MODULE_BACKOFF_THRESHOLD
            and time.monotonic() - last_poll < _MODULE_BACKOFF_SECONDS
        ):
            return fallback

        try:
            self._module_last_poll[name] = time.monotonic()
            # Module functions are sync (psutil/pywin32); run in executor to
            # avoid blocking the event loop on I/O-heavy gathers.
            result = await asyncio.to_thread(fn)
            self._module_failures[name] = 0
            return result
        except Exception:
            self._module_failures[name] = failures + 1
            logger.warning(
                "Module '%s' failed (consecutive=%d) — degraded",
                name,
                self._module_failures[name],
                exc_info=True,
            )
            return fallback

    async def _maybe_sample_top_others(self) -> list[TopProcess]:
        """Sample the top-5 'other' processes every ~15s (D20)."""
        interval = self._config.collection.top_others_poll_seconds
        now_mono = time.monotonic()
        if now_mono - self._last_top_others_mono < interval and self._top_others:
            return self._top_others
        self._last_top_others_mono = now_mono
        try:
            self._top_others = await asyncio.to_thread(_sample_top_processes, n=5)
        except Exception:
            logger.exception("top-others process sampling failed")
        return self._top_others

    async def _gather_self(self, cycle_start_mono: int) -> dict[str, Any]:
        """Dashboard self-metrics (CPU/RAM/cycle_ms/subscribers)."""
        try:
            proc = psutil.Process()
            with proc.oneshot():
                cpu = proc.cpu_percent()
                rss = proc.memory_info().rss
        except Exception:
            cpu, rss = 0.0, 0
        subs = getattr(self._broker, "subscriber_count", 0)
        return {
            "cpu_percent": cpu,
            "rss_bytes": rss,
            "cycle_ms": self.last_cycle_ms,
            "subscribers": subs,
        }

    # ------------------------------------------------------------------ #
    # Persistence (D14)                                                  #
    # ------------------------------------------------------------------ #
    async def _persist(self, snapshot: MetricsSnapshot) -> None:
        """Persist the scalar subset of the snapshot to the wide table (D14)."""
        sys_m = snapshot.system
        fika = snapshot.fika

        # Determine game-drive free bytes (largest game volume or first volume).
        game_free: int | None = None
        if sys_m.disk_volumes:
            game_free = sys_m.disk_volumes[0].free_bytes

        # Aggregate net totals for the wide table.
        net_sent = sum(n.sent_bps for n in sys_m.net_by_nic.values()) if sys_m.net_by_nic else None
        net_recv = sum(n.recv_bps for n in sys_m.net_by_nic.values()) if sys_m.net_by_nic else None
        net_errs = (
            sum(n.errin + n.errout for n in sys_m.net_by_nic.values()) if sys_m.net_by_nic else None
        )

        row = {
            "ts": snapshot.ts,
            "cpu_total_percent": sys_m.cpu_total_percent,
            "ram_percent": sys_m.ram_percent,
            "ram_used_bytes": sys_m.ram_used_bytes,
            "swap_percent": sys_m.swap_percent,
            "pages_per_sec": sys_m.pages_per_sec,
            "disk_read_bps": sys_m.disk_read_bps,
            "disk_write_bps": sys_m.disk_write_bps,
            "disk_queue_length": sys_m.disk_queue_length,
            "disk_avg_sec_per_transfer": sys_m.disk_avg_sec_per_transfer,
            "disk_game_free_bytes": game_free,
            "net_sent_bps": net_sent,
            "net_recv_bps": net_recv,
            "net_errs_total": net_errs,
            "temp_cpu_celsius": sys_m.temp_cpu_celsius,
            "whea_count_2h": sys_m.whea_count_2h,
            "fika_spt_cpu_percent": fika.spt_server.cpu_percent,
            "fika_spt_rss_bytes": fika.spt_server.rss_bytes,
            "fika_headless_count": fika.headless_count,
            "fika_headless_cpu_total": fika.headless_cpu_total,
            "fika_headless_rss_total": fika.headless_rss_total,
        }
        await self._db.insert_metrics_row(row)


# --------------------------------------------------------------------------- #
# Top-others process sampling (D20)                                           #
# --------------------------------------------------------------------------- #
def _sample_top_processes(n: int = 5) -> list[TopProcess]:
    """Return the top-N processes by CPU usage (excluding known game procs later)."""
    procs: list[TopProcess] = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            info = p.info
            cpu = info.get("cpu_percent", 0.0) or 0.0
            mem = info.get("memory_info")
            rss = mem.rss if mem else 0
            procs.append(
                TopProcess(
                    pid=info["pid"],
                    name=info["name"] or "?",
                    cpu_percent=cpu,
                    rss_bytes=rss,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: x.cpu_percent, reverse=True)
    return procs[:n]
