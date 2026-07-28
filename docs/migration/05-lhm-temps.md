# 05 — LHM temperatures

Status: Backlog

## Goal

Read CPU package temperature on the Ryzen 5 5500 (Zen3) using btop4win's
`LHM_Enabled` build path and the LHM-CppExport `CPPdll.dll`, apply the
Tctl offset, persist `temp_cpu_celsius` to `metrics_history`, and surface it in
the cpu box. This unblocks the `cpu_thermal` gate (armed in step 07). Per T8,
this uses the **C++ export** path — not the legacy pythonnet path — and ships a
version-matched LHM DLL set.

## Requires

- [04 — WHEA + advanced PerfMon counters](04-whea-advanced-counters.md).

## Tasks

1. **Enable the `LHM_Enabled` build configuration** in the vcxproj (the
   upstream btop4win switch that pulls in the LibreHardwareMonitor code path).
   Build with it on for the rest of the migration.
2. **Obtain/build LHM-CppExport `CPPdll.dll`** (source:
   github.com/aristocratos/LHM-CppExport) plus the **matched LHM DLL set**
   (btop4win v1.0.5 ships LHM **0.9.4** — T8 version-skew rule). Vendor them
   under `native/btop4win/` (or the run directory) wherever btop4win's
   `LHM_Enabled` path expects them.
3. **Port the fetch script** to `scripts/fetch_lhm_export.ps1`, modeled on the
   existing `scripts/fetch_lhm.ps1`: pin the exact LHM-CppExport + LHM versions
   by URL, **checksum-verify** every downloaded DLL (SHA-256), and abort on any
   mismatch. Document the pinned versions in the script header.
4. **Read CPU package temp** in `rw_collector.cpp` via the CPPdll export:
   - Select the CPU package sensor (configurable via
     `temps.cpu_sensor_name`; if blank, fall back to the first CPU-package /
     temperature sensor CPPdll enumerates).
   - Apply `temps.tctl_offset` (default `0.0`) by **subtracting** it from the
     raw reading — Zen3 (Ryzen 5 5500) reports true die temperature, so the
     default offset is 0; a non-zero offset is only needed for chips whose
     package sensor reports an offset value (e.g. legacy Zen1). This matches the
     legacy `modules/temps.py` behavior and the `config.yaml.example` comment
     ("Zen3 (Ryzen 5 5500) — confirm via probe").
   - Store the corrected value in `snapshot.system.temp_cpu_celsius` (D8:
     wrap the whole read in try/catch; on failure leave `temp_cpu_celsius`
     `std::nullopt` and bump the temps error counter).
5. Persist `temp_cpu_celsius` into the `metrics_history.temp_cpu_celsius`
   column (the row mapping from step 03 now fills this column instead of
   leaving it NULL).
6. Surface the temp in the cpu box now (a single numeric read is enough for
   this step; the full TUI treatment lands in step 08).

## Files

**Created:**
- `scripts/fetch_lhm_export.ps1` — pinned + checksum-verified DLL fetcher.
- `native/btop4win/` (or run dir) — `CPPdll.dll` + the matched LHM 0.9.4 DLL
  set + their LICENSE/attribution.

**Modified:**
- `native/btop4win/btop4win.vcxproj` — enable `LHM_Enabled`.
- `native/btop4win/src/raidwatch/rw_collector.cpp` — read + offset + persist
  `temp_cpu_celsius`; D8-isolated.

## Definition of Done

- [ ] `LHM_Enabled` is on in the vcxproj and the project builds with it.
- [ ] `CPPdll.dll` + the version-matched LHM 0.9.4 DLL set are vendored;
      `scripts/fetch_lhm_export.ps1` re-fetches them with SHA-256 verification.
- [ ] **No pythonnet LHM 0.9.6 DLLs** are used on the C++ path (T8
      version-skew rule upheld).
- [ ] On the Ryzen 5 5500, `snapshot.system.temp_cpu_celsius` is populated each cycle
      with the offset-corrected value.
- [ ] `metrics_history.temp_cpu_celsius` is non-NULL after a run.
- [ ] The offset math matches the LibreHardwareMonitor GUI's reported die
      temperature (manual cross-check on the host).
- [ ] The cpu box shows the current temperature.
- [ ] **Fallback documented:** if CPPdll cannot be built/obtained, temps
      degrade to NULL (D8) and `cpu_thermal` stays disarmed — recorded in the
      Log, **not** a blocker for steps 06–10 (per T8).

## Verification

On the Windows host:

```powershell
# Re-fetch is reproducible + verified:
.\scripts\fetch_lhm_export.ps1
#   Expected: every DLL reports "OK (sha256 match)"; non-zero exit on mismatch.

msbuild native\btop4win\btop4win.sln /p:Configuration=Release /p:Platform=x64
.\x64\Release\raidwatch.exe
#   (run ~20s, then quit)
sqlite3 data\raidwatch.db "SELECT ts, temp_cpu_celsius FROM metrics_history ORDER BY ts DESC LIMIT 3;"
#   Expected: temp_cpu_celsius populated, values in a plausible idle range
#             (e.g. ~30–55 °C on a Zen3 idle box), offset-corrected.

# Manual cross-check: open the LibreHardwareMonitor GUI, note the CPU package
# temp, compare to the raidwatch value at the same moment — should agree within
# the offset.
```

Record the column query, a sample value, and the GUI cross-check in the Log.
If CPPdll could not be obtained, record the fallback state instead and confirm
`temp_cpu_celsius` is NULL and `cpu_thermal` is disarmed.

## Log

<!-- On Done: date + the fetch_lhm_export OK lines, the temp column query, a
     sample value, and the LHM-GUI cross-check (or the documented fallback).
     Empty until executed. -->
