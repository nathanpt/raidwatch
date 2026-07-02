"""Broadcast broker — non-blocking fan-out with bounded per-subscriber queues (D28).

The broker sits between the collector and SSE subscribers. The collector calls
:meth:`Broker.publish` (non-blocking); the broker fans out into one bounded
``asyncio.Queue(maxlen=K)`` per subscriber (drop-oldest on overflow). Each SSE
handler drains its own queue. Concurrent subscribers are capped (e.g. 20).

This decouples the collector from clients: a slow/stuck SSE client never blocks
the 5s collection cycle or other clients. A new subscriber receives a full
snapshot first (resync after reconnect; D25).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_SUBSCRIBERS = 20
QUEUE_MAXLEN = 64  # bounded per-subscriber queue (drop-oldest on overflow)


class Broker:
    """Non-blocking fan-out broker (D28).

    Each subscriber gets its own bounded queue. ``publish`` is non-blocking —
    if a subscriber's queue is full, the oldest snapshot is dropped (that client
    falls behind but others are unaffected).
    """

    def __init__(
        self, max_subscribers: int = MAX_SUBSCRIBERS, queue_maxlen: int = QUEUE_MAXLEN
    ) -> None:
        self._max_subscribers = max_subscribers
        self._queue_maxlen = queue_maxlen
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._lock = asyncio.Lock()

    @property
    def subscriber_count(self) -> int:
        """Current number of active subscribers."""
        return len(self._subscribers)

    async def subscribe(self, latest: Any | None = None) -> asyncio.Queue[Any]:
        """Register a new subscriber and return its bounded queue.

        If ``latest`` is provided, it is enqueued first so the subscriber gets a
        full snapshot immediately on (re)connect (resync; D25).

        Raises :class:`RuntimeError` if the subscriber cap is reached.
        """
        async with self._lock:
            if len(self._subscribers) >= self._max_subscribers:
                raise RuntimeError(f"Subscriber cap reached ({self._max_subscribers})")
            q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxlen)
            self._subscribers.add(q)
        # Enqueue the latest snapshot first (outside lock — no contention needed).
        if latest is not None:
            self._enqueue(q, latest)
        logger.debug("Subscriber added (total=%d)", len(self._subscribers))
        return q

    async def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        """Remove a subscriber's queue."""
        async with self._lock:
            self._subscribers.discard(q)
        logger.debug("Subscriber removed (total=%d)", len(self._subscribers))

    def publish(self, snapshot: Any) -> None:
        """Fan out a snapshot to all subscribers (non-blocking; D28).

        This is **not** async and **never awaits** a client. If a subscriber's
        queue is full, the oldest item is dropped (drop-oldest backpressure).
        """
        for q in list(self._subscribers):
            self._enqueue(q, snapshot)

    def _enqueue(self, q: asyncio.Queue[Any], item: Any) -> None:
        """Enqueue with drop-oldest backpressure on a bounded queue (D28)."""
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()  # drop oldest
            logger.debug("Subscriber queue full — dropped oldest snapshot")
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(item)

    async def close(self) -> None:
        """Clear all subscribers on shutdown (D27)."""
        async with self._lock:
            self._subscribers.clear()
        logger.info("Broker closed (all subscribers cleared)")
