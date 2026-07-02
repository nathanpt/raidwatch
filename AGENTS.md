# AGENTS.md — Project Briefing for AI Sessions

> **Read this first.** This file gives you the full context of the RaidWatch project
> so you can be productive immediately without re-reading the entire design docs.

## What Is RaidWatch

RaidWatch is a lightweight, browser-served monitoring dashboard for a dedicated
SPT + Project Fika (Escape From Tarkov single-player/coop mod) host running
Windows 11 IoT LTSC. It provides real-time hardware health metrics (CPU, RAM,
storage, temps, WHEA errors), a Fika context module (process/config/logs), and
**upgrade gates** — stateful thresholds that turn sustained metric crossings into
concrete hardware upgrade recommendations.

**Repo:** https://github.com/nathanpt/raidwatch (public)
**Production host:** AMD Ryzen 1800X (Zen1, AM4) Windows 11 IoT LTSC box

## Current State

**v1 is functionally complete and deployed.** The service installs and runs on
the Windows host. The dashboard loads, login works, and live system metrics
(CPU/RAM/disk/net) stream over SSE. 73 unit tests pass, ruff is clean.

**What's proven working on Windows:**
- One-command installer (`install.ps1`) — venv, deps, token, NSSM service, firewall, watchdog
- Service runs as SYSTEM via NSSM with proper stop/restart handling
- Dashboard accessible at `http://localhost:8080`, cookie auth works
- SSE live streaming, Chart.js charts, status cards
- SQLite persistence with on-the-fly downsampling

**What has NOT yet been validated on the Windows host (needs testing):**
- `probe_temps.py` — LHM sensor enumeration on the 1800X (D9)
- `discover_processes.py` — headless client cmdline pattern (D4)
- Gate triggers with real load (`stress_test_sim.py`)
- WHEA event collection via `win32evtlog` (D16)
- pywin32 PerfMon counters (disk queue, pages/sec) (D7)
- 48h soak test (DB size stability, memory, no log spam)

## Dev/Prod Split

**Development is on Linux** (Python 3.12 via uv). **Production is Windows 11 IoT LTSC**
(Python 3.14). Windows-only dependencies (`pywin32`, `pythonnet`) use PEP 508
`sys_platform == "win32"` markers — they are NOT installed on Linux.

On Linux the app runs in **degraded mode**: psutil system metrics work; pywin32
(WHEA, disk queue, pages/sec), temps (LHM/pythonnet), and Fika process discovery
degrade to `None`. This is by design (D8 isolation). All pure logic is fully
unit-testable on Linux.

## Architecture (10-second tour)

```
Browser (SSE + REST, cookie-authed)
       ↓
FastAPI/Uvicorn (0.0.0.0:8080)
  ├── lifespan: open DB, start collector + supervisor
  ├── REST: /api/metrics/{current,history,export.csv}, /api/gates, /api/fika/*
  ├── SSE: /api/stream (full snapshot every 5s via broker)
  ├── /health (machine-readable, D35 contract)
  └── SQLite (single shared connection, D21)
       ↑
Collector (5s loop, loop-body wrapped D27, per-module isolated D8)
  ├── modules/system.py (psutil + pywin32)
  ├── modules/fika.py (process discovery + config + log tail)
  ├── modules/temps.py (LHM via pythonnet, Windows-only)
  └── gates.py (sustained-duration state machine, monotonic clock D19)
       ↑
Broker (bounded per-subscriber queues, drop-oldest D28)
Supervisor (restarts collector on exit D27)
```

## File Map

