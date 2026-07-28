# 06 — Fika module

Status: Backlog

## Goal

Port `raidwatch/modules/fika.py` to `rw_fika.cpp`: process discovery
(SPT.Server + headless clients), a read-only Fika/headless config summary,
rotation-safe log tailing into `fika_events` + a 200-event in-memory ring, and
headless-crash detection. This populates the `RwFikaMetrics` stub reserved in
step 03 and the five `fika_*` columns of `metrics_history`.

> **Config relocation (reconciliation of T4):** the legacy Fika settings lived
> under `server:` (`spt_path`, `log_paths`, `headless_path`, `raid_udp_port`,
> `risky_mod_names`). T4 drops the `server:` section because it carried the
> HTTP surface (`bind_host`, `port`) — but the Fika-relevant fields are not
> HTTP. They relocate to a new top-level **`fika:`** section, which step 02's
> config schema already lists. Only `bind_host`/`port` and the whole `auth:`
> section are truly deleted.

## Requires

- [05 — LHM temperatures](05-lhm-temps.md).

## Tasks

1. **Process discovery** in `rw_fika.cpp` via **WMI** (`Win32_Process.CommandLine`,
   `Name`, `ProcessId`, `WorkingSetSize`, `CreationDate`, `KernelModeTime` +
   `UserModeTime` for CPU %) — replaces the psutil path (T6):
   - **SPT.Server** — match where `Name` equals
     `processes.spt_server_process_name` (case-insensitive).
   - **Headless clients** — match where `Name` equals
     `processes.headless_process_name` (case-insensitive) **AND** the joined
     `CommandLine` matches the compiled `processes.headless_cmdline_pattern`
     regex (invalid regex is a hard config error at load — D4).
   - Build `RwProcessInfo{pid, cpu_percent, rss_bytes, uptime_seconds,
     handle_count}` for each. CPU % needs a cross-cycle delta (store
     `KernelModeTime+UserModeTime` per pid between cycles).
2. **Read-only config summary** (all D8-isolated; each read blanks only itself
   on failure):
   - **Fika server config** — parse `<fika.spt_path>/user/mods/fika-server/config.json`;
     extract `maxPlayers`, `botLimits`, `sendMeterRate` (or `sendRate`) into
     `config_summary`. Re-read at most every 60s (cache).
   - **boot.config** — parse
     `<fika.headless_path>/EscapeFromTarkov_Data/boot.config` key=value lines
     (no comments). Derive `job_worker_count` (int), `optimized` (true iff
     `gfx-enable-gfx-jobs=1` AND `gfx-enable-native-gfx-jobs=1` AND
     `gfx-disable-mt-rendering=1` AND both `job-worker-count` and
     `gc-max-time-slice` present), and `expected_workers` (`logical CPU count − 1`).
   - **com.fika.core.cfg** — parse
     `<fika.headless_path>/BepInEx/config/com.fika.core.cfg` (`#` comments,
     `key = value`); `force_ip_set` is true iff the `Force IP` key is
     non-empty.
   - **Risky-mod scan** — recurse `<fika.headless_path>/BepInEx/plugins` and
     list entry names containing (case-insensitive) any token from
     `fika.risky_mod_names`; cap at 50 matches.
   - **UDP raid port** — `raid_udp_port_open` true iff a UDP socket is bound on
     `fika.raid_udp_port`.
3. **Log tail (D17 analog)** for every path in `fika.log_paths`
   (`server` / `fika` / `watchdog`):
   - **Per-file byte offsets in memory** (`std::unordered_map<path, offset>`).
   - **First open seeks to end** (skip backlog — live feed, not archive).
   - **Rotation-safe:** if `offset > file_size`, reset offset to current size
     (file was truncated/rotated) and log it.
   - Read new bytes from the last offset, classify each non-empty line.
   - **Env-var expansion** of paths (`%APPDATA%`, `$VAR`).
   - Classify via the 6 regex patterns ported verbatim from `fika.py::_PATTERNS`:
     `raid.*(start|begin|spawn)`→info, `raid.*(end|finish|stop|complete)`→info,
     `player.*(join|connect|leave|disconnect)`→info, `bot.*(spawn|count|limit)`→info,
     `(error|exception|fail|crash)`→error, `(warn|deprecat)`→warn. Truncate message
     to 200 chars.
   - New classified events → `fika_events` table (`ts`, `source`, `severity`,
     `message`, `raw_line`) **and** the 200-event in-memory ring
     (`_MAX_INMEMORY_EVENTS = 200`); the snapshot carries the newest ≤50.
4. **Headless-crash detection** — port `fika.py`'s in-memory state: track
   `_ever_seen_headless`; while SPT is up, a headless count of 0 increments a
   zero-streak; `headless_crashed` is true iff SPT is up, headless count == 0,
   we've ever seen headless, and the zero-streak ≥ 2 cycles.
5. **Aggregate** `headless_count`, `headless_cpu_total` (sum of cpu_percent),
   `headless_rss_total` (sum of rss_bytes) for the snapshot + wide-row columns.
6. Wire `rw_fika` into the collector; the step-03 row mapping now fills
   `fika_spt_cpu_percent`, `fika_spt_rss_bytes`, `fika_headless_count`,
   `fika_headless_cpu_total`, `fika_headless_rss_total`.

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_fika.hpp` / `rw_fika.cpp`.
- `native/tests/test_rw_fika.cpp` — ported `test_log_regex.py` (the 6 patterns
  + rotation/seek-to-end + 200-cap behavior).

**Modified:**
- `native/btop4win/src/raidwatch/rw_collector.cpp` — call into `rw_fika` each
  cycle; persist `fika_events` + the five `fika_*` columns.
- `config.yaml.example` — add the `fika:` section relocating `spt_path`,
  `log_paths`, `headless_path`, `raid_udp_port`, `risky_mod_names` (drop only
  `bind_host`/`port`).

## Definition of Done

- [ ] `rw_fika` discovers SPT.Server by name and headless clients by name +
      cmdline regex via WMI; invalid regex is a load-time error.
- [ ] The read-only config summary populates (fika-server JSON, boot.config,
      com.fika.core.cfg, risky-mod scan, UDP port) — each D8-isolated.
- [ ] Log tailing seeks to end on first open, is rotation-safe, expands env
      vars, classifies via the 6 ported patterns, caps the ring at 200, and
      persists new events to `fika_events`.
- [ ] `headless_crashed` fires after a 2-cycle zero-streak post-ever-seen.
- [ ] With SPT.Server + one headless client running on the host, the snapshot
      carries procs + config + recent events, and the five `fika_*`
      `metrics_history` columns populate.
- [ ] Ported `test_log_regex` tests are green in `rw_tests`.

## Verification

On the Windows host with SPT.Server + one headless client running:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: "All tests passed" (incl. the ported log-regex tests).

.\x64\Release\raidwatch.exe
#   (run ~30s with SPT.Server + a headless client up, then quit)
sqlite3 data\raidwatch.db "SELECT ts, fika_spt_cpu_percent, fika_spt_rss_bytes, fika_headless_count FROM metrics_history ORDER BY ts DESC LIMIT 3;"
#   Expected: fika_spt_* populated; fika_headless_count == 1.
sqlite3 data\raidwatch.db "SELECT source, severity, count(*) FROM fika_events GROUP BY source, severity;"
#   Expected: classified events present (or 0 rows if logs were quiet — acceptable).
```

Record the column query, the `fika_events` grouping, and a note on which
config-summary fields populated in the Log.

## Log

<!-- On Done: date + the rw_tests pass line, the column query, the fika_events
     grouping, and the config-summary observation. Empty until executed. -->
