"""Transcript caching module for yt-is package.

Caches YouTube transcripts in a shared SQLite database.
All terminals can read any cached transcript; writes go to a shared DB.
Transcripts are immutable - once cached, they don't expire.
"""

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# Validation
_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Shared transcript cache DB (all terminals share the same pool)
# Stored in .data alongside other CSF runtime data
_DEFAULT_SHARED_DB_PATH = Path("P:/.data/yt-is/transcripts.sqlite")
# Backward-compatible alias for callers that import the constant directly.
_SHARED_DB_PATH = _DEFAULT_SHARED_DB_PATH

# Per-terminal in-memory index of what's cached locally (optional read cache)
_cache_storages: dict[str, "_CacheStorage"] = {}
_storage_lock = threading.Lock()
_db_access_lock = threading.RLock()


def _connect_shared_db() -> sqlite3.Connection:
    """Open the shared transcript DB with a conservative lock timeout."""
    return sqlite3.connect(get_shared_db_path(), timeout=30.0)


def get_shared_db_path() -> Path:
    """Return the active transcript cache path.

    Tests may override this with YTIS_TRANSCRIPT_CACHE_DB_PATH so they do not
    touch the live shared cache.
    """
    override = os.environ.get("YTIS_TRANSCRIPT_CACHE_DB_PATH")
    if override:
        return Path(override)
    return _DEFAULT_SHARED_DB_PATH


@dataclass
class TranscriptCache:
    """Cache entry for a YouTube video transcript."""

    video_id: str  # Must be 11 chars, alphanumeric + hyphen/underscore
    lang: str  # ISO 639-1 language code
    source: str  # 'cli' | 'youtube_transcript_api' | 'youtubei' | 'sdk'
    transcript: str
    cached_at: datetime
    terminal_id: str  # Which terminal wrote this entry
    metadata_json: str = "{}"

    @property
    def metadata(self) -> dict[str, Any]:
        """Return parsed transcript metadata, or an empty dict on bad JSON."""
        if not self.metadata_json:
            return {}
        try:
            parsed = json.loads(self.metadata_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


class _CacheStorage:
    """Internal SQLite storage for transcript cache.

    Uses WAL mode for concurrent reads and synchronous writes.
    All terminals share the same DB.
    """

    def __init__(self, terminal_id: str) -> None:
        self._terminal_id = terminal_id

    def _ensure_table(self) -> None:
        """Create cache table if not exists in shared DB."""
        with _db_access_lock:
            db_path = get_shared_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect_shared_db()
            conn.execute("PRAGMA journal_mode=WAL")
            _ensure_transcript_cache_schema(conn)
            conn.commit()
            conn.close()

    def _write_entry(
        self,
        cache_key: str,
        video_id: str,
        lang: str,
        source: str,
        transcript: str,
        cached_at: datetime,
        metadata_json: str,
    ) -> None:
        """Write a single entry to the database synchronously."""
        with _db_access_lock:
            self._ensure_table()
            conn = _connect_shared_db()
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO transcript_cache
                    (cache_key, video_id, lang, source, transcript, metadata_json, cached_at, terminal_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        video_id,
                        lang,
                        source,
                        transcript,
                        metadata_json,
                        cached_at.isoformat(),
                        self._terminal_id,
                    ),
                )
                conn.commit()
                # Checkpoint WAL to prevent unbounded WAL file growth (SEC-004)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()  # Commit checkpoint operation
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
        metadata_json: str,
    ) -> None:
        """Write a transcript entry to the database synchronously."""
        self._write_entry(
            cache_key,
            video_id,
            lang,
            source,
            transcript,
            cached_at,
            metadata_json,
        )

    def _read_entry(self, cache_key: str) -> TranscriptCache | None:
        """Read a single entry from the shared database."""
        self._ensure_table()
        conn = _connect_shared_db()
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id, metadata_json
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
            metadata_json=row[6] if len(row) > 6 and row[6] is not None else "{}",
        )

    def get(self, cache_key: str) -> TranscriptCache | None:
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


def _normalize_metadata(metadata: Mapping[str, Any] | None) -> str:
    """Serialize metadata to a stable JSON string for storage."""
    if metadata is None:
        return "{}"
    try:
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        # Fall back to a stringified wrapper only if the structure is not JSON-safe.
        return json.dumps({"value": str(metadata)}, ensure_ascii=False, sort_keys=True)


def _ensure_transcript_cache_schema(conn: sqlite3.Connection) -> None:
    """Create or migrate the transcript cache schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_cache (
            cache_key TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            lang TEXT NOT NULL,
            source TEXT NOT NULL,
            transcript TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            cached_at TEXT NOT NULL,
            terminal_id TEXT NOT NULL
        )
        """
    )
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transcript_cache)").fetchall()
    }
    if "metadata_json" not in columns:
        conn.execute(
            "ALTER TABLE transcript_cache ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )
    # Index on video_id for has_cached_transcript lookups (batch pre-check)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcript_cache_video_id ON transcript_cache(video_id)"
    )


