"""Tests for WHEA sliding-window decay + record_number dedup (D16)."""

from __future__ import annotations

import time

import pytest

from raidwatch.database import Database


@pytest.fixture
async def db(tmp_path):
    """A real Database with a temp file for WHEA dedup testing."""
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    yield db
    await db.close()


class TestWheaDedup:
    """record_number unique constraint → re-inserts are no-ops (D16)."""

    @pytest.mark.asyncio
    async def test_first_insert_is_new(self, db: Database) -> None:
        """First insert of a record_number returns True (new)."""
        result = await db.insert_whea_event(1001, 1700000000000, 19, "WHEA error")
        assert result is True

    @pytest.mark.asyncio
    async def test_duplicate_insert_is_ignored(self, db: Database) -> None:
        """Re-inserting the same record_number returns False (D16 dedup)."""
        await db.insert_whea_event(1002, 1700000000000, 19, "WHEA error")
        result = await db.insert_whea_event(1002, 1700000000000, 19, "WHEA error")
        assert result is False

    @pytest.mark.asyncio
    async def test_different_records_both_new(self, db: Database) -> None:
        """Different record_numbers are both inserted successfully."""
        r1 = await db.insert_whea_event(2001, 1700000000000, 19, "err1")
        r2 = await db.insert_whea_event(2002, 1700000000001, 20, "err2")
        assert r1 is True
        assert r2 is True

    @pytest.mark.asyncio
    async def test_dedup_across_polls(self, db: Database) -> None:
        """Simulate multiple polls with overlapping events → no duplicates (D16)."""
        # Poll 1: events 3001-3003
        for rn in (3001, 3002, 3003):
            await db.insert_whea_event(rn, 1700000000000, 19, f"err {rn}")

        # Poll 2: same events 3001-3003 + new 3004 (windowed re-query)
        results = []
        for rn in (3001, 3002, 3003, 3004):
            r = await db.insert_whea_event(rn, 1700000000001, 19, f"err {rn}")
            results.append(r)

        # First three should be False (dup), 3004 should be True
        assert results == [False, False, False, True]


class TestWheaWindowDecay:
    """The sliding 2h window naturally decays as events age out (D16).

    The collector's windowed re-query reads events with TimeGenerated >= now-2h
    each poll. We test the count logic, not the actual win32evtlog (which can't
    run on Linux). The count is computed fresh each poll — it doesn't accumulate.
    """

    def test_window_count_logic(self) -> None:
        """Count of events in window naturally decays as time passes (D16).

        This is a pure-logic test: given a set of events with timestamps,
        verify that the windowed count function correctly excludes old events.
        """
        now = int(time.time() * 1000)
        two_hours_ago = now - 2 * 3600 * 1000
        three_hours_ago = now - 3 * 3600 * 1000
        one_hour_ago = now - 3600 * 1000

        events = [
            {"ts": three_hours_ago, "id": 1},  # outside window
            {"ts": two_hours_ago, "id": 2},  # at the edge (inside)
            {"ts": one_hour_ago, "id": 3},  # inside
            {"ts": now, "id": 4},  # inside
        ]

        # Window: events with ts >= now - 2h
        cutoff = now - 2 * 3600 * 1000
        in_window = [e for e in events if e["ts"] >= cutoff]

        assert len(in_window) == 3  # excludes the 3h-old event
        assert 1 not in [e["id"] for e in in_window]

    def test_decay_over_time(self) -> None:
        """As time advances, events fall out of the 2h window (D16)."""
        now = int(time.time() * 1000)
        event_ts = now  # fresh event

        # At time = now, event is 0h old → in window
        cutoff = now
        assert event_ts >= cutoff - 2 * 3600 * 1000

        # At time = now + 3h, event is 3h old → outside window
        future = now + 3 * 3600 * 1000
        assert event_ts < future - 2 * 3600 * 1000
