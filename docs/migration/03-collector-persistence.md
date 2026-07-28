# 03 — Collector & persistence

Status: Backlog

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

- [ ] `RwSnapshot` mirrors every field of `MetricsSnapshot` (system + fika stub
      + process + self); nullable scalars are `std::optional`.
- [ ] `RwCollector` runs a 5s loop with the whole body try/catch-wrapped and
      per-source isolation; a thrown source blanks only its own fields and
      bumps its own error counter.
- [ ] After 5 consecutive whole-body failures, the loop backs off to ~60s and
      recovers (resets to 5s) on the next clean cycle.
- [ ] With the TUI running on the 1800X, a new `metrics_history` row lands
      roughly every 5s with the cpu/mem/net/disk scalar columns populated.
- [ ] Simulated module failure (e.g., point a source at an unreachable counter)
      blanks only that source's columns; other columns and subsequent cycles
      are unaffected.
- [ ] `self.last_cycle_ms` is populated and non-zero every cycle.
- [ ] catch2 `test_rw_collector` asserts the snapshot→row mapping table above
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

<!-- On Done: date + the rw_tests pass line, the row-count/timestamp query
     output, a sample row, and the isolation observation. Empty until executed.
-->
