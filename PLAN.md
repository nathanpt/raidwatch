# RaidWatch v1 — Implementation Plan

> Built from `.docs/DESIGN.md` (living spec) + `.docs/DECISIONS.md` (D1–D35 ADRs). This plan translates the spec into a concrete, sequenced, checkable build. Decision references (e.g. D21) point at `DECISIONS.md`.

## Context

**RaidWatch** is a lightweight, browser-served monitoring dashboard for a dedicated SPT + Project Fika host on Windows 11 IoT LTSC. It surfaces real-time + historical hardware/system health, a Fika context module (process/config/best-effort logs), and **upgrade gates** that turn sustained metric crossings into concrete recommendations. It runs 24/7 as a self-healing service.

This is a **greenfield** repo: only `.docs/` exists. The spec is decision-correct (every fork resolved in `DECISIONS.md`), so this plan executes the chosen design rather than re-litigating it.

**Dev/prod split (confirmed):** Development happens on **Linux**; production is **Windows 11 IoT LTSC**. Windows-only deps (`pywin32`, `pythonnet`/LibreHardwareMonitor) cannot run here. The app must **start in degraded mode on Linux** (system metrics + collector + broker + SSE + UI + gates all work; Fika/temps/WHEA show "unavailable"), and all pure logic must be fully unit-testable locally. The Windows-specific paths are correct-by-construction and validated later in prod (consistent with the spec's "temps debugged in prod" stance, D9).

## Decisions baked into this plan

- **Scope:** full v1, all 8 milestones (spec §8 order).
- **Python:** pin **3.12** via `uv`/`.python-version` for dev↔prod parity (spec: "3.12+"; pythonnet 3.1.0 + pywin32 both confirmed there).
- **Cross-platform deps:** Windows-only packages use PEP 508 `sys_platform == "win32"` markers; imports are runtime-guarded on `sys.platform` and degrade to `None` per the D8 contract.
- **SSE transport:** `sse-starlette` (`EventSourceResponse`) — idiomatic, handles keepalive/reconnect; cookie auth (D12) works with native `EventSource`.
- **Asset strategy:** Tailwind compiled once via standalone CLI → committed `static/vendor/tailwind.css`; Chart.js UMD vendored (D29).

## Dependency strategy (`pyproject.toml`)

```
[project] dependencies (all platforms):
  fastapi, uvicorn[standard], aiosqlite, psutil, pydantic>=2, pydantic-settings,
  pyyaml, python-multipart (login form POST), sse-starlette

[project.optional-dependencies] / markers (Windows only):
  pywin32  ; sys_platform == "win32"
  pythonnet ; sys_platform == "win32"
```
`uv` skips the marker'd deps on Linux → the project installs and boots degraded. `requirements.txt` is the `uv export`/lock output, pinned (D32).

---

## Milestones

### M0 — Project scaffold, config & data layer

**Goal:** repo skeleton, config load/validate/auto-generate, DB with migrations — nothing runs an app yet, but everything downstream depends on it.

- [ ] `pyproject.toml` — uv project, ruff (lint+format), pytest config, project metadata, dep markers above; `.python-version` → 3.12; `.gitignore` (`data/`, `.venv/`, `__pycache__/`, `*.db`)
- [ ] `raidwatch/config.py`
  - `pydantic-settings` + yaml loader; load `data/config.yaml`; on missing file, auto-generate from `config.yaml.example` with safe defaults (D23)
  - Pydantic models for `server`, `processes` (compile `headless_cmdline_pattern` regex at load — invalid regex → clear `ValidationError`, D4), `collection`, `temps`, `gates` (per-gate `enabled/threshold/duration_seconds/severity/recommendation`), `auth`
  - `config.yaml.example` mirroring spec §5 example (conservative gate defaults per D10; `cpu_thermal.enabled: false` per D9)
- [ ] `raidwatch/database.py`
  - Single shared `aiosqlite` connection opened in lifespan, reused by collector (writes) + API (reads) + pruning — serialized on the event loop (D21); defensive `busy_timeout`
  - `PRAGMA user_version` + idempotent DDL migrations run at startup (D32); create: `metrics_history` (wide table, §3.4 columns), `fika_events`, `gate_events`, `whea_events(record_number UNIQUE)` (D14)
  - `insert_metrics_row(snapshot)`, `insert_event(...)`, gate-state get/set
  - History query with on-the-fly bucketing `GROUP BY (ts / bucket)` — `max()` for CPU/RAM, `avg()` for rates; bucket sized to cap ~720 pts/range (24h→2min, 6h→30s, ≤1h→raw 5s) (D15)
  - Prune `metrics_history > 48h` on an hourly schedule (not per cycle)
- [ ] `raidwatch/models.py` — pydantic models for the §3.4 snapshot contract (all nullable fields nullable), gate config, `/health` contract (D35), API response envelopes
- [ ] `tests/test_config_validation.py` — regex compile errors, missing-file auto-generate, defaults (D32)

### M1 — Core collector + system metrics + auth + REST  (first runnable slice)

**Goal:** `collect_once.py` dumps psutil metrics; `uvicorn raidwatch.main:app` serves login + `/api/metrics/current` JSON + history. On Linux: WHEA/disk-queue degrade to `None`.

- [ ] `raidwatch/modules/system.py`
  - `gather() -> dict` returning `system.*` per §3.4: `cpu_total_percent`, `cpu_per_core_percent` (`psutil.cpu_percent(percpu=True, interval=0.5)`), RAM/swap, disk I/O + volumes, net by NIC
  - pywin32 block guarded by `sys.platform == 'win32'`: `win32pdh` for `\Memory\Pages/sec`, `\PhysicalDisk(_Total)\Current Disk Queue Length` + avg sec/Transfer (D7)
  - WHEA: `win32evtlog` windowed re-query `TimeGenerated >= now−2h`, count + dedup via `whea_events.record_number` (D16); poll cadence driven by `whea_poll_seconds` (~60s), not 5s
  - Each source in own try/except → `None` for that key + per-module error counter (D8)
- [ ] `raidwatch/collector.py`
  - `async def run()`: 5s loop; `gather_metrics()` (merge module dicts under namespaced keys, D6); `persist()`; `check_gates()` (stub until M5); `publish()` to broker (non-blocking; M2)
  - **Entire loop body in try/except-log-continue** (D27) + per-module isolation (D8); schedule next cycle 5s after completion (no overlap); backoff after N consecutive module failures (~60s)
  - In-memory `collections.deque(maxlen=720)` live buffer for charts
  - `self.*` metrics: own CPU/RAM/cycle_ms/subscribers
- [ ] `raidwatch/auth.py`
  - `GET /login` (Jinja form, single password field), `POST /login` → constant-time compare against `auth.token` → set `HttpOnly`/`SameSite=Lax` ~90-day cookie → redirect to `/` (D24)
  - Dependency `require_auth` checks cookie (constant-time); protect all `/api/*` + `/`
  - Logging filter redacting the token from all records (D33)
- [ ] `raidwatch/main.py`
  - FastAPI app; **lifespan**: run migrations, open shared DB conn, start collector + supervisor tasks (M2), on shutdown cancel collector + close SSE subscribers + close DB (D27)
  - Jinja templates dir + `static/` mount; routers
  - `GET /api/metrics/current` (latest snapshot, §3.4), `GET /api/metrics/history?minutes=…&metrics=…` (D15), `GET /api/metrics/export.csv?minutes=…` (wide-table columns)
- [ ] `scripts/collect_once.py` — standalone cross-platform metrics dump (psutil subset) for local smoke testing

**Verify (local):** `uv run python scripts/collect_once.py` prints metrics; `uv run uvicorn raidwatch.main:app --reload` → `/login` renders, login mints cookie, `/api/metrics/current` returns JSON with real CPU/RAM, `/health` present (M2 enriches it).

### M2 — Broker + supervisor + `/health` + SSE stream

**Goal:** live data path: collector → broker → SSE → browser; collector self-heals; staleness is machine-detectable.

- [ ] `raidwatch/broker.py`
  - `publish(snapshot)` non-blocking; per-subscriber `asyncio.Queue(maxlen=K)` drop-oldest on overflow; subscriber cap (~20); new subscriber gets a full snapshot first (resync, D25) (D28)
- [ ] `raidwatch/supervisor.py`
  - Separate task `await`s the collector task; on unexpected return → log + restart; exposes `last_tick_ts` for `/health` + D22 pill (D27)
- [ ] `raidwatch/health.py`
  - Build `/health` per the D35 contract: `{status, version, started_at, collector:{last_tick_ts,last_tick_age_seconds,last_cycle_ms,consecutive_failures}, modules:{system/fika/temps:{state}}, sse_subscribers, db_size_mb}`
  - Status precedence server-side mirrors D22: stale-core (>3 cycles) → critical; else operational (gates enrich in M5)
- [ ] `GET /api/stream` (SSE) — `EventSourceResponse` subscribing to broker; cookie-authed (D12); full snapshot first, then every cycle (D5/D25/D28)

**Verify (local):** open `/api/stream` (authed) → receives full snapshot then ~5s ticks; simulate slow/stuck client (curl with tiny read buffer) → other subscribers + collector unaffected; debug-kill collector task → supervisor restarts, `/health` shows a brief blip not a silent stall.

### M3 — Frontend (single Overview page)

**Goal:** full dark-theme dashboard running locally off live SSE + REST pre-fill.

- [ ] Vendor assets: download Chart.js UMD → `static/vendor/chart.umd.min.js`; author `tailwind.config.js` + `static/src/tailwind.css` (`@tailwind` directives); `scripts/build_tailwind.py` runs standalone CLI → `static/vendor/tailwind.css --minify` (authoring-time only; D29)
- [ ] `templates/base.html` — nav (RaidWatch + hostname, status pill, uptime/last-run/Tailscale hint, Refresh Now / Export CSV, settings gear), vendored CSS/JS includes, SSE client init, footer
- [ ] `templates/login.html` — single password field (D24)
- [ ] `templates/dashboard.html` — status cards row, active alerts/gates banner (M5), mini charts row, top processes table, recent Fika events feed (M4)
- [ ] `static/app.js` — REST pre-fill on load (D25) → `EventSource('/api/stream', {withCredentials:true})`; Chart.js update on each snapshot; gate banner + toast rendering; status pill client-side (D22); keyboard shortcuts (`?` `/` `r`); loading/empty/error states; dark/light toggle persisted
- [ ] Cards: Overall Health, CPU gauge+%+sparkline, RAM gauge+used/avail+paging, CPU Temp (warning if unvalidated), Storage (free %+I/O sparkline/queue); gauges show current-vs-threshold headroom (D10)

**Verify (local):** browser loads, pre-fills from REST, then ticks live via SSE; cards/charts update every ~5s; CSV export downloads; reconnect (throttle network) → resyncs via full snapshot; status pill reflects degraded modules.

### M4 — Fika module

**Goal:** process tracking + read-only config + decorative log events (never feeds gates; D3).

- [ ] `raidwatch/modules/fika.py`
  - Process discovery via psutil: match `spt_server_process_name`, `headless_process_name` + compiled `headless_cmdline_pattern`; per-PID cpu%/rss/uptime/handles; aggregates `headless_count/cpu_total/rss_total` (D4)
  - Read-only config parse: `user/mods/fika-server/config.json` → `max_players/bot_limits/send_rate` (display-only); SPT `http.json` port
  - Periodic log tail each cycle (D17): per-file byte offsets in memory; seek→read→regex-classify (raid lifecycle/players/bots/network/crash); rotation-safe (offset>size → reset to end); restart resumes from end; persist important events to `fika_events`; keep last 100–500 in memory
  - All in D8 isolation; invalid paths disable only this module (D23)
- [ ] `GET /api/fika/status`, `GET /api/logs/tail?source=…&lines=…`
- [ ] `scripts/discover_processes.py` — dump candidate processes + cmdlines → fill `headless_cmdline_pattern` (D4)
- [ ] Wire `fika.*` + `events_recent` into dashboard cards/feed

**Verify (local, degraded):** with no SPT paths configured, Fika shows "not configured" banner and never blocks startup; unit-test regex classification + rotation logic offline with fixture logs (M8).

### M5 — Gate logic

**Goal:** the killer feature — sustained-duration state machines with conservative defaults + layered status pill.

- [ ] `raidwatch/gates.py`
  - `GateEvaluator`: per-gate state `{last_crossed_monotonic, currently_triggered}` persisted in SQLite (survives restarts)
  - `evaluate(snapshot) -> list[triggered]`: compare metric vs threshold with operator; sustained via `time.monotonic()` deltas (D19); WHEA uses windowed count (D16); cooldown/hysteresis (re-alert after 30 min or value drops 10% below)
  - Ship 5 hardware gates **enabled** with **conservative** defaults (D10): `ram_high`(>90%,5m), `cpu_sustained`(>88%,8–10m), `storage_io`(queue>2.5–4 or latency>10–20ms), `storage_space`(<15% or <40–50GB), `stability_whea`(>2–5 in 2h); `cpu_thermal` **disabled** (D9)
  - On trigger: log `gate_events`, publish banner via broker
  - Status pill layered precedence (D22): stale-core > High gate > Medium gate > Operational
- [ ] `GET /api/gates` (current status + history); wire banner into UI (M3 placeholder → real)
- [ ] `scripts/stress_test_sim.py` — fake load to exercise gates

**Verify (local):** unit tests for timing/decay (M8) pass; `stress_test_sim.py` pushes a metric past threshold for `duration_seconds` → banner + recommendation fires, then clears on cooldown; pill flips green→yellow/red correctly.

### M6 — Deployment (Windows)

**Goal:** install as a self-healing SYSTEM service with firewall + external watchdog.

- [ ] `scripts/install_service.ps1` — vendored NSSM installs `RaidWatch` running venv `python.exe main.py` as **SYSTEM** (D9/D31); SCM restart on 1/2/3 failures (D18); firewall rule scoped to LAN subnet + Tailscale `100.64.0.0/10`, excluding guest/IoT VLANs (D11); ACL `config.yaml` to SYSTEM+Administrators (D33); register external Scheduled Task that curls `/health` on short timeout and restarts the service on failure (the irreducible native-hang backstop; D27)
- [ ] `scripts/uninstall_service.ps1` — reverse of the above
- [ ] Vendor `nssm.exe` (~300KB) at repo root (D18)
- [ ] `README.md` (overview, quickstart) + `SETUP_GUIDE.md` (spec §6 step-by-step: .NET runtime prereq (D30), venv, first-run auto-config, service install, firewall, temps validation, process discovery, baselining)

**Verify (Windows host, later):** service starts as SYSTEM, survives reboot, `/health` green, firewall scopes reachability, killing the collector internally → supervisor restart; external watchdog task restarts on a forced hang.

### M7 — Temps (LHM via pythonnet)

**Goal:** CPU temp displayed from launch; `cpu_thermal` gate armed only after probe validation.

- [ ] Vendor `vendor/lhm/` — `LibreHardwareMonitorLib.dll` + deps (e.g. `HidSharp.dll`) + `LICENSE` (MPL-2.0) from official LHM GitHub release; path configurable `temps.lhm_dll_path` (D30)
- [ ] `raidwatch/modules/temps.py`
  - pythonnet loads vendored DLL (path from config); `Computer` object, enable CPU/GPU/HDD; iterate Temperature sensors; pick configured `cpu_sensor_name`; apply `tctl_offset` (Zen1 +20°C; D9)
  - Failure-tolerant (D8): any LHM/driver error → `temp_cpu_celsius=None` + UI warning, never crashes collector
  - Import-guarded `sys.platform == 'win32'` + `.NET runtime` required (documented)
- [ ] `scripts/probe_temps.py` — enumerate LHM sensors on the 1800X → dump names/values → validate identity + observe Tctl offset → fill config, then arm `cpu_thermal` (D9)
- [ ] Wire temp into CPU Temp card (M3) + enable `cpu_thermal` gate (M5, still disabled by default)

**Verify (Windows 1800X, later):** `probe_temps.py` output matches LHM UI; after config fill + `cpu_thermal.enabled: true`, sustained high temp → gate fires.

### M8 — Tests & quality (throughout; consolidated)

**Goal:** lock down the tricky pure logic (D32) so it's trustworthy even where hardware is uncertain (D9).

- [ ] `tests/test_gate_timing.py` — sustained duration, monotonic deltas, cooldown/hysteresis, restart state restore
- [ ] `tests/test_whea_window.py` — sliding 2h decay, `record_number` dedup across polls (D16)
- [ ] `tests/test_downsampling.py` — bucket math for 24h/6h/1h ranges, max vs avg selection (D15)
- [ ] `tests/test_log_regex.py` — classification patterns + rotation-safe offset logic (D17)
- [ ] `tests/test_config_validation.py` — regex compile, auto-generate, defaults (D4/D23)
- [ ] `tests/test_broker.py` — overflow drop-oldest, subscriber cap, full-snapshot-first (D28)
- [ ] Full type hints; `ruff check` + `ruff format` clean; `uv lock` committed (D32)

**Verify (local):** `uv run pytest` all green; `uv run ruff check` clean; `uv run uvicorn raidwatch.main:app` boots degraded (system metrics live, Fika/temps/WHEA unavailable) with no exceptions.

---

## Reuse / patterns to follow
- No existing code (greenfield). Apply these spec patterns uniformly:
  - **D8 contract:** every module/source in its own try/except → `None` for the failed key + per-module error counter; UI shows "X unavailable".
  - **D21:** one shared aiosqlite connection; never connection-per-request.
  - **D19:** persist/query timestamps as UTC epoch ms; gate durations via `time.monotonic()` only.
  - **D6:** modules return dicts merged under namespaced keys (`system.*`, `fika.*`); no plugin framework.
  - **D27:** loop body fully wrapped + supervisor; `/health` staleness signal.

## Verification (end-to-end summary)
- **On Linux (now):** `uv run pytest` green; `python scripts/collect_once.py` works; `uv run uvicorn raidwatch.main:app` → UI loads, login works, system cards populate, SSE live, gates/broker tests pass, `/health` operational.
- **On Windows host (later, via SETUP_GUIDE):** service as SYSTEM survives reboot; temps probe + process discovery + raid baseline to tune gates; external watchdog restarts on forced hang.

## Out of scope (v1.x, per D2 / spec Non-Goals)
Deep-dive/Fika/logs/settings tabs, config-editor UI, Discord webhooks, weighted health score, `ram_paging`/`fika_instability`/`network_errors` gates, HTTPS/TLS, Windows Event Log secondary sink, Prometheus exporter, multi-host.

## Open follow-ups needing the user (not blocking build; from DECISIONS.md)
- Confirm exact headless-client launch arg from the WATCHDOG/Fika setup → `headless_cmdline_pattern` (D4).
- Run `probe_temps.py` on the 1800X → real LHM sensor name + Tctl offset → arm `cpu_thermal` (D9).
- Baseline a real raid → tune conservative gate thresholds to real headroom (D10).
- Conscious acceptance of WinRing0-in-SYSTEM risk (D31) — or revisit deferring temps to v1.1.
