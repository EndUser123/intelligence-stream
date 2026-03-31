"""Transcript caching module for intelligence-stream package.

Caches YouTube transcripts in a shared SQLite database.
All terminals can read any cached transcript; writes go to a shared DB.
Transcripts are immutable - once cached, they don't expire.
"""

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Validation
_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Shared transcript cache DB (all terminals share the same pool)
# Stored in __csf/.data alongside other CSF runtime data
_SHARED_DB_PATH = Path(
    "P:/__csf/.data/intelligence-stream/transcripts/transcripts.sqlite"
)

# Per-terminal in-memory index of what's cached locally (optional read cache)
_cache_storages: dict[str, "_CacheStorage"] = {}
_storage_lock = threading.Lock()


@dataclass
class TranscriptCache:
    """Cache entry for a YouTube video transcript."""

    video_id: str  # Must be 11 chars, alphanumeric + hyphen/underscore
    lang: str  # ISO 639-1 language code
    source: str  # 'cli' | 'youtube_transcript_api' | 'youtubei' | 'sdk'
    transcript: str
    cached_at: datetime
    terminal_id: str  # Which terminal wrote this entry


class _CacheStorage:
    """Internal SQLite storage for transcript cache.

    Uses WAL mode for concurrent reads and synchronous writes.
    All terminals share the same DB.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id

    def _ensure_table(self) -> None:
        """Create cache table if not exists in shared DB."""
        _SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_cache (
                cache_key TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                lang TEXT NOT NULL,
                source TEXT NOT NULL,
                transcript TEXT NOT NULL,
                cached_at TEXT NOT NULL,
                terminal_id TEXT NOT NULL
            )
            """
        )
        # Index on video_id for has_cached_transcript lookups (batch pre-check)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcript_cache_video_id ON transcript_cache(video_id)"
        )
        conn.close()

    def _write_entry(self, cache_key: str, video_id: str, lang: str, source: str, transcript: str, cached_at: datetime) -> None:
        """Write a single entry to the database synchronously."""
        self._ensure_table()
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO transcript_cache
                (cache_key, video_id, lang, source, transcript, cached_at, terminal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    video_id,
                    lang,
                    source,
                    transcript,
                    cached_at.isoformat(),
                    self._terminal_id,
                ),
            )
            conn.commit()
            # Checkpoint WAL to prevent unbounded WAL file growth (SEC-004)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def enqueue_write(
        self,
        cache_key: str,
        video_id: str,
        lang: str,
        source: str,
        transcript: str,
        cached_at: datetime,
    ) -> None:
        """Write a transcript entry to the database synchronously."""
        self._write_entry(cache_key, video_id, lang, source, transcript, cached_at)

    def _read_entry(self, cache_key: str) -> Optional[TranscriptCache]:
        """Read a single entry from the shared database."""
        self._ensure_table()
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id
            FROM transcript_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return TranscriptCache(
            video_id=row[0],
            lang=row[1],
            source=row[2],
            transcript=row[3],
            cached_at=datetime.fromisoformat(row[4]),
            terminal_id=row[5],
        )

    def get(self, cache_key: str) -> Optional[TranscriptCache]:
        """Get cache entry if exists."""
        return self._read_entry(cache_key)


def _get_storage(terminal_id: str) -> _CacheStorage:
    """Get or create cache storage for terminal."""
    with _storage_lock:
        if terminal_id not in _cache_storages:
            _cache_storages[terminal_id] = _CacheStorage(terminal_id)
        return _cache_storages[terminal_id]


def clear_all_storages() -> None:
    """Clear all in-memory cache storages.

    Used by test fixtures to ensure a clean state between tests.
    """
    with _storage_lock:
        _cache_storages.clear()


def _make_cache_key(video_id: str, lang: str, source: str) -> str:
    """Build cache key from components. Terminal-agnostic for sharing."""
    return f"{video_id}:{lang}:{source}"


def _validate_video_id(video_id: str) -> bool:
    """Validate video_id format.

    Returns True if valid (11 chars, alphanumeric + hyphen/underscore).
    Returns False otherwise.
    """
    return bool(_VIDEO_ID_PATTERN.match(video_id))


def get_cached_transcript(
    video_id: str, lang: str, source: str
) -> Optional[TranscriptCache]:
    """Get cached transcript if exists.

    Reads from the shared transcript pool - any terminal's cached
    transcripts are visible to all terminals.

    Args:
        video_id: YouTube video ID (must be 11 chars)
        lang: ISO 639-1 language code
        source: Transcript source ('cli', 'youtube_transcript_api', 'youtubei', 'sdk')

    Returns:
        TranscriptCache if found, None otherwise.
        Returns None for invalid video_id without raising.
    """
    if not _validate_video_id(video_id):
        return None

    from csf.terminal_context import resolve_tid

    terminal_id = resolve_tid()
    cache_key = _make_cache_key(video_id, lang, source)
    storage = _get_storage(terminal_id)
    return storage.get(cache_key)


def set_cached_transcript(
    video_id: str, lang: str, source: str, transcript: str
) -> None:
    """Cache a transcript entry.

    Writes to the shared transcript pool - all terminals can read it.

    Args:
        video_id: YouTube video ID (must be 11 chars)
        lang: ISO 639-1 language code
        source: Transcript source ('cli', 'youtube_transcript_api', 'youtubei', 'sdk')
        transcript: The transcript text to cache

    Raises:
        Silently ignored for invalid video_id (no exception raised).
    """
    if not _validate_video_id(video_id):
        return

    from csf.terminal_context import resolve_tid

    terminal_id = resolve_tid()
    cache_key = _make_cache_key(video_id, lang, source)
    now = datetime.now()

    storage = _get_storage(terminal_id)
    storage.enqueue_write(
        cache_key=cache_key,
        video_id=video_id,
        lang=lang,
        source=source,
        transcript=transcript,
        cached_at=now,
    )


def list_cached_transcripts(lang: str | None = None) -> list[TranscriptCache]:
    """List all cached transcripts, optionally filtered by language.

    Args:
        lang: ISO 639-1 language code to filter by. None means all languages.

    Returns:
        List of TranscriptCache entries.
    """
    _SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_SHARED_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    if lang:
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id
            FROM transcript_cache
            WHERE lang = ?
            ORDER BY cached_at DESC
            """,
            (lang,),
        )
    else:
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id
            FROM transcript_cache
            ORDER BY cached_at DESC
            """
        )
    rows = cursor.fetchall()
    conn.close()
    return [
        TranscriptCache(
            video_id=row[0],
            lang=row[1],
            source=row[2],
            transcript=row[3],
            cached_at=datetime.fromisoformat(row[4]),
            terminal_id=row[5],
        )
        for row in rows
    ]


def has_cached_transcript(video_id: str) -> bool:
    """Check whether any transcript is cached for a given video_id.

    This is a fast existence check that does not require knowing the
    language or source — any cached entry for this video_id qualifies.

    Args:
        video_id: YouTube video ID (must be 11 chars).

    Returns:
        True if at least one transcript exists for this video_id, False otherwise.
    """
    if not _validate_video_id(video_id):
        return False
    _SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_SHARED_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    # Ensure table exists before querying (may not exist on first use)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_cache (
            cache_key TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            lang TEXT NOT NULL,
            source TEXT NOT NULL,
            transcript TEXT NOT NULL,
            cached_at TEXT NOT NULL,
            terminal_id TEXT NOT NULL
        )
        """
    )
    cursor = conn.execute(
        "SELECT 1 FROM transcript_cache WHERE video_id = ? LIMIT 1",
        (video_id,),
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists
