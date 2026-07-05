# Vendored LibreHardwareMonitor (LHM)

These DLLs provide CPU temperature sensing via the LibreHardwareMonitor .NET
library, loaded headlessly through `pythonnet` by `raidwatch/modules/temps.py`
(D9 / D30 / D31).

## Source

| Field | Value |
|-------|-------|
| **Upstream** | [LibreHardwareMonitor/LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) |
| **Release** | `v0.9.6` (2026-02-14) |
| **Asset** | `LibreHardwareMonitor.zip` (the **.NET Framework 4.x** build) |
| **Asset SHA-256** | `086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001` |
| **Download URL** | https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/v0.9.6/LibreHardwareMonitor.zip |
| **License** | Mozilla Public License 2.0 — see `LICENSE.txt` (D30 attribution) |

## Why the .NET Framework build (not .NET 10)

RaidWatch loads LHM through `pythonnet`'s `clr.AddReference(path)`, whose default
Windows runtime is the **.NET Framework** CLR. .NET Framework 4.8 is pre-installed
on Windows 10/11 (including Win11 IoT LTSC), so temps work with **no extra runtime
install**. The `.NET 10` asset would require installing the .NET 10 runtime *and*
shipping a `pythonnet.runtimeconfig.json` — strictly more fragile for a
set-and-forget service. Use the `LibreHardwareMonitor.zip` asset only.

## What is vendored

All root-level managed assemblies from the release zip that `LibreHardwareMonitorLib.dll`
can transitively reference. Deliberately **excluded**:

- `LibreHardwareMonitor.exe` + `.config` — the GUI app (we use only the library).
- `*.pdb` / `*.xml` — debug symbols / XML docs (not needed at runtime).
- `OxyPlot*.dll`, `Aga.Controls.dll` — chart/tree UI controls referenced only by the
  GUI app, never by the headless library.
- Locale satellite dirs (`de/`, `es/`, …) — `Microsoft.Win32.TaskScheduler` falls back
  to the invariant culture; sensor reading is unaffected.

## Upgrading / re-fetching

Run `scripts/fetch_lhm.ps1` (Windows) to re-download the pinned release, verify the
expected SHA-256, and extract the same DLL subset into this directory. Bump the pinned
version + checksums in the script (and this README) when upgrading.

## Security note (D31)

LHM reads hardware sensors via a **ring-0 kernel driver** (PawnIO in 0.9.6; WinRing0
in older releases). The driver is embedded inside the vendored DLLs and extracted at
`Computer.Open()`, loaded only when the service runs as **SYSTEM**. The DLLs here come
**only from the official LHM GitHub release** (SHA-256 verified above) per D31 — never
re-download from third-party mirrors. The risk (LPE driver in a SYSTEM-privileged,
network-reachable process) is accepted and documented; temps is the sole feature that
forces SYSTEM privilege.
