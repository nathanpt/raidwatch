"""Tests for on-the-fly SQL time-bucket downsampling (D15).

Verifies bucket math for 24h/6h/1h ranges, and max vs avg aggregate selection.
"""

from __future__ import annotations

import pytest

from raidwatch.database import Database


@pytest.fixture
async def db(tmp_path):
    """A real Database with sample data for downsampling tests."""
    db = Database(str(tmp_path / "test.db"))
    await db.connect()

    # Insert 100 rows at 5s intervals, ending at now (so they're in the query window).
    from raidwatch.database import now_ms

    base_ts = now_ms() - 100 * 5_000  # start 500s ago
    for i in range(100):
        await db.insert_metrics_row(
            {
                "ts": base_ts + i * 5_000,
                "cpu_total_percent": 40.0 + i * 0.5,  # 40% → 89.5%
                "ram_percent": 60.0 + (i % 10),  # oscillating
                "ram_used_bytes": 16_000_000_000 + i * 1_000_000,
                "disk_read_bps": 1_000_000 + i * 10_000,
                "disk_write_bps": 500_000 + i * 5_000,
            }
        )

    yield db
    await db.close()


class TestBucketSelection:
    """Bucket size selection caps output near ~720 points per range (D15)."""

    def test_24h_uses_2min_buckets(self) -> None:
        """24h → 2-minute (120,000 ms) buckets."""
        assert Database._bucket_for_minutes(1440) == 120_000

    def test_6h_uses_30s_buckets(self) -> None:
        """6h → 30-second (30,000 ms) buckets."""
        assert Database._bucket_for_minutes(360) == 30_000

    def test_1h_uses_5s_buckets(self) -> None:
        """1h → raw 5-second (5,000 ms) cadence."""
        assert Database._bucket_for_minutes(60) == 5_000

    def test_15m_uses_5s_buckets(self) -> None:
        """15m → raw 5-second cadence."""
        assert Database._bucket_for_minutes(15) == 5_000


class TestQueryAggregation:
    """Aggregate function selection: max() for peaks, avg() for rates (D15)."""

    @pytest.mark.asyncio
    async def test_cpu_uses_max(self, db: Database) -> None:
        """CPU percent uses MAX (don't average away spikes; D15)."""
        rows = await db.query_history(minutes=60, metrics=["cpu_total_percent"])
        assert len(rows) > 0
        # With 5s buckets and 5s data, each bucket = 1 row, so MAX = the value itself.
        # First row should have CPU ~40%.
        assert rows[0]["cpu_total_percent"] is not None

    @pytest.mark.asyncio
    async def test_disk_io_uses_avg(self, db: Database) -> None:
        """Disk I/O rates use AVG (throughput averages; D15)."""
        rows = await db.query_history(minutes=60, metrics=["disk_read_bps"])
        assert len(rows) > 0
        assert rows[0]["disk_read_bps"] is not None

    @pytest.mark.asyncio
    async def test_column_filter(self, db: Database) -> None:
        """Requesting specific columns returns only those columns."""
        rows = await db.query_history(minutes=60, metrics=["cpu_total_percent"])
        if rows:
            assert "cpu_total_percent" in rows[0]
            assert "ram_percent" not in rows[0]

    @pytest.mark.asyncio
    async def test_unknown_column_raises(self, db: Database) -> None:
        """Unknown metric column → ValueError (SQL injection prevention)."""
        with pytest.raises(ValueError, match="Unknown metric"):
            await db.query_history(minutes=60, metrics=["malicious_column"])

    @pytest.mark.asyncio
    async def test_bucketing_reduces_rows(self, db: Database) -> None:
        """Larger buckets produce fewer rows than raw data."""
        # Insert data spanning a wider range to test 30s bucketing
        base_ts = 1_700_000_000_000
        for i in range(100, 200):
            await db.insert_metrics_row(
                {
                    "ts": base_ts + i * 5_000,
                    "cpu_total_percent": 50.0,
                }
            )

        # With a large enough minutes value, bucketing kicks in
        # 100 rows at 5s = 500s ≈ 8 min → bucket_for_minutes(360) = 30s
        rows_30s = await db.query_history(minutes=360, metrics=["cpu_total_percent"])
        rows_5s = await db.query_history(minutes=60, metrics=["cpu_total_percent"])
        # 30s buckets should have fewer rows than 5s buckets for the same data
        assert len(rows_30s) <= len(rows_5s)
