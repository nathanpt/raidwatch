# 02 — Config + storage foundation

Status: Backlog

## Goal

Lay the two foundational layers every later step depends on: a YAML config
loader (`rw_config.cpp`) for the RaidWatch settings, and a SQLite persistence
layer (`rw_database.cpp`) that opens, migrates, inserts into, and
downsample-queries the v1 schema. Also stand up the catch2 test project so
every subsequent step has a place to land ported tests. No collector yet — this
step delivers libraries + tests only.

## Requires

- [01 — Fork, build & baseline btop4win](01-fork-build-baseline.md) (build
  toolchain + `native/btop4win/` baseline present).

## Tasks

1. **Vendor third-party single headers** into `native/third_party/`:
   - rapidyaml single header as `ryml_all.hpp` (T4). Build config note: if ryml
     fights the vcxproj (PCH or `/W4` noise), fall back to yaml-cpp via vcpkg
     and record the swap in the Log — the config schema and T4 semantics are
     unchanged (see [Assumptions](../README.md)).
   - SQLite official amalgamation as `sqlite3.c` + `sqlite3.h` (T5).
   - catch2 **v2.13.10** single header as `catch.hpp` (T11).
2. Add the three headers to the vcxproj as existing items (compile `sqlite3.c`
   with `/FS` and disable `/W4` on it — it is third-party C).
3. **`rw_config.cpp` / `rw_config.hpp`** — parse `config.yaml` per T4:
   - Schema is the existing `config.yaml.example` with the `server:` and
     `auth:` sections dropped (T4). The HTTP-only fields `server.bind_host` and
     `server.port` are deleted outright. The Fika-relevant fields that lived
     under `server:` (`spt_path`, `log_paths`, `headless_path`,
     `raid_udp_port`, `risky_mod_names`) relocate to a new top-level `fika:`
     section (consumed by step 06) — they are not HTTP surface, so deleting
     them would orphan the Fika module. Surviving sections: `processes:`,
     `collection:`, `temps:`, `gates:`, `fika:` (and the values they carry
     today: `processes.{spt_server_process_name, headless_process_name,
     headless_cmdline_pattern}`, `collection.{interval_seconds,
     history_retention_hours, whea_poll_seconds, top_others_poll_seconds}`,
     `temps.{lhm_dll_path, cpu_sensor_name, tctl_offset}`, the full `gates:`
     map with `{enabled, threshold, operator, duration_seconds, severity,
     recommendation, metric}` per gate, and `fika.{spt_path, log_paths,
     headless_path, raid_udp_port, risky_mod_names}`).
   - **First-run auto-generation (D23):** if `data/config.yaml` is missing and
     `config.yaml.example` exists, copy the example to `data/config.yaml`;
     otherwise write the built-in defaults.
   - **Unknown keys warn-and-ignore** (log a warning, do not fail). This
     replaces pydantic's `extra="forbid"` — a hard failure is wrong for a local
     TUI whose config ships in-repo.
   - Validate gate `operator` ∈ `{>, <, >=, <=, ==, !=}`,
     `severity` ∈ `{low, medium, high}`, `duration_seconds >= 0`. Invalid values
     here *are* hard errors (real config mistakes, D4), surfaced as a load
     exception.
4. **`rw_database.cpp` / `rw_database.hpp`** — port
   `raidwatch/database.py`:
   - `open()` the connection to `data/raidwatch.db`.
   - Run the **exact v1 schema** via idempotent `IF NOT EXISTS` DDL, then
     `PRAGMA user_version`. Port these statements verbatim from
     `raidwatch/database.py::_MIGRATIONS`:
     ```sql
     CREATE TABLE IF NOT EXISTS metrics_history (
         ts                          INTEGER PRIMARY KEY,
         cpu_total_percent           REAL,
         ram_percent                 REAL,
         ram_used_bytes              INTEGER,
         swap_percent                REAL,
         pages_per_sec               REAL,
         disk_read_bps               INTEGER,
         disk_write_bps              INTEGER,
         disk_queue_length           REAL,
         disk_avg_sec_per_transfer   REAL,
         disk_game_free_bytes        INTEGER,
         net_sent_bps                INTEGER,
         net_recv_bps                INTEGER,
         net_errs_total              INTEGER,
         temp_cpu_celsius            REAL,
         whea_count_2h               INTEGER,
         fika_spt_cpu_percent        REAL,
         fika_spt_rss_bytes          INTEGER,
         fika_headless_count         INTEGER,
         fika_headless_cpu_total     REAL,
         fika_headless_rss_total     INTEGER
     );
     CREATE TABLE IF NOT EXISTS fika_events (
         id          INTEGER PRIMARY KEY AUTOINCREMENT,
         ts          INTEGER NOT NULL,
         source      TEXT NOT NULL,
         severity    TEXT NOT NULL,
         message     TEXT NOT NULL,
         raw_line    TEXT
     );
     CREATE TABLE IF NOT EXISTS gate_events (
         id          INTEGER PRIMARY KEY AUTOINCREMENT,
         ts          INTEGER NOT NULL,
         gate_id     TEXT NOT NULL,
         action      TEXT NOT NULL,            -- 'triggered' | 'cleared'
         value       REAL,
         severity    TEXT
     );
     CREATE TABLE IF NOT EXISTS whea_events (
         record_number   INTEGER PRIMARY KEY,  -- dedup across polls (D16)
         ts_generated    INTEGER NOT NULL,
         event_id        INTEGER,
         message         TEXT
     );
     CREATE TABLE IF NOT EXISTS gate_state (
         gate_id                TEXT PRIMARY KEY,
         last_crossed_monotonic REAL,          -- steady_clock when first crossed
         currently_triggered    INTEGER DEFAULT 0,
         last_triggered_ts      INTEGER,
         trigger_count          INTEGER DEFAULT 0
     );
     CREATE INDEX IF NOT EXISTS idx_metrics_ts     ON metrics_history(ts);
     CREATE INDEX IF NOT EXISTS idx_fika_events_ts  ON fika_events(ts);
     CREATE INDEX IF NOT EXISTS idx_gate_events_ts  ON gate_events(ts);
     CREATE INDEX IF NOT EXISTS idx_whea_ts         ON whea_events(ts_generated);
     ```
     Then `PRAGMA user_version = 1` (port the `SCHEMA_VERSION` / migration
     pattern — future column adds bump the version and append a migration).
   - **Single connection serialized on the collector thread** (D21 analog): one
     `sqlite3*`, all access from the collector thread, `WAL` off (single
     writer). Wrap opens with `sqlite3_busy_timeout`.
   - `now_ms()` returns `int64_t` UTC epoch ms (D19).
   - `insert_metrics(snapshot)` — the wide-row insert (one row per cycle,
     `ts` is the PK).
   - `query_history(minutes, metrics?)` — on-the-fly downsampling (D15). Port
     the bucket selector exactly:
     `minutes >= 720 → 120000ms`, `>= 360 → 30000ms`, else `5000ms`. Build the
     `GROUP BY (ts / :bucket) * :bucket` query with the **exact aggregate map**
     from `database.py` (`MAX` for peak metrics, `AVG` for rates, `MIN` for
     `disk_game_free_bytes`). Column names in the `metrics` filter must be
     validated against the allow-list (SQL-injection guard).