def get_cached_transcript(
    video_id: str, lang: str, source: str
) -> TranscriptCache | None:
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
    video_id: str,
    lang: str,
    source: str,
    transcript: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Cache a transcript entry.

    Writes to the shared transcript pool - all terminals can read it.

    Args:
        video_id: YouTube video ID (must be 11 chars)
        lang: ISO 639-1 language code
        source: Transcript source ('cli', 'youtube_transcript_api', 'youtubei', 'sdk')
        transcript: The transcript text to cache
        metadata: Optional structured metadata payload to persist losslessly as JSON.

    Raises:
        Silently ignored for invalid video_id (no exception raised).
    """
    if not _validate_video_id(video_id):
        return

    from csf.terminal_context import resolve_tid

    terminal_id = resolve_tid()
    cache_key = _make_cache_key(video_id, lang, source)
    now = datetime.now()
    metadata_json = _normalize_metadata(metadata)

    storage = _get_storage(terminal_id)
    storage.enqueue_write(
        cache_key=cache_key,
        video_id=video_id,
        lang=lang,
        source=source,
        transcript=transcript,
        cached_at=now,
        metadata_json=metadata_json,
    )


def delete_cached_transcripts(video_ids: list[str]) -> int:
    """Delete all cached transcript rows for the given video IDs.

    Returns the number of rows deleted. Invalid video IDs are ignored.
    """
    valid_video_ids = sorted({video_id for video_id in video_ids if _validate_video_id(video_id)})
    if not valid_video_ids:
        return 0
    with _db_access_lock:
        _ensure_db_initialized()
        conn = _connect_shared_db()
        conn.execute("PRAGMA journal_mode=WAL")
        placeholders = ",".join("?" for _ in valid_video_ids)
        cursor = conn.execute(
            f"DELETE FROM transcript_cache WHERE video_id IN ({placeholders})",
            valid_video_ids,
        )
        deleted = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
    return deleted


def promote_transcript_cache(source_db: Path, dest_db: Path | None = None) -> int:
    """Promote transcript rows from a staging DB into the live DB.

    The merge is append-only:
    - existing rows in the destination are preserved
    - source rows are inserted when their cache_key is new
    """
    dest_path = dest_db or get_shared_db_path()
    source_path = Path(source_db)
    if not source_path.exists():
        raise FileNotFoundError(f"source transcript DB does not exist: {source_path}")
    if dest_path.exists() and source_path.resolve() == dest_path.resolve():
        raise ValueError("source and destination transcript DBs must differ")

    with _db_access_lock:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(source_path)
        dest_conn = sqlite3.connect(dest_path)
        try:
            source_conn.execute("PRAGMA journal_mode=WAL")
            dest_conn.execute("PRAGMA journal_mode=WAL")
            _ensure_transcript_cache_schema(source_conn)
            source_conn.commit()
            _ensure_transcript_cache_schema(dest_conn)
            dest_conn.commit()

            dest_conn.execute("ATTACH DATABASE ? AS staging_db", (str(source_path),))
            before = dest_conn.total_changes
            dest_conn.execute(
                """
                INSERT OR IGNORE INTO main.transcript_cache
                    (cache_key, video_id, lang, source, transcript, metadata_json, cached_at, terminal_id)
                SELECT
                    cache_key, video_id, lang, source, transcript, metadata_json, cached_at, terminal_id
                FROM staging_db.transcript_cache
                """
            )
            dest_conn.commit()
            promoted = int(dest_conn.total_changes - before)
            dest_conn.execute("DETACH DATABASE staging_db")
            return promoted
        finally:
            dest_conn.close()
            source_conn.close()


def backup_transcript_cache(backup_root: Path | None = None) -> Path | None:
    """Create a timestamped SQLite backup of the active transcript cache.

    Returns the backup path, or None if the active cache does not exist yet.
    The backup is a consistent SQLite copy, not a filesystem-level copy.
    """
    with _db_access_lock:
        db_path = get_shared_db_path()
        if not db_path.exists():
            return None

        backup_dir = backup_root or db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"transcripts-{stamp}.sqlite"
        suffix = 1
        while backup_path.exists():
            backup_path = backup_dir / f"transcripts-{stamp}-{suffix}.sqlite"
            suffix += 1

        source_conn = sqlite3.connect(db_path)
        dest_conn = sqlite3.connect(backup_path)
        try:
            source_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
            source_conn.close()
        return backup_path


def _ensure_db_initialized() -> None:
    """Ensure database tables exist (shared initialization for read/write paths)."""
    with _db_access_lock:
        db_path = get_shared_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect_shared_db()
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_transcript_cache_schema(conn)
        conn.close()


def list_cached_transcripts(lang: str | None = None) -> list[TranscriptCache]:
    """List all cached transcripts, optionally filtered by language.

    Args:
        lang: ISO 649-1 language code to filter by. None means all languages.

    Returns:
        List of TranscriptCache entries.
    """
    # FIX: Ensure tables exist before querying (prevents empty database bug)
    _ensure_db_initialized()

    conn = _connect_shared_db()
    conn.execute("PRAGMA journal_mode=WAL")
    if lang:
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id, metadata_json
            FROM transcript_cache
            WHERE lang = ?
            ORDER BY cached_at DESC
            """,
            (lang,),
        )
    else:
        cursor = conn.execute(
            """
            SELECT video_id, lang, source, transcript, cached_at, terminal_id, metadata_json
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
            metadata_json=row[6] if len(row) > 6 and row[6] is not None else "{}",
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
    db_path = get_shared_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_shared_db()
    conn.execute("PRAGMA journal_mode=WAL")
    # Ensure table exists before querying (may not exist on first use)
    _ensure_transcript_cache_schema(conn)
    cursor = conn.execute(
        "SELECT 1 FROM transcript_cache WHERE video_id = ? LIMIT 1",
        (video_id,),
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists
