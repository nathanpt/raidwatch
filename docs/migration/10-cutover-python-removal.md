# 10 — Cutover — Python removal

Status: Backlog

## Goal

Final step: retire the legacy Python web stack entirely, port any surviving
scripts to C++ equivalents where still needed, rewrite the top-level docs for
the C++ project, fold still-relevant `.docs/` validation notes into committed
docs, and run the 48h soak that is the migration's final gate. After this step
the repo builds and runs with **zero Python present**, and the board shows every
step Done. This is T13.

## Requires

- [09 — Headless & service mode](09-headless-service.md).

## Tasks

1. **Decommission the legacy Python service** before deleting its source:
   - Run the legacy uninstaller (`.\install.ps1 -Uninstall` and/or
     `scripts/uninstall_service.ps1`) to remove the Python NSSM service, its
     watchdog Scheduled Task, and its firewall rule.
   - Confirm the C++ `raidwatch.exe --headless` service (from step 09) is the
     only RaidWatch service running.
2. **Delete the entire Python web stack** (T13):
   - `raidwatch/` package (all `.py`).
   - `tests/` (the Python pytest suite).
   - `templates/` (Jinja2).
   - `static/` (including `vendor/` Chart.js + the compiled Tailwind).
   - `pyproject.toml`, `uv.lock`.
   - Root `install.ps1` (the Python one-command installer — superseded by
     `scripts/install_rw_service.ps1`).
   - `build_tailwind.py` (no web UI to compile Tailwind for).
3. **Port surviving scripts** to C++ CLI equivalents where still needed:
   - `collect_once.py` → `raidwatch.exe --collect-once` (one-shot metrics dump
     to stdout; useful for ad-hoc checks).
   - `smoke_test_m2.py` / `smoke_test_m3.py` → retired; the 48h soak (task 6)
     and the catch2 suite supersede them.
   - `stress_test_sim.py` → a C++ gate-baselining helper (`--stress-gates`) only
     if post-baselining still needs scripted load; otherwise retire.
   - `probe_temps.py`, `probe_whea.py`, `discover_processes.py` → already have
     C++ equivalents from steps 04–06 (`--probe-whea`, etc.); delete the Python
     versions.
   - `fetch_lhm.ps1` (pythonnet LHM fetcher) → superseded by
     `scripts/fetch_lhm_export.ps1` (step 05); delete the old one.
   - Keep: `scripts/install_rw_service.ps1`, `scripts/uninstall_rw_service.ps1`,
     `scripts/fetch_lhm_export.ps1`, `nssm.exe` (still used by the C++ service),
     and any C++-side helper scripts.
4. **Rewrite top-level docs** for the C++ project:
   - `README.md` — new build/run instructions (MSBuild, the Windows-only
     prerequisite set from T2), both runtime modes (T3), the config layout
     (`config.yaml` + btop `.conf`/`.theme`), and the service install.
   - `AGENTS.md` — replace the Python/FastAPI project briefing with the C++/TUI
     briefing: vendored btop4win base, the `native/` layout, the module map, the
     WIP=1 migration board pointer, and the D-decision carryovers that still
     apply.
