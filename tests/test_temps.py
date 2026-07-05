"""Tests for the temps module's diagnostic state (D9).

On Linux, pythonnet is unavailable so ``_init_lhm`` takes the degraded path.
These verify the module surfaces a human-readable failure reason in
``_last_init_error`` (consumed by ``scripts/probe_temps.py`` for one-paste
diagnosis) instead of silently returning [] with no clue why.
"""

from __future__ import annotations

import pytest

from raidwatch.modules import temps

_NO_PYTHONNET = pytest.mark.skipif(
    temps._PYTHONNET_AVAILABLE,
    reason="verifies the no-pythonnet degraded path (Linux dev)",
)


@pytest.fixture
def clean_temps_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset temps module init state before each test (it is normally sticky)."""
    monkeypatch.setattr(temps, "_lhm_initialized", False)
    monkeypatch.setattr(temps, "_lhm_computer", None)
    monkeypatch.setattr(temps, "_last_init_error", None)


class TestInitDiagnostics:
    """``_init_lhm`` must populate ``_last_init_error`` on failure (D9)."""

    def test_default_state_has_no_error(self, clean_temps_state: None) -> None:
        assert temps._last_init_error is None

    @_NO_PYTHONNET
    def test_no_pythonnet_records_reason(self, clean_temps_state: None) -> None:
        result = temps._init_lhm("vendor/lhm/LibreHardwareMonitorLib.dll")

        assert result is False
        assert temps._lhm_computer is None
        assert temps._lhm_initialized is True
        assert temps._last_init_error is not None
        assert "pythonnet" in temps._last_init_error.lower()

    @_NO_PYTHONNET
    def test_second_call_keeps_persistent_error(
        self, clean_temps_state: None
    ) -> None:
        """A repeat call short-circuits on the already-init flag without clearing the error."""
        temps._init_lhm("vendor/lhm/LibreHardwareMonitorLib.dll")
        first_error = temps._last_init_error
        assert first_error is not None

        result = temps._init_lhm("vendor/lhm/LibreHardwareMonitorLib.dll")

        assert result is False
        # Error persists across the short-circuit (probe reads it after enumerate).
        assert temps._last_init_error == first_error
