"""Tests for broker overflow: drop-oldest, subscriber cap, full-snapshot-first (D28)."""

from __future__ import annotations

import asyncio

import pytest

from raidwatch.broker import Broker


@pytest.fixture
def broker():
    """A Broker with small queue for testing overflow."""
    return Broker(max_subscribers=5, queue_maxlen=3)


class TestSubscribeUnsubscribe:
    """Basic subscribe/unsubscribe lifecycle."""

    @pytest.mark.asyncio
    async def test_subscribe_returns_queue(self, broker: Broker) -> None:
        q = await broker.subscribe()
        assert isinstance(q, asyncio.Queue)
        assert broker.subscriber_count == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_queue(self, broker: Broker) -> None:
        q = await broker.subscribe()
        assert broker.subscriber_count == 1
        await broker.unsubscribe(q)
        assert broker.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_subscriber_cap(self, broker: Broker) -> None:
        """Exceeding max_subscribers raises RuntimeError (D28)."""
        for _ in range(5):
            await broker.subscribe()
        with pytest.raises(RuntimeError, match="cap"):
            await broker.subscribe()


class TestFullSnapshotFirst:
    """New subscriber receives the latest snapshot immediately (D25 resync)."""

    @pytest.mark.asyncio
    async def test_latest_sent_on_subscribe(self, broker: Broker) -> None:
        """subscribe(latest=...) enqueues the latest snapshot first (D25)."""
        snapshot = {"ts": 12345, "system": {"cpu": 50}}
        q = await broker.subscribe(latest=snapshot)
        item = q.get_nowait()
        assert item == snapshot

    @pytest.mark.asyncio
    async def test_no_latest_no_initial_data(self, broker: Broker) -> None:
        """subscribe() without latest → empty queue initially."""
        q = await broker.subscribe()
        assert q.empty()


class TestPublishFanOut:
    """Publish fans out to all subscribers (D28)."""

    @pytest.mark.asyncio
    async def test_publish_reaches_all(self, broker: Broker) -> None:
        q1 = await broker.subscribe()
        q2 = await broker.subscribe()
        snapshot = {"ts": 1}
        broker.publish(snapshot)
        assert q1.get_nowait() == snapshot
        assert q2.get_nowait() == snapshot

    @pytest.mark.asyncio
    async def test_publish_is_non_blocking(self, broker: Broker) -> None:
        """publish() never blocks even if subscriber is slow (D28)."""
        q = await broker.subscribe()
        # Publish many items rapidly (more than queue size)
        for i in range(10):
            broker.publish({"ts": i})
        # Should not have blocked; queue holds at most maxlen items (drop-oldest)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) <= 3  # queue_maxlen=3


class TestDropOldestOverflow:
    """Bounded queues drop the oldest on overflow (D28)."""

    @pytest.mark.asyncio
    async def test_drop_oldest_on_overflow(self, broker: Broker) -> None:
        """When queue is full, oldest snapshot is dropped (D28)."""
        q = await broker.subscribe()

        # Fill the queue (maxlen=3)
        for i in range(3):
            broker.publish({"ts": i})
        assert q.qsize() == 3

        # One more publish → oldest (ts=0) dropped
        broker.publish({"ts": 3})
        assert q.qsize() == 3

        # First item should now be ts=1 (ts=0 was dropped)
        first = q.get_nowait()
        assert first["ts"] == 1

    @pytest.mark.asyncio
    async def test_slow_client_does_not_block_others(self, broker: Broker) -> None:
        """A slow/stuck client never blocks the collector or other clients (D28)."""
        await broker.subscribe()  # slow client (never reads)
        q_fast = await broker.subscribe()

        # Flood publish — slow client's queue will overflow
        for i in range(20):
            broker.publish({"ts": i})

        # Fast client should still receive the latest items
        fast_items = []
        while not q_fast.empty():
            fast_items.append(q_fast.get_nowait())
        # Should have at most 3 (maxlen), and they should be the latest (ts=17,18,19)
        assert len(fast_items) <= 3
        assert fast_items[-1]["ts"] == 19

    @pytest.mark.asyncio
    async def test_close_clears_all(self, broker: Broker) -> None:
        """close() removes all subscribers (D27 shutdown)."""
        await broker.subscribe()
        await broker.subscribe()
        assert broker.subscriber_count == 2
        await broker.close()
        assert broker.subscriber_count == 0
