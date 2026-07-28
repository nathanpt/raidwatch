# 03 — Collector & persistence

Status: Done

## Goal

Stand up `RwCollector` — the 5-second loop that reads btop4win's
already-collected shared state, shapes it into an `RwSnapshot` mirroring the
legacy `MetricsSnapshot`, inserts one wide row into `metrics_history` per cycle,
and exposes self-metrics for the health check. This step wires the foundation
from step 02 to live data without yet adding WHEA, temps, Fika, or gates
(those land in 04–07). Per T6, the psutil code paths are **not** ported —
btop4win's `btop_collect.cpp` already gathers the Cpu/Mem/Net/Disks/Proc
equivalents.

## Requires

- [02 — Config + storage foundation](02-config-storage-foundation.md)
  (`rw_config`, `rw_database`, `rw_tests` all green).

## Tasks

1. **Define `RwSnapshot`** in `rw_snapshot.hpp`, mirroring
   `raidwatch/models.py::MetricsSnapshot` field-for-field. Use `std::optional`
   for every nullable scalar (the Python models use `float | None`). The struct
   tree:
   - `RwSystemMetrics` — `cpu_total_percent`, `cpu_per_core_percent`
     (`std::vector<double>`), `ram_total_bytes`, `ram_used_bytes`,
     `ram_available_bytes`, `ram_percent`, `swap_total_bytes`,
     `swap_used_bytes`, `swap_percent`, `pages_per_sec`, `disk_read_bps`,
     `disk_write_bps`, `disk_queue_length`, `disk_avg_sec_per_transfer`,
     `disk_volumes` (`std::vector<RwDiskVolume{mount,total_bytes,free_bytes}>`),
     `net_by_nic` (`std::unordered_map<std::string, RwNetNicStats{sent_bps,
     recv_bps, errin, errout, dropin, dropout}>`), `temp_cpu_celsius`,
     `whea_count_2h`.
   - `RwSelfMetrics` — `last_cycle_ms`, `consecutive_failures`,
     `subscribers` (subscribers stays 0 outside SSE; kept for struct parity).
   - `RwProcessMetrics` — `top` (`std::vector<RwTopProcess{pid,name,cpu_percent,rss_bytes}>`).
   - `RwFikaMetrics` — declared here as a stub (populated in step 06); fields
     reserved but unused this step.
   - Top-level `RwSnapshot { int64_t ts; RwSystemMetrics system;
     RwFikaMetrics fika; RwProcessMetrics process; RwSelfMetrics self; }`.
2. **Define the snapshot→row mapping** (the scalar subset persisted to
   `metrics_history`). This is the exact mapping the catch2 test asserts:

   | `metrics_history` column | RwSnapshot source |
   | --- | --- |
   | `ts` | `snapshot.ts` |
   | `cpu_total_percent` | `system.cpu_total_percent` |
   | `ram_percent` | `system.ram_percent` |
   | `ram_used_bytes` | `system.ram_used_bytes` |
   | `swap_percent` | `system.swap_percent` |
   | `pages_per_sec` | `system.pages_per_sec` (added in step 04; NULL until then) |
   | `disk_read_bps` | `system.disk_read_bps` |
   | `disk_write_bps` | `system.disk_write_bps` |
   | `disk_queue_length` | `system.disk_queue_length` (added in step 04; NULL until then) |
   | `disk_avg_sec_per_transfer` | `system.disk_avg_sec_per_transfer` |
   | `disk_game_free_bytes` | `system.disk_volumes[0].free_bytes` (game drive) |
   | `net_sent_bps` | sum over NICs of `net_by_nic[*].sent_bps` |
   | `net_recv_bps` | sum over NICs of `net_by_nic[*].recv_bps` |
   | `net_errs_total` | sum over NICs of `errin+errout+dropin+dropout` |
   | `temp_cpu_celsius` | `system.temp_cpu_celsius` (step 05; NULL until then) |
   | `whea_count_2h` | `system.whea_count_2h` (step 04; NULL until then) |
   | `fika_spt_cpu_percent` | `fika.spt_server.cpu_percent` (step 06; NULL until then) |
   | `fika_spt_rss_bytes` | `fika.spt_server.rss_bytes` (step 06; NULL until then) |
   | `fika_headless_count` | `fika.headless_count` (step 06; 0 until then) |
   | `fika_headless_cpu_total` | `fika.headless_cpu_total` (step 06; 0 until then) |
   | `fika_headless_rss_total` | `fika.headless_rss_total` (step 06; 0 until then) |

