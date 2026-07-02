"""Supervisor task — awaits the collector and restarts it on exit (D27).

The collector loop is wrapped end-to-end (D27), so it should never die from an
exception. But if the task ever *returns* unexpectedly (cancellation,
interpreter-level fault), the supervisor restarts it and logs.

This is the asyncio-level backstop. For the irreducible native-interop GIL hang
case (pythonnet/LHM), the external Scheduled Task curling ``/health`` is the
final backstop (D27 — documented in install_service.ps1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)

# Brief pause between restart attempts to avoid a tight crash loop.
_RESTART_DELAY_SECONDS = 2.0


class Supervisor:
    """Supervises the collector task, restarting it on unexpected exit (D27).

    Created in lifespan alongside the collector. The supervisor task watches
    ``collector._task``; if it exits without ``stop()`` being called, the
    supervisor logs the event and restarts the collector after a short delay.
    """

    def __init__(self, collector: object) -> None:
        self._collector = collector
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self.restart_count = 0

    def start(self) -> None:
        """Start the supervisor task."""
        if self._task is None or self._task.done():
            self._stop_requested = False
            self._task = asyncio.create_task(self._run(), name="supervisor")

    async def stop(self) -> None:
        """Signal the supervisor to stop (does not restart the collector)."""
        self._stop_requested = True
        await self._collector.stop()  # type: ignore[attr-defined]
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        """Watch the collector task; restart on unexpected exit (D27)."""
        logger.info("Supervisor started — watching collector")
        while not self._stop_requested:
            task = getattr(self._collector, "_task", None)
            if task is None or task.done():
                if self._stop_requested:
                    break
                # Collector exited unexpectedly — restart it.
                self.restart_count += 1
                logger.error(
                    "Collector task exited unexpectedly (restart #%d at %s) — restarting",
                    self.restart_count,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                await asyncio.sleep(_RESTART_DELAY_SECONDS)
                self._collector.start()  # type: ignore[attr-defined]
                # Give it a moment to start before checking again.
                await asyncio.sleep(0.5)
            else:
                # Collector is running — poll periodically.
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except TimeoutError:
                    pass  # still running — good
                except asyncio.CancelledError:
                    break

        logger.info("Supervisor stopped.")
