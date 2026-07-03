"""System metrics via psutil + pywin32 (in-process, no PowerShell; D7).

On Linux (dev), the pywin32-backed metrics (disk queue length, pages/sec, WHEA)
degrade to ``None`` — the import is guarded and the module returns whatever the
platform supports. Each source is in its own try/except per D8 isolation.

Collection cadence:
- Core psutil metrics: every 5s (collector cycle).
- pywin32 PerfMon counters (disk queue, pages/sec): every 5s (same cycle).
- WHEA (win32evtlog): every ~60s via :data:`whea_poll_seconds` (D7/D16).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import psutil

from raidwatch.models import DiskVolume, NetNicStats

logger = logging.getLogger(__name__)

# Guard the pywin32 import (Windows-only). On Linux these are simply unavailable.
_WIN32_AVAILABLE = False
try:
    if _is_win := (__import__("sys").platform == "win32"):
        import win32evtlog  # noqa: F401
        import win32pdh  # noqa: F401

        _WIN32_AVAILABLE = True
except ImportError:
    _is_win = False

# Track last WHEA poll time so we only re-query the event log every whea_poll_seconds.
_last_whea_poll_monotonic: float = 0.0
# Cache of the most recent WHEA count so it stays visible between ~60s polls
# (otherwise the count is null on 11/12 cycles and looks unavailable; D16).
_last_whea_count: int | None = None


# --------------------------------------------------------------------------- #
# psutil metrics (cross-platform)                                            #
# --------------------------------------------------------------------------- #
def _gather_cpu() -> dict[str, Any]:
    """CPU total % and per-core list."""
    try:
        per_core = psutil.cpu_percent(percpu=True, interval=0.5)
        total = sum(per_core) / len(per_core) if per_core else 0.0
        return {"cpu_total_percent": total, "cpu_per_core_percent": per_core}
    except Exception:
        logger.exception("cpu_percent failed")
        return {"cpu_total_percent": None, "cpu_per_core_percent": []}


def _gather_ram() -> dict[str, Any]:
    """RAM + swap metrics."""
    out: dict[str, Any] = {}
    try:
        vm = psutil.virtual_memory()
        out.update(
            {
                "ram_total_bytes": vm.total,
                "ram_used_bytes": vm.used,
                "ram_available_bytes": vm.available,
                "ram_percent": vm.percent,
            }
        )
    except Exception:
        logger.exception("virtual_memory failed")
        out.update(
            {
                "ram_total_bytes": None,
                "ram_used_bytes": None,
                "ram_available_bytes": None,
                "ram_percent": None,
            }
        )
    try:
        sm = psutil.swap_memory()
        out.update(
            {
                "swap_total_bytes": sm.total,
                "swap_used_bytes": sm.used,
                "swap_percent": sm.percent,
            }
        )
    except Exception:
        logger.exception("swap_memory failed")
        out.update({"swap_total_bytes": None, "swap_used_bytes": None, "swap_percent": None})
    return out


# Module-level previous disk I/O reading for rate computation.
_prev_disk_io: dict[str, float] | None = None


def _gather_disk_io() -> dict[str, Any]:
    """Disk I/O throughput (bytes/s) — computed as rate since last call."""
    global _prev_disk_io
    out: dict[str, Any] = {}
    try:
        counters = psutil.disk_io_counters(perdisk=False)
        if counters is None:
            out["disk_read_bps"] = None
            out["disk_write_bps"] = None
            return out

        now_mono = time.monotonic()
        current = {"read": counters.read_bytes, "write": counters.write_bytes, "ts": now_mono}
        if _prev_disk_io is not None:
            dt = now_mono - _prev_disk_io["ts"]
            if dt > 0:
                read_bps = int((current["read"] - _prev_disk_io["read"]) / dt)
                write_bps = int((current["write"] - _prev_disk_io["write"]) / dt)
                out["disk_read_bps"] = max(read_bps, 0)
                out["disk_write_bps"] = max(write_bps, 0)
            else:
                out["disk_read_bps"] = 0
                out["disk_write_bps"] = 0
        else:
            # First call — no baseline yet.
            out["disk_read_bps"] = 0
            out["disk_write_bps"] = 0
        _prev_disk_io = current
    except Exception:
        logger.exception("disk_io_counters failed")
        out["disk_read_bps"] = None
        out["disk_write_bps"] = None
    return out


def _gather_disk_volumes() -> dict[str, Any]:
    """Free space per mounted volume."""
    volumes: list[DiskVolume] = []
    try:
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                volumes.append(
                    DiskVolume(
                        mount=part.mountpoint,
                        total_bytes=usage.total,
                        free_bytes=usage.free,
                    )
                )
            except (PermissionError, OSError):
                continue
    except Exception:
        logger.exception("disk_partitions failed")
    return {"disk_volumes": volumes}


def _gather_net() -> dict[str, Any]:
    """Network bytes/s + errors/drops per NIC."""
    out: dict[str, Any] = {"net_by_nic": {}}
    try:
        per_nic = psutil.net_io_counters(pernic=True)
        for nic, stats in per_nic.items():
            out["net_by_nic"][nic] = NetNicStats(
                sent_bps=stats.bytes_sent,
                recv_bps=stats.bytes_recv,
                errin=stats.errin,
                errout=stats.errout,
                dropout=stats.dropout,
            )
    except Exception:
        logger.exception("net_io_counters failed")
    return out


# --------------------------------------------------------------------------- #
# pywin32 PerfMon counters (Windows only; D7)                                #
# --------------------------------------------------------------------------- #
def _gather_win32_perfmon() -> dict[str, Any]:
    """Disk queue length, avg sec/transfer, pages/sec via win32pdh (D7).

    Rate/average counters (Avg. Disk sec/Transfer, Pages/sec) require TWO
    ``CollectQueryData`` samples for PDH to compute a value; a single sample
    raises ``PDH_CALC_NEGATIVE_DENOMINATOR``. All counters share one query:
    add them, collect, wait briefly, collect again, then read each. Each value
    read is isolated (D8) so one bad counter can't blank the others.
    """
    if not _WIN32_AVAILABLE:
        return {
            "disk_queue_length": None,
            "disk_avg_sec_per_transfer": None,
            "pages_per_sec": None,
        }

    paths = {
        "disk_queue_length": r"\PhysicalDisk(_Total)\Current Disk Queue Length",
        "disk_avg_sec_per_transfer": r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer",
        "pages_per_sec": r"\Memory\Pages/sec",
    }
    out: dict[str, Any] = dict.fromkeys(paths)

    try:
        import win32pdh  # type: ignore[import-not-found]

        query = win32pdh.OpenQuery()
        counters: dict[str, Any] = {}
        try:
            for key, path in paths.items():
                try:
                    counters[key] = win32pdh.AddCounter(query, path)
                except Exception:
                    logger.exception("win32pdh AddCounter failed: %s", path)

            # Two samples so rate/average counters can compute (D7).
            win32pdh.CollectQueryData(query)
            time.sleep(0.1)
            win32pdh.CollectQueryData(query)

            for key, handle in counters.items():
                try:
                    _msg_type, value = win32pdh.GetFormattedCounterValue(
                        handle, win32pdh.PDH_FMT_DOUBLE
                    )
                    out[key] = float(value)
                except Exception:
                    logger.exception("win32pdh GetFormattedCounterValue failed: %s", paths[key])
        finally:
            for handle in counters.values():
                with contextlib.suppress(Exception):
                    win32pdh.RemoveCounter(handle)
            with contextlib.suppress(Exception):
                win32pdh.CloseQuery(query)
    except Exception:
        logger.exception("win32pdh query setup failed")
    return out


# --------------------------------------------------------------------------- #
# WHEA (Windows only; D16)                                                   #
# --------------------------------------------------------------------------- #
def gather_whea(window_hours: float = 2.0) -> dict[str, Any]:
    """Windowed WHEA re-query: count events in the last ``window_hours`` (D16).

    Returns ``{"whea_count_2h": <int>, "whea_events": [...]}``. On non-Windows,
    the count is None (module unavailable).
    """
    if not _WIN32_AVAILABLE:
        return {"whea_count_2h": None, "whea_events": []}
    try:
        events = _query_whea_events(window_hours)
        return {
            "whea_count_2h": len(events),
            "whea_events": events,
        }
    except Exception:
        logger.exception("WHEA query failed")
        return {"whea_count_2h": None, "whea_events": []}


def _query_whea_events(window_hours: float) -> list[dict[str, Any]]:
    """Read WHEA-Logger events from the System log within the time window (D16)."""
    import win32evtlog  # type: ignore[import-not-found]

    server = "localhost"
    logtype = "System"
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    cutoff = time.time() - window_hours * 3600  # seconds since epoch

    events: list[dict[str, Any]] = []
    handle = win32evtlog.OpenEventLog(server, logtype)
    try:
        while True:
            records = win32evtlog.ReadEventLog(handle, flags, 0)
            if not records:
                break
            stop = False
            for rec in records:
                # WHEA-Logger provider GUID varies; check by provider name.
                provider = str(rec.SourceName)
                if provider != "Microsoft-Windows-WHEA-Logger":
                    continue
                if rec.TimeGenerated.timestamp() < cutoff:
                    stop = True
                    break
                events.append(
                    {
                        "record_number": rec.RecordNumber,
                        "ts_generated": int(rec.TimeGenerated.timestamp() * 1000),
                        "event_id": rec.EventID & 0xFFFF,
                        "message": str(rec.StringInserts),
                    }
                )
            if stop:
                break
    finally:
        win32evtlog.CloseEventLog(handle)
    return events


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #
def gather(*, whea_interval_seconds: float = 60.0) -> dict[str, Any]:
    """Gather all system metrics, merged under namespaced keys (D6/D8).

    Each sub-source is isolated; a failure yields ``None`` for that key only.
    WHEA is polled at most every ``whea_interval_seconds`` (D7/D16).
    """
    global _last_whea_poll_monotonic, _last_whea_count

    metrics: dict[str, Any] = {}
    metrics.update(_gather_cpu())
    metrics.update(_gather_ram())
    metrics.update(_gather_disk_io())
    metrics.update(_gather_disk_volumes())
    metrics.update(_gather_net())
    metrics.update(_gather_win32_perfmon())

    # WHEA polled less frequently than the 5s core (D7/D16). Retain the last
    # good count between polls so the metric stays available on every cycle
    # (the 2h-windowed count is slow-moving; ~60s stale beats null 91% of the
    # time, and a failed poll is still logged inside gather_whea).
    now_mono = time.monotonic()
    if now_mono - _last_whea_poll_monotonic >= whea_interval_seconds:
        _last_whea_poll_monotonic = now_mono
        fresh = gather_whea()
        if fresh["whea_count_2h"] is not None:
            _last_whea_count = fresh["whea_count_2h"]
    metrics["whea_count_2h"] = _last_whea_count

    return metrics
