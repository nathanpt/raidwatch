"""Tests for config validation: regex compile, auto-generate, defaults (D4/D23/D32)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from raidwatch.config import (
    AppConfig,
    GateConfig,
    ProcessesConfig,
    load_config,
)

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config.yaml.example"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _write_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _example_data() -> dict:
    return yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Auto-generation (D23)                                                       #
# --------------------------------------------------------------------------- #
class TestAutoGenerate:
    def test_missing_file_auto_generates(self, tmp_path: Path) -> None:
        """A missing config.yaml is auto-generated from the example (D23)."""
        target = tmp_path / "config.yaml"
        assert not target.exists()

        cfg = load_config(target)
        assert target.exists(), "Config should be auto-generated"
        # Auto-generated from example has known defaults.
        assert cfg.server.name == "tarkov-fika-host"
        assert cfg.collection.interval_seconds == 5

    def test_auto_generated_has_conservative_gate_defaults(self, tmp_path: Path) -> None:
        """Conservative gate thresholds ship by default (D10)."""
        target = tmp_path / "config.yaml"
        cfg = load_config(target)

        assert cfg.gates["ram_high"].enabled is True
        assert cfg.gates["ram_high"].threshold == 90
        # cpu_thermal is DISABLED until probe-validated (D9).
        assert cfg.gates["cpu_thermal"].enabled is False

    def test_system_metrics_need_zero_config(self, tmp_path: Path) -> None:
        """System metrics work with no config at all (D23)."""
        target = tmp_path / "config.yaml"
        cfg = load_config(target)
        # Even an empty/missing config yields a usable AppConfig.
        assert isinstance(cfg, AppConfig)
        assert cfg.server.bind_host in ("0.0.0.0", "::")


# --------------------------------------------------------------------------- #
# Regex validation (D4)                                                       #
# --------------------------------------------------------------------------- #
class TestRegexValidation:
    def test_valid_regex_compiles(self) -> None:
        cfg = ProcessesConfig(headless_cmdline_pattern=r"--fika-headless")
        assert cfg.headless_re is not None
        assert cfg.headless_re.search("EscapeFromTarkov.exe --fika-headless --instance 2")

    def test_invalid_regex_raises_validation_error(self, tmp_path: Path) -> None:
        """Invalid regex surfaces as a clear ValidationError, never a runtime crash (D4)."""
        data = _example_data()
        data["processes"]["headless_cmdline_pattern"] = "[unclosed-bracket"
        path = _write_config(tmp_path, data)

        with pytest.raises(ValidationError) as exc_info:
            load_config(path)
        assert "invalid regex" in str(exc_info.value).lower()

    def test_complex_regex_works(self) -> None:
        cfg = ProcessesConfig(headless_cmdline_pattern=r"--fika-headless(?:\s+--instance\s+\d+)?")
        assert cfg.headless_re is not None
        assert cfg.headless_re.search("foo --fika-headless --instance 3")
        assert cfg.headless_re.search("foo --fika-headless")
        assert not cfg.headless_re.search("foo --player-mode")

    def test_empty_pattern_disables_match(self) -> None:
        cfg = ProcessesConfig(headless_cmdline_pattern="")
        assert cfg.headless_re is None


# --------------------------------------------------------------------------- #
# Gate config validation                                                      #
# --------------------------------------------------------------------------- #
class TestGateValidation:
    def test_bad_operator_rejected(self) -> None:
        with pytest.raises(ValidationError, match="operator"):
            GateConfig(threshold=90, operator="explode")

    def test_bad_severity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="severity"):
            GateConfig(threshold=90, severity="apocalyptic")

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duration"):
            GateConfig(threshold=90, duration_seconds=-1)

    def test_valid_gate_accepted(self) -> None:
        g = GateConfig(enabled=True, threshold=90, operator=">", duration_seconds=300)
        assert g.enabled is True
        assert g.severity == "medium"  # default

    def test_all_operators_accepted(self) -> None:
        for op in (">", "<", ">=", "<=", "==", "!="):
            g = GateConfig(threshold=50, operator=op)
            assert g.operator == op


# --------------------------------------------------------------------------- #
# Token validation (D13/D33)                                                  #
# --------------------------------------------------------------------------- #
class TestTokenValidation:
    def test_placeholder_token_accepted(self) -> None:
        """The CHANGE_ME placeholder is accepted before install (D23)."""
        from raidwatch.config import AuthConfig

        cfg = AuthConfig(token="CHANGE_ME_to_something")
        assert cfg.token.startswith("CHANGE_ME")

    def test_weak_non_placeholder_token_rejected(self) -> None:
        from raidwatch.config import AuthConfig

        with pytest.raises(ValidationError, match="32 bytes"):
            AuthConfig(token="tooshort")

    def test_strong_token_accepted(self) -> None:
        from raidwatch.config import AuthConfig

        strong = "x" * 48
        cfg = AuthConfig(token=strong)
        assert cfg.token == strong


# --------------------------------------------------------------------------- #
# Round-trip                                                                  #
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_example_loads_cleanly(self) -> None:
        """The shipped config.yaml.example validates without errors."""
        cfg = load_config(EXAMPLE_PATH, allow_missing=False)
        assert len(cfg.gates) == 6
        # All gates have recommendations.
        for gid, gate in cfg.gates.items():
            assert gate.recommendation, f"Gate {gid} missing recommendation"

    def test_port_validation(self, tmp_path: Path) -> None:
        data = _example_data()
        data["server"]["port"] = 99999
        path = _write_config(tmp_path, data)
        with pytest.raises(ValidationError, match="port"):
            load_config(path)
