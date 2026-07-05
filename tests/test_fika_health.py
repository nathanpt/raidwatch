"""Tests for Fika headless health sources: boot.config (#3), Force-IP/UDP (#4),
risky-mod scan (#5b), and crash detection (#5a).

Mirrors the fixture style of ``tests/test_log_regex.py``: each test builds a
``FikaModule(AppConfig(...))`` with ``server.headless_path`` pointed at a tmp
tree and writes fixture files under it.
"""

from __future__ import annotations

from pathlib import Path

import psutil
import pytest

from raidwatch.config import AppConfig, ServerConfig
from raidwatch.models import ProcessInfo
from raidwatch.modules.fika import FikaModule

# The exact 9-line recommended boot.config from FIKA_HEADLESS_NOTES.md #3.
OPTIMIZED_BOOT_CONFIG = """\
gfx-enable-gfx-jobs=1
gfx-enable-native-gfx-jobs=1
gfx-disable-mt-rendering=1
wait-for-native-debugger=0
vr-enabled=0
hdr-display-enabled=0
gc-max-time-slice=10
job-worker-count=11
single-instance=
"""


def _module(headless_path: str) -> FikaModule:
    """Build a FikaModule whose headless_path points at the given root."""
    return FikaModule(AppConfig(server=ServerConfig(headless_path=headless_path)))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
# boot.config (#3)                                                            #
# --------------------------------------------------------------------------- #
class TestBootConfig:
    def test_optimized_fixture(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "EscapeFromTarkov_Data" / "boot.config",
            OPTIMIZED_BOOT_CONFIG,
        )
        job, optimized, expected = _module(str(tmp_path))._read_boot_config()
        assert job == 11
        assert optimized is True
        assert expected == psutil.cpu_count(logical=True) - 1

    def test_empty_headless_path(self) -> None:
        assert _module("")._read_boot_config() == (None, False, None)

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _module(str(tmp_path))._read_boot_config() == (None, False, None)

    def test_gfx_jobs_disabled_breaks_optimized(self, tmp_path: Path) -> None:
        content = OPTIMIZED_BOOT_CONFIG.replace("gfx-enable-gfx-jobs=1", "gfx-enable-gfx-jobs=0")
        _write(tmp_path / "EscapeFromTarkov_Data" / "boot.config", content)
        job, optimized, _ = _module(str(tmp_path))._read_boot_config()
        assert optimized is False
        assert job == 11  # job-worker-count still parsed independently

    def test_missing_job_worker_count(self, tmp_path: Path) -> None:
        content = OPTIMIZED_BOOT_CONFIG.replace("job-worker-count=11\n", "")
        _write(tmp_path / "EscapeFromTarkov_Data" / "boot.config", content)
        job, optimized, _ = _module(str(tmp_path))._read_boot_config()
        assert optimized is False
        assert job is None


# --------------------------------------------------------------------------- #
# Force IP / com.fika.core.cfg (#4)                                           #
# --------------------------------------------------------------------------- #
class TestForceIp:
    @staticmethod
    def _module(tmp_path: Path) -> FikaModule:
        return _module(str(tmp_path))

    def test_force_ip_populated(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "BepInEx" / "config" / "com.fika.core.cfg",
            "Force IP = 10.0.0.1\n",
        )
        assert self._module(tmp_path)._read_force_ip() is True

    def test_force_ip_empty(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "BepInEx" / "config" / "com.fika.core.cfg",
            "Force IP =\n",
        )
        assert self._module(tmp_path)._read_force_ip() is False

    def test_force_ip_commented(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "BepInEx" / "config" / "com.fika.core.cfg",
            "# Force IP = 10.0.0.1\n",
        )
        assert self._module(tmp_path)._read_force_ip() is False

    def test_only_force_bind_ip(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "BepInEx" / "config" / "com.fika.core.cfg",
            "Force Bind IP = 10.0.0.1\n",
        )
        assert self._module(tmp_path)._read_force_ip() is False

    def test_missing_file(self, tmp_path: Path) -> None:
        assert self._module(tmp_path)._read_force_ip() is False

    def test_empty_headless_path(self) -> None:
        assert _module("")._read_force_ip() is False