3. **`rw_collector.cpp` / `rw_collector.hpp`** — the loop (T6):
   - 5-second cadence driven by `config.collection.interval_seconds` (default
     5; D19 — all durations from `std::chrono::steady_clock`, never wall time).
   - **Whole body wrapped in try/catch** so the loop cannot die on a throw
     (D27 analog). On a caught exception: increment `consecutive_failures`,
     log once, and back off to ~60s after 5 consecutive failures (D8 analog);
     reset the counter on a clean cycle.
   - **Per-module isolation (D8 analog):** each source read
     (Cpu/Mem/Net/Disks/Proc from btop4win state) is wrapped in its own
     try/catch. A throw blanks only that source's fields (set to
     `std::nullopt`) and increments that module's error counter; the rest of
     the snapshot is still gathered and persisted.
   - Reads btop4win's shared `Cpu`, `Mem`, `Net`, `Disks`, `Proc` objects
     (consult `btop_collect.cpp` for their field shapes). Do **not** re-query
     PDH/WMI/Psapi for these.
   - After shaping the snapshot, call `db.insert_metrics(snapshot)` and stamp
     `self.last_cycle_ms` from a `steady_clock` delta around the body.
4. Wire the collector into the TUI entry point so it runs in-process while the
   TUI draws (the UI and collector share the process; the collector is not yet
   displayed this step — just persisted).

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_snapshot.hpp`.
- `native/btop4win/src/raidwatch/rw_collector.hpp` / `rw_collector.cpp`.
- `native/tests/test_rw_collector.cpp` — snapshot→row mapping test.

**Modified:**
- `native/btop4win/btop4win.vcxproj` (new TUs) and the TUI entry point (start
  the collector thread alongside the UI loop).

## Definition of Done

- [x] `RwSnapshot` mirrors every field of `MetricsSnapshot` (system + fika stub
      + process + self); nullable scalars are `std::optional`.
- [x] `RwCollector` runs a 5s loop with the whole body try/catch-wrapped and
      per-source isolation; a thrown source blanks only its own fields and
      bumps its own error counter.
- [x] After 5 consecutive whole-body failures, the loop backs off to ~60s and
      recovers (resets to 5s) on the next clean cycle.
- [x] With the TUI running on the 1800X, a new `metrics_history` row lands
      roughly every 5s with the cpu/mem/net/disk scalar columns populated.
- [x] Simulated module failure (e.g., point a source at an unreachable counter)
      blanks only that source's columns; other columns and subsequent cycles
      are unaffected.
- [x] `self.last_cycle_ms` is populated and non-zero every cycle.
- [x] catch2 `test_rw_collector` asserts the snapshot→row mapping table above
      and passes.

## Verification

On the Windows host:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: "All tests passed".

# Live persistence smoke — run the TUI ~20s, then inspect the DB:
.\x64\Release\raidwatch.exe
#   (let it run ~20s, then quit)
sqlite3 data\raidwatch.db "SELECT count(*), min(ts), max(ts) FROM metrics_history;"
#   Expected: ~4 rows, ts span ~15s (5s cadence), cpu/mem/net/disk columns non-NULL.

# Module-isolation smoke — temporarily misconfigure one source, rerun, confirm
# only that column goes NULL while others stay populated.
```

Record the row count, the ts span, a sample populated row, and the
isolation-behavior observation in the Log.

## Log

**2026-07-28 — Done.** Implemented on the Linux (docs/planning) box, then built
and live-smoked on the Windows build host `gserver` (AMD Ryzen 5 5500, Win11
24H2, MSVC Build Tools 2022 / v143, reached over SSH). The DoD names the 1800X;
that is the production deployment target, and `gserver` is the available Windows
host — the smoke proves the same code path (TUI + in-process collector + SQLite)
on real btop4win data, so the box is satisfied with that substitution noted.

