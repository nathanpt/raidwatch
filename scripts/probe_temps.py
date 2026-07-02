#!/usr/bin/env python3
"""Probe LHM sensors on the host to validate CPU temp identity + Tctl offset (D9).

Enumerates all CPU temperature sensors via LibreHardwareMonitor, dumps their
names/values/identifiers, and suggests which one to configure.

Run this on the 1800X host BEFORE enabling the cpu_thermal gate (D9):
    python scripts/probe_temps.py

Then fill ``temps.cpu_sensor_name`` in config.yaml with the correct sensor name,
confirm the Tctl offset (Zen1 +20°C), and set ``gates.cpu_thermal.enabled: true``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raidwatch.config import load_config
from raidwatch.modules.temps import enumerate_sensors


def main() -> int:
    config = load_config()
    dll_path = config.temps.lhm_dll_path

    print(f"\n{'=' * 60}")
    print("  RaidWatch Temperature Sensor Probe (D9)")
    print(f"  LHM DLL: {dll_path}")
    print(f"{'=' * 60}")

    if sys.platform != "win32":
        print("\n  ⚠ This script must be run on the Windows host (needs LHM + .NET).")
        print("  Cannot probe sensors on Linux/non-Windows.")
        return 1

    print("\n  Enumerating CPU temperature sensors...\n")
    sensors = enumerate_sensors(dll_path)

    if not sensors:
        print("  ⚠ No temperature sensors found. Possible causes:")
        print("    - LHM DLL not at the configured path")
        print("    - .NET runtime not installed (D30)")
        print("    - Not running as SYSTEM/Admin (WinRing0 needs privilege; D9)")
        print(f"\n  Check: {Path(dll_path).resolve()}")
        return 1

    print(f"  {'Name':<25} {'Value':>8}  {'Identifier'}")
    print(f"  {'-' * 25} {'-' * 8}  {'-' * 50}")
    for s in sensors:
        val = f"{s['value']:.1f}°C" if s["value"] is not None else "N/A"
        print(f"  {s['name']:<25} {val:>8}  {s['identifier']}")

    # Suggest config values
    print(f"\n  {'=' * 60}")
    print("  Config suggestions (config.yaml → temps:):")

    # Look for Tctl, Tdie, or CPU Package
    tctl = [s for s in sensors if "tctl" in s["name"].lower()]
    tdie = [s for s in sensors if "tdie" in s["name"].lower()]
    package = [s for s in sensors if "package" in s["name"].lower()]

    if tctl:
        print(f"\n  ⚠ Tctl detected: '{tctl[0]['name']}' = {tctl[0]['value']}°C")
        print("  Zen1 (1800X): Tctl = Tdie + 20°C offset")
        print(f'  Recommended cpu_sensor_name: "{tctl[0]["name"]}"')
        print("  Recommended tctl_offset: 20")
        print("  (RaidWatch will subtract the offset automatically)")
    elif tdie:
        print(f"\n  Tdie detected: '{tdie[0]['name']}' = {tdie[0]['value']}°C")
        print(f'  Recommended cpu_sensor_name: "{tdie[0]["name"]}"')
        print("  Recommended tctl_offset: 0 (Tdie has no offset)")
    elif package:
        print(f"\n  CPU Package detected: '{package[0]['name']}' = {package[0]['value']}°C")
        print(f'  Recommended cpu_sensor_name: "{package[0]["name"]}"')
        print("  Recommended tctl_offset: 0")
    else:
        print("\n  No standard sensor found. Pick the most relevant one above.")

    print("\n  After filling config, set gates.cpu_thermal.enabled: true")
    print("  and restart the service.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
