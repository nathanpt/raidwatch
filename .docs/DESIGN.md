# DESIGN.md

> **Document relationship.** This is the **living spec** — it states what we are building, current. It is decision-correct: every fork has been resolved. The **why** behind each decision (alternatives considered, rationale) lives in `.docs/DECISIONS.md`, an ADR-style log (D1–D34). Where this doc states a choice as fact, `DECISIONS.md` records how we got there.

**Project**: **RaidWatch** — a light, browser-served monitoring dashboard for a dedicated SPT + Project Fika host on Windows 11 IoT Enterprise LTSC.
**Context**: A bespoke personal tool for the homelab (a combined AM4 1800X build and/or a 6800H mini-PC). Core focus is basic hardware health with **upgrade gates** that turn observed metrics into concrete upgrade/stability recommendations. A Fika module adds game-specific context (process monitoring, read-only config, best-effort log events).
**Philosophy**: Minimal resource footprint (<2% CPU, <150–250 MB RAM idle), practical homelab scripting, **thin generic seams** (namespaced modules; generalize only when a second game is imminent — D1/D6), no heavy stacks (no Prometheus/Grafana initially), self-contained Python deployment.

---

## 1. Overview & Goals

### Purpose
Provide a clean, always-accessible web dashboard (LAN + Tailscale) showing real-time + historical hardware/system health (CPU, RAM, storage, temps, processes), plus a Fika context module (SPT.Server + headless process monitoring, read-only config, best-effort log events). **Upgrade gates** translate sustained metric crossings into concrete recommendations. Low maintenance: auto-starts as a Windows service, self-monitors, self-heals.

### Primary Goals
- **Lightweight & Reliable**: < 150–250 MB RAM idle for the dashboard; runs 24/7 on debloated LTSC with negligible impact on raid hosting. Collector runs every 5s with **<2% CPU overhead**.
- **Core Metrics**: hardware/system vitals (CPU, RAM, disk I/O + free space, net, temps, processes, WHEA stability).
- **Fika Context Module**: SPT.Server + headless client process tracking, read-only Fika/SPT config display, and best-effort parsed log events (decorative only — never feeds gates; D3).
- **Upgrade Gates**: stateful, configurable thresholds with concrete recommendation text (the killer feature; D10).
- **Practical for Homelab**: install as a service on Win11 IoT LTSC, Tailscale + LAN reachable, log tailing, BIOS/AM4 tuning notes.
- **Browser UI**: dark theme, responsive cards/gauges/charts, live updates, **no deploy build step** (frontend assets are vendored; D29).
- **Self-monitoring & self-healing**: the dashboard exposes its own health and recovers from collector failure without manual intervention (D27/D35).

### Non-Goals / Out of v1 Scope
- No full time-series DB (Influx/Prometheus) — bounded in-memory deque + SQLite with pruning (D14/D15).
- No weighted "Overall Health Score" — v1 uses "any armed gate active → degraded" via a layered status pill (D2/D22).
- No config-editor UI, deep-dive metrics tab, Fika/raids tab, or logs-viewer tab in v1 (edit `config.yaml` by hand; tabs deferred to v1.x; D2).
- No Discord webhook in v1 (deferred; D2).
- No HTTPS in v1 (cleartext HTTP accepted for the LAN-trust model; TLS deferred to v1.x via mkcert/Caddy; D13).
- No PWA or native app.
- No automatic mod/config editing (view + recommendations only; manual file edits for safety).
- No "kill process" button (dropped permanently; D2).
- No React/Vue/Svelte build. **SSE + vanilla JS + Chart.js + vendored Tailwind** (no HTMX in v1; D5/D29).
- Not a replacement for WATCHDOG (complementary; surface WATCHDOG status if detected).

### Success Metrics
- Collector runs reliably every 5s with **<2% CPU overhead**.
- UI feels "live" (<1s perceived update) via SSE push at collection cadence.
- Gates trigger accurately on sustained crossings (no false-alert flood after baselining; D10).
- First-run yields a working hardware dashboard immediately (system metrics need no config; Fika activates after path setup; D23).
- A failed collector loop is detected and recovered within one cycle window; staleness never persists silently (D27/D35).

---

## 2. Architecture

### High-Level Components
```
Browser (modern Chrome/Edge/Firefox on LAN/Tailscale)
        ↓ HTTP (REST + login) + SSE (EventSource, one-way push)
FastAPI (Uvicorn) on Win11 IoT LTSC — bound 0.0.0.0:8080, firewall scoped to LAN subnet + Tailscale 100.64.0.0/10
        ├── Lifespan startup: open shared aiosqlite connection; run migrations; start collector + supervisor tasks
        ├── Lifespan shutdown: cancel collector, close SSE subscribers, close DB (D27)
        ├── REST endpoints (/api/*) + GET /login + POST /login (cookie minting; D24)
        ├── SSE /api/stream (full-snapshot push every cycle via broker; D5/D25/D28)
        ├── /health (machine-readable liveness — status-pill + external-watchdog backbone; D35)
        ├── Jinja2 base template + static/ (index.html + app.js + VENDORED Tailwind + Chart.js; D29)
        ├── Broker: non-blocking fan-out, bounded per-subscriber queues (D28)
        └── SQLite (data/raidwatch.db) — single shared connection (D21), fixed wide table + event tables (D14)
                ↓
Collector Loop (asyncio, 5s interval, per-module isolated, fully loop-body-wrapped; D8/D27)
        ├── psutil (CPU, RAM, disk I/O, net, known game processes)
        ├── pywin32 in-process: win32pdh (disk queue length, paging), win32evtlog (WHEA; D7)
        ├── Fika module: psutil process discovery (D4) + read-only config parse + periodic log tail (D17)
        ├── Temperature: LibreHardwareMonitor via pythonnet (D9) — display from launch, gate disabled until probe-validated
        ├── Gate evaluator: sustained-duration state machine (D8/D10/D16/D19)
        └── publish(snapshot) → broker (non-blocking) + persist(SQLite) — notifications deferred
```

