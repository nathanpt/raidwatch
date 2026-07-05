"""Configuration loading, validation, and first-run auto-generation (D23).

Loads ``data/config.yaml``. On first run (missing file), auto-generates a safe
config from ``config.yaml.example`` so the dashboard is immediately useful with
zero configuration — system metrics work out of the box; Fika stays disabled
until paths are set (D23).

Design notes:
- Regex patterns (``headless_cmdline_pattern``) are compiled at load time. An
  invalid regex surfaces as a clear :class:`ValidationError`, never a runtime
  crash (D4).
- Pydantic v2 models validate all gate thresholds, operators, and durations.
- Config changes apply by service restart (no live reload in v1).
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# Resolve paths relative to the repo root (this file is raidwatch/config.py).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "data" / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.yaml.example"


# --------------------------------------------------------------------------- #
# Sub-models                                                                  #
# --------------------------------------------------------------------------- #
class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "raidwatch-host"
    spt_path: str = ""
    log_paths: dict[str, str] = Field(default_factory=dict)
    bind_host: str = "0.0.0.0"
    port: int = 8080
    headless_path: str = ""
    raid_udp_port: int = 25565
    risky_mod_names: list[str] = Field(default_factory=list)

    @field_validator("port", "raid_udp_port")
    @classmethod
    def _valid_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError(f"port must be 1-65535, got {v}")
        return v


class ProcessesConfig(BaseModel):
    """Config-driven process discovery (D4)."""

    model_config = ConfigDict(extra="forbid")

    spt_server_process_name: str = "SPT.Server.exe"
    headless_process_name: str = "EscapeFromTarkov.exe"
    headless_cmdline_pattern: str = r"--fika-headless"
    # Compiled at load; None only if pattern is empty (module disabled).
    _compiled_headless_re: re.Pattern[str] | None = None

    @model_validator(mode="after")
    def _compile_patterns(self) -> ProcessesConfig:
        """Compile the cmdline regex eagerly so invalid regex is a clear error (D4)."""
        if self.headless_cmdline_pattern:
            try:
                self.__dict__["_compiled_headless_re"] = re.compile(self.headless_cmdline_pattern)
            except re.error as exc:
                raise ValueError(
                    f"processes.headless_cmdline_pattern is invalid regex: {exc}"
                ) from exc
        return self

    @property
    def headless_re(self) -> re.Pattern[str] | None:
        """The compiled headless cmdline regex, or None if unset (module disabled)."""
        return self.__dict__.get("_compiled_headless_re")


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = 5.0
    history_retention_hours: int = 48
    whea_poll_seconds: float = 60.0
    top_others_poll_seconds: float = 15.0

    @field_validator("interval_seconds")
    @classmethod
    def _positive_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("collection.interval_seconds must be > 0")
        return v


class TempsConfig(BaseModel):
    """Temperature sensor config (D9/D30). Gate armed only after probe validation."""

    model_config = ConfigDict(extra="forbid")

    lhm_dll_path: str = "vendor/lhm/LibreHardwareMonitorLib.dll"
    cpu_sensor_name: str = ""
    tctl_offset: float = 20.0


# Valid operators for gate conditions.
VALID_OPERATORS = {">", "<", ">=", "<=", "==", "!="}


class GateConfig(BaseModel):
    """A single upgrade gate (D10).

    ``threshold`` semantics depend on ``operator``: for ``>`` the gate fires when
    the metric exceeds the threshold (sustained for ``duration_seconds``).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    threshold: float
    operator: str = ">"
    duration_seconds: float = 0.0
    severity: str = "medium"
    recommendation: str = ""
    # Which metric this gate watches (resolved at evaluation time, e.g.
    # "system.ram_percent"). Set explicitly so gates are self-describing.
    metric: str = ""

    @field_validator("operator")
    @classmethod
    def _valid_operator(cls, v: str) -> str:
        if v not in VALID_OPERATORS:
            raise ValueError(f"gate operator must be one of {sorted(VALID_OPERATORS)}, got {v!r}")
        return v

    @field_validator("severity")
    @classmethod
    def _valid_severity(cls, v: str) -> str:
        allowed = {"low", "medium", "high"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"gate severity must be one of {sorted(allowed)}, got {v!r}")
        return v_lower

    @field_validator("duration_seconds")
    @classmethod
    def _non_negative_duration(cls, v: float) -> float:
        if v < 0:
            raise ValueError("gate duration_seconds must be >= 0")
        return v


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = ""

    @field_validator("token")
    @classmethod
    def _warn_weak_token(cls, v: str) -> str:
        # We don't hard-fail (auto-generated config has a placeholder), but a
        # non-placeholder token must be reasonably strong (D13: ≥32 bytes).
        if v and not v.startswith("CHANGE_ME") and len(v) < 32:
            raise ValueError(
                "auth.token must be ≥32 bytes (or the CHANGE_ME placeholder before install)"
            )
        return v


# --------------------------------------------------------------------------- #
# Top-level config                                                            #
# --------------------------------------------------------------------------- #
class AppConfig(BaseModel):
    """The full application configuration tree."""

    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    processes: ProcessesConfig = Field(default_factory=ProcessesConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)
    temps: TempsConfig = Field(default_factory=TempsConfig)
    gates: dict[str, GateConfig] = Field(default_factory=dict)
    auth: AuthConfig = Field(default_factory=AuthConfig)


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def _generate_default_config(target: Path, *, example_path: Path = EXAMPLE_CONFIG_PATH) -> None:
    """Auto-generate ``data/config.yaml`` from the example (D23).

    If the example file exists, copy it; otherwise dump the model defaults.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if example_path.exists():
        shutil.copyfile(example_path, target)
        logger.info("Auto-generated config from example: %s", target)
    else:
        # Fallback: write the default model tree as YAML.
        data = _model_to_yaml_dict(AppConfig())
        target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        logger.info("Auto-generated default config (no example found): %s", target)


def _model_to_yaml_dict(model: BaseModel) -> dict[str, Any]:
    """Convert a pydantic model to a plain dict suitable for YAML dump."""
    return yaml.safe_load(model.model_dump_json())


def load_config(path: Path | str | None = None, *, allow_missing: bool = True) -> AppConfig:
    """Load and validate configuration.

    Args:
        path: Path to ``config.yaml``. Defaults to :data:`DEFAULT_CONFIG_PATH`.
        allow_missing: If True (default), a missing file is auto-generated from
            ``config.yaml.example`` and loaded with safe defaults (D23).

    Returns:
        Validated :class:`AppConfig`.

    Raises:
        ValidationError: If the config exists but is invalid (bad regex, bad
            thresholds, etc.) — these are real config errors, not auto-generate
            cases (D4).
        FileNotFoundError: If ``allow_missing=False`` and the file is absent.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        if not allow_missing:
            raise FileNotFoundError(f"Config file not found: {config_path}")
        logger.info("Config not found at %s — auto-generating (D23).", config_path)
        _generate_default_config(config_path)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Config root must be a mapping, got {type(raw).__name__} in {config_path}"
        )

    return AppConfig.model_validate(raw)
