"""Round-robin batch scheduler with shared channel cooldown and persistent download archive."""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from csf.batch_status import _get_batch_status_storage

# Jitter bounds — match transcript.py values for consistency
_JITTER_MIN = 2.0
_JITTER_MAX = 10.0
_COOLDOWN_SECONDS = 300  # 5 minutes per ADR
_STALE_ATTEMPTING_SECONDS = 1800  # 30 minutes
_RETRY_FAILED_SECONDS = 86400  # 24 hours


class BatchScheduler:
    """Yields video IDs in round-robin order across all channels with cooldown and archive support."""

    __slots__ = ("_channels", "_iterators", "_db_path")

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _get_batch_status_storage()._db_path
        self._ensure_tables()
        self._recover_stale_attempting()
        self._channels = self._get_pending_channels()
        # Checkpoint WAL opened during init so connections are clean on Windows
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

    def _ensure_tables(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_archive (
                video_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'skipped', 'attempting')),
                source TEXT,
                attempted_at REAL NOT NULL,  -- unix timestamp (REAL) for reliable numeric comparison
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS channel_cooldown (
                source TEXT PRIMARY KEY,
                cooldown_until REAL NOT NULL
            );
        """)
        # Remove consecutive_429s column from existing channel_cooldown tables.
        # Wrapped in try/except for SQLite versions that don't support DROP COLUMN.
        try:
            conn.execute("ALTER TABLE channel_cooldown DROP COLUMN consecutive_429s")
        except sqlite3.OperationalError:
            pass  # Column already absent or SQLite doesn't support DROP COLUMN
        conn.close()

    def _recover_stale_attempting(self) -> None:
        """Promote stale 'attempting' entries to 'failed' on startup."""
        cutoff = time.time() - _STALE_ATTEMPTING_SECONDS
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "UPDATE download_archive SET status='failed' WHERE status='attempting' AND attempted_at < ?",
            (cutoff,),
        )
        conn.commit()
        conn.close()

    def _get_pending_channels(self) -> list[str]:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT DISTINCT source FROM analysis_status WHERE status='pending' AND source IS NOT NULL"
        )
        channels = [row[0] for row in cursor.fetchall()]
        conn.close()
        return channels

    def _count_pending(self) -> int:
        """Return the count of all pending videos across all channels (runtime-accurate)."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM analysis_status WHERE status='pending'"
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0

    def _get_pending_videos(self, source: str) -> Iterator[str]:
        """Yield pending video IDs for a source, excluding archived videos (always fresh iterator)."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            """SELECT video_id FROM analysis_status
               WHERE source=? AND status='pending'
               AND video_id NOT IN (SELECT video_id FROM download_archive)
               ORDER BY published_at ASC""",
            (source,),
        )
        for row in cursor:
            yield row[0]
        conn.close()

    def _is_in_cooldown(self, source: str) -> bool:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT cooldown_until FROM channel_cooldown WHERE source=?", (source,)
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return False
        return row[0] > time.monotonic()

    def _record_attempting(self, video_id: str, source: str) -> None:
        # EXCLUSIVE mode prevents inter-process races where two terminals
        # simultaneously yield the same video within the same pass.
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN EXCLUSIVE")
        # Re-check archive inside transaction to catch races
        row = conn.execute(
            "SELECT status FROM download_archive WHERE video_id=?", (video_id,)
        ).fetchone()
        if row and row[0] in ("success", "failed", "attempting"):
            conn.rollback()
            conn.close()
            return  # Skip — another terminal won the race
        conn.execute(
            "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) VALUES (?, 'attempting', ?, ?)",
            (video_id, source, time.time()),
        )
        conn.commit()
        conn.close()

    def record_429(self, source: str) -> None:
        """Record a 429 for this channel. Sets cooldown until _COOLDOWN_SECONDS from now."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        conn.execute(
            "INSERT OR REPLACE INTO channel_cooldown (source, cooldown_until) VALUES (?, ?)",
            (source, cooldown_until),
        )
        conn.commit()
        conn.close()

    def record_success(self, source: str) -> None:
        """Clear cooldown for this channel on successful fetch."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("DELETE FROM channel_cooldown WHERE source=?", (source,))
        conn.commit()
        conn.close()

    def reset_failed_videos(self, source: str | None = None) -> int:
        """Manual Reset: Promote failed videos back to the pending pool.

        Deletes 'failed' entries from download_archive, allowing the scheduler
        to pick them up again immediately (bypassing the 24-hour retry window).

        Args:
            source: Optional channel URL to reset only that source.

        Returns:
            Number of videos promoted.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        if source:
            cursor = conn.execute(
                "DELETE FROM download_archive WHERE status='failed' AND source=?", (source,)
            )
        else:
            cursor = conn.execute("DELETE FROM download_archive WHERE status='failed'")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

    def archive_finalize(
self, video_id: str, status: str, source: str | None = None, error: str | None = None) -> None:
        """Write final status to download_archive after worker completes.

        Must be called by batch.py workers after mark_complete/mark_failed.
        Uses EXCLUSIVE transaction to prevent inter-process races on same video_id.
        """
        if status not in ("success", "failed", "skipped"):
            raise ValueError(f"Invalid archive status: {status!r}")
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute(
            "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (video_id, status, source, time.time(), error),
        )
        conn.commit()
        conn.close()

    def _archive_status(self, video_id: str) -> str | None:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT status FROM download_archive WHERE video_id=?", (video_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def yield_next(self) -> Iterator[tuple[str, str]]:
        """Yield (video_id, source) pairs in round-robin order, skipping archived/cooldown videos.

        Yields one video at a time, cycling through all pending channels. Skips channels in cooldown.
        """
        if not self._channels:
            return

        # Recover stale attempting entries on startup
        self._recover_stale_attempting()

        channel_idx = 0  # Track position for round-robin

        while True:
            yielded_any = False
            start_idx = channel_idx

            while True:
                channel = self._channels[channel_idx]
                channel_idx = (channel_idx + 1) % len(self._channels)

                if self._is_in_cooldown(channel):
                    if channel_idx == start_idx:
                        break  # All channels in cooldown
                    continue

                # Get one pending video from this channel
                # RETRY LOGIC: Allow videos that failed more than 24 hours ago to be retried
                # by excluding only success/attempting/recent-failed from the pending pool.
                retry_cutoff = time.time() - _RETRY_FAILED_SECONDS
                conn = sqlite3.connect(self._db_path)
                cursor = conn.execute(
                    """SELECT video_id FROM analysis_status
                       WHERE source=? AND status='pending'
                       AND video_id NOT IN (
                           SELECT video_id FROM download_archive 
                           WHERE status IN ('success', 'attempting')
                           OR (status='failed' AND attempted_at > ?)
                       )
                       ORDER BY published_at ASC LIMIT 1""",
                    (channel, retry_cutoff),
                )
                row = cursor.fetchone()
                conn.close()

                if not row:
                    if channel_idx == start_idx:
                        break  # Checked all channels, none have pending videos
                    continue  # No pending videos for this channel

                video_id = row[0]

                # Mark as attempting before yielding
                self._record_attempting(video_id, channel)

                yielded_any = True
                yield video_id, channel
                break  # Yielded one video, next call continues from next channel

            if not yielded_any:
                break  # All channels exhausted
