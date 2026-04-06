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


class BatchScheduler:
    """Yields video IDs in round-robin order across all channels with cooldown and archive support."""

    __slots__ = ("_channels", "_iterators", "_db_path")

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _get_batch_status_storage()._db_path
        self._ensure_tables()
        self._recover_stale_attempting()
        self._channels = self._get_pending_channels()
        self._iterators: dict[str, Iterator[str]] = {
            ch: iter(self._get_pending_videos(ch)) for ch in self._channels
        }
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
                cooldown_until REAL NOT NULL,
                consecutive_429s INTEGER NOT NULL DEFAULT 0
            );
        """)
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

    def _get_pending_videos(self, source: str) -> list[str]:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT video_id FROM analysis_status WHERE source=? AND status='pending' ORDER BY published_at ASC",
            (source,),
        )
        videos = [row[0] for row in cursor.fetchall()]
        conn.close()
        return videos

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
        """Record a 429 for this channel. Opens circuit after threshold."""
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT consecutive_429s FROM channel_cooldown WHERE source=?", (source,)
        )
        row = cursor.fetchone()
        if row:
            consecutive = row[0] + 1
        else:
            consecutive = 1
        cooldown_until = time.monotonic() + _COOLDOWN_SECONDS
        conn.execute(
            "INSERT OR REPLACE INTO channel_cooldown (source, cooldown_until, consecutive_429s) VALUES (?, ?, ?)",
            (source, cooldown_until, consecutive),
        )
        conn.close()

    def record_success(self, source: str) -> None:
        """Clear cooldown for this channel on successful fetch."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("DELETE FROM channel_cooldown WHERE source=?", (source,))
        conn.close()

    def archive_finalize(self, video_id: str, status: str, source: str | None = None) -> None:
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
            "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) "
            "VALUES (?, ?, ?, ?)",
            (video_id, status, source, time.time()),
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

        Yields one video at a time, cycling through all pending channels. Applies jitter between
        yields to diffuse request timing. Skips channels that are in cooldown.
        """
        if not self._channels:
            return

        # Rebuild iterators for any channels that are exhausted
        active_channels = [ch for ch in self._channels if self._iterators[ch]]

        yielded_this_pass: set[str] = set()
        while active_channels:
            for channel in list(active_channels):
                if self._is_in_cooldown(channel):
                    continue

                # Refresh iterator if exhausted
                if not self._iterators[channel]:
                    self._iterators[channel] = iter(self._get_pending_videos(channel))

                try:
                    video_id = next(self._iterators[channel])
                except StopIteration:
                    self._iterators[channel] = iter([])
                    active_channels.remove(channel)
                    continue

                # Archive check — skip if already attempted
                arch_status = self._archive_status(video_id)
                if arch_status in ("success", "failed", "attempting"):
                    continue

                # Mark attempting before yielding
                self._record_attempting(video_id, channel)

                yielded_this_pass.add(video_id)
                yield video_id, channel

                # Jitter between dispatches
                time.sleep(random.uniform(_JITTER_MIN, _JITTER_MAX))

            # If we yielded nothing this pass, break to avoid infinite loop
            if not yielded_this_pass:
                break
            yielded_this_pass.clear()