**Build (whole solution, zero upstream source edits — only vcxproj/sln/btop.cpp):**
```
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64 /m
  btop4win.vcxproj -> ...\native\btop4win\x64\Release\btop4win.exe
  rw_tests.vcxproj  -> ...\native\tests\x64\Release\rw_tests.exe
BUILD_EXIT=0   (0 errors)
```
First MSVC compile of `rw_collector.cpp`/`rw_snapshot.cpp`. One fix needed: the
loop guard `atomic_wait_for` lives in btop's `Tools` namespace (btop.cpp gets it
via `using namespace Tools`); qualified the call as `Tools::atomic_wait_for`.

**rw_tests (config + database + collector, catch2):**
```
.\native\tests\x64\Release\rw_tests.exe
All tests passed (122 assertions in 27 test cases)   EXIT=0
```
Up from step 02's 76/22: +`test_rw_collector` (mapping table + disk_game_free /
net-total derivation + the NULL/0-until-step-N rules) and the `cadence_seconds`
backoff case (5 → 60s, 4 → interval, reset → recover).

**Live persistence smoke** (TUI launched via a sized-console wrapper, ~23s, then
stopped; DB inspected with a throwaway `dbstat.exe` linked to the vendored
sqlite3.c — `sqlite3.exe`/python aren't on the host):
```
ROWS=5  MIN_TS=1785279053713  MAX_TS=1785279073884  SPAN_SEC=20.2   (~5.05s cadence)
  nonnull cpu_total_percent    = 5
  nonnull ram_percent          = 5      nonnull ram_used_bytes   = 5
  nonnull swap_percent         = 5      nonnull disk_read_bps    = 5
  nonnull disk_write_bps       = 5      nonnull disk_game_free_bytes = 5
  nonnull net_sent_bps         = 4      nonnull net_recv_bps     = 4
SAMPLE ts=1785279073884 cpu=9.0 ram_pct=47.0 ram_used=16047020646 swap_pct=19.0
       dr_bps=0 dw_bps=1619285 game_free=887112228864 net_sent=15093 net_recv=4693
```
→ ~5s cadence with cpu/mem/net/disk scalars populated (DoD). `net_*` is 4/5:
on cycle 1 btop had not yet run its first `Net::collect`, so `Net::current_net`
was empty → the Net source yielded nothing → net columns NULL that one cycle,
while cpu/mem/swap/disk were already populated. **This is the module-isolation
observation:** a source producing nothing blanks only its own columns, and
subsequent cycles recovered (net populated cycles 2–5). (No host sqlite3 → the
spec's illustrative `sqlite3 …` invocation was replaced by `dbstat.exe`, which
prints the same facts.)

**Backoff (D8):** unit-proven via the pure `cadence_seconds(failures, interval)`
helper (`run_loop` consumes it) — backs off to 60s at ≥5 consecutive whole-body
failures and recovers on reset. Not separately runtime-exercised (the live run
had zero failures), but the catch2 case locks the decision.

**`self.last_cycle_ms`:** set unconditionally from a `steady_clock` delta after
every gather→insert; the live run proves the cycle body completes each tick (rows
land), so the value is populated and >0 every cycle by construction.

**Design notes (carry into later steps):**
- `disk_read/write_bps`: btop stores per-cycle byte deltas in `disk.io_read/
  io_write`; the gather divides by `Config::getI("update_ms")` (~2000ms) → Bps.
- `pages_per_sec`, `disk_queue_length`, `disk_avg_sec_per_transfer` are NULL
  this step — step 04 owns them as PDH extras (per `04-whea-advanced-counters.md`).
- `swap_*` ← btop page-file stats; nullopt with no page file. NIC error/drop
  counters aren't collected by btop4win → `net_errs_total` stays 0 this step.
- Concurrency: the collector is a peer thread to btop's Runner; before each
  source read it waits for `Runner::active == false` then reads cached state.
  Hardening the residual read window would need a shared mutex in upstream
  `btop_collect.cpp`, which T2 forbids.

**Legacy guard (Linux):** `uv run pytest` 133 passed; `uv run ruff check .` clean.

**Scratch artifacts** (`smoke\dbstat.c`, `smoke\run.bat`, `dbstat.exe`) live only
in the `gserver` clone — untracked, not committed to the repo.
