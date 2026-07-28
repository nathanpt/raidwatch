# 07 — Gates engine

Status: Backlog

## Goal

Port `raidwatch/gates.py` 1:1 to `rw_gates.cpp` (T9): the sustained-duration
state machine that turns metric crossings into upgrade recommendations, with
hysteresis to prevent oscillation, state persisted to `gate_state`, transitions
logged to `gate_events`, and the layered status pill driving the TUI header.
Timing behavior is verified with catch2 on a **fake clock** (never wall time).

## Requires

- [06 — Fika module](06-fika-module.md) (snapshot now carries every metric the
  gates reference).

## Tasks

1. **Constants** in `rw_gates.hpp` (verbatim from `gates.py`):
   - `HYSTERESIS_FACTOR = 0.9` — value must drop/rise this fraction past
     threshold to clear.
   - `COOLDOWN_SECONDS = 1800` — re-alert cooldown (don't re-alert within 30 min
     of a prior trigger on the same gate; D10).
2. **`GATE_METRIC_MAP`** as C++ accessors over `RwSnapshot` — port the 6-entry
   map verbatim. Each gate id resolves to a snapshot field (or the special
   `storage.free_percent` derivation):
   - `ram_high` → `system.ram_percent`
   - `cpu_sustained` → `system.cpu_total_percent`
   - `storage_io` → `system.disk_queue_length`
   - `storage_space` → `storage.free_percent` *(special: derive from
     `system.disk_volumes[0]` as `free_bytes / total_bytes * 100`)*
   - `stability_whea` → `system.whea_count_2h`
   - `cpu_thermal` → `system.temp_cpu_celsius`
3. **Default operator map:** only `storage_space` defaults to `<` (fires when
   free space drops *below* threshold); every other gate defaults to `>`.
   Explicit per-gate `operator` in config overrides the default.
4. **Operator set** `{>, <, >=, <=, ==, !=}` — port `_check_operator` exactly.
   Invalid operator is a load-time config error (step 02 already validates).
5. **`RwGateEvaluator`** — the state machine (port
   `GateEvaluator::_evaluate_gate` 1:1). Per gate, per cycle:
   - Resolve the metric value from the snapshot (dotted-path extract; the
     `storage.free_percent` special case).
   - Load persisted state from `gate_state` (or defaults: `last_crossed = none`,
     `currently_triggered = false`, `last_triggered_ts = none`,
     `trigger_count = 0`).
   - **Crossing:** if the operator is satisfied, record `last_crossed` (a
     `steady_clock` time point) on first crossing; when
     `now − last_crossed >= duration_seconds` **and** not already triggered,
     transition to triggered: set `currently_triggered`, stamp
     `last_triggered_ts = now_ms()` (epoch ms), `trigger_count++`, and insert a
     `gate_events` row `{ts, gate_id, action='triggered', value, severity}`.
   - **Clearing (hysteresis):** when not crossing, compute
     `clear_threshold_gt = threshold * 0.9` and `clear_threshold_lt = threshold / 0.9`.
     For `>`/`>=` gates clear when `value < clear_threshold_gt`; for `<`/`<=`
     gates clear when `value > clear_threshold_lt`. Values inside the
     hysteresis band do **not** clear (prevents oscillation). If the metric
     became unavailable while triggered, clear anyway (no stuck gate). On clear:
     reset `currently_triggered=false`, `last_crossed=none`, and insert a
     `gate_events` row `action='cleared'`.
   - Persist updated `gate_state` (`gate_id` PK, `last_crossed_monotonic` REAL,
     `currently_triggered` INT, `last_triggered_ts` INT, `trigger_count` INT).
   - `all_statuses()` returns every gate (enabled + disabled) for the gates box.
6. **`compute_status_pill(stale, triggered, all)`** — port precedence exactly
   (D22): `stale` → `("critical", "Monitoring Degraded — Stale Data")`; else
     first triggered gate with `severity == "high"` → `("critical",
   "Critical: <gate_id>")`; else first triggered `severity == "medium"` →
   `("degraded", "Degraded: <gate_id>")`; else `("operational", "Operational")`.
7. Wire the evaluator into the collector (runs each cycle after the snapshot is
   shaped) and feed its result to `compute_status_pill` for the header (the pill
   renders in step 08).
8. **catch2 timing tests** in `test_rw_gates.cpp`, porting
   `tests/test_gate_timing.py` section-for-section, all on a **fake clock**
   (injectable `steady_clock` now-function; never wall time):
   - `TestOperatorCheck` — all six operators + boundaries.
   - `TestSustainedDuration` — does not trigger before duration elapses;
     triggers exactly once at the boundary; no double-trigger while held.
   - `TestHysteresisClear` — does not clear inside the 0.9 band; clears once
     past it.
   - `TestStatusPill` — stale > high > medium > operational precedence.
   - `TestDisabledGate` — disabled gates (e.g. `cpu_thermal` by default) never
     trigger.

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_gates.hpp` / `rw_gates.cpp`.
- `native/tests/test_rw_gates.cpp` — ported `test_gate_timing.py`.

**Modified:**
- `native/btop4win/src/raidwatch/rw_collector.cpp` — run the evaluator each
  cycle; feed status pill (rendering lands in step 08).
- `native/btop4win/src/raidwatch/rw_database.cpp` — `get_gate_state` /
  `upsert_gate_state` / `insert_gate_event` (the `gate_state` and `gate_events`
  tables exist from step 02).

## Definition of Done

- [ ] `HYSTERESIS_FACTOR = 0.9` and `COOLDOWN_SECONDS = 1800` present; the
      6-entry `GATE_METRIC_MAP` and the `storage_space → <` default match
      `gates.py` verbatim.
- [ ] `_check_operator` handles all six operators with the same boundary
      behavior as the Python tests.
- [ ] A config with near-zero `duration_seconds` + low thresholds fires a gate
      under scripted high load and clears it (with hysteresis) when load drops.
- [ ] Sustained-duration: no trigger before the duration elapses; exactly one
      trigger at the boundary; no double-trigger while held.
- [ ] Hysteresis: no clear inside the 0.9 band; clear once past it.
- [ ] Disabled gates never trigger.
- [ ] `gate_state` persists across a restart (state restored on reload);
      `gate_events` rows appear on trigger/clear transitions.
- [ ] `compute_status_pill` precedence is stale > high > medium > operational,
      matching `tests/test_gate_timing.py::TestStatusPill`.
- [ ] All catch2 gate tests pass on the **fake clock** (no wall-time dependency).

## Verification

On the Windows host:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: "All tests passed", incl. every ported gate-timing section.

# Scripted-load smoke (near-zero durations, low thresholds; drive CPU/RAM up):
.\x64\Release\raidwatch.exe
#   (apply load, watch a gate trigger; release load, watch it clear)
sqlite3 data\raidwatch.db "SELECT ts, gate_id, action, value FROM gate_events ORDER BY id DESC LIMIT 6;"
sqlite3 data\raidwatch.db "SELECT gate_id, currently_triggered, trigger_count FROM gate_state;"
#   Expected: 'triggered'/'cleared' rows on transitions; gate_state reflects
#             the final triggered/count state.
```

On the Linux box, confirm the legacy suite that the C++ tests are validated
against still passes:

```bash
uv run pytest tests/test_gate_timing.py -v
```

Record the rw_tests pass line, the `gate_events` query, and the `gate_state`
query in the Log.

## Log

<!-- On Done: date + the rw_tests pass line, the gate_events rows, the gate_state
     row, and a note that the fake-clock tests hold. Empty until executed. -->