### Stack (purpose; rationale in DECISIONS.md)
- **Python + FastAPI/Uvicorn**: async ASGI; auto OpenAPI at `/docs`.
- **psutil**: Windows process/disk/net support.
- **pywin32 in-process** (D7): `win32pdh` (PerfMon counters) + `win32evtlog` (WHEA). No PowerShell in the collection path.
- **SSE + vanilla JS + Chart.js** (D5): one-way push; native `EventSource`.
- **Vendored frontend assets** (D29): compiled `tailwind.css` + `chart.umd.min.js` served as static files. No CDN, no deploy build, offline-resilient.
- **SQLite, single shared connection** (D14/D21): one `aiosqlite` connection serialized on the event loop — no `database is locked` by construction.
- **Temps via LibreHardwareMonitor + pythonnet** (D9): the only reliable Ryzen path; requires SYSTEM (kernel driver). DLL set vendored from the official release (D30).
- **NSSM service wrapper** (D18): reliably kills/restarts a wedged process on the headless host.
- **Broker + supervision** (D27/D28): collector is decoupled from SSE clients and can't die silently.

### Process supervision & liveness (D27)
The collector is the heartbeat of a 24/7 service, so its failure modes are handled explicitly:
- **Loop body fully wrapped:** the entire per-cycle body (gather + persist + gates + publish) is inside try/except-log-continue, so an exception *anywhere* logs and the loop ticks again next cycle — the task cannot die from a raised exception (D8 covers modules; this covers the orchestration).
- **Supervisor task:** awaits the collector task; if it ever returns (cancellation, interpreter-level fault), the supervisor restarts it and logs.
- **`/health` staleness signal:** exposes `last_tick_age_seconds`; the UI status pill turns red after >3 missed cycles (D22), and `/health` is machine-checkable.
- **Irreducible native-hang case:** a GIL-wedge in pythonnet/LHM stalls the event loop in a way Python can't catch (D18/D31). As belt-and-suspenders, an **external Windows Scheduled Task** curls `/health` on a short timeout and restarts the `RaidWatch` service on failure/timeout. This is the only backstop for a hung-but-alive process; NSSM alone won't catch it (it monitors process liveness, not application liveness).

### Data Retention Strategy
- In-memory `collections.deque(maxlen=720)` (1 hour @ 5s) for live charts — fast, no DB hit on the hot path.
- SQLite **fixed wide table** `metrics_history` (one row per cycle, scalar numeric columns only — see §3.4 for the contract; D14). Separate append-only tables: `fika_events` (parsed log events), `gate_events` (gate transitions), `whea_events` (WHEA, `record_number` unique — D16). Prune `metrics_history` rows `>48h` on an hourly schedule (not every cycle).
- **History downsampling on the fly** (D15): `GROUP BY (ts / bucket)` with aggregates at query time, bucket-sized to cap output ~720 points per range (24h→2-min, 6h→30s, 1h/15m→raw 5s). `max()` for peak metrics (CPU/RAM), `avg()` for throughput rates. No pre-computed rollup tables (~34,560 total rows → trivially fast).
- **Schema versioning** (D32): `PRAGMA user_version` + idempotent migrations run at startup, so v1.x column adds don't require hand-SQL.

### Live Updates
- **Primary and only: SSE** (`EventSource`) pushing a **full metrics snapshot** every cycle (D5/D25), fanned out through the **broker** (D28). No WebSocket, no HTMX, no delta/diff logic.
- On page load: REST `GET /api/metrics/history` pre-fills charts for the selected range and REST `GET /api/metrics/current` populates cards; then the SSE stream opens. On any (re)connect, the broker sends a full snapshot first → guaranteed resync after `EventSource`'s silent auto-reconnect, with zero client-side merge state.

---

## 3. Key Metrics, Fika Integration & Upgrade Gates

### 3.1 Core System Metrics (Collector Priority)
Collected via `psutil` + targeted `pywin32` in-process calls (**no PowerShell in the hot path**; D7):

| Category | Metrics | Collection Method | Notes / Edge Cases |
|----------|---------|-------------------|--------------------|
| **CPU** | Total %, per-core list | `psutil.cpu_percent(percpu=True, interval=0.5)` | High during bot-heavy raids or many headless clients. |
| **RAM** | Total/used/available, %, commit, pagefile, Pages/sec | `psutil.virtual_memory()`, `psutil.swap_memory()`, `pywin32` `win32pdh` for `\Memory\Pages/sec` | **#1 priority**. Sustained high % or paging = immediate gate. Track SPT + headless working sets separately. |
| **Disk I/O & Storage** | Read/write bytes/s, Current Disk Queue Length, free space, avg sec/transfer (latency) | `psutil.disk_io_counters(perdisk=True)`, `pywin32` `win32pdh` for `\PhysicalDisk(_Total)\Current Disk Queue Length` + avg sec/Transfer | **#3 priority**. Queue >2–3 sustained during raid load = storage gate. Monitor game drive separately. |
| **Network** | Bytes sent/recv per interface, packets, errors/drops | `psutil.net_io_counters(pernic=True)` + `net_if_addrs` | Interface labels config-driven (game vs Tailscale/WAN), mirroring D4. Errors → networking review. |
| **Temperatures** | CPU Package / Tdie / per-core, drive temps | **LibreHardwareMonitor via `pythonnet`** (`Computer` object, enable CPU/GPU/HDD, iterate Temperature sensors) — D9 | Critical for 1800X AIO health. **Display from launch; `cpu_thermal` gate disabled until validated** (D9 + probe). Sensor identity (Tdie vs Tctl vs "CPU Package") is config-mapped and confirmed via `scripts/probe_temps.py` on the 1800X before arming the gate. **Note the Zen1 Tctl +20°C offset.** |
| **Stability** | Recent WHEA errors (Event Log) | `pywin32` `win32evtlog`, polled **every ~60s** (D7/D16) | Windowed re-query: each poll reads System log for WHEA-Logger events with `TimeGenerated >= now − 2h` and counts them (count naturally decays). Dedup persisted UI list via `whea_events.record_number` unique. Count all WHEA uniformly (19/20/41); surface raw text for human severity. |