5. **`rw_tests` project** — add a new vcxproj under `native/tests/` to the
   solution, with `#define CATCH_CONFIG_MAIN` in one TU and `catch.hpp` as the
   test framework (T11). Seed it with:
   - `test_rw_config.cpp` — auto-generate from example; warn-and-ignore unknown
     keys; hard-fail on a bad gate operator; `tctl_offset` default `20.0`.
   - `test_rw_database.cpp` — fresh DB has `user_version == 1`; all five tables
     + four indexes exist; insert→query roundtrip; downsampling bucket sizes
     correct for 15m / 6h / 24h windows.

## Files

**Created:**
- `native/third_party/ryml_all.hpp`, `sqlite3.c`, `sqlite3.h`, `catch.hpp`.
- `native/btop4win/src/raidwatch/rw_config.hpp` / `rw_config.cpp`.
- `native/btop4win/src/raidwatch/rw_database.hpp` / `rw_database.cpp`.
- `native/tests/rw_tests.vcxproj` + `test_rw_config.cpp` + `test_rw_database.cpp`.
- `data/.gitkeep` (so the runtime data dir exists in a fresh clone).

**Modified:**
- `native/btop4win/btop4win.vcxproj` / `.sln` — add the new RaidWatch TUs and
  the `rw_tests` project (T2: new TUs only, upstream files otherwise untouched).
- `config.yaml.example` — drop `server:` (`bind_host`/`port`) and `auth:`;
  relocate the Fika fields to a new `fika:` section; keep `processes`,
  `collection`, `temps`, `gates`.

## Definition of Done

- [ ] `native/third_party/` contains `ryml_all.hpp`, `sqlite3.c`, `sqlite3.h`,
      `catch.hpp` (catch is v2.13.10).
- [ ] `rw_config` auto-generates `data/config.yaml` from
      `config.yaml.example` on first run; parses the surviving schema; warns
      and ignores an unknown key; throws on an invalid gate operator.
- [ ] `config.yaml.example` no longer contains `server:` or `auth:`.
- [ ] `rw_database` opens `data/raidwatch.db`, runs the v1 DDL, and
      `PRAGMA user_version` returns `1` on a fresh DB.
- [ ] All five tables (`metrics_history`, `fika_events`, `gate_events`,
      `whea_events`, `gate_state`) and all four indexes exist on a fresh DB.
- [ ] An insert into `metrics_history` followed by `query_history(60)` returns
      the row with correct aggregates (no double-count, bucket = 5000ms for a
      60-minute window).
- [ ] `rw_tests` builds and all config + database tests pass.
- [ ] `uv run pytest` + `uv run ruff check .` still green/clean on the Linux
      box (legacy untouched).

## Verification

On the Windows host:

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: "All tests passed (N assertions in M test cases)".

# Fresh-DB roundtrip:
del data\raidwatch.db 2>$null; .\rw_db_smoke.exe
#   Expected: user_version=1; tables present; insert+downsample query returns
#             the inserted row with the correct aggregate.
```

On the Linux box (legacy guard):

```bash
uv run pytest && uv run ruff check .
#   Expected: all green, 0 lint errors.
```

## Log

<!-- On Done: date + the rw_tests pass line, the PRAGMA user_version=1 output,
     and the roundtrip query result. Empty until executed. -->
