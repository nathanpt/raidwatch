# RaidWatch

A lightweight, browser-served monitoring dashboard for a dedicated SPT + Project
Fika host on Windows 11 IoT LTSC. Real-time hardware health (CPU, RAM, storage,
temps, processes, WHEA stability), a Fika context module (process monitoring,
read-only config, best-effort log events), and **upgrade gates** that turn
sustained metric crossings into concrete recommendations.

## Quick Start

**Windows (one command):**
```powershell
git clone https://github.com/nathanpt/raidwatch.git
cd raidwatch
.\install.ps1
```
The installer handles venv, deps, token generation, service install, firewall, and health watchdog. See **[SETUP_GUIDE.md](SETUP_GUIDE.md)** for details.

**Linux dev (degraded mode — system metrics work, Fika/temps/WHEA unavailable):**
```bash
uv sync
uv run uvicorn raidwatch.main:app --port 8080
```

## Key Features

- **Real-time dashboard**: SSE-pushed metrics every 5s, dark Tarkov-inspired theme
- **Upgrade gates**: stateful thresholds with concrete recommendations (the killer feature)
- **Fika module**: process tracking + read-only config + decorative log events (D3)
- **Self-healing**: supervisor restarts the collector; external watchdog for native hangs (D27)
- **Low footprint**: <2% CPU, <150-250 MB RAM idle
- **Offline-resilient**: vendored Tailwind + Chart.js, no CDN dependency (D29)

## Architecture

```
Browser (SSE + REST)
       ↓
FastAPI/Uvicorn (0.0.0.0:8080, cookie-authed)
  ├── Collector (5s loop, per-module isolated, loop-body wrapped)
  │     ├── psutil (CPU/RAM/disk/net)
  │     ├── pywin32 (disk queue, pages/sec, WHEA)
  │     ├── Fika module (process/config/logs)
  │     ├── Temps (LibreHardwareMonitor via pythonnet)
  │     └── Gate evaluator (monotonic durations)
  ├── Broker (bounded per-subscriber queues)
  └── SQLite (single shared connection, wide table + events)
```

## Development

```bash
uv sync                    # install deps
uv run pytest              # run tests (73 tests)
uv run ruff check .        # lint
uv run ruff format .       # format
uv run python scripts/collect_once.py  # standalone metrics dump
```

Windows-only deps (pywin32, pythonnet) use platform markers and are skipped on
Linux. The app runs in degraded mode on non-Windows (D8 isolation).

## Documentation

- **[SETUP_GUIDE.md](SETUP_GUIDE.md)** — full deployment guide
- **[.docs/DESIGN.md](.docs/DESIGN.md)** — living spec
- **[.docs/DECISIONS.md](.docs/DECISIONS.md)** — ADR log (D1–D35)
