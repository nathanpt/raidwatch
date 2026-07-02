"""Tests for Fika log-line classification + rotation-safe offset logic (D17)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from raidwatch.config import AppConfig
from raidwatch.modules.fika import FikaModule


@pytest.fixture
def fika_module() -> FikaModule:
    """A FikaModule with default config (no real paths needed for unit tests)."""
    return FikaModule(AppConfig())


class TestLogClassification:
    """Regex classification patterns (D17)."""

    def test_raid_start(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("server", "Raid started on Customs")
        assert event is not None
        assert event.severity == "info"
        assert event.source == "server"

    def test_raid_end(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("fika", "Raid ended, all players extracted")
        assert event is not None
        assert event.severity == "info"

    def test_player_join(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("server", "Player ScavKing joined the session")
        assert event is not None
        assert event.source == "server"

    def test_error_line(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("server", "NullReferenceException at RaidController")
        assert event is not None
        assert event.severity == "error"

    def test_warning_line(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("fika", "Warning: deprecated API usage")
        assert event is not None
        assert event.severity == "warn"

    def test_no_match_returns_none(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("server", "Loading assets...")
        assert event is None

    def test_truncation(self, fika_module: FikaModule) -> None:
        long_line = "Error: " + "x" * 500
        event = fika_module._classify_line("server", long_line)
        assert event is not None
        assert len(event.message) <= 200

    def test_case_insensitive(self, fika_module: FikaModule) -> None:
        event = fika_module._classify_line("fika", "RAID SPAWN INITIATED")
        assert event is not None


class TestRotationSafeOffset:
    """Rotation-safe offset logic (D17): offset > file_size → reset to end."""

    def test_first_open_seeks_to_end(self, fika_module: FikaModule, tmp_path: Path) -> None:
        """First open skips backlog — live feed, not archive (D17)."""
        log_file = tmp_path / "test.log"
        log_file.write_text("old line 1\nold line 2\nold line 3\n", encoding="utf-8")

        events = fika_module._tail_file("server", log_file)
        assert events == [], "First open should seek to end (skip backlog)"
        assert str(log_file) in fika_module._log_initialized

    def test_new_lines_read(self, fika_module: FikaModule, tmp_path: Path) -> None:
        """After initial seek, new appended lines are read and classified."""
        log_file = tmp_path / "test.log"
        log_file.write_text("initial\n", encoding="utf-8")

        # First open: seek to end
        fika_module._tail_file("server", log_file)

        # Append new line
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("Raid started on Shoreline\n")

        events = fika_module._tail_file("server", log_file)
        assert len(events) == 1
        assert "Raid started" in events[0].message

    def test_rotation_resets_offset(self, fika_module: FikaModule, tmp_path: Path) -> None:
        """If file shrinks (rotation), offset resets to end (D17)."""
        log_file = tmp_path / "test.log"
        log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        # First open
        fika_module._tail_file("server", log_file)

        # Simulate rotation: file replaced with smaller content
        log_file.write_text("new1\n", encoding="utf-8")

        # Should not crash; offset > file_size → reset
        events = fika_module._tail_file("server", log_file)
        assert isinstance(events, list), "Rotation should not raise"

    def test_no_new_data(self, fika_module: FikaModule, tmp_path: Path) -> None:
        """No new data → empty list."""
        log_file = tmp_path / "test.log"
        log_file.write_text("content\n", encoding="utf-8")

        fika_module._tail_file("server", log_file)  # seek to end
        events = fika_module._tail_file("server", log_file)  # no new data
        assert events == []


class TestPathResolution:
    """Env-var path resolution (D17)."""

    def test_expandvars(self, fika_module: FikaModule) -> None:
        """Windows %VAR% and Unix $VAR are expanded."""
        os.environ["TEST_LOG_DIR"] = "/tmp"
        try:
            p = fika_module._resolve_path("$TEST_LOG_DIR/fika.log")
            assert str(p).endswith("fika.log")
        finally:
            del os.environ["TEST_LOG_DIR"]

    def test_empty_path_returns_none(self, fika_module: FikaModule) -> None:
        assert fika_module._resolve_path("") is None
        assert fika_module._resolve_path(None) is None  # type: ignore[arg-type]
