"""Shared retry pool for NotebookLM fetch recovery.

This queue coordinates retryable NotebookLM source fetches across worker
processes. It stores only the video ID and retry metadata because each worker
re-adds the source into its own reusable notebook when it claims work.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_POOL_DB_PATH = Path("P:/__csf/.data/yt-is/nlm_shared_retry_pool.sqlite")
_POOL_LOCK = threading.Lock()
_POOL_INITIALIZED = False


@dataclass(frozen=True)
class SharedRetryEntry:
    """A retryable NotebookLM video entry."""

    video_id: str
    retry_count: int
    next_retry_at: str
    last_error: str
    created_at: str
    status: str
    claimed_by: str | None = None
    claimed_at: str | None = None


def _ensure_pool() -> None:
    global _POOL_INITIALIZED
    if _POOL_INITIALIZED:
        return
    with _POOL_LOCK:
        if _POOL_INITIALIZED:
            return
        _POOL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_POOL_DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shared_retry_pool (
                    video_id TEXT PRIMARY KEY,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    claimed_by TEXT,
                    claimed_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shared_retry_pool_ready "
                "ON shared_retry_pool(status, next_retry_at, created_at)"
            )
            conn.commit()
        finally:
            conn.close()
        _POOL_INITIALIZED = True


def _ensure_pool_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_retry_pool (
            video_id TEXT PRIMARY KEY,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            claimed_by TEXT,
            claimed_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_retry_pool_ready "
        "ON shared_retry_pool(status, next_retry_at, created_at)"
    )


def _connect() -> sqlite3.Connection:
    _ensure_pool()
    conn = sqlite3.connect(_POOL_DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_pool_schema(conn)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now().isoformat()


def enqueue(
    video_id: str,
    *,
    retry_count: int = 0,
    delay_s: float = 30.0,
    last_error: str = "",
    status: str = "pending",
) -> bool:
    """Insert or update a retryable item into the shared pool."""
    if not video_id or len(video_id.strip()) != 11:
        return False
    queued_at = datetime.now()
    next_retry_at = queued_at + timedelta(seconds=max(0.0, float(delay_s)))
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO shared_retry_pool (
                video_id, retry_count, next_retry_at, last_error,
                created_at, status, claimed_by, claimed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                retry_count=excluded.retry_count,
                next_retry_at=excluded.next_retry_at,
                last_error=excluded.last_error,
                status=excluded.status,
                claimed_by=NULL,
                claimed_at=NULL,
                updated_at=excluded.updated_at
            """,
            (
                video_id,
                int(retry_count),
                next_retry_at.isoformat(),
                str(last_error or ""),
                queued_at.isoformat(),
                status,
                queued_at.isoformat(),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def claim_ready(
    *,
    limit: int = 25,
    claimant_id: str = "",
    stale_claim_s: float = 900.0,
) -> list[SharedRetryEntry]:
    """Atomically claim retry items that are ready for processing."""
    if limit < 1:
        return []
    now = datetime.now()
    now_iso = now.isoformat()
    stale_cutoff = (now - timedelta(seconds=max(0.0, float(stale_claim_s)))).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT video_id, retry_count, next_retry_at, last_error, created_at, status, claimed_by, claimed_at
            FROM shared_retry_pool
            WHERE (
                status = 'pending' AND next_retry_at <= ?
            ) OR (
                status = 'claimed' AND claimed_at IS NOT NULL AND claimed_at <= ?
            )
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now_iso, stale_cutoff, limit),
        ).fetchall()
        claimed: list[SharedRetryEntry] = []
        for row in rows:
            updated = conn.execute(
                """
                UPDATE shared_retry_pool
                SET status='claimed',
                    claimed_by=?,
                    claimed_at=?,
                    updated_at=?
                WHERE video_id=?
                  AND (
                    (status='pending' AND next_retry_at <= ?)
                    OR (status='claimed' AND claimed_at IS NOT NULL AND claimed_at <= ?)
                  )
                """,
                (
                    claimant_id or "",
                    now_iso,
                    now_iso,
                    row[0],
                    now_iso,
                    stale_cutoff,
                ),
            )
            if updated.rowcount == 1:
                claimed.append(
                    SharedRetryEntry(
                        video_id=row[0],
                        retry_count=int(row[1]),
                        next_retry_at=str(row[2]),
                        last_error=str(row[3] or ""),
                        created_at=str(row[4]),
                        status="claimed",
                        claimed_by=claimant_id or None,
                        claimed_at=now_iso,
                    )
                )
        conn.commit()
        return claimed
    finally:
        conn.close()


def reschedule(
    video_id: str,
    *,
    retry_count: int,
    delay_s: float,
    last_error: str,
) -> bool:
    """Return a claimed item to the shared pool with a later retry window."""
    if not video_id:
        return False
    now = datetime.now()
    next_retry_at = now + timedelta(seconds=max(0.0, float(delay_s)))
    conn = _connect()
    try:
        updated = conn.execute(
            """
            UPDATE shared_retry_pool
            SET retry_count=?,
                next_retry_at=?,
                last_error=?,
                status='pending',
                claimed_by=NULL,
                claimed_at=NULL,
                updated_at=?
            WHERE video_id=?
            """,
            (
                int(retry_count),
                next_retry_at.isoformat(),
                str(last_error or ""),
                now.isoformat(),
                video_id,
            ),
        )
        conn.commit()
        return updated.rowcount == 1
    finally:
        conn.close()


def mark_complete(video_id: str) -> bool:
    """Mark a retry item as completed successfully."""
    if not video_id:
        return False
    now = _now_iso()
    conn = _connect()
    try:
        updated = conn.execute(
            """
            UPDATE shared_retry_pool
            SET status='completed',
                claimed_by=NULL,
                claimed_at=NULL,
                updated_at=?
            WHERE video_id=?
            """,
            (now, video_id),
        )
        conn.commit()
        return updated.rowcount == 1
    finally:
        conn.close()


def mark_permanent_failure(video_id: str, last_error: str = "") -> bool:
    """Mark a retry item as terminal."""
    if not video_id:
        return False
    now = _now_iso()
    conn = _connect()
    try:
        updated = conn.execute(
            """
            UPDATE shared_retry_pool
            SET status='permanent_failure',
                last_error=?,
                claimed_by=NULL,
                claimed_at=NULL,
                updated_at=?
            WHERE video_id=?
            """,
            (str(last_error or ""), now, video_id),
        )
        conn.commit()
        return updated.rowcount == 1
    finally:
        conn.close()


def pending_count() -> int:
    """Return the number of pending retry items."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM shared_retry_pool WHERE status='pending'"
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def reset_pool() -> None:
    """Remove all shared retry pool entries (test helper)."""
    _ensure_pool()
    conn = sqlite3.connect(_POOL_DB_PATH)
    try:
        _ensure_pool_schema(conn)
        conn.execute("DELETE FROM shared_retry_pool")
        conn.commit()
    finally:
        conn.close()
