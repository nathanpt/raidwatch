"""Database layer — single shared aiosqlite connection (D21), versioned schema (D32).

Design notes:
- **Single shared connection** (D21): one :class:`aiosqlite.Connection` opened in
  the FastAPI lifespan and reused by the collector (writes), API handlers
  (reads), and pruning. All access is serialized on the event loop, so there is
  no ``database is locked`` / ``SQLITE_BUSY`` to hit by construction.
- **Fixed wide table** ``metrics_history`` (one row per cycle, scalar numerics
  only — D14). Separate append-only event tables: ``fika_events``,
  ``gate_events``, ``whea_events`` (``record_number`` unique — D16).
- **Schema versioning** (D32): ``PRAGMA user_version`` + idempotent migrations
  run at startup, so v1.x column adds don't require hand-SQL.
- **On-the-fly downsampling** (D15): ``GROUP BY (ts / bucket)`` with aggregates
  at query time; ``max()`` for peak metrics, ``avg()`` for throughput rates.

Timestamps are UTC epoch milliseconds everywhere (D19).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Current schema version. Bump when adding migrations.
SCHEMA_VERSION = 1

# DDL is idempotent (IF NOT EXISTS) so migrations are safe to re-run.
_MIGRATIONS: list[str] = [
    # --- v1: initial schema ------------------------------------------------ #
    """
    CREATE TABLE IF NOT EXISTS metrics_history (
        ts                          INTEGER PRIMARY KEY,
        cpu_total_percent           REAL,
        ram_percent                 REAL,
        ram_used_bytes              INTEGER,
        swap_percent                REAL,
        pages_per_sec               REAL,
        disk_read_bps               INTEGER,
        disk_write_bps              INTEGER,
        disk_queue_length           REAL,
        disk_avg_sec_per_transfer   REAL,
        disk_game_free_bytes        INTEGER,
        net_sent_bps                INTEGER,
        net_recv_bps                INTEGER,
        net_errs_total              INTEGER,
        temp_cpu_celsius            REAL,
        whea_count_2h               INTEGER,
        fika_spt_cpu_percent        REAL,
        fika_spt_rss_bytes          INTEGER,
        fika_headless_count         INTEGER,
        fika_headless_cpu_total     REAL,
        fika_headless_rss_total     INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fika_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          INTEGER NOT NULL,
        source      TEXT NOT NULL,
        severity    TEXT NOT NULL,
        message     TEXT NOT NULL,
        raw_line    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gate_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          INTEGER NOT NULL,
        gate_id     TEXT NOT NULL,
        action      TEXT NOT NULL,   -- 'triggered' | 'cleared'
        value       REAL,
        severity    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS whea_events (
        record_number   INTEGER PRIMARY KEY,   -- dedup across polls (D16)
        ts_generated    INTEGER NOT NULL,
        event_id        INTEGER,
        message         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gate_state (
        gate_id             TEXT PRIMARY KEY,
        last_crossed_monotonic  REAL,  -- time.monotonic() when metric first crossed (D19)
        currently_triggered    INTEGER DEFAULT 0,
        last_triggered_ts      INTEGER,
        trigger_count          INTEGER DEFAULT 0
    )
    """,
    # Helpful indexes for history range queries and event recency.
    "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_history(ts)",
    "CREATE INDEX IF NOT EXISTS idx_fika_events_ts ON fika_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_gate_events_ts ON gate_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_whea_ts ON whea_events(ts_generated)",
]


def now_ms() -> int:
    """Current UTC time as epoch milliseconds (D19)."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Connection management                                                        #
# --------------------------------------------------------------------------- #
class Database:
    """Wraps a single shared aiosqlite connection (D21).

    All methods assume they're called from the same event loop that opened the
    connection. The connection is serialized by asyncio's single-threaded model,
    so concurrent calls are inherently ordered.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._last_prune_monotonic = 0.0

    async def connect(self) -> None:
        """Open the shared connection and run migrations. Called from lifespan."""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self._db_path)
        # Defensive settings — single-writer model makes these belt-and-suspenders.
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._run_migrations()
        await self._conn.commit()
        logger.info("Database connected: %s (schema v%d)", self._db_path, SCHEMA_VERSION)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Database closed.")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._conn

    async def _run_migrations(self) -> None:
        """Run idempotent migrations up to SCHEMA_VERSION (D32)."""
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
            current = row[0] if row else 0

        if current < SCHEMA_VERSION:
            for stmt in _MIGRATIONS:
                await self.conn.execute(stmt)
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            logger.info(
                "Migrated database from v%d to v%d (%d statements)",
                current,
                SCHEMA_VERSION,
                len(_MIGRATIONS),
            )

    # ------------------------------------------------------------------ #
    # Metrics writes                                                     #
    # ------------------------------------------------------------------ #
    _INSERT_METRICS_SQL = """
        INSERT OR REPLACE INTO metrics_history (
            ts, cpu_total_percent, ram_percent, ram_used_bytes, swap_percent,
            pages_per_sec, disk_read_bps, disk_write_bps, disk_queue_length,
            disk_avg_sec_per_transfer, disk_game_free_bytes, net_sent_bps,
            net_recv_bps, net_errs_total, temp_cpu_celsius, whea_count_2h,
            fika_spt_cpu_percent, fika_spt_rss_bytes, fika_headless_count,
            fika_headless_cpu_total, fika_headless_rss_total
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    async def insert_metrics_row(self, row: dict[str, Any]) -> None:
        """Insert one cycle's scalar metrics into the wide table (D14).

        ``row`` keys must match the §3.4 wide-table column names. Missing/None
        values are stored as NULL.
        """
        async with self._write_lock:
            await self.conn.execute(
                self._INSERT_METRICS_SQL,
                (
                    row["ts"],
                    row.get("cpu_total_percent"),
                    row.get("ram_percent"),
                    row.get("ram_used_bytes"),
                    row.get("swap_percent"),
                    row.get("pages_per_sec"),
                    row.get("disk_read_bps"),
                    row.get("disk_write_bps"),
                    row.get("disk_queue_length"),
                    row.get("disk_avg_sec_per_transfer"),
                    row.get("disk_game_free_bytes"),
                    row.get("net_sent_bps"),
                    row.get("net_recv_bps"),
                    row.get("net_errs_total"),
                    row.get("temp_cpu_celsius"),
                    row.get("whea_count_2h"),
                    row.get("fika_spt_cpu_percent"),
                    row.get("fika_spt_rss_bytes"),
                    row.get("fika_headless_count"),
                    row.get("fika_headless_cpu_total"),
                    row.get("fika_headless_rss_total"),
                ),
            )
            await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Event writes                                                       #
    # ------------------------------------------------------------------ #
    async def insert_fika_event(
        self, ts: int, source: str, severity: str, message: str, raw_line: str | None
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO fika_events (ts, source, severity, message, raw_line) "
                "VALUES (?,?,?,?,?)",
                (ts, source, severity, message, raw_line),
            )
            await self.conn.commit()

    async def insert_gate_event(
        self, ts: int, gate_id: str, action: str, value: float | None, severity: str | None
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO gate_events (ts, gate_id, action, value, severity) VALUES (?,?,?,?,?)",
                (ts, gate_id, action, value, severity),
            )
            await self.conn.commit()

    async def insert_whea_event(
        self, record_number: int, ts_generated: int, event_id: int, message: str
    ) -> bool:
        """Insert a WHEA event; returns True if it was new (D16 dedup)."""
        async with self._write_lock:
            cur = await self.conn.execute(
                "INSERT OR IGNORE INTO whea_events "
                "(record_number, ts_generated, event_id, message) VALUES (?,?,?,?)",
                (record_number, ts_generated, event_id, message),
            )
            await self.conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # Gate state (D19)                                                   #
    # ------------------------------------------------------------------ #
    async def get_gate_state(self, gate_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT last_crossed_monotonic, currently_triggered, "
            "last_triggered_ts, trigger_count FROM gate_state WHERE gate_id = ?",
            (gate_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "last_crossed_monotonic": row[0],
            "currently_triggered": bool(row[1]),
            "last_triggered_ts": row[2],
            "trigger_count": row[3],
        }

    async def upsert_gate_state(
        self,
        gate_id: str,
        last_crossed_monotonic: float | None,
        currently_triggered: bool,
        last_triggered_ts: int | None = None,
        trigger_count: int = 0,
    ) -> None:
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO gate_state "
                "(gate_id, last_crossed_monotonic, currently_triggered, "
                " last_triggered_ts, trigger_count) VALUES (?,?,?,?,?) "
                "ON CONFLICT(gate_id) DO UPDATE SET "
                " last_crossed_monotonic=excluded.last_crossed_monotonic,"
                " currently_triggered=excluded.currently_triggered,"
                " last_triggered_ts=excluded.last_triggered_ts,"
                " trigger_count=excluded.trigger_count",
                (
                    gate_id,
                    last_crossed_monotonic,
                    int(currently_triggered),
                    last_triggered_ts,
                    trigger_count,
                ),
            )
            await self.conn.commit()

    # ------------------------------------------------------------------ #
    # History queries with on-the-fly downsampling (D15)                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bucket_for_minutes(minutes: int) -> int:
        """Choose a bucket size (ms) that caps output near ~720 points (D15).

        24h→2-min buckets, 6h→30s, 1h/15m→raw 5s.
        """
        if minutes >= 720:  # ~12h+
            return 120_000  # 2 minutes
        if minutes >= 360:  # ~6h+
            return 30_000  # 30 seconds
        return 5_000  # raw 5-second cadence

    async def query_history(
        self,
        minutes: int = 60,
        metrics: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return time-bucketed history rows (D15).

        Args:
            minutes: How far back to look.
            metrics: Optional column filter. None → all wide-table columns.
        """
        cutoff = now_ms() - minutes * 60_000
        bucket = self._bucket_for_minutes(minutes)

        # Map of column → aggregate function. Peak metrics use max(), rates avg() (D15).
        agg_map: dict[str, str] = {
            "cpu_total_percent": "MAX",
            "ram_percent": "MAX",
            "ram_used_bytes": "MAX",
            "swap_percent": "MAX",
            "pages_per_sec": "AVG",
            "disk_read_bps": "AVG",
            "disk_write_bps": "AVG",
            "disk_queue_length": "MAX",
            "disk_avg_sec_per_transfer": "MAX",
            "disk_game_free_bytes": "MIN",
            "net_sent_bps": "AVG",
            "net_recv_bps": "AVG",
            "net_errs_total": "MAX",
            "temp_cpu_celsius": "MAX",
            "whea_count_2h": "MAX",
            "fika_spt_cpu_percent": "AVG",
            "fika_spt_rss_bytes": "MAX",
            "fika_headless_count": "MAX",
            "fika_headless_cpu_total": "AVG",
            "fika_headless_rss_total": "MAX",
        }

        all_cols = list(agg_map.keys())
        cols = metrics if metrics else all_cols
        # Validate requested columns to prevent SQL injection via column names.
        for c in cols:
            if c not in agg_map:
                raise ValueError(f"Unknown metric column: {c}")

        select_parts = [f"{agg_map[c]}({c}) AS {c}" for c in cols]
        select_parts.insert(0, "(ts / :bucket) * :bucket AS bucket_ts")
        sql = (
            f"SELECT {', '.join(select_parts)} "
            "FROM metrics_history WHERE ts >= :cutoff "
            "GROUP BY bucket_ts ORDER BY bucket_ts"
        )

        async with self.conn.execute(sql, {"bucket": bucket, "cutoff": cutoff}) as cur:
            rows = await cur.fetchall()
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, r, strict=False)) for r in rows]

    async def query_history_csv(self, minutes: int = 1440) -> list[dict[str, Any]]:
        """Return downsampled rows for CSV export (all wide-table columns)."""
        return await self.query_history(minutes=minutes)

    # ------------------------------------------------------------------ #
    # Recent events                                                      #
    # ------------------------------------------------------------------ #
    async def recent_fika_events(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT ts, source, severity, message FROM fika_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"ts": r[0], "source": r[1], "severity": r[2], "message": r[3]} for r in rows]

    async def recent_gate_events(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT ts, gate_id, action, value, severity FROM gate_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"ts": r[0], "gate_id": r[1], "action": r[2], "value": r[3], "severity": r[4]}
            for r in rows
        ]

    async def log_tail(self, source: str, lines: int = 100) -> list[dict[str, Any]]:
        """Tail recent fika_events for a given log source."""
        async with self.conn.execute(
            "SELECT ts, source, severity, message, raw_line FROM fika_events "
            "WHERE source = ? ORDER BY ts DESC LIMIT ?",
            (source, lines),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"ts": r[0], "source": r[1], "severity": r[2], "message": r[3], "raw_line": r[4]}
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Maintenance                                                        #
    # ------------------------------------------------------------------ #
    async def prune_old_metrics(self, retention_hours: int = 48) -> int:
        """Delete metrics_history rows older than retention. Returns count deleted."""
        cutoff = now_ms() - retention_hours * 3_600_000
        async with self._write_lock:
            cur = await self.conn.execute("DELETE FROM metrics_history WHERE ts < ?", (cutoff,))
            await self.conn.commit()
            deleted = cur.rowcount
        if deleted:
            logger.info("Pruned %d metrics rows older than %dh", deleted, retention_hours)
        return deleted

    async def maybe_prune(self, retention_hours: int = 48) -> None:
        """Prune at most once per hour (not every cycle)."""
        now = time.monotonic()
        if now - self._last_prune_monotonic < 3600:
            return
        self._last_prune_monotonic = now
        await self.prune_old_metrics(retention_hours)

    async def db_size_mb(self) -> float:
        """Return the database file size in MB (for /health, D35)."""
        import os

        try:
            return os.path.getsize(self._db_path) / (1024 * 1024)
        except OSError:
            return 0.0