```
raidwatch/
├── main.py            # FastAPI app, lifespan, REST routes, SSE, entry point
├── collector.py       # 5s async loop, gathers modules, persists, evaluates gates, publishes
├── broker.py          # Non-blocking fan-out, bounded queues (D28)
├── supervisor.py      # Restarts collector on unexpected exit (D27)
├── health.py          # /health D35 contract + staleness logic
├── auth.py            # Login form, constant-time token, HttpOnly cookie (D24)
├── config.py          # Pydantic + YAML, auto-generate, regex validation (D4/D23)
├── database.py        # Single aiosqlite connection (D21), migrations (D32), downsampling (D15)
├── gates.py           # Sustained-duration state machine, hysteresis, status pill (D10/D19/D22)
├── models.py          # Pydantic models for §3.4 snapshot contract
├── modules/
│   ├── system.py      # psutil (CPU/RAM/disk/net) + pywin32 (queue/pages/WHEA)
│   ├── fika.py        # Process discovery (D4) + config read (D3) + log tail (D17)
│   └── temps.py       # LHM via pythonnet (D9), import-guarded
├── templates/         # base.html, dashboard.html, login.html (Jinja2)
├── static/
│   ├── app.js         # SSE client, Chart.js, gauges, toasts, keyboard shortcuts
│   └── vendor/        # chart.umd.min.js + tailwind.css (vendored, D29)
├── tests/             # 73 tests: gates, WHEA, downsampling, config, broker, log regex
├── scripts/           # install/uninstall ps1, probe_temps, discover_processes, etc.
├── install.ps1        # One-command Windows installer
├── nssm.exe           # Vendored NSSM 2.24 (D18)
├── pyproject.toml     # uv project, ruff, pytest, platform markers
└── config.yaml.example
```

## Key Patterns & Conventions

- **D8 isolation**: every module/source wrapped in its own try/except → `None`
  for the failed key + error counter. A crashing module never blanks others.
- **D19 time discipline**: persist/query timestamps as UTC epoch ms; gate
  durations use `time.monotonic()` only.
- **D21 single connection**: one shared aiosqlite connection, never per-request.
- **D27 self-healing**: loop body fully wrapped + supervisor task + external
  `/health` watchdog Scheduled Task.
- **D6 dict-merge modules**: modules return dicts merged under namespaced keys
  (`system.*`, `fika.*`); no plugin framework.
- **D29 vendored assets**: Tailwind compiled once at authoring time (not CDN);
  Chart.js UMD is a static file. No deploy build step.
- **PowerShell encoding**: all `.ps1` files must be pure ASCII, saved with
  UTF-8 BOM. PowerShell 5.1 reads as Windows-1252 by default.
- **NSSM service**: run `python -m raidwatch.main` (not `main.py`); use
  `AppStopMethodSkip=1` (no console = no Ctrl+C); reconfigure in-place on
  reinstall (never `sc.exe delete` mid-install — causes zombie services).

## How to Run

```bash
# Linux dev (degraded mode)
uv sync
uv run pytest                    # 73 tests
uv run ruff check .              # lint
uv run uvicorn raidwatch.main:app --port 8080
# Or: uv run python -m raidwatch.main

# Windows production
git clone https://github.com/nathanpt/raidwatch.git
cd raidwatch
.\install.ps1                    # one-command install
.\install.ps1 -Uninstall        # clean uninstall
```

## Design Docs (local only, gitignored)

`.docs/DESIGN.md` — the living spec (what we're building, current).
`.docs/DECISIONS.md` — ADR log (D1-D35, the *why* behind every choice).
These are **not in the public repo** (gitignored) but exist in the working copy.
Reference D-numatures (e.g. "D21") point at specific decisions in DECISIONS.md.

## Known Issues / Next Steps

1. **Validate Windows-only paths on the 1800X host** — temps probe, process
   discovery, WHEA, pywin32 counters. These were written correct-by-construction
   but never tested on real hardware.
2. **Vendor `vendor/lhm/` DLLs** — LibreHardwareMonitorLib.dll + deps from
   official LHM release (D30). Currently the path is configured but DLLs aren't
   committed.
3. **Gate threshold baselining** — after a real raid, lower conservative defaults
   to actual headroom (D10).
4. **Frontend polish** — the dashboard HTML/JS was built functionally but not
   refined; likely has rough edges and untested edge cases in production.
5. **Soak test** — 48h+ uptime to verify DB pruning, memory stability, log rotation.
6. **Status pill client-side logic** — `app.js` has placeholder logic for the
   D22 layered status pill; needs wiring to real `/health` + gate data.
7. **Temp card wiring** — the collector gathers temps but the gate wiring for
   `cpu_thermal` needs the probe to run first.

## Testing Commands

```bash
uv run pytest                                    # all 73 tests
uv run pytest tests/test_gate_timing.py -v       # gate logic specifically
uv run python scripts/collect_once.py            # standalone metrics dump
uv run python scripts/smoke_test_m2.py           # SSE + health smoke test
uv run python scripts/smoke_test_m3.py           # frontend smoke test
uv run ruff check raidwatch/ tests/ scripts/     # lint
uv run ruff format raidwatch/ tests/ scripts/    # format
```
