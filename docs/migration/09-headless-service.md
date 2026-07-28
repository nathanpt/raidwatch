# 09 — Headless & service mode

Status: Backlog

## Goal

Make `raidwatch.exe` run without a UI as an NSSM service: `--headless` skips all
UI init and runs collector + gates + persistence; `--health-check` exposes the
D35 staleness contract as process exit codes (no HTTP); `--export-csv` works
headless. Port the install/uninstall/watchdog PowerShell to the new binary. This
delivers T3's second runtime mode and T12's ops story. The Python service is
**not** removed yet — that is step 10 — so both run side by side during this
step's validation (on different ports/paths).

## Requires

- [08 — TUI panels & theming](08-tui-panels-theming.md).

## Tasks

1. **`rw_headless.cpp` / `rw_headless.hpp`** — a `--headless` mode that:
   - Loads config + opens the DB (steps 02–07).
   - Starts `RwCollector` + the gates evaluator with **no UI initialization** at
     all (skip btop4win's terminal/box/draw setup — just the collector loop).
   - Runs until SIGTERM/SIGINT/`SetConsoleCtrlHandler`, then shuts down cleanly
     (flush + close the DB).
2. **`--health-check`** — implements the D35 contract without HTTP (T12). Reads
   the collector's last-tick `steady_clock` time and the module states, then
   exits:
   - `0` — ok (collector ticked within `STALE_THRESHOLD_CYCLES` = 3 cycles ≈
     15s; no module in a hard-error state).
   - `1` — stale (collector hasn't ticked in >3 cycles ≈ 15s — port
     `health.py::STALE_THRESHOLD_CYCLES` and `compute_status` exactly).
   - `2` — error (DB unreachable, or a module in a persistent error/backoff
     state past the D8 threshold).
   Print a one-line human summary to stdout; the exit code is what the
   watchdog consumes. This replaces the legacy HTTP `/health` probe.
3. **`--export-csv <path>`** — works headless (same output as the TUI key from
   step 08).
4. **Port the ops scripts** to the new binary (model on the existing
   `scripts/install_service.ps1`, `scripts/uninstall_service.ps1`, and the
   watchdog Scheduled Task — all PowerShell pure-ASCII with UTF-8 BOM per repo
   convention):
   - `scripts/install_rw_service.ps1` — vcpkg-free; creates the NSSM service for
     `raidwatch.exe --headless` running as SYSTEM; sets
     `AppStopMethodSkip=1` (no console = no Ctrl+C; matches the legacy
     convention); opens the firewall only if a remote UI is ever needed (TUI is
     local, so usually none); reconfigures in-place on reinstall (never
     `sc.exe delete` mid-install — causes zombie services); creates the watchdog
     Scheduled Task.
   - `scripts/uninstall_rw_service.ps1` — stops + removes the service, the
     watchdog Task, and the firewall rule.
   - **Watchdog Scheduled Task** — replaces the legacy "curl `/health`" task:
     runs `raidwatch.exe --health-check` on a timer and restarts the NSSM
     service when the exit code is `1` (stale) or `2` (error).
5. **NSSM restart-on-exit (D27 analog)** — configure NSSM to restart the
   service on unexpected exit. The collector loop body is already fully
   try/catch-wrapped (step 03), so the loop cannot die on a throw; NSSM covers
   the case where the whole process exits (crash, OOM, OS kill).

## Files

**Created:**
- `native/btop4win/src/raidwatch/rw_headless.hpp` / `rw_headless.cpp`.
- `scripts/install_rw_service.ps1`, `scripts/uninstall_rw_service.ps1`,
  plus the watchdog Scheduled Task definition (authored by the install script).

**Modified:**
- CLI entry — dispatch `--headless`, `--health-check`, `--export-csv`.

## Definition of Done

- [ ] `raidwatch.exe --headless` starts the collector + gates with zero UI init
      and collects with the TUI closed; rows land in `metrics_history` every
      ~5s.
- [ ] Gates fire and clear in headless mode exactly as in TUI mode (shared DB).
- [ ] `--health-check` exits `0` when fresh, `1` when the collector is stale
      (>3 cycles ≈ 15s), `2` on DB/module hard error.
- [ ] `--export-csv <path>` produces the correct CSV headless.
- [ ] `scripts/install_rw_service.ps1` installs the NSSM service running
      `raidwatch.exe --headless` as SYSTEM; reinstall reconfigures in-place.
- [ ] Killing the `raidwatch.exe` process → NSSM restarts it automatically.
- [ ] Stalling the collector (e.g., simulating a hang) → the watchdog Task
      detects the `1`/`2` exit and restarts the service.
- [ ] `scripts/uninstall_rw_service.ps1` cleanly removes the service, Task, and
      firewall rule.

## Verification

On the Windows host (Python legacy service stopped or on a different path so the
two do not collide on `data/raidwatch.db`):

```powershell
msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64

# Install + run headless:
.\scripts\install_rw_service.ps1
Start-Sleep -Seconds 20
sqlite3 data\raidwatch.db "SELECT count(*) FROM metrics_history;"
#   Expected: rows accumulating (~4 per 20s).

# Health check while healthy:
.\x64\Release\raidwatch.exe --health-check; "exit=$LASTEXITCODE"
#   Expected: exit=0.

# Process-crash recovery:
Stop-Process -Name raidwatch -Force
Start-Sleep -Seconds 10
Get-Service raidwatch | Format-List Name, Status
#   Expected: Status=Running (NSSM restarted it).

# Stalled-collector recovery (simulate a hang; watchdog fires):
#   Expected: the watchdog Task restarts the service after the health check
#             returns exit 1/2.

# Clean removal:
.\scripts\uninstall_rw_service.ps1
Get-Service raidwatch -ErrorAction SilentlyContinue   # Expected: not found.
```

Record the row count, the `--health-check` exit codes, the post-kill service
status, the watchdog-triggered restart, and the clean uninstall in the Log.

## Log

<!-- On Done: date + row count, health-check exit codes, post-kill service
     status, watchdog restart confirmation, and the uninstall result. Empty
     until executed. -->
