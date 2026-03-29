"""Retry queue with exponential backoff — isolated SQLite DB.

Retry queue is stored in a SEPARATE SQLite DB from the transcript cache
(LOGIC-002 fix: prevents retry queue from inheriting concurrent-write
corruption if the transcript DB becomes corrupted).

Architecture matches cache.py: WAL mode + single background writer thread
per terminal. This is the same pattern proven to work for transcript caching.
"""

import queue
import random
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Validation — same pattern as transcript.py
_VIDEO_ID_PATTERN = __import__("re").compile(r"^[a-zA-Z0-9_-]{11}$")

# Separate DB from transcript cache — isolation blast radius (LOGIC-002)
_SHARED_DB_PATH: Path = Path(
    "P:/__csf/.data/intelligence-stream/retry/retry_queue.sqlite"
)

# Exponential backoff parameters
_BACKOFF_BASE_MINUTES = 5  # LOGIC-007: specify backoff algorithm
_BACKOFF_CAP_MINUTES = 1440  # 24 hours cap
_MAX_RETRIES = 5

# Per-terminal in-memory index
_retry_storages: dict[str, "_RetryStorage"] = {}
_storage_lock = threading.Lock()


def _validate_video_id(video_id: str) -> bool:
    """Validate video_id format. Returns True if valid, False otherwise."""
    return bool(_VIDEO_ID_PATTERN.match(video_id))


@dataclass
class RetryEntry:
    """An entry in the retry queue."""

    video_id: str
    retry_count: int
    next_retry_at: datetime
    last_error: str
    created_at: datetime
    status: str  # 'pending' | 'permanent_failure'