# --------------------------------------------------------------------------- #
# UDP port probe (#4)                                                         #
# --------------------------------------------------------------------------- #
class _Addr:
    def __init__(self, port: int) -> None:
        self.port = port


class _Conn:
    def __init__(self, laddr: _Addr | None) -> None:
        self.laddr = laddr


class TestUdpPort:
    def test_port_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil, "net_connections", lambda kind="udp": [_Conn(_Addr(25565))]
        )
        assert _module("")._check_udp_port() is True

    def test_port_not_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            psutil, "net_connections", lambda kind="udp": [_Conn(_Addr(53))]
        )
        assert _module("")._check_udp_port() is False

    def test_access_denied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(kind: str = "udp"):
            raise psutil.AccessDenied()

        monkeypatch.setattr(psutil, "net_connections", _raise)
        assert _module("")._check_udp_port() is False


# --------------------------------------------------------------------------- #
# Risky-mod scan (#5b)                                                        #
# --------------------------------------------------------------------------- #
class TestRiskyMods:
    @staticmethod
    def _module(tmp_path: Path, risky: list[str]) -> FikaModule:
        return FikaModule(
            AppConfig(server=ServerConfig(headless_path=str(tmp_path), risky_mod_names=risky))
        )

    @staticmethod
    def _seed(tmp_path: Path) -> Path:
        plugins = tmp_path / "BepInEx" / "plugins"
        for name in ("AmandsGraphics.dll", "Realism.dll", "SafeMod.dll"):
            _write(plugins / name, "")
        return plugins

    def test_matches_sorted(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        matches = self._module(tmp_path, ["amands", "realism"])._scan_risky_mods()
        assert matches == ["AmandsGraphics.dll", "Realism.dll"]

    def test_case_insensitive_query(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        # Uppercase query still matches (lowercased once in __init__).
        assert self._module(tmp_path, ["AMANDS"])._scan_risky_mods() == ["AmandsGraphics.dll"]

    def test_empty_risky_list_is_noop(self, tmp_path: Path) -> None:
        self._seed(tmp_path)
        assert self._module(tmp_path, [])._scan_risky_mods() == []

    def test_missing_plugins_dir(self, tmp_path: Path) -> None:
        assert self._module(tmp_path, ["amands"])._scan_risky_mods() == []

    def test_empty_headless_path(self) -> None:
        assert _module("")._scan_risky_mods() == []


# --------------------------------------------------------------------------- #
# Crash detection (#5a)                                                       #
# --------------------------------------------------------------------------- #
class TestCrashDetection:
    @staticmethod
    def _module() -> FikaModule:
        # Default AppConfig: empty spt_path / headless_path → other sources inert.
        return FikaModule(AppConfig())

    @staticmethod
    def _patch_discover(monkeypatch: pytest.MonkeyPatch, sequence: list) -> None:
        """Drive gather() through a scripted sequence of (spt, headless) tuples."""
        it = iter(sequence)

        def _fake(self, *args, **kwargs):
            return next(it)

        monkeypatch.setattr(FikaModule, "_discover_processes", _fake)

    def test_crash_then_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = self._module()
        spt_up = ProcessInfo(pid=1)
        head = [ProcessInfo(pid=100)]
        self._patch_discover(
            monkeypatch,
            [
                (spt_up, head),      # headless up → not crashed
                (spt_up, []),        # down cycle 1 → debounce, not crashed
                (spt_up, []),        # down cycle 2 → crashed
                (spt_up, head),      # recovered → not crashed
            ],
        )
        assert m.gather().headless_crashed is False   # up
        assert m.gather().headless_crashed is False   # debounce
        assert m.gather().headless_crashed is True    # crashed
        assert m.gather().headless_crashed is False   # recovered

    def test_initial_idle_is_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # SPT up, headless never seen yet — must NOT flag a crash.
        m = self._module()
        self._patch_discover(monkeypatch, [(ProcessInfo(pid=1), []), (ProcessInfo(pid=1), [])])
        assert m.gather().headless_crashed is False
        assert m.gather().headless_crashed is False

    def test_spt_down_is_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both down (no SPT) — not a headless crash.
        m = self._module()
        self._patch_discover(monkeypatch, [(ProcessInfo(pid=None), []), (ProcessInfo(pid=None), [])])
        assert m.gather().headless_crashed is False
        assert m.gather().headless_crashed is False