### 3.2 Fika / SPT Module (Reliable Core + Decorative Logs; D3)
Fika has **no public rich HTTP API** (in-game UDP + lobby only). The module's reliable, version-stable sources are process monitoring and read-only config; log events are **best-effort decorative context that never feeds gates or health** (so log-format churn on SPT/Fika updates can't corrupt decisions).

- **Process monitoring** (first-class; D4): config-driven discovery via `spt_server_process_name`, `headless_process_name`, `headless_cmdline_pattern` (regex, compiled at config load — invalid regex is a clear config error, not a runtime crash). Collector enumerates via `psutil`, matches name + cmdline regex, reports per-PID CPU%/RSS/uptime/handles. Ship `scripts/discover_processes.py` to dump candidate processes + cmdlines so the real headless signature is filled from the actual WATCHDOG/Fika setup. Process data yields count + per-PID metrics; "hosting a raid?" state comes from logs (decorative), not the process.
- **Log parsing** (decorative Recent Events feed; D17): **periodic tail each collector cycle** (5s) — seek to last byte offset per file, read new lines, regex-classify. Per-file offsets in memory only; on restart, resume from end (skip backlog); rotation-safe (offset > file size → reset to end). **`watchdog` is not a dependency.** Log sources (paths configured in `config.yaml`): SPT/BepInEx `LogOutput.log`; Fika `user/mods/fika-server/logs/fika.log`; client/headless `Player.log`; WATCHDOG logs. Key patterns (configurable regex/keywords): raid lifecycle, players, bots, network/stability, crashes. Persist important events to `fika_events` (append-only); keep last 100–500 in memory.
- **Config parsing** (read-only display): Fika server config JSON (`user/mods/fika-server/config.json`): `maxPlayers`, bot/spawn limits, network send rates, NAT settings. Display current values + "edit manually" note. Highlight if non-optimal. SPT `http.json` for port/backend.
- **WATCHDOG integration** (optional): monitor process presence + its log for "stabilized"/restarts/headless-management events; surface status prominently if detected.

### 3.3 Upgrade Gates (Core Value — Configurable; D10)
Gates are **stateful** (track "crossed at" via `time.monotonic()` — D19; sustained duration). UI shows: current value vs threshold (gauge green/yellow/red); "Triggered" banner with exact recommendation text; historical trigger count + last triggered.

> **v1 arming (D10):** all hardware gates ship **enabled** with **conservative** thresholds (higher than the examples below) to minimize false triggers during baselining, plus visible headroom gauges. After observing a real raid, lower thresholds to real headroom via config. `cpu_thermal` ships **disabled** until the temps probe validates it (D9).

Example gate table (user-editable in config; **defaults are conservative**, tune after baselining):

| Gate ID | Category | Metric & Condition | Threshold (example, conservative in v1) | Sustained | Severity | Recommended Action |
|---------|----------|--------------------|------------------------------------------|-----------|----------|--------------------|
| `ram_high` | RAM (P1) | RAM % used | > 82% (v1 default higher) | > 5 min | High | **Add RAM first** (16–32GB DDR4 3000+ kit, dual channel). Check SPT + headless working sets for leaks. |
| `cpu_sustained` | CPU (P2) | Total CPU % | > 75% (v1 default higher) | > 8–10 min | Medium-High | **CPU upgrade path**: Zen 3 5800X3D, or add/optimize headless clients. Tune Fika bot/spawn limits first. |
| `storage_io` | Storage (P3) | Disk Queue Length or avg latency | > 2.5–4 sustained or latency > 10–20ms | During raid load | Medium | **Faster storage**: NVMe for SPT/game files (separate from OS). Or reduce map complexity/mods. |
| `storage_space` | Storage | Free space on game/SPT volume | < 15% or < 40–50 GB | N/A | Medium | Clean old profiles, logs, temp files, or expand drive. |
| `stability_whea` | Stability | WHEA errors in last 2h (windowed; D16) | > 2–5 in last 2h | N/A | High | **BIOS/RAM tuning**: verify XMP/DOCP, FCLK sync, DRAM voltages/timings. Reseat RAM, update chipset/BIOS. |
| `cpu_thermal` | CPU/Temp | CPU Package / Tdie temp | > 88–92°C | > 3 min | High | Check AIO pump/fans/dust/repaste. **Disabled in v1 until probe-validated (D9).** |

**Deferred to v1.x** (D2): `ram_paging`, `fika_instability`, `network_errors`.

