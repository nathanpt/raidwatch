"""Fika module — process discovery (D4) + read-only config (D3) + log tail (D17).

All three sources are best-effort and isolated per D8. Log events are decorative
context that **never feeds gates** (D3). Invalid paths disable only this module
(D23), never blocking startup.

Process monitoring: psutil-based discovery via config-driven name + cmdline regex.
Config parsing: read-only JSON display of Fika server config.
Log parsing: periodic tail each cycle with per-file byte offsets in memory;
rotation-safe; restart resumes from end (D17).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import ClassVar

import psutil

from raidwatch.config import AppConfig
from raidwatch.models import FikaConfigSummary, FikaEvent, FikaMetrics, ProcessInfo

logger = logging.getLogger(__name__)

# Keep last N events in memory for the Recent Events feed.
_MAX_INMEMORY_EVENTS = 200

# Conventional paths derived from server.headless_path (Fika headless layout).
_BOOT_CONFIG_REL = "EscapeFromTarkov_Data/boot.config"
_FIKA_CORE_CFG_REL = "BepInEx/config/com.fika.core.cfg"
_BEPINEX_PLUGINS_REL = "BepInEx/plugins"


class FikaModule:
    """Collects Fika/SPT context: processes, config, and decorative log events (D3/D4/D17)."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._server_proc_name = config.processes.spt_server_process_name
        self._headless_proc_name = config.processes.headless_process_name
        self._headless_re = config.processes.headless_re

        # Log tail state: {path: offset_bytes} (D17 — in-memory only)
        self._log_offsets: dict[str, int] = {}
        self._log_initialized: set[str] = set()

        # In-memory recent events ring buffer
        self._recent_events: list[FikaEvent] = []

        # Config cache (re-read periodically)
        self._config_summary: FikaConfigSummary | None = None
        self._last_config_read = 0.0

        # Headless health sources derive from server.headless_path (Fika layout).
        self._headless_path = config.server.headless_path
        self._raid_udp_port = config.server.raid_udp_port
        self._risky_mod_names = [n.lower() for n in config.server.risky_mod_names]

        # Crash detection: cross-cycle in-memory state (#5a).
        self._ever_seen_headless = False
        self._headless_zero_streak = 0

        # Consecutive failure counter for backoff (D8)
        self.consecutive_failures = 0

    # ------------------------------------------------------------------ #
    # Main entry point                                                   #
    # ------------------------------------------------------------------ #
    def gather(self, db=None) -> FikaMetrics:
        """Gather all Fika metrics. Each source isolated per D8."""
        spt_proc, headless_procs = self._discover_processes()
        config_summary = self._read_config()
        events = self._tail_logs(db)
        self.consecutive_failures = 0

        headless_count = len(headless_procs)
        headless_cpu_total = sum(p.cpu_percent or 0 for p in headless_procs)
        headless_rss_total = sum(p.rss_bytes or 0 for p in headless_procs)

        # Crash detection (D8-isolated by construction — pure int/bool ops).
        # spt_server.pid present means SPT is up; a 2-cycle headless-zero streak
        # after we've ever seen headless => headless crashed (#5a).
        spt_up = spt_proc.pid is not None
        if headless_count > 0:
            self._ever_seen_headless = True
            self._headless_zero_streak = 0
        elif spt_up:
            self._headless_zero_streak += 1
        else:
            self._headless_zero_streak = 0
        headless_crashed = (
            spt_up
            and headless_count == 0
            and self._ever_seen_headless
            and self._headless_zero_streak >= 2
        )

        # Headless health sources (each internally D8-isolated).
        boot_job_worker_count, boot_optimized, boot_expected_workers = self._read_boot_config()
        force_ip_set = self._read_force_ip()
        raid_udp_port_open = self._check_udp_port()
        risky_mods = self._scan_risky_mods()

        return FikaMetrics(
            spt_server=spt_proc,
            headless=headless_procs,
            headless_count=headless_count,
            headless_cpu_total=headless_cpu_total,
            headless_rss_total=headless_rss_total,
            config_summary=config_summary,
            events_recent=events,
            boot_job_worker_count=boot_job_worker_count,
            boot_optimized=boot_optimized,
            boot_expected_workers=boot_expected_workers,
            force_ip_set=force_ip_set,
            raid_udp_port_open=raid_udp_port_open,
            headless_crashed=headless_crashed,
            risky_mods=risky_mods,
        )

    # ------------------------------------------------------------------ #
    # Process discovery (D4)                                             #
    # ------------------------------------------------------------------ #
    def _discover_processes(self) -> tuple[ProcessInfo, list[ProcessInfo]]:
        """Find SPT.Server and headless instances via psutil (D4)."""
        spt_info = ProcessInfo()
        headless_list: list[ProcessInfo] = []

        for proc in psutil.process_iter(
            ["pid", "name", "cmdline", "cpu_percent", "memory_info", "create_time"]
        ):
            try:
                info = proc.info
                name = info.get("name", "") or ""
                cmdline = info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)

                # SPT.Server match (by name)
                if name.lower() == self._server_proc_name.lower():
                    spt_info = self._make_process_info(proc, info)

                # Headless match (name + cmdline regex; D4)
                elif (
                    name.lower() == self._headless_proc_name.lower()
                    and self._headless_re
                    and self._headless_re.search(cmdline_str)
                ):
                    headless_list.append(self._make_process_info(proc, info))

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return spt_info, headless_list

    @staticmethod
    def _make_process_info(proc: psutil.Process, info: dict) -> ProcessInfo:
        """Build a ProcessInfo from psutil process data."""
        mem = info.get("memory_info")
        rss = mem.rss if mem else None
        uptime = None
        with contextlib.suppress(TypeError, OSError):
            uptime = int(time.time() - info.get("create_time", time.time()))
        handles = None
        with contextlib.suppress(Exception):
            handles = proc.num_handles() if hasattr(proc, "num_handles") else None
        return ProcessInfo(
            pid=info.get("pid"),
            cpu_percent=info.get("cpu_percent"),
            rss_bytes=rss,
            uptime_seconds=uptime,
            handle_count=handles,
        )

    # ------------------------------------------------------------------ #
    # Config parsing (read-only display; D3)                             #
    # ------------------------------------------------------------------ #
    def _read_config(self) -> FikaConfigSummary:
        """Read Fika server config JSON for display-only summary (D3)."""
        # Re-read at most every 60s.
        now_mono = time.monotonic()
        if self._config_summary is not None and now_mono - self._last_config_read < 60:
            return self._config_summary
        self._last_config_read = now_mono

        spt_path = self._config.server.spt_path
        if not spt_path:
            self._config_summary = FikaConfigSummary()
            return self._config_summary

        config_path = Path(spt_path) / "user" / "mods" / "fika-server" / "config.json"
        try:
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                self._config_summary = FikaConfigSummary(
                    max_players=data.get("maxPlayers"),
                    bot_limits=str(data.get("botLimits", "")),
                    send_rate=str(data.get("sendMeterRate", data.get("sendRate", ""))),
                )
            else:
                self._config_summary = FikaConfigSummary()
        except Exception:
            logger.debug("Fika config read failed for %s", config_path, exc_info=True)
            self._config_summary = FikaConfigSummary()

        return self._config_summary

    # ------------------------------------------------------------------ #
    # Headless health sources (#3/#4/#5)                                #
    # ------------------------------------------------------------------ #
    def _read_boot_config(self) -> tuple[int | None, bool, int | None]:
        """Parse headless boot.config threading flags (#3).

        Returns ``(job_worker_count, optimized, expected_workers)``.
        """
        if not self._headless_path:
            return None, False, None
        path = Path(self._headless_path) / _BOOT_CONFIG_REL
        try:
            if not path.exists():
                return None, False, None
            kv: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip() or "=" not in line:
                    continue  # skip blank + malformed lines (boot.config has no comments)
                key, _, value = line.partition("=")
                kv[key.strip()] = value.strip()

            job: int | None = None
            if "job-worker-count" in kv:
                with contextlib.suppress(ValueError):
                    job = int(kv["job-worker-count"])

            optimized = (
                kv.get("gfx-enable-gfx-jobs") == "1"
                and kv.get("gfx-enable-native-gfx-jobs") == "1"
                and kv.get("gfx-disable-mt-rendering") == "1"
                and "job-worker-count" in kv
                and "gc-max-time-slice" in kv  # value ignored — tuning int
            )

            try:
                expected = psutil.cpu_count(logical=True) - 1
            except Exception:
                expected = None
            return job, optimized, expected
        except Exception:
            logger.debug("boot.config read failed for %s", path, exc_info=True)
            return None, False, None

    def _read_force_ip(self) -> bool:
        """True iff Force IP is populated in com.fika.core.cfg (#4)."""
        if not self._headless_path:
            return False
        path = Path(self._headless_path) / _FIKA_CORE_CFG_REL
        try:
            if not path.exists():
                return False
            kv: dict[str, str] = {}
            line_re = re.compile(r"^\s*([^=#]\S.*?\S?)\s*=\s*(.*)$")
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                m = line_re.match(line)
                if m:
                    kv[m.group(1).strip().lower()] = m.group(2).strip()
            return bool(kv.get("force ip"))
        except Exception:
            logger.debug("Force-IP read failed for %s", path, exc_info=True)
            return False

    def _check_udp_port(self) -> bool:
        """True iff the configured UDP raid port is bound/listening (#4)."""
        try:
            conns = psutil.net_connections(kind="udp")
            return any(c.laddr and c.laddr.port == self._raid_udp_port for c in conns)
        except (psutil.AccessDenied, psutil.Error, OSError):
            return False

    def _scan_risky_mods(self) -> list[str]:
        """Scan headless BepInEx/plugins for names matching risky_mod_names (#5)."""
        if not self._headless_path or not self._risky_mod_names:
            return []
        path = Path(self._headless_path) / _BEPINEX_PLUGINS_REL
        try:
            if not path.is_dir():
                return []
            matches: set[str] = set()
            for entry in path.rglob("*"):
                name = entry.name.lower()
                if any(token in name for token in self._risky_mod_names):
                    matches.add(entry.name)
                    if len(matches) >= 50:
                        break
            return sorted(matches)
        except Exception:
            logger.debug("Risky-mod scan failed for %s", path, exc_info=True)
            return []

    # ------------------------------------------------------------------ #
    # Log tail (D17 — periodic, rotation-safe, in-memory offsets)        #
    # ------------------------------------------------------------------ #
    # Classification patterns (configurable keywords; D17).
    _PATTERNS: ClassVar[list[tuple[str, re.Pattern[str], str]]] = [
        ("raid_start", re.compile(r"raid.*(start|begin|spawn)", re.I), "info"),
        ("raid_end", re.compile(r"raid.*(end|finish|stop|complete)", re.I), "info"),
        ("player", re.compile(r"player.*(join|connect|leave|disconnect)", re.I), "info"),
        ("bot", re.compile(r"bot.*(spawn|count|limit)", re.I), "info"),
        ("error", re.compile(r"(error|exception|fail|crash)", re.I), "error"),
        ("warning", re.compile(r"(warn|deprecat)", re.I), "warn"),
    ]

    def _tail_logs(self, db=None) -> list[FikaEvent]:
        """Tail all configured log files for new lines (D17).

        Per-file byte offsets held in memory only. On first open, seek to end
        (skip backlog — live feed, not archive). Rotation-safe: offset > file
        size → reset to end.
        """
        new_events: list[FikaEvent] = []
        log_paths = self._config.server.log_paths
        if not log_paths:
            return new_events

        for source_name, raw_path in log_paths.items():
            path = self._resolve_path(raw_path)
            if path is None or not path.exists():
                continue

            try:
                events = self._tail_file(source_name, path, db)
                new_events.extend(events)
            except Exception:
                logger.debug("Log tail failed for %s (%s)", source_name, path, exc_info=True)

        # Merge new events into the in-memory ring buffer.
        if new_events:
            self._recent_events.extend(new_events)
            if len(self._recent_events) > _MAX_INMEMORY_EVENTS:
                self._recent_events = self._recent_events[-_MAX_INMEMORY_EVENTS:]

        return new_events[:50]  # Return just the newest for the snapshot feed

    def _tail_file(self, source: str, path: Path, db=None) -> list[FikaEvent]:
        """Tail a single log file, returning classified new events (D17)."""
        key = str(path)
        file_size = path.stat().st_size

        # Initialize: seek to end on first open (skip backlog; D17).
        if key not in self._log_initialized:
            self._log_offsets[key] = file_size
            self._log_initialized.add(key)
            return []

        offset = self._log_offsets.get(key, 0)

        # Rotation-safe: if offset > file size, file was rotated/truncated (D17).
        if offset > file_size:
            offset = file_size
            logger.info("Log rotation detected for %s, resetting offset", path)

        if offset == file_size:
            return []  # No new data

        # Read new bytes from the last offset.
        new_events: list[FikaEvent] = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                for line in f:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    event = self._classify_line(source, line)
                    if event:
                        new_events.append(event)
                        # Persist important events to DB (D14/D17).
                        if db is not None:
                            try:
                                import asyncio

                                task = asyncio.ensure_future(
                                    db.insert_fika_event(
                                        event.ts, event.source, event.severity, event.message, line
                                    )
                                )
                                _ = task  # prevent RUF006
                            except Exception:
                                pass
                self._log_offsets[key] = f.tell()
        except OSError:
            logger.debug("Cannot read log file %s", path, exc_info=True)

        return new_events

    @classmethod
    def _classify_line(cls, source: str, line: str) -> FikaEvent | None:
        """Regex-classify a log line into a FikaEvent (D17). Returns None if no match."""
        for _label, pattern, severity in cls._PATTERNS:
            if pattern.search(line):
                return FikaEvent(
                    ts=int(time.time() * 1000),
                    source=source,
                    severity=severity,
                    message=line[:200],  # truncate long lines
                )
        return None

    @staticmethod
    def _resolve_path(raw: str) -> Path | None:
        """Resolve a path string, expanding env vars like %APPDATA% (D17)."""
        if not raw:
            return None
        # Expand Windows-style %VAR% and Unix-style $VAR.
        expanded = os.path.expandvars(os.path.expanduser(raw))
        return Path(expanded)

    @property
    def recent_events(self) -> list[FikaEvent]:
        """The in-memory ring buffer of recent events for the feed."""
        return list(self._recent_events)
