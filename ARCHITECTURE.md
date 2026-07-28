# Architecture

> A map for a contributor who has read the README but does not yet know where
> things live. For the exhaustive file list and the decision log, see
> `AGENTS.md`; this document is about *how the pieces relate* and the
> invariants that are not obvious from reading any single file.
>
> Keep this short and stable: it describes structure that rarely changes, not
> implementation detail. Name symbols in prose; do not link them (links go
> stale — use symbol search).

## Bird's eye

RaidWatch is a single-process Python service that watches a dedicated
SPT + Project Fika (Escape From Tarkov) host. On a fixed 5-second cadence it
gathers hardware metrics (CPU, RAM, disk, net, temps, WHEA errors) plus Fika
context (processes, config, logs), persists a scalar subset to SQLite, evaluates
stateful "upgrade gates" that turn sustained metric crossings into hardware
recommendations, and pushes each snapshot to any connected browser over SSE.
The browser is a read-only viewer; all collection, storage, and decision-making
happens server-side in one process.

## Codemap

Three lanes: **collection**, **distribution**, and **serving**. They meet at the
`MetricsSnapshot` contract (`models.py`) and the single SQLite database
(`database.py`).

```
                        lifespan (main.py)
              load_config → Database.connect → Broker → Collector → Supervisor
                              collector.start() + supervisor.start()
                                          │
                                          ▼
   ┌────────────── collection (5s) ──────────────┐
   │ Collector._cycle  (collector.py)            │
   │   gather ─► persist ─► gates ─► publish     │
   │      │                       │       │      │
   │      ▼                       ▼       ▼      │
   │ modules/                 GateEvaluator  Broker.publish
   │  system.py  fika.py  temps.py   (gates.py)   │ (non-blocking)
   │      │        │        │                     │
   │      └────────┴────────┴──► MetricsSnapshot  │
   └──────────────────────────────────────────────┘
                   │                        │
                   ▼                        ▼
            Database                 ┌── SSE /api/stream
          (scalar row)               └── collector.latest  (REST /current)
```

**Entry & lifecycle**
- `main.py` — `create_app` builds the FastAPI app; `lifespan` is the single
  wiring point: it constructs `Database`, `Broker`, `Collector`, `Supervisor`,
  stores them on `app.state`, and starts the collector + supervisor. All routes
  are registered in `_register_routes`. `main()` is the NSSM/`python -m` entry.
- `supervisor.py` — `Supervisor` watches `collector._task` and restarts it on
  unexpected exit. The asyncio-level backstop; the external Scheduled Task
  curling `/health` is the final backstop for native-interop hangs.

**Collection core**
- `collector.py` — `Collector._run_forever` runs `_cycle` every 5s
  (no-overlap: next cycle scheduled 5s after *completion*). `_cycle` is the
  whole story: `_gather_snapshot` → `_persist` → `GateEvaluator.evaluate` →
  `Broker.publish` → `maybe_prune`. The latest snapshot is cached on
  `collector.latest` so `/api/metrics/current` never hits the DB.
- `modules/system.py`, `modules/fika.py`, `modules/temps.py` — the metric
  sources. Each exposes a `gather*` function returning a dict merged under
  namespaced keys (`system.*`, `fika.*`). They do not import each other.
  `fika.py` is the largest module: process discovery, read-only config summary,
  and rotation-safe log tailing.

**Distribution**
- `broker.py` — `Broker` sits between the collector and SSE subscribers. One
  bounded `asyncio.Queue` per subscriber; `publish` is synchronous and never
  awaits a client. A new subscriber gets the latest snapshot first (resync).

**Persistence & decisions**
- `database.py` — `Database` owns the single shared `aiosqlite` connection,
  runs idempotent schema migrations on connect, and does query-time
  downsampling. This is the *only* module that touches SQLite.
- `gates.py` — `GateEvaluator`, the sustained-duration state machine. It reads
  a plain dict snapshot (`model_dump()`), not live objects; its state lives in
  the `gate_state` table so it survives restarts.
- `health.py` — `build_health` assembles the machine-readable `/health`
  contract from collector liveness + module states.

**Contract & config**
- `models.py` — the pydantic snapshot/event models. This is the wire format and
  the shared vocabulary every lane speaks.
- `config.py` — pydantic + YAML config with first-run auto-generation.
- `auth.py` — cookie auth, constant-time token check, and the token-redaction
  log filter.

