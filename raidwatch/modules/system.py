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
    """Disk queue length, avg sec/transfer, pages/sec via win32pdh (D7)."""
    if not _WIN32_AVAILABLE:
        return {
            "disk_queue_length": None,
            "disk_avg_sec_per_transfer": None,
            "pages_per_sec": None,
        }
    out: dict[str, Any] = {}
    try:
        out["disk_queue_length"] = _read_pdh_counter(
            r"\PhysicalDisk(_Total)\Current Disk Queue Length"
        )
    except Exception:
        logger.exception("win32pdh disk queue failed")
        out["disk_queue_length"] = None
    try:
        out["disk_avg_sec_per_transfer"] = _read_pdh_counter(
            r"\PhysicalDisk(_Total)\Avg. Disk sec/Transfer"
        )
    except Exception:
        logger.exception("win32pdh avg sec/transfer failed")
        out["disk_avg_sec_per_transfer"] = None
    try:
        out["pages_per_sec"] = _read_pdh_counter(r"\Memory\Pages/sec")
    except Exception:
        logger.exception("win32pdh pages/sec failed")
        out["pages_per_sec"] = None
    return out


def _read_pdh_counter(path: str) -> float | None:
    """Read a single Windows PerfMon counter value via win32pdh (D7)."""
    import win32pdh  # type: ignore[import-not-found]

    # win32pdh path: (machine, object_name, counter_name, instance, parent, instance_index)
    # Easiest: use CollectQueryData with a counter path.
    query = win32pdh.OpenQuery()
    try:
        counter = win32pdh.AddCounter(query, path)
        try:
            win32pdh.CollectQueryData(query)
            _msg_type, value = win32pdh.GetFormattedCounterValue(counter, win32pdh.PDH_FMT_DOUBLE)
            return float(value)
        finally:
            win32pdh.RemoveCounter(counter)
    finally:
        win32pdh.CloseQuery(query)


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
    global _last_whea_poll_monotonic

    metrics: dict[str, Any] = {}
    metrics.update(_gather_cpu())
    metrics.update(_gather_ram())
    metrics.update(_gather_disk_io())
    metrics.update(_gather_disk_volumes())
    metrics.update(_gather_net())
    metrics.update(_gather_win32_perfmon())

    # WHEA polled less frequently than the 5s core (D7/D16).
    now_mono = time.monotonic()
    if now_mono - _last_whea_poll_monotonic >= whea_interval_seconds:
        _last_whea_poll_monotonic = now_mono
        metrics.update(gather_whea())
    else:
        metrics["whea_count_2h"] = None  # not polled this cycle

    return metrics