**Gate Logic Implementation** (D8/D10/D16/D19):
- Config: `gates:` dict with per-gate `metric`, `operator`, `threshold`, `duration_seconds`, `severity`, `recommendation_text`, `enabled`. Validated by pydantic at load (negative threshold, bad operator → clear error).
- `gate_state` (last_crossed via `time.monotonic()`, currently_triggered) persisted in SQLite (survives restarts). Each cycle evaluates; if crossed and sustained → trigger (log to `gate_events`, publish banner via broker). **Durations use `time.monotonic()` deltas — wall-clock only labels (D19).**
- Cooldown/hysteresis (re-alert only after 30 min or value drops 10% below).
- **Status pill (D22):** layered precedence — core-collector-stale (>3 missed cycles) → red "Monitoring degraded"; High gate → red "Critical: {gate}"; Medium gate → yellow "Degraded: {gate}"; else green "Operational". Optional-module failures color only their own card, never the pill. No weighted health score in v1.

**BIOS / Stability Tips** (docs + "System Info" card):
- AM4 B350 + 1800X + 3000 MHz target: enable **DOCP/XMP** profile (not manual). Set FCLK to 1500 if stable (test with TM5 anta777 Extreme, Karhu, HCI MemTest). For Zen1, 1:1:1 sync may cap lower; monitor WHEA. Power plan "Ryzen Balanced" or High Performance. Disable C6/C-states only if instability. Update chipset drivers + latest B350 BIOS (AGESA). For a future Zen3 swap: much better FCLK headroom (1800–2000+), stronger IMC — dashboard metrics will show the gains.
- Mini PC (6800H): newer platform, better efficiency, DDR5; gates still apply but expect headroom for small groups.
- Combined build: consolidate best PSU/AIO/case/RAM into one host; use the dashboard to validate post-assembly (stress-test raids, watch gates).

### 3.4 Metrics & Data Contract (canonical — D31)
The snapshot, the wide table, and the wire format are defined once here. Units are explicit so collector and UI never drift.

**Live snapshot** (JSON pushed over SSE, returned by `/api/metrics/current`). `*` = nullable; lists of variable cardinality are live-only (not persisted as wide columns):
```
{
  "ts": <int epoch ms, UTC>,
  "system": {
    "cpu_total_percent": <float 0-100>,
    "cpu_per_core_percent": [<float>],
    "ram_total_bytes": <int>, "ram_used_bytes": <int>, "ram_available_bytes": <int>, "ram_percent": <float>,
    "swap_total_bytes": <int>, "swap_used_bytes": <int>, "swap_percent": <float>,
    "pages_per_sec": <float>,
    "disk_read_bps": <int>, "disk_write_bps": <int>, "disk_queue_length": <float>, "disk_avg_sec_per_transfer": <float>,
    "disk_volumes": [{"mount":"D:", "total_bytes":<int>, "free_bytes":<int>}],
    "net_by_nic": {"<nic>": {"sent_bps":<int>, "recv_bps":<int>, "errin":<int>, "errout":<int>, "dropout":<int>}},
    "temp_cpu_celsius": <float*>,            # null until LHM provides; sensor per config
    "whea_count_2h": <int>
  },
  "fika": {
    "spt_server": {"pid":<int*>, "cpu_percent":<float*>, "rss_bytes":<int*>, "uptime_seconds":<int*>, "handle_count":<int*>},
    "headless": [{"pid":<int>, "cpu_percent":<float>, "rss_bytes":<int>, "uptime_seconds":<int>}],
    "headless_count": <int>,
    "headless_cpu_total": <float>, "headless_rss_total": <int>,
    "config_summary": {"max_players":<int*>, "bot_limits":<str*>, "send_rate":<str*>},   # display-only
    "events_recent": [{"ts":<int>, "source":<str>, "severity":<info|warn|error>, "message":<str>}]   # decorative (D3)
  },
  "process": {                              # top-others, 15s-sampled (D20)
    "top": [{"pid":<int>, "name":<str>, "cpu_percent":<float>, "rss_bytes":<int>}]
  },
  "self": {                                 # dashboard self-metrics
    "cpu_percent": <float>, "rss_bytes": <int>, "cycle_ms": <float>, "subscribers": <int>
  }
}
```
**`metrics_history` wide table** (persisted; scalar numerics + ts only — variable-cardinality lists stay live):
`ts INTEGER (epoch ms UTC) PK-ish`, `cpu_total_percent REAL`, `ram_percent REAL`, `ram_used_bytes INTEGER`, `swap_percent REAL`, `pages_per_sec REAL`, `disk_read_bps INTEGER`, `disk_write_bps INTEGER`, `disk_queue_length REAL`, `disk_avg_sec_per_transfer REAL`, `disk_game_free_bytes INTEGER`, `net_sent_bps INTEGER`, `net_recv_bps INTEGER`, `net_errs_total INTEGER`, `temp_cpu_celsius REAL NULL`, `whea_count_2h INTEGER`, `fika_spt_cpu_percent REAL NULL`, `fika_spt_rss_bytes INTEGER NULL`, `fika_headless_count INTEGER`, `fika_headless_cpu_total REAL`, `fika_headless_rss_total INTEGER`.
**Event tables:** `fika_events(id, ts, source, severity, message, raw_line)`; `gate_events(id, ts, gate_id, action, value, severity)`; `whea_events(record_number UNIQUE, ts_generated, event_id, message)`.

---

## 4. Frontend Design (Lightweight, Homelab Practical)