5. **Fold `.docs/` validation notes into committed docs.** `.docs/DESIGN.md` and
   `.docs/DECISIONS.md` are gitignored (local only). Extract the
   still-load-bearing decisions (the `DNN` references that carry into the C++
   port — D8 isolation, D15 downsampling, D19 time discipline, D21 single
   connection, D22 pill, D27 self-healing, D35 health) into a committed
   `docs/decisions.md` (or extend `docs/migration/README.md`'s decision table).
   The C++ project must not reference gitignored `.docs/`.
6. **48h soak** — the final gate (T13). Run the C++ `--headless` service for ≥48
   continuous hours on the Ryzen 5 5500 with the real Fika workload, then capture:
   - **DB size stability** under pruning (`history_retention_hours` keeps
     `metrics_history` bounded; confirm `data/raidwatch.db` plateaus, does not
     grow unbounded).
   - **Memory + handle stability** (working set and HANDLE count of
     `raidwatch.exe` flat over 48h — no leak).
   - **No log spam** (the collector's once-per-failure logging holds; no
     repeated stack traces flooding the log).
   - **Gate behavior under real load** (gates fire/clear correctly during
     actual raids; lower conservative thresholds to real headroom per the
     baselining note — D10).
   - Write the soak report into this file's `## Log`.
7. **Flip the board:** mark every step `Done` in `docs/migration/README.md`'s
   status table (each step file already carries its own `Status: Done` + Log
   from its execution).

## Files

**Deleted:**
- `raidwatch/` (package), `tests/`, `templates/`, `static/`.
- `pyproject.toml`, `uv.lock`, root `install.ps1`, `scripts/build_tailwind.py`.
- Python scripts retired/port-superseded: `scripts/collect_once.py`,
  `scripts/smoke_test_m2.py`, `scripts/smoke_test_m3.py`,
  `scripts/stress_test_sim.py`, `scripts/probe_temps.py`,
  `scripts/probe_whea.py`, `scripts/discover_processes.py`,
  `scripts/fetch_lhm.ps1`.

**Created:**
- `docs/decisions.md` (committed carryover of the still-load-bearing `DNN`
  decisions).
- C++ CLI equivalents for any surviving helper (`--collect-once`, etc.).

**Modified (rewritten):**
- `README.md`, `AGENTS.md` — C++ project briefing.
- `docs/migration/README.md` — status table all `Done`.

## Definition of Done

- [ ] The legacy Python NSSM service, its watchdog Task, and its firewall rule
      are removed.
- [ ] Every file/path in T13 is deleted; `find . -name '*.py'` (excluding
      `native/btop4win/` upstream) returns nothing RaidWatch-authored.
- [ ] The repo builds with **zero Python present**: `msbuild` of
      `native/btop4win/btop4win.sln` succeeds and `rw_tests` passes with no
      `pyproject.toml`/`uv.lock`/`raidwatch/` in the tree.
- [ ] Surviving scripts are ported or consciously retired (each recorded in the
      Log).
- [ ] `README.md` and `AGENTS.md` describe the C++/TUI project (build, both
      modes, config, service install); no stale Python/FastAPI instructions.
- [ ] Still-load-bearing `DNN` decisions are committed under `docs/`; the C++
      project references no gitignored `.docs/` path.
- [ ] **48h soak report** in this file's Log: DB size stable under pruning,
      memory + handles flat, no log spam, gates behave under real load.
- [ ] The board's status table shows all 10 steps `Done` (and each step file
      carries `Status: Done` + its Log).

## Verification

On the Windows host:

```powershell
# Zero Python present (excluding vendored upstream, which has none anyway):
Get-ChildItem -Recurse -Filter *.py -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notlike '*\native\btop4win\*' }
#   Expected: no output (nothing RaidWatch-authored).

# Clean build + tests with no Python toolchain:
Remove-Item -Recurse -Force .venn -ErrorAction SilentlyContinue
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\native\tests\x64\Release\rw_tests.exe
#   Expected: build succeeds; "All tests passed".

# Service still installs + runs headless after cutover:
.\scripts\install_rw_service.ps1
.\x64\Release\raidwatch.exe --health-check; "exit=$LASTEXITCODE"   # Expected: exit=0.

# Soak instrumentation (run periodically across the 48h):
sqlite3 data\raidwatch.db "SELECT count(*), min(ts), max(ts) FROM metrics_history;"
Get-Process raidwatch | Select-Object WorkingSet64, HandleCount
Get-Content <log_path> -Tail 50    # Expected: no repeated stack traces.
```

Record the `*.py` search (empty), the build + rw_tests result, the
`--health-check` exit, and the full 48h soak report in the Log.

## Log

<!-- On Done: date + the empty *.py search, the build/tests pass line, the
     health-check exit, and the 48h soak report (DB size curve, memory/handle
     trend, log-spam check, gate baselining outcome). Then flip the board to
     all-Done. Empty until executed. -->