class _RetryStorage:
    """Internal SQLite storage for retry queue.

    Uses WAL mode for concurrent reads and a single writer thread
    to prevent write contention. Isolated DB from transcript cache.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id
        self._write_queue: queue.Queue[Optional[dict]] = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._started = False
        self._stopped = False
        self._conn: Optional[sqlite3.Connection] = None  # writer's connection

    def _stop(self) -> None:
        """Stop the writer thread gracefully (drain queue, then exit)."""
        self._stopped = True
        self._write_queue.put(None)  # Shutdown signal
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=5.0)
        # Close the writer's connection after thread exits (LOGIC-002: prevents
        # Windows file-lock issues when test fixture deletes the DB)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _ensure_table(self) -> None:
        """Create retry queue table if not exists in isolated DB."""
        _SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retry_queue (
                video_id TEXT PRIMARY KEY,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.close()

    def _start_writer(self) -> None:
        """Start the background writer thread."""
        if self._started:
            return
        self._started = True
        self._ensure_table()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="retry-queue-writer"
        )
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        """Background thread that processes write requests."""
        self._conn = sqlite3.connect(_SHARED_DB_PATH, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")  # 30s blocking
        while True:
            item = self._write_queue.get()
            if item is None:  # Shutdown signal
                break
            if self._stopped:
                self._write_queue.task_done()
                continue
            self._write_entry(item)
            self._write_queue.task_done()
        self._conn.close()
        self._conn = None

    def _write_entry(self, item: dict) -> None:
        """Write a single entry to the database."""
        assert self._conn is not None, "_write_entry called before writer loop started"
        conn = self._conn
        conn.execute(
            """
            INSERT OR REPLACE INTO retry_queue
            (video_id, retry_count, next_retry_at, last_error, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item["video_id"],
                item["retry_count"],
                item["next_retry_at"].isoformat(),
                item["last_error"],
                item["created_at"].isoformat(),
                item["status"],
            ),
        )
        conn.commit()
        # Checkpoint WAL to prevent unbounded WAL growth
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def enqueue(
        self,
        video_id: str,
        retry_count: int,
        next_retry_at: datetime,
        last_error: str,
        created_at: datetime,
        status: str = "pending",
    ) -> None:
        """Enqueue a write operation to be processed by the writer thread."""
        self._start_writer()
        self._write_queue.put(
            {
                "video_id": video_id,
                "retry_count": retry_count,
                "next_retry_at": next_retry_at,
                "last_error": last_error,
                "created_at": created_at,
                "status": status,
            }
        )

    def _read_entry(self, video_id: str) -> Optional[RetryEntry]:
        """Read a single entry from the isolated DB."""
        self._ensure_table()
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            """
            SELECT video_id, retry_count, next_retry_at, last_error, created_at, status
            FROM retry_queue
            WHERE video_id = ?
            """,
            (video_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return RetryEntry(
            video_id=row[0],
            retry_count=row[1],
            next_retry_at=datetime.fromisoformat(row[2]),
            last_error=row[3],
            created_at=datetime.fromisoformat(row[4]),
            status=row[5],
        )

    def get(self, video_id: str) -> Optional[RetryEntry]:
        """Get retry entry if exists."""
        return self._read_entry(video_id)

    def get_pending(self, limit: int = 50) -> list[RetryEntry]:
        """Get videos ready for retry (next_retry_at <= now), ordered by created_at."""
        self._ensure_table()
        now = datetime.now().isoformat()
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            """
            SELECT video_id, retry_count, next_retry_at, last_error, created_at, status
            FROM retry_queue
            WHERE status = 'pending' AND next_retry_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, limit),
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            RetryEntry(
                video_id=row[0],
                retry_count=row[1],
                next_retry_at=datetime.fromisoformat(row[2]),
                last_error=row[3],
                created_at=datetime.fromisoformat(row[4]),
                status=row[5],
            )
            for row in rows
        ]

    def mark_permanent_failure(self, video_id: str, last_error: str) -> None:
        """Mark a video as permanently failed (no more retries)."""
        self._start_writer()
        self._write_queue.put(
            {
                "video_id": video_id,
                "retry_count": _MAX_RETRIES,
                "next_retry_at": datetime.now(),
                "last_error": last_error,
                "created_at": datetime.now(),
                "status": "permanent_failure",
            }
        )


def _get_storage(terminal_id: str) -> _RetryStorage:
    """Get or create retry storage for terminal."""
    with _storage_lock:
        if terminal_id not in _retry_storages:
            _retry_storages[terminal_id] = _RetryStorage(terminal_id)
        return _retry_storages[terminal_id]


def clear_all_storages() -> None:
    """Stop all writer threads and clear in-memory retry storages.

    Used by test fixtures to ensure a clean state between tests.
    """
    with _storage_lock:
        for storage in _retry_storages.values():
            storage._stop()
        _retry_storages.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue_retry(
    video_id: str,
    error: str,
    retry_count: int = 0,
) -> bool:
    """Enqueue a video for retry with exponential backoff.

    Call this when a video analysis fails.

    Args:
        video_id: YouTube video ID (must be 11 chars)
        error: Error message from the last attempt
        retry_count: Current retry count (0 = first attempt)

    Returns:
        True if enqueued, False if permanently failed or max retries exceeded.
    """
    if not _validate_video_id(video_id):
        return False

    next_retry_count = retry_count + 1
    if next_retry_count > _MAX_RETRIES:
        # Mark permanent failure
        from csf.terminal_context import resolve_tid

        storage = _get_storage(resolve_tid())
        storage.mark_permanent_failure(video_id, error)
        return False

    # Exponential backoff with jitter (LOGIC-007 fix)
    delay_minutes = min(_BACKOFF_BASE_MINUTES * (2**retry_count), _BACKOFF_CAP_MINUTES)
    jitter_minutes = random.uniform(0, delay_minutes * 0.1)  # 0-10% jitter
    next_retry_at = datetime.now() + timedelta(minutes=delay_minutes + jitter_minutes)

    from csf.terminal_context import resolve_tid

    terminal_id = resolve_tid()
    storage = _get_storage(terminal_id)
    storage.enqueue(
        video_id=video_id,
        retry_count=next_retry_count,
        next_retry_at=next_retry_at,
        last_error=error,
        created_at=datetime.now(),
        status="pending",
    )
    return True


def get_pending_retries(limit: int = 50) -> list[RetryEntry]:
    """Get videos that are ready for retry (backoff elapsed)."""
    from csf.terminal_context import resolve_tid

    storage = _get_storage(resolve_tid())
    return storage.get_pending(limit=limit)


def get_retry_entry(video_id: str) -> Optional[RetryEntry]:
    """Get retry entry for a specific video."""
    from csf.terminal_context import resolve_tid

    storage = _get_storage(resolve_tid())
    return storage.get(video_id)