**Theme**: dark slate (#0f172a) + Tarkov-inspired accents (green #22c55e healthy, amber warnings, red gates). System sans or Inter. High contrast. Responsive (desktop primary, tablet ok).

**No deploy build toolchain — vendored assets (D29).** Single `index.html` + `static/app.js` + vendored frontend assets served by FastAPI:
- **`static/vendor/tailwind.css`** — a compiled stylesheet produced once via the standalone Tailwind CLI *at authoring time* (committed). The Tailwind **Play CDN is dev-only** (per Tailwind's own docs: ~500KB JS, FOUC, and it breaks offline) and is **not used**.
- **`static/vendor/chart.umd.min.js`** — Chart.js, vendored. No CDN dependency.
- **No HTMX in v1** (D5) — returns in v1.x when partial-swap UI lands.
- Minimal custom JS: SSE (`EventSource`) connection, chart update logic, toasts, gate banner rendering.

**v1 page = single Overview** (other tabs deferred to v1.x; D2):
1. **Top Nav**: "RaidWatch" + hostname; status pill (D22); uptime + last collector run + Tailscale IP hint; quick actions ("Refresh Now", "Export CSV last 24h"); gear → settings modal.
2. **Status Cards Row** (4–6): Overall Health (top issue; pill color), Active Raids/Players + headless count, CPU gauge (% + 30min sparkline), RAM gauge (% + used/avail + paging indicator), CPU Temp (value + trend; warning if unvalidated), Storage (game drive free % + I/O sparkline/queue).
3. **Active Alerts / Gates Banner** (collapsible, prominent if any triggered): metric, value, duration, recommendation.
4. **Mini Charts Row**: last 30–60 min CPU total, RAM %, disk I/O (stacked), network (in/out). Click to expand.
5. **Top Processes Table** (D20): SPT.Server + headless + dashboard itself (5s) + top 5 others (15s). Highlight game processes. No "kill" button.
6. **Recent Fika Events** (decorative; D3): scrollable timestamped events, colored by type.

**CSV export** (`?minutes=1440`): columns = the §3.4 wide-table fields; served as a single downsampled query.

**Deferred to v1.x tabs** (D2): System Metrics deep dive, Fika & Raids, Alerts/Gates editor, Logs viewer, Settings/config editor. In v1, edit `config.yaml` by hand and restart.

**UI/UX polish**: toasts for alerts/gate triggers; keyboard shortcuts (`?` help, `/` search, `r` refresh); loading/empty states; error boundary (collector down → red banner "Monitoring degraded"); print/kiosk CSS; accessibility (semantic HTML, contrast, ARIA on gauges); dark/light toggle (persist). Cards stack on mobile.

---

## 5. Backend & Implementation Specifications

### File Structure
```
raidwatch/
├── main.py                 # FastAPI app, lifespan (migrations, shared conn, collector+supervisor, shutdown), routers, static
├── collector.py            # Async loop, gather_metrics() per-module isolated + loop-body wrapped (D8/D27), persist(), check_gates(), publish()->broker
├── broker.py               # Non-blocking fan-out; bounded per-subscriber queues; subscriber cap (D28)
├── supervisor.py           # Awaits collector task; restarts on exit; exposes last_tick (D27)
├── modules/
│   ├── system.py           # psutil + pywin32 (win32pdh, win32evtlog) → system.* metrics (D7)
│   ├── temps.py            # LHM via pythonnet → system.temp_* (D9); failure-tolerant
│   └── fika.py             # process discovery (D4) + config parse + periodic log tail (D17) → fika.*
├── auth.py                 # login form, token (constant-time, never logged), HttpOnly cookie mint (D12/D24/D33)
├── models.py               # Pydantic models: snapshot, gate config, API responses (D31)
├── config.py               # pydantic-settings + yaml; load/save/validation (regex compile etc.); auto-generate first run (D23)
├── gates.py                # GateEvaluator: monotonic durations (D19), WHEA windowed re-query (D16), recommendations
├── database.py             # single shared aiosqlite connection (D21); wide + event tables (D14); PRAGMA user_version migrations (D32); on-the-fly bucketing (D15)
├── health.py               # /health builder + staleness logic (D35)
├── templates/
│   ├── base.html           # nav, footer, vendored Tailwind+Chart.js, SSE client init
│   ├── dashboard.html      # overview cards, mini charts, processes, events
│   └── login.html          # single password field (D24)
├── static/
│   ├── app.js              # EventSource SSE, chart update, gate banner, toasts (D5/D25)
│   └── vendor/
│       ├── tailwind.css    # compiled once at authoring time (D29)
│       └── chart.umd.min.js
├── vendor/
│   └── lhm/                # LibreHardwareMonitorLib.dll + deps from official release + LICENSE (MPL-2.0) (D30)
├── tests/                  # pytest — pure-logic coverage (D32): gate timing, WHEA window decay, downsampling, regex, config validation, broker backpressure
├── scripts/
│   ├── install_service.ps1 # vendor NSSM, create "RaidWatch" as SYSTEM, recovery, firewall rule, ACL config.yaml, external /health watchdog task (D11/D18/D27/D33)
│   ├── uninstall_service.ps1
│   ├── probe_temps.py      # enumerate LHM sensors on the 1800X → validate identity + Tctl offset (D9)
│   ├── discover_processes.py  # dump candidate processes + cmdlines → fill headless pattern (D4)
│   ├── build_tailwind.py   # one-shot: run standalone Tailwind CLI → static/vendor/tailwind.css (authoring only; D29)
│   ├── collect_once.py     # standalone metrics dump for testing
│   └── stress_test_sim.py  # fake load for gate testing
├── nssm.exe                # vendored static exe (~300KB) (D18)
├── data/                   # .gitignore'd; raidwatch.db, config.yaml (auto-generated, ACL'd), logs via NSSM
├── pyproject.toml          # ruff config, pytest config, project metadata (D32)
├── requirements.txt        # pinned deps (uv lock output) (D32)
├── config.yaml.example
├── README.md
├── SETUP_GUIDE.md
└── DESIGN.md (this file)
```

### Key Implementation Notes
- **Collector (D8/D27):** started in `lifespan` as a task **wrapped end-to-end** (gather + persist + gates + publish all inside try/except-log-continue), so the loop cannot die from any raised exception. Each metric source in its **own** try/except (failure → `None` for that key + per-module error counter; UI shows "X unavailable"). Schedule next cycle 5s after **completion** (cycles never overlap). After N consecutive failures a module **backs off** (~60s). `publish()` is **non-blocking** (drops to broker; never awaits a client).
- **Supervisor (D27):** a separate task awaits the collector task; on unexpected return, logs and restarts it. Exposes `last_tick_ts` for `/health` and the D22 pill.
- **Broker (D28):** collector publishes one snapshot per cycle; broker fans out via one bounded `asyncio.Queue(maxlen=K)` per SSE subscriber (drop-oldest on overflow). SSE handlers drain their own queue. Cap subscribers (e.g., 20). A slow/stuck client never blocks the collector or other clients.
- **Process table (D20):** known game/dashboard processes (PIDs from D4) at 5s; full `psutil.process_iter()` for "top 5 others" on a separate ~15s timer under D8 isolation.
- **Temps (D9/D30/D31):** `pythonnet` loads the **vendored** `vendor/lhm/LibreHardwareMonitorLib.dll` (+ deps) from the official release; enable CPU/GPU/HDD; iterate Temperature sensors. Any LHM/driver error → return `None` + UI warning (never crashes collector). **Display ON from launch; `cpu_thermal` gate disabled by default.** Run `scripts/probe_temps.py` on the 1800X first to dump sensor names + values, observe the Zen1 Tctl +20°C offset, fill the config sensor mapping, then arm the gate. Requires the **.NET runtime** present on the host.
- **pywin32 helpers (D7):** `win32pdh` for PerfMon counters; `win32evtlog` for WHEA. No PowerShell subprocess in the collection path.
- **Log parsing (D17):** periodic tail folded into the collector cycle under D8 isolation; per-file byte offsets in memory; resume-from-end on restart; rotation-safe reset.
- **Gates (D8/D10/D16/D19):** separate module; `evaluate()` returns triggered list; durations via `time.monotonic()`; WHEA via windowed re-query + `record_number` dedup; state persisted in SQLite.
- **API:**
  - `GET /login` / `POST /login` → mint HttpOnly cookie (D24)
  - `GET /api/metrics/current` → latest snapshot (§3.4 contract)
  - `GET /api/metrics/history?minutes=…&metrics=…` → time-bucketed for charts (D15)
  - `GET /api/metrics/export.csv?minutes=…` → CSV (§3.4 columns)
  - `GET /api/fika/status` → process/config/events summary
  - `GET /api/gates` → current status + history
  - `GET /api/stream` (SSE) → broker subscription; full snapshot first, then every cycle (D5/D25/D28)
  - `GET /api/logs/tail?source=fika&lines=100` → recent lines + metadata
  - `GET /health` → machine-readable liveness (D35)
- **`/health` contract (D35):** `{"status":"operational|degraded|critical","version":<str>,"started_at":<int>,"collector":{"last_tick_ts":<int>,"last_tick_age_seconds":<float>,"last_cycle_ms":<float>,"consecutive_failures":<int>},"modules":{"system":{"state":"ok|degraded|backoff|error"},"fika":{...},"temps":{...}},"sse_subscribers":<int>,"db_size_mb":<float>}`. Drives the D22 status pill server-side; consumed by the external watchdog task (D27).
- **Error handling & self-monitoring:** dashboard tracks its own CPU/RAM/cycle ms (in `self.*`); collector errors > threshold → degrade mode (last known + warning).
- **Deployment (D9/D11/D12/D13/D18/D24/D33):** NSSM-vendored service running the venv `python.exe main.py` as **SYSTEM** (needed for LHM driver; D9/D31), SCM restart on 1st/2nd/3rd failure. Bind `0.0.0.0:8080`; firewall scoped to LAN subnet + Tailscale `100.64.0.0/10` (exclude guest/IoT VLANs). Bearer token **required** (≥32-byte random, install-generated), ACL'd to **SYSTEM + Administrators** only, minted into a long-lived `HttpOnly`/`SameSite=Lax` cookie via login. The token is **never logged** (redacted everywhere). Cleartext HTTP in v1 (TLS deferred; D13).
- **Logging (D26):** Python `logging` (leveled, default INFO, configurable via `config.yaml`) → stdout/stderr; NSSM `AppStdout`/`AppStderr` + `AppRotateOnline` captures to one file and owns rotation (catches native-interop crash tracebacks too). **Token is filtered/redacted from all log records.** Windows Event Log as a secondary sink deferred to v1.x.
- **First run (D23):** missing `config.yaml` → auto-generate from example with safe defaults; start with system metrics live (zero config) + Fika disabled (banner: set paths in `config.yaml`); invalid paths disable only their module (D8), never block startup. Config changes apply by service restart.

### Quality bars (D32)
- Full **type hints**; **ruff** for lint + format; **pytest** for the pure-logic modules (gate timing, WHEA window decay, downsampling bucket math, regex classification, config validation, broker overflow).
- **Pinned dependencies** via `uv` lock (committed `requirements.txt`); critical for pythonnet ↔ LHM ↔ .NET version compat on a rarely-updated server.
- **Schema migrations** via `PRAGMA user_version` + idempotent DDL at startup.

### Config.yaml Example (D4/D9/D23)
```yaml
server:
  name: "tarkov-fika-host"
  spt_path: "D:\\SPTarkov"          # user edits; Fika module disabled until valid
  log_paths:
    server: "D:\\SPTarkov\\BepInEx\\LogOutput.log"
    fika: "D:\\SPTarkov\\user\\mods\\fika-server\\logs\\fika.log"
    watchdog: "%APPDATA%\\WATCHDOG"
  bind_host: "0.0.0.0"              # firewall scopes reachability (D11)
  port: 8080

processes:                          # config-driven discovery (D4)
  spt_server_process_name: "SPT.Server.exe"
  headless_process_name: "EscapeFromTarkov.exe"
  headless_cmdline_pattern: "--fika-headless"   # confirm from your setup via discover_processes.py

collection:
  interval_seconds: 5
  history_retention_hours: 48
  whea_poll_seconds: 60             # D7/D16
  top_others_poll_seconds: 15       # D20

temps:                              # D9/D30
  lhm_dll_path: "vendor/lhm/LibreHardwareMonitorLib.dll"
  cpu_sensor_name: ""               # fill from probe_temps.py output, then arm cpu_thermal
  tctl_offset: 20                   # Zen1 (1800X) — confirm via probe

gates:                              # conservative v1 defaults; lower after baselining (D10)
  ram_high:
    enabled: true
    threshold: 90                   # conservative; lower to ~82 after baselining
    duration_seconds: 300
    recommendation: "Add 16-32GB DDR4 3000MHz+ kit (dual channel). Verify XMP + SPT/headless working sets."
  cpu_thermal:
    enabled: false                  # D9: disabled until probe-validated
  # ... other gates

auth:                               # D12/D13/D24/D33
  token: "<generated>"             # ≥32-byte random, install-generated, ACL'd to SYSTEM+Admins, never logged
```

---

## 6. Deployment, Testing & Maintenance

### Step-by-Step Deployment (LTSC)
1. Prepare Win11 IoT LTSC (debloat, AM4/LAN/AIO drivers, updates). Static IP or Tailscale up. Ensure the **.NET runtime** is present (required by pythonnet/LHM; D30).
2. Install Python 3.12+ (PATH; venv recommended). `python -m pip install --upgrade pip`.
3. Create folder `D:\Tools\RaidWatch` (non-OS drive). Copy project files (incl. `vendor/lhm/`, `nssm.exe`); `python -m venv .venv`; `.venv\Scripts\activate`; `pip install -r requirements.txt`.
4. (Optional) pre-edit `config.yaml`; otherwise it **auto-generates on first run** with system metrics live + Fika disabled (D23).
5. `python main.py` (foreground test). Verify browser loads, login works (D24), collector populates metrics, `/health` is green, no errors.
6. Service install (D18): run `scripts\install_service.ps1` (admin) — uses the **vendored NSSM** to install "RaidWatch" running the venv python as **SYSTEM** (D9/D31), SCM restart on 1/2/3 failures, **ACLs `config.yaml` to SYSTEM+Administrators** (D33), and registers the **external `/health` watchdog** Scheduled Task (D27).
7. Firewall (D11): `New-NetFirewallRule -DisplayName "RaidWatch" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow -RemoteAddress @("<your-LAN-subnet>", "100.64.0.0/10")` (exclude guest/IoT VLANs).
8. Access from gaming PC: `http://<host>:8080` (LAN) or `http://<tailscale-ip>:8080`. Log in once (cookie minted; D24).
9. Temps validation (D9): `python scripts\probe_temps.py` on the host → fill `temps.cpu_sensor_name` + confirm `tctl_offset`, then set `gates.cpu_thermal.enabled: true` and restart.
10. Process discovery (D4): `python scripts\discover_processes.py` → confirm/fill `processes.headless_cmdline_pattern`.
11. Baseline (D10): launch a test raid; watch CPU/RAM during bot spawns; confirm conservative gates don't false-positive; then lower thresholds to real headroom.

### Testing & Validation
- **Automated (D32):** `pytest tests/` covers gate timing, WHEA window decay, downsampling bucket math, regex classification, config validation, broker overflow — the tricky pure logic, locked down before prod.
- Collector health: service logs (NSSM-captured; D26) or `/health` (D35).
- Metrics accuracy: cross-check Task Manager / Resource Monitor / HWiNFO vs dashboard.
- Gates: `scripts/stress_test_sim.py`; confirm banner + recommendation.
- Fika parsing: start SPT + Fika, join raid, check Recent Events updates (decorative; D3).
- Temps: `probe_temps.py` output vs LHM UI on the 1800X (D9).
- WHEA: windowed re-query vs `wevtutil qe System /q:"*[System[Provider[@Name='WHEA-Logger']]]" /f:text /c:5`.
- SSE + broker: open multiple tabs/devices; simulate a slow client; confirm other clients and the collector are unaffected (D28).
- Supervision: kill the collector task internally (debug hook) → confirm supervisor restarts it and `/health` reflects a brief blip, not a silent stall (D27).
- Longevity: 48h+ soak — DB size, dashboard memory stability, no log spam, `/health` stable.

### Maintenance
- Update: stop service, `git pull`/replace files, `uv sync`/`pip install -U -r requirements.txt`, restart, run `pytest`, test collector.
- Log rotation: owned by NSSM (D26). Prune SQLite `>48h` via internal hourly schedule.
- Troubleshooting: NSSM-captured stdout log + `/health` + "Collector degraded" banner guide to root cause.
- Resource audit: dashboard should not be a top process; if it is, reduce collection freq or optimize parsers.

---

## 7. Risks, Mitigations & Future Roadmap

**Risks & Mitigations**
- **WinRing0 kernel driver in a SYSTEM, network-reachable service (D31):** LHM reads sensors via WinRing0, which carries **CVE-2020-14979 (local privilege escalation)** and is AV-flagged. We load it into a SYSTEM process on `0.0.0.0`. Mitigations: obtain the driver/DLL **only from the official LHM GitHub release** (vendored in `vendor/lhm/`); rely on the firewall scope (D11) to limit network reach; accept that temps is the **only** feature forcing SYSTEM + WinRing0, which is itself the argument for the D9 "ship temps in v1" trade-off being revisited if the threat environment changes (deferring temps to v1.1 removes SYSTEM + WinRing0 entirely and lets the service run least-privilege).
- **Inaccurate/unvalidated temps on Ryzen (D9):** sensor identity (Tdie vs Tctl vs CPU Package) and the Zen1 +20°C offset can't be known without enumerating on the 1800X. Mitigated by **decoupling display from gating** — temps display from launch, `cpu_thermal` ships **disabled** until `probe_temps.py` validates identity/offset and the config mapping is filled.
- **Silent collector staleness (D27):** a dead/hung collector on a 24/7 service. Mitigated by (a) loop-body wrapping so it can't die from exceptions, (b) supervisor restart on task exit, (c) `/health` staleness signal + D22 pill, (d) external Scheduled-Task watchdog curling `/health` for the irreducible native-hang case.
- **SSE backpressure (D28):** a slow/stuck client. Mitigated by the broker's bounded per-subscriber queues (drop-oldest) and subscriber cap — collector and other clients are never blocked.
- **Log format changes (SPT/Fika updates):** contained by D3 — log events are decorative and never gate-feeding; patterns/keywords are config-overridable.
- **Native-interop GIL hang (LHM):** can wedge the interpreter in ways try/except can't catch. Mitigated by NSSM (D18) + the external `/health` watchdog (D27).
- **Resource creep:** bounded deques + pruning; dashboard's own usage visible in Top Processes; interval user-tunable; per-module isolation (D8).
- **DB lock contention:** impossible by construction via the single shared connection (D21).
- **Timestamp/DST corruption:** UTC everywhere + `time.monotonic()` durations (D19).
- **Frontend CDN/offline fragility:** eliminated by vendoring assets (D29).
- **Dependency drift:** pinned via `uv` lock (D32).
- **Security (D11/D12/D13/D33):** SYSTEM service on `0.0.0.0` → cleartext cookie-over-HTTP accepted for the LAN-trust model; mitigated by firewall scope, ≥32-byte install-generated token, ACL'd config (SYSTEM+Admins), token never logged. TLS (mkcert/Caddy) deferred to v1.x; revisit if the LAN includes untrusted devices.

**Future Extensibility (post-MVP; D6 trigger)**
- Prometheus `/metrics` exporter for central Grafana.
- Long-term storage (InfluxDB export) if needed.
- Generalize the module contract to a real framework **only when a second game is actually imminent** (Rule-of-Three; D6).
- Config-editor UI, deep-dive/logs tabs, Discord webhooks, weighted health score (all v1.x; D2).
- Multi-host support.
- Richer Fika integration if a local status HTTP surface appears.

---

## 8. Conclusion & Next Steps

**RaidWatch** is a practical, homelab-native, upgrade-gate-focused monitoring dashboard for an SPT + Fika dedicated host: a reliable hardware core, decorative Fika context, and self-monitoring/self-healing for unattended 24/7 operation. It directly supports finalizing the combined AM4 (or mini-PC) build, the debloated LTSC setup, and data-driven RAM/CPU/storage decisions.

**Implementation Order**
1. Core collector + psutil/pywin32 metrics + SQLite (single connection, fixed wide table, migrations) + basic FastAPI JSON endpoints + auth/login (D7/D14/D21/D24/D32).
2. Broker + supervisor + `/health` (D27/D28/D35).
3. Single `index.html` + vendored Tailwind/Chart.js + SSE full-snapshot push + REST pre-fill (D5/D25/D29).
4. Fika process discovery + config read + periodic log tail (D3/D4/D17).
5. Gate logic (monotonic durations, conservative defaults, layered status pill) (D8/D10/D16/D19/D22).
6. NSSM service install + SETUP_GUIDE + firewall + first-run auto-config + external watchdog (D11/D18/D23/D26/D27).
7. Temps (LHM via pythonnet) — display ON, gate disabled; ship probe + discovery scripts (D9/D30/D31).
8. Tests + quality bars throughout (D32). UTC/monotonic time discipline throughout (D19).

**Open follow-ups requiring the user's input** (from `DECISIONS.md`):
- Confirm the exact headless-client launch arg from the WATCHDOG/Fika setup (D4).
- Run `probe_temps.py` on the 1800X to capture the real LHM sensor name + observe the Tctl offset, then arm `cpu_thermal` (D9).
- Baseline a real raid to tune gate thresholds from conservative defaults to real headroom (D10).
- Conscious acceptance of the WinRing0-in-SYSTEM risk (D31) — or revisit deferring temps to v1.1.

**References:** psutil docs, FastAPI tutorial, aiosqlite examples, pywin32 (`win32pdh`/`win32evtlog`), pythonnet + LibreHardwareMonitorLib, Fika wiki (headless-client), WATCHDOG mod page. See `.docs/DECISIONS.md` (D1–D34) for the rationale behind every choice above.
