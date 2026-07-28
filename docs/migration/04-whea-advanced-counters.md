# 04 — WHEA + advanced PerfMon counters

Status: Backlog

## Goal

Add the two Windows-only metric streams btop4win does not provide: WHEA
hardware-error events (ported to wevtapi), and the PerfMon counters btop4win's
`btop_collect.cpp` does not already expose (pages/sec, disk queue length, avg
sec/transfer). These populate `system.whea_count_2h`, `system.pages_per_sec`,
`system.disk_queue_length`, and `system.disk_avg_sec_per_transfer` in the
snapshot and persist into `metrics_history`; new WHEA events also land in the
`whea_events` table (deduped by record number).

## Requires

- [03 — Collector & persistence](03-collector-persistence.md).

## Tasks

1. **Audit `btop_collect.cpp`** for what it already exposes before writing any
   PDH code (T6 / [Assumption](../README.md)):
   - If btop4win already exposes **disk queue length** and/or **avg
     sec/transfer**, reuse those values and skip the duplicate PDH counter for
     them — record the reuse in the Log.
   - `pages/sec` is almost certainly not collected by btop4win (it tracks
     memory usage, not paging rate); plan to add it via PDH unless the audit
     shows otherwise.
2. **`rw_whea.cpp` / `rw_whea.hpp`** — port
   `raidwatch/modules/system.py::_query_whea_events` to wevtapi (T7):
   - Query the **System** log with an XPath filter on provider
     `Microsoft-Windows-WHEA-Logger`, restricted to the **2-hour** window.
   - Use `EvtQuery` + `EvtNext` + `EvtRender` (XML render) to enumerate events.
   - Parse each event to `{record_number, ts_generated (epoch ms),
     event_id (low 16 bits, i.e. `& 0xFFFF`), message}`.
   - **Dedupe by `record_number`** (it is the `whea_events` PK — insert with
     `INSERT OR IGNORE`).
   - Return the count of events inside the 2h window (`whea_count_2h`) plus the
     new event rows for persistence.
3. **Cached count between polls (D16 analog):** WHEA is expensive, so it is
   polled on its own ~60s timer (`config.collection.whea_poll_seconds`,
   default 60). Between polls, the last count is cached in `RwCollector` and
   surfaced every cycle so the value is never `NULL` 11/12 cycles. Track the
   last-poll `steady_clock` time; only re-query when the interval has elapsed.
4. **PDH extras in `rw_collector.cpp`** — port
   `raidwatch/modules/system.py::_gather_win32_perfmon` semantics:
   - Counters (only those btop4win does NOT already provide, per the audit):
     `\PhysicalDisk(_Total)\Current Disk Queue Length`,
     `\PhysicalDisk(_Total)\Avg. Disk sec/Transfer`,
     `\Memory\Pages/sec`.
   - **Rate/average counters require two samples** (`PdhCollectQueryData`,
     brief wait, sample again) before `PdhGetFormattedCounterValue` returns a
     usable value — single-sample reads raise
     `PDH_CALC_NEGATIVE_DENOMINATOR`. Maintain a persistent PDH query across
     cycles so the two-sample requirement is satisfied naturally (each cycle's
     read uses the previous cycle's sample as the first of the pair).
   - Each counter read is **D8-isolated**: a failing counter blanks only its
     own field.
5. Add a `--probe-whea` CLI flag that runs the WHEA query once, prints the
   parsed events to stdout, and exits — for first-run validation on the host.
6. Wire WHEA + the PDH extras into the collector so `metrics_history` columns
   `whea_count_2h`, `pages_per_sec`, `disk_queue_length`,
   `disk_avg_sec_per_transfer` populate each cycle.

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_whea.hpp` / `rw_whea.cpp`.
- `native/tests/test_rw_whea.cpp` — window + dedupe logic (use a fake event
  list, no live event log).

**Modified:**
- `native/btop4win/src/raidwatch/rw_collector.cpp` — call into `rw_whea` on the
  60s timer; add the PDH query for the counters btop4win lacks; persist new
  events + columns.
- CLI entry — add `--probe-whea`.

## Definition of Done

- [ ] `rw_whea` queries the System log for `Microsoft-Windows-WHEA-Logger`
      within a 2h window via wevtapi; parses `record_number`, `ts_generated`
      (ms), `event_id` (low 16 bits), `message`.
- [ ] WHEA is polled on the ~60s timer; the cached `whea_count_2h` is surfaced
      every 5s cycle (never `NULL` between polls after the first poll).
- [ ] New WHEA events insert into `whea_events` deduped by `record_number`
      (`INSERT OR IGNORE`).
- [ ] `pages_per_sec` populates every cycle via the persistent two-sample PDH
      query; `disk_queue_length` and `disk_avg_sec_per_transfer` populate
      either from btop4win (if reused) or from PDH — recorded in the Log.
- [ ] `--probe-whea` prints parsed events and exits.
- [ ] On a healthy 1800X, `whea_count_2h == 0` and the PerfMon columns are
      populated (0 / small values are acceptable — the point is they are no
      longer `NULL`).
- [ ] Ported WHEA window/dedupe tests are green in `rw_tests`.
- [ ] One D8-isolated counter failure blanks only that counter's column.

## Verification

On the Windows host:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: "All tests passed" (incl. the new whea tests).

.\x64\Release\raidwatch.exe --probe-whea
#   Expected: a list of WHEA events (likely empty on a healthy box) OR a clean
#   "0 events in last 2h" line.

# Live persistence — run ~70s (one full WHEA poll interval), inspect:
.\x64\Release\raidwatch.exe
#   (quit after ~70s)
sqlite3 data\raidwatch.db "SELECT whea_count_2h, pages_per_sec, disk_queue_length FROM metrics_history ORDER BY ts DESC LIMIT 3;"
#   Expected: whea_count_2h >= 0 (not NULL); pages_per_sec and disk_queue_length
#             populated (not NULL).
```

Record the `--probe-whea` output, the column query, and the btop_collect.cpp
reuse decision in the Log.

## Log

<!-- On Done: date + probe-whea output, the column query, and a one-line note on
     which PerfMon counters were reused from btop4win vs added via PDH. Empty
     until executed. -->
