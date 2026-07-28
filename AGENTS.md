# AGENTS.md — Project Briefing for AI Sessions

> **Read this first.** Operational briefing: what RaidWatch is, how to run and
> test it, what's open, and the editing guardrails. For the structural map —
> module relations, invariants, and layer boundaries — see `ARCHITECTURE.md` in
> the repo root.

## What Is RaidWatch

A lightweight, browser-served monitoring dashboard for a dedicated SPT +
Project Fika (Escape From Tarkov) host on Windows 11 IoT LTSC. It streams
hardware metrics (CPU/RAM/disk/net/temps/WHEA) plus Fika context
(processes/config/logs) to a browser over SSE, persists a scalar subset to
SQLite, and runs **upgrade gates** — stateful thresholds that turn sustained
metric crossings into hardware upgrade recommendations.

**Repo:** https://github.com/nathanpt/raidwatch (public)
**Production host:** AMD Ryzen 1800X (Zen1, AM4) Windows 11 IoT LTSC box

**v1 is functionally complete and deployed:** one-command installer, NSSM service
as SYSTEM, dashboard + cookie auth at `http://localhost:8080`, SSE streaming,
SQLite with on-the-fly downsampling. Test suite + ruff are clean.

## Dev/Prod Split

Dev is **Linux** (Python 3.12, uv); prod is **Windows 11 IoT LTSC** (Python 3.14).
Windows-only deps (`pywin32`, `pythonnet`) use `sys_platform == "win32"` markers
and are absent on Linux, so the app runs there in **degraded mode**: psutil
metrics work; WHEA, temps, and Fika process discovery degrade to `None`. All pure
logic (gates, downsampling, config, broker) is unit-testable on Linux.

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
├── tests/             # gates, WHEA, downsampling, config, broker, log regex, collector, auth, health
├── scripts/           # install/uninstall ps1, probe_temps, discover_processes, etc.
├── install.ps1        # One-command Windows installer
├── nssm.exe           # Vendored NSSM 2.24 (D18)
├── pyproject.toml     # uv project, ruff, pytest, platform markers
└── config.yaml.example
```

## How to Run

```bash
# Linux dev (degraded mode)
uv sync
uv run pytest                                    # 133 tests
uv run ruff check .                              # lint
uv run uvicorn raidwatch.main:app --port 8080
# Or: uv run python -m raidwatch.main

# Windows production
git clone https://github.com/nathanpt/raidwatch.git
cd raidwatch
.\install.ps1                    # one-command install
.\install.ps1 -Uninstall        # clean uninstall
```

## Testing & Probes

```bash
uv run pytest tests/test_gate_timing.py -v       # gate logic specifically
uv run python scripts/collect_once.py            # standalone metrics dump
uv run python scripts/smoke_test_m2.py           # SSE + health smoke test
uv run python scripts/smoke_test_m3.py           # frontend smoke test
uv run ruff format raidwatch/ tests/ scripts/    # format
```

## Open Work

Validated on Linux; **not yet validated on the 1800X Windows host** (written
correct-by-construction):

1. **Windows-only paths** — temps probe (LHM DLLs vendored, ready to test),
   headless-client process discovery, WHEA via `win32evtlog`, pywin32 PerfMon
   counters (disk queue, pages/sec).
2. **`cpu_thermal` gate wiring** — blocked on the temps probe running first (now
   unblocked by the vendored DLLs).
3. **Gate threshold baselining** — lower conservative defaults to actual headroom
   after a real raid (D10).
4. **Frontend polish** — dashboard HTML/JS is functional but unrefined.
5. **48h soak** — verify DB pruning, memory/handle stability, no log spam.

> **Migration planned:** `docs/migration/` is a WIP=1 board for porting RaidWatch
> to a C++ TUI forked from btop4win, removing the Python web stack at the end.

## Editing Guardrails

- **PowerShell encoding:** all `.ps1` files must be pure ASCII, saved with UTF-8
  BOM (PowerShell 5.1 reads as Windows-1252 by default).
- **NSSM service:** run `python -m raidwatch.main` (not `main.py`); set
  `AppStopMethodSkip=1` (no console = no Ctrl+C); reconfigure in-place on
  reinstall (never `sc.exe delete` mid-install — causes zombie services).
- **Before structural changes:** read `ARCHITECTURE.md` for the load-bearing
  invariants (single DB connection, collector never blocks on clients, per-module
  isolation, time discipline) and the layer boundaries.

## Design Docs (local only, gitignored)

`.docs/DESIGN.md` — the living spec. `.docs/DECISIONS.md` — ADR log (D1–D35).
`.docs/HANDOFF.md` — session handoff: build-host SSH/elevation, C++ build
gotchas, and migration step status (read first when resuming the migration).
Not in the public repo; `DNN` references in code/docs resolve there.