**Presentation**
- `templates/` (Jinja2) and `static/` (vendored Chart.js + compiled Tailwind,
  plus `app.js`) consume the snapshot shape from `models.py`. They contain no
  logic and depend on nothing server-side except the JSON the routes emit.

## Invariants (the non-obvious ones, several are *absences*)

- **One database connection, never per-request.** A single `aiosqlite`
  connection is opened in `lifespan` and shared by the collector (writes) and
  the API (reads). All access is serialized on the event loop, so
  `SQLITE_BUSY` is unreachable by construction. There is no pool and no
  `Database()` constructed inside a handler.
- **The collector never blocks on a client.** `Broker.publish` is synchronous
  and drop-oldest; SSE handlers drain their own queues. A stuck browser cannot
  stall the 5s cycle or other subscribers. There is no `await` on a client in
  the collection path.
- **A failing module never blanks its siblings.** Every source is wrapped in its
  own try/except that returns `None` for the failed key plus an error counter;
  after repeated failures the module backs off to ~60s. No module's exception
  can reach another module's fields.
- **The collection loop cannot die from an exception.** The entire `_cycle` body
  is try/except-log-continue; `Supervisor` is the backstop only if the task
  itself returns. No uncaught exception stops collection.
- **Gates never read Fika log events.** Fika events are decorative context only
  (`models.FikaEvent`); `GateEvaluator` reads scalar metrics. There is no path
  from `fika_events` into gate evaluation.
- **Only a scalar subset is persisted.** `metrics_history` is a fixed wide table
  of scalars (one row per cycle). Variable-cardinality data (per-core CPU, per-
  NIC stats, process lists, recent events) is live-only — it is never written as
  columns. Downsampling happens at query time, never on write.
- **Time is disciplined.** Persisted and queried timestamps are UTC epoch
  milliseconds; gate durations use `time.monotonic()` only. Monotonic values are
  never persisted; wall-clock values are never used for duration math.
- **The model layer depends on nothing above it.** `models.py` is pure data.
  Templates and static assets depend on the snapshot *shape*, never on server
  internals. (The web layer is downstream of the contract, not the other way
  around.)
- **Circular imports are avoided by lazy import.** `Collector` lazy-imports
  `FikaModule` and `GateEvaluator`; `gates.py` is reached only through the
  collector. If you add a new cross-package dependency, follow this pattern
  rather than top-level imports.

## Boundaries

- **Collector → Broker → SSE** is the load-bearing decoupling boundary. The
  collector knows nothing about subscribers; the broker knows nothing about
  collection. Anything that wants to push to browsers goes through
  `Broker.publish`.
- **Module boundary.** Each `modules/*.py` exposes a `gather*` entry point and
  returns a dict; the collector merges them. To add a metric source, add a
  module and a `_call_module` site in `_gather_snapshot` — do not wire it into
  the broker, gates, or routes directly.
- **Database boundary.** `Database` is the sole touchpoint for SQLite. Anything
  needing persistence adds a method here rather than opening its own
  connection.
- **Gate boundary.** `GateEvaluator` consumes a dict snapshot, so it is testable
  with no live collection. Persistable gate state lives in `gate_state`; the
  pill precedence (`compute_status_pill`) is pure and shared by `/health` and
  the frontend.

## Cross-cutting concerns

- **`DNN` references.** Code and docs cite decisions as `D8`, `D19`, `D21`, etc.
  These resolve to entries in `.docs/DECISIONS.md` (a gitignored, local-only
  ADR log). Treat them as stable pointers to *why* a choice was made. The most
  load-bearing: **D8** per-module isolation, **D19** time discipline, **D21**
  single connection, **D22** status-pill precedence, **D27** self-healing,
  **D28** non-blocking fan-out, **D35** health contract.
- **Isolation idiom.** The same try/except → `None` + error-counter shape
  repeats across modules, the gate evaluator, the collector cycle, and the
  broker enqueue. When adding a new source or step, mirror it.
- **Linux dev / Windows prod split.** Windows-only deps (`pywin32`, `pythonnet`)
  carry `sys_platform == "win32"` markers and are absent on Linux, where the
  app runs in *degraded mode*: psutil metrics work, WHEA/temps/Fika-process
  discovery return `None`. All pure logic (gates, downsampling, config, broker)
  is fully unit-testable on Linux; do not gate that logic on `platform`.

## Future direction

`docs/migration/` holds a WIP=1 board for porting RaidWatch to a C++ TUI forked
from btop4win, with the Python web stack removed at the end. This document
describes the *current* Python architecture; revisit it when the cutover step
lands.
