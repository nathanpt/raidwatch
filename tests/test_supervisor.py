"""Tests for the supervisor: restart on unexpected exit (D27), no-restart on clean shutdown.

Uses a FakeCollector whose ``start()`` spawns an asyncio task with a tunable
lifetime, so we can make the "collector" exit unexpectedly or stay alive without
touching the real collector loop.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from raidwatch import supervisor as supervisor_mod
from raidwatch.supervisor import Supervisor


class FakeCollector:
    """Stand-in for Collector exposing the surface the supervisor touches."""

    def __init__(self, run_seconds: float = 3600.0) -> None:
        self.run_seconds = run_seconds
        self._task: asyncio.Task[None] | None = None
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        run_seconds = self.run_seconds

        async def run() -> None:
            await asyncio.sleep(run_seconds)

        self._task = asyncio.create_task(run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class TestRestartOnUnexpectedExit:
    """D27: the supervisor spawns a replacement when the collector task exits."""

    @pytest.mark.asyncio
    async def test_restarts_after_unexpected_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supervisor_mod, "_RESTART_DELAY_SECONDS", 0.0)

        fc = FakeCollector(run_seconds=0.01)  # exits almost immediately
        fc.start()  # pre-start so the supervisor first sees a running task

        sup = Supervisor(fc)
        sup.start()

        # Allow the supervisor to observe the exit and spawn replacement(s).
        await asyncio.sleep(1.2)
        await sup.stop()

        assert sup.restart_count >= 1
        # Initial start plus at least one supervisor-spawned replacement.
        assert fc.start_calls >= 2


class TestCleanShutdown:
    """A graceful stop does NOT trigger a restart."""

    @pytest.mark.asyncio
    async def test_clean_shutdown_does_not_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supervisor_mod, "_RESTART_DELAY_SECONDS", 0.0)

        fc = FakeCollector(run_seconds=3600.0)  # stays alive
        fc.start()

        sup = Supervisor(fc)
        sup.start()

        # Supervisor observes a healthy, running collector.
        await asyncio.sleep(0.3)
        assert sup.restart_count == 0

        # Graceful stop — no restart should be recorded.
        await sup.stop()
        assert sup.restart_count == 0
