"""Temperature via LibreHardwareMonitor + pythonnet (D9/D30/D31).

Loads the **vendored** ``vendor/lhm/LibreHardwareMonitorLib.dll`` (+ deps) from
the official LHM release. Enables CPU/GPU/HDD sensors; iterates Temperature
sensors; picks the configured ``cpu_sensor_name``; applies ``tctl_offset``.

**Display ON from launch; ``cpu_thermal`` gate DISABLED until probe-validated
(D9).** Any LHM/driver error → return ``None`` + UI warning (never crashes
collector per D8).

Requires: .NET runtime on the host, SYSTEM privilege (WinRing0 kernel driver;
D9/D31). Import-guarded — degrades to ``None`` on Linux/non-Windows.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_PYTHONNET_AVAILABLE = False
try:
    if sys.platform == "win32":
        import clr  # pythonnet  # noqa: F401

        _PYTHONNET_AVAILABLE = True
except ImportError:
    pass

# Module-level LHM Computer object (initialized once, reused).
_lhm_computer: Any = None
_lhm_initialized = False
_consecutive_failures = 0
# Last LHM init failure, surfaced by probe_temps.py for one-paste diagnosis (D9).
# None if init succeeded or was never attempted; a human-readable string otherwise.
_last_init_error: str | None = None


def _init_lhm(dll_path: str) -> bool:
    """Initialize the LHM Computer object. Returns True on success.

    Loads the vendored DLL via pythonnet, creates a Computer instance, enables
    CPU/GPU/HDD sensors, and opens the connection (loads the WinRing0 driver).
    """
    global _lhm_computer, _lhm_initialized, _last_init_error

    if _lhm_initialized:
        return _lhm_computer is not None

    # Fresh attempt; clear any stale error from a prior probe run.
    _last_init_error = None
    if not _PYTHONNET_AVAILABLE:
        _last_init_error = (
            "pythonnet (clr) not importable on this platform. "
            "On Windows: run scripts/install_win_deps.ps1. "
            "On Linux: temps are unsupported (degraded mode)."
        )
        logger.info("pythonnet not available — temps disabled")
        _lhm_initialized = True
        return False

    try:
        import clr  # type: ignore[import-not-found]

        # Load the vendored LHM DLL (D30 — official release only).
        clr.AddReference(dll_path)
        from LibreHardwareMonitor.Hardware import Computer  # type: ignore[import-not-found]

        computer = Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = False  # v1: CPU only (D2 cut line)
        computer.IsMotherboardEnabled = False
        computer.IsControllerEnabled = False
        computer.IsStorageEnabled = False
        computer.Open()

        _lhm_computer = computer
        _lhm_initialized = True
        logger.info("LHM initialized from %s", dll_path)
        return True

    except Exception as exc:
        _last_init_error = f"{type(exc).__name__}: {exc}"
        logger.exception("LHM initialization failed — temps will be unavailable (D8)")
        _lhm_initialized = True
        _lhm_computer = None
        return False


def gather_temps(dll_path: str, sensor_name: str, tctl_offset: float) -> float | None:
    """Read the configured CPU temperature sensor.

    Args:
        dll_path: Path to LibreHardwareMonitorLib.dll (D30).
        sensor_name: The sensor identifier to read (from probe_temps.py; D9).
        tctl_offset: Zen1 Tctl +20°C offset to subtract (D9).

    Returns:
        Temperature in °C, or None if unavailable (D8 failure-tolerant).
    """
    global _consecutive_failures

    if not _init_lhm(dll_path):
        return None

    if _lhm_computer is None:
        return None

    try:
        for hardware in _lhm_computer.Hardware:
            hardware.Update()  # refresh sensor readings
            for sensor in hardware.Sensors:
                if sensor.SensorType == "Temperature" and sensor_identifier_matches(
                    sensor, sensor_name
                ):
                    raw_temp = float(sensor.Value)
                    _consecutive_failures = 0
                    # Apply Tctl offset if configured (D9: Zen1 +20°C).
                    # Only subtract if the sensor name suggests Tctl.
                    if "tctl" in sensor_name.lower() or "tctl" in str(sensor.Name).lower():
                        return raw_temp - tctl_offset
                    return raw_temp

        # Sensor not found — may not exist on this hardware.
        logger.debug("Temperature sensor '%s' not found", sensor_name)
        return None

    except Exception:
        _consecutive_failures += 1
        logger.warning(
            "LHM temp read failed (consecutive=%d) — degraded (D8)",
            _consecutive_failures,
            exc_info=True,
        )
        return None


def sensor_identifier_matches(sensor: Any, configured_name: str) -> bool:
    """Check if a sensor matches the configured name (fuzzy; D9).

    LHM sensor names include paths like '/amdcpu/0/temperature/5' and display
    names like 'Tctl' or 'CPU Package'. Match on either.
    """
    if not configured_name:
        return False
    configured_lower = configured_name.lower()
    sensor_name = str(sensor.Name).lower()
    sensor_id = str(sensor.Identifier).lower()
    return configured_lower in sensor_name or configured_lower in sensor_id


def enumerate_sensors(dll_path: str) -> list[dict[str, Any]]:
    """Enumerate all CPU temperature sensors for probe_temps.py (D9).

    Returns a list of dicts: {name, identifier, value, type}.
    """
    if not _init_lhm(dll_path) or _lhm_computer is None:
        return []

    sensors: list[dict[str, Any]] = []
    try:
        for hardware in _lhm_computer.Hardware:
            hardware.Update()
            for sensor in hardware.Sensors:
                if sensor.SensorType == "Temperature":
                    sensors.append(
                        {
                            "name": str(sensor.Name),
                            "identifier": str(sensor.Identifier),
                            "value": float(sensor.Value) if sensor.Value else None,
                            "hardware": str(hardware.Name),
                        }
                    )
    except Exception:
        logger.exception("Sensor enumeration failed")

    return sensors


def module_state() -> str:
    """Return the module health state for /health (D8/D35)."""
    if not _PYTHONNET_AVAILABLE:
        return "error"
    if _lhm_computer is None:
        return "error" if _lhm_initialized else "degraded"
    if _consecutive_failures > 5:
        return "backoff"
    return "ok"
