"""Batch status tracking for idempotent restart — PROC-02.

Stores analysis_status table with (video_id, status, updated_at).
On batch restart, skip videos where status='complete'.
Separate DB from transcript cache and quota tracker (isolation blast radius).

Multi-terminal safe: all terminals share the same DB with WAL mode.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

# Type alias for batch entries: (video_id, status, source, published_at)
BatchEntry = tuple[
    str, Literal["pending", "complete", "failed"], str | None, str | None
]

# Status values
_STATUS_PENDING = "pending"
_STATUS_COMPLETE = "complete"
_STATUS_FAILED = "failed"

# Default DB path — separate from transcript cache and quota DBs
_DEFAULT_DB_DIR = Path("P:/__csf/.data/intelligence-stream/batch_status")
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "batch_status.sqlite"

_storage_lock = threading.Lock()
_batch_status_storage: "_BatchStatusStorage | None" = None


def _get_default_db_path() -> Path:
    """Return the default batch status DB path."""
    return _DEFAULT_DB_PATH


def _get_batch_status_storage() -> "_BatchStatusStorage":
    """Get or create the batch status storage singleton."""
    global _batch_status_storage
    if _batch_status_storage is None:
        with _storage_lock:
            if _batch_status_storage is None:
                _batch_status_storage = _BatchStatusStorage()
    return _batch_status_storage


class _BatchStatusStorage:
    """Thread-safe batch status backed by SQLite with WAL mode."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._conn: sqlite3.Connection | None = None
        self._db_path = db_path or _get_default_db_path()
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create analysis_status and channel_metadata tables, migrate columns if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_status (
                video_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT,
                published_at TEXT
            )
            """
        )
        # Migrate existing DBs that predate the source column
        try:
            conn.execute("SELECT source FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            # source column missing — add it (existing rows get NULL)
            conn.execute("ALTER TABLE analysis_status ADD COLUMN source TEXT")
        # Migrate existing DBs that predate the published_at column
        try:
            conn.execute("SELECT published_at FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN published_at TEXT")
        # Index for get_pending_by_source queries (source, status) — avoids full table scan
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_status_source_status ON analysis_status(source, status)"
        )
        # Checkpoint WAL to prevent unbounded WAL file growth (matches cache.py pattern)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        self._ensure_nlm_export_state()
        self._ensure_channel_metadata()

    def _ensure_channel_metadata(self) -> None:
        """Create or migrate channel_metadata table to current schema.

        Current schema: channel_url, playlist_id, last_checked NOT NULL,
        last_full_enumeration, video_count_estimate DEFAULT 0, next_page_token,
        quota_exhausted_at, schema_version.
        """
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_metadata (
                channel_url TEXT PRIMARY KEY,
                playlist_id TEXT,
                last_checked TEXT NOT NULL,
                last_full_enumeration TEXT,
                video_count_estimate INTEGER DEFAULT 0,
                next_page_token TEXT,
                quota_exhausted_at TEXT,
                schema_version INTEGER DEFAULT 1
            )
            """
        )
        # Migrate pre-existing tables that lack new columns
        try:
            conn.execute("SELECT next_page_token FROM channel_metadata LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE channel_metadata ADD COLUMN next_page_token TEXT")
        try:
            conn.execute("SELECT quota_exhausted_at FROM channel_metadata LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE channel_metadata ADD COLUMN quota_exhausted_at TEXT"
            )
        try:
            conn.execute("SELECT schema_version FROM channel_metadata LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE channel_metadata ADD COLUMN schema_version INTEGER DEFAULT 1"
            )
        conn.close()

    def _ensure_nlm_export_state(self) -> None:
        """Create or migrate nlm_export_state table to current schema.

        Current schema: composite_id (PK), notebook_id, batch_key, video_ids,
        content_hash, word_count, nlm_source_id, created_at, updated_at.
        """
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nlm_export_state (
                composite_id TEXT PRIMARY KEY,
                notebook_id TEXT,
                batch_key TEXT,
                video_ids TEXT,
                content_hash TEXT,
                word_count INTEGER,
                nlm_source_id TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        # Migrate pre-existing tables that lack new columns
        for col, dtype in [
            ("batch_key", "TEXT"),
            ("content_hash", "TEXT"),
            ("word_count", "INTEGER"),
            ("nlm_source_id", "TEXT"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM nlm_export_state LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE nlm_export_state ADD COLUMN {col} {dtype}")
        # Index for get_nlm_exports_by_video queries
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nlm_export_video_ids ON nlm_export_state(video_ids)"
        )
        conn.close()

    def _get_nlm_export_state(self, composite_id: str) -> dict | None:
        """Get nlm_export_state by composite_id. Returns dict or None."""
        self._ensure_nlm_export_state()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT composite_id, notebook_id, batch_key, video_ids, content_hash, "
            "word_count, nlm_source_id, created_at, updated_at "
            "FROM nlm_export_state WHERE composite_id = ?",
            (composite_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "composite_id": row[0],
            "notebook_id": row[1],
            "batch_key": row[2],
            "video_ids": row[3],
            "content_hash": row[4],
            "word_count": row[5],
            "nlm_source_id": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        }

    def _upsert_nlm_export_state(
        self,
        composite_id: str,
        batch_key: str,
        video_ids: str,
        content_hash: str,
        word_count: int,
        notebook_id: str | None = None,
        nlm_source_id: str | None = None,
    ) -> None:
        """Insert or update nlm_export_state.

        Uses BEGIN IMMEDIATE to acquire a write lock and prevent TOCTOU races.
        """
        self._ensure_nlm_export_state()
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Preserve existing notebook_id and nlm_source_id if already set
            cursor = conn.execute(
                "SELECT notebook_id, nlm_source_id FROM nlm_export_state WHERE composite_id = ?",
                (composite_id,),
            )
            row = cursor.fetchone()
            existing_notebook_id = row[0] if row else None
            existing_nlm_source_id = row[1] if row else None
            conn.execute(
                "INSERT OR REPLACE INTO nlm_export_state "
                "(composite_id, notebook_id, batch_key, video_ids, content_hash, "
                "word_count, nlm_source_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    composite_id,
                    notebook_id or existing_notebook_id,
                    batch_key,
                    video_ids,
                    content_hash,
                    word_count,
                    nlm_source_id or existing_nlm_source_id,
                    now,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_pending_nlm_exports(self) -> list[dict]:
        """Get all nlm_export_state rows where notebook_id IS NULL (not yet exported)."""
        self._ensure_nlm_export_state()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT composite_id, notebook_id, batch_key, video_ids, content_hash, "
            "word_count, nlm_source_id, created_at, updated_at "
            "FROM nlm_export_state WHERE notebook_id IS NULL"
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "composite_id": row[0],
                "notebook_id": row[1],
                "batch_key": row[2],
                "video_ids": row[3],
                "content_hash": row[4],
                "word_count": row[5],
                "nlm_source_id": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
            for row in rows
        ]

    def _get_nlm_exports_by_video(self, video_id: str) -> list[dict]:
        """Get all nlm_export_state rows that contain a given video_id."""
        self._ensure_nlm_export_state()
        conn = self._get_conn()
        # video_ids is pipe-delimited; match video_id at start, end, or between pipes
        cursor = conn.execute(
            "SELECT composite_id, notebook_id, batch_key, video_ids, content_hash, "
            "word_count, nlm_source_id, created_at, updated_at "
            "FROM nlm_export_state WHERE video_ids = ? OR video_ids LIKE ? OR video_ids LIKE ? OR video_ids LIKE ?",
            (video_id, f"{video_id}|%", f"%|{video_id}|%", f"%|{video_id}"),
        )
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                "composite_id": row[0],
                "notebook_id": row[1],
                "batch_key": row[2],
                "video_ids": row[3],
                "content_hash": row[4],
                "word_count": row[5],
                "nlm_source_id": row[6],
                "created_at": row[7],
                "updated_at": row[8],
            }
            for row in rows
        ]

    def _get_conn(self) -> sqlite3.Connection:
        """Get a connection to the batch status DB."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def get_status(self, video_id: str) -> str | None:
        """Get status for a video_id. Returns 'complete', 'failed', or None."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT status FROM analysis_status WHERE video_id = ?", (video_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _get_status_batch(self, video_ids: list[str]) -> dict[str, str | None]:
        """Batch lookup of status for multiple video_ids — O(1) single query.

        Returns dict mapping video_id -> status (or None if not found).
        All requested video_ids are included in the result dict.
        """
        if not video_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(video_ids))
        cursor = conn.execute(
            f"SELECT video_id, status FROM analysis_status WHERE video_id IN ({placeholders})",
            video_ids,
        )
        rows = cursor.fetchall()
        conn.close()
        result = {row[0]: row[1] for row in rows}
        # Fill in None for missing IDs to match docstring contract
        for vid in video_ids:
            if vid not in result:
                result[vid] = None
        return result

    def get_source(self, video_id: str) -> str | None:
        """Get source for a video_id. Returns channel URL or None."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT source FROM analysis_status WHERE video_id = ?", (video_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def get_published_at(self, video_id: str) -> str | None:
        """Get published_at for a video_id. Returns ISO timestamp or None."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT published_at FROM analysis_status WHERE video_id = ?", (video_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def set_status(
        self,
        video_id: str,
        status: Literal["pending", "complete", "failed"],
        source: str | None = None,
        published_at: str | None = None,
    ) -> None:
        """Set status for a video_id with current timestamp and optional source/published_at.

        Uses BEGIN IMMEDIATE to acquire a write lock and prevent TOCTOU races
        between reading the existing source/published_at and writing the new row.
        """
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Preserve existing source and published_at if not provided
            if source is None or published_at is None:
                cursor = conn.execute(
                    "SELECT source, published_at FROM analysis_status WHERE video_id = ?",
                    (video_id,),
                )
                row = cursor.fetchone()
                if row:
                    if source is None:
                        source = row[0]
                    if published_at is None:
                        published_at = row[1]
            conn.execute(
                "INSERT OR REPLACE INTO analysis_status (video_id, status, updated_at, source, published_at) VALUES (?, ?, ?, ?, ?)",
                (video_id, status, now, source, published_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def clear_video(self, video_id: str) -> None:
        """Remove entry for a video_id."""
        conn = self._get_conn()
        conn.execute("DELETE FROM analysis_status WHERE video_id = ?", (video_id,))
        conn.commit()
        conn.close()

    def clear_all(self) -> None:
        """Remove all entries."""
        conn = self._get_conn()
        conn.execute("DELETE FROM analysis_status")
        conn.commit()
        conn.close()

    # ---------------------------------------------------------------------------
    # channel_metadata table
    # ---------------------------------------------------------------------------

    def get_channel_metadata(self, channel_url: str) -> dict | None:
        """Get channel metadata by channel_url. Returns dict or None."""
        self._ensure_channel_metadata()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT channel_url, playlist_id, video_count_estimate, last_checked, last_full_enumeration, next_page_token, quota_exhausted_at, schema_version FROM channel_metadata WHERE channel_url = ?",
            (channel_url,),
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return None
        return {
            "channel_url": row[0],
            "playlist_id": row[1],
            "video_count_estimate": row[2],
            "last_checked": row[3],
            "last_full_enumeration": row[4],
            "next_page_token": row[5],
            "quota_exhausted_at": row[6],
            "schema_version": row[7],
        }

    def set_channel_metadata(
        self,
        channel_url: str,
        playlist_id: str | None = None,
        last_checked: str | None = None,
        last_full_enumeration: str | None = None,
        video_count_estimate: int | None = None,
        next_page_token: str | None = None,
        quota_exhausted_at: str | None = None,
    ) -> None:
        """Set channel metadata for channel_url (insert or replace)."""
        self._ensure_channel_metadata()
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO channel_metadata (channel_url, playlist_id, last_checked, last_full_enumeration, video_count_estimate, next_page_token, quota_exhausted_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                channel_url,
                playlist_id,
                last_checked or now,
                last_full_enumeration,
                video_count_estimate,
                next_page_token,
                quota_exhausted_at,
            ),
        )
        conn.commit()
        conn.close()

    def upsert_channel(self, channel_url: str, **kwargs: str | int | None) -> None:
        """Upsert channel metadata, updating only provided fields.

        Uses BEGIN IMMEDIATE to acquire a write lock and prevent TOCTOU races.
        Only updates the fields passed in kwargs; all others are preserved.
        """
        self._ensure_channel_metadata()
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Read existing within the same transaction
            cursor = conn.execute(
                "SELECT channel_url, playlist_id, video_count_estimate, last_checked, "
                "last_full_enumeration, next_page_token, quota_exhausted_at "
                "FROM channel_metadata WHERE channel_url = ?",
                (channel_url,),
            )
            row = cursor.fetchone()

            now = datetime.now(timezone.utc).isoformat()
            if row is None:
                # Insert with defaults for non-provided fields
                vals = {
                    "channel_url": channel_url,
                    "playlist_id": None,
                    "video_count_estimate": None,
                    "last_checked": now,
                    "last_full_enumeration": None,
                    "next_page_token": None,
                    "quota_exhausted_at": None,
                }
                vals.update(kwargs)
                conn.execute(
                    "INSERT INTO channel_metadata "
                    "(channel_url, playlist_id, video_count_estimate, last_checked, "
                    "last_full_enumeration, next_page_token, quota_exhausted_at, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        vals["channel_url"],
                        vals["playlist_id"],
                        vals["video_count_estimate"],
                        vals["last_checked"],
                        vals["last_full_enumeration"],
                        vals["next_page_token"],
                        vals["quota_exhausted_at"],
                    ),
                )
            else:
                # Update only the fields provided in kwargs; preserve rest
                existing = {
                    "channel_url": row[0],
                    "playlist_id": row[1],
                    "video_count_estimate": row[2],
                    "last_checked": row[3],
                    "last_full_enumeration": row[4],
                    "next_page_token": row[5],
                    "quota_exhausted_at": row[6],
                }
                for key in (
                    "playlist_id",
                    "video_count_estimate",
                    "last_checked",
                    "last_full_enumeration",
                    "next_page_token",
                    "quota_exhausted_at",
                ):
                    if key in kwargs:
                        existing[key] = kwargs[key]
                # last_checked always updated to now when upsert is called
                existing["last_checked"] = now
                conn.execute(
                    "UPDATE channel_metadata SET "
                    "playlist_id=?, video_count_estimate=?, last_checked=?, "
                    "last_full_enumeration=?, next_page_token=?, quota_exhausted_at=? "
                    "WHERE channel_url=?",
                    (
                        existing["playlist_id"],
                        existing["video_count_estimate"],
                        existing["last_checked"],
                        existing["last_full_enumeration"],
                        existing["next_page_token"],
                        existing["quota_exhausted_at"],
                        channel_url,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_pending_by_source(self, channel_url: str) -> list[str]:
        """Get all pending video_ids for a given channel/source."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT video_id FROM analysis_status WHERE source = ? AND status = ?",
            (channel_url, _STATUS_PENDING),
        )
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

    def get_newest_published_for_source(self, channel_url: str) -> str | None:
        """Get the most recent published_at timestamp for a channel/source.

        Used for gap detection. Returns the MAX(published_at) across all
        videos from this source, or None if no videos have published_at set.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT MAX(published_at) FROM analysis_status WHERE source = ?",
            (channel_url,),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    def set_status_batch(self, entries: Sequence["BatchEntry"]) -> int:
        """Bulk insert/update status for multiple videos — best-effort.

        Uses a regular BEGIN (not IMMEDIATE) so readers are not blocked.
        Each entry is wrapped in try/except: if one fails, the others still
        succeed. Use busy_timeout PRAGMA to handle writer-writer contention.

        Args:
            entries: List of (video_id, status, source, published_at) tuples.

        Returns:
            Number of rows inserted/updated.
        """
        if not entries:
            return 0
        conn = self._get_conn()
        conn.execute("BEGIN")
        count = 0
        try:
            now = datetime.now(timezone.utc).isoformat()
            for video_id, status, source, published_at in entries:
                try:
                    # Preserve existing source/published_at if not provided
                    if source is None or published_at is None:
                        row = conn.execute(
                            "SELECT source, published_at FROM analysis_status WHERE video_id = ?",
                            (video_id,),
                        ).fetchone()
                        if row:
                            if source is None:
                                source = row[0]
                            if published_at is None:
                                published_at = row[1]
                    conn.execute(
                        "INSERT OR REPLACE INTO analysis_status "
                        "(video_id, status, updated_at, source, published_at) VALUES (?, ?, ?, ?, ?)",
                        (video_id, status, now, source, published_at),
                    )
                    count += 1
                except Exception:
                    # Best-effort: skip bad entries, continue with the rest
                    pass
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_status(
    video_id: str,
    status: Literal["pending", "complete", "failed"],
    source: str | None = None,
    published_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Set status for a video_id with current timestamp and optional source/published_at.

    Args:
        video_id: The YouTube video ID.
        status: One of 'pending', 'complete', 'failed'.
        source: Optional channel URL or source identifier for attribution.
        published_at: Optional ISO timestamp of video publish date (for gap detection).
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, status, source=source, published_at=published_at
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, status, source=source, published_at=published_at
        )


def get_analysis_status(video_id: str, db_path: Path | None = None) -> str | None:
    """Get analysis status for a video_id.

    Returns 'complete', 'failed', or None if not found.
    """
    if db_path is None:
        return _get_batch_status_storage().get_status(video_id)
    return _BatchStatusStorage(db_path=db_path).get_status(video_id)


def is_complete(video_id: str, db_path: Path | None = None) -> bool:
    """Return True if video_id has status='complete'.

    Videos marked 'failed' return False (retry allowed).
    Unknown video IDs return False.
    """
    status = get_analysis_status(video_id, db_path=db_path)
    return status == _STATUS_COMPLETE


def get_status_batch(
    video_ids: list[str],
    db_path: Path | None = None,
) -> dict[str, str | None]:
    """Batch lookup of analysis status for multiple video_ids.

    Returns a dict mapping video_id -> status ('complete', 'failed', or None).
    Uses a single SELECT ... WHERE IN (...) query — O(1) vs O(N) individual calls.

    Args:
        video_ids: List of video IDs to look up.
        db_path: Optional path to a non-default batch_status DB.

    Returns:
        Dict mapping video_id to status string or None.
    """
    if not video_ids:
        return {}
    if db_path is None:
        return _get_batch_status_storage()._get_status_batch(video_ids)
    return _BatchStatusStorage(db_path=db_path)._get_status_batch(video_ids)


def get_source(video_id: str, db_path: Path | None = None) -> str | None:
    """Get the source (channel URL) for a video_id.

    Returns the channel URL if set, or None if not yet attributed.
    """
    if db_path is None:
        return _get_batch_status_storage().get_source(video_id)
    return _BatchStatusStorage(db_path=db_path).get_source(video_id)


def mark_complete(
    video_id: str,
    source: str | None = None,
    published_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Mark video_id as successfully analyzed, optionally attributing a source.

    Args:
        video_id: The YouTube video ID.
        source: Optional channel URL or source identifier for attribution.
        published_at: Optional ISO timestamp of video publish date (for gap detection).
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, _STATUS_COMPLETE, source=source, published_at=published_at
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, _STATUS_COMPLETE, source=source, published_at=published_at
        )


def mark_failed(
    video_id: str, published_at: str | None = None, db_path: Path | None = None
) -> None:
    """Mark video_id as failed (retry allowed on restart)."""
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, _STATUS_FAILED, published_at=published_at
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, _STATUS_FAILED, published_at=published_at
        )


def reset_status(video_id: str, db_path: Path | None = None) -> None:
    """Clear status entry for a specific video_id."""
    if db_path is None:
        _get_batch_status_storage().clear_video(video_id)
    else:
        _BatchStatusStorage(db_path=db_path).clear_video(video_id)


def reset_all(db_path: Path | None = None) -> None:
    """Clear all status entries (for testing only)."""
    if db_path is None:
        _get_batch_status_storage().clear_all()
    else:
        _BatchStatusStorage(db_path=db_path).clear_all()


# ---------------------------------------------------------------------------
# channel_metadata public API
# ---------------------------------------------------------------------------


def get_channel_metadata(channel_url: str, db_path: Path | None = None) -> dict | None:
    """Get channel metadata by channel_url.

    Returns dict with keys: channel_url, playlist_id, video_count_estimate,
    last_checked, last_full_enumeration. Returns None if not found.
    """
    if db_path is None:
        return _get_batch_status_storage().get_channel_metadata(channel_url)
    return _BatchStatusStorage(db_path=db_path).get_channel_metadata(channel_url)


def set_channel_metadata(
    channel_url: str,
    playlist_id: str | None = None,
    last_checked: str | None = None,
    last_full_enumeration: str | None = None,
    video_count_estimate: int | None = None,
    db_path: Path | None = None,
) -> None:
    """Set channel metadata for channel_url (insert or replace)."""
    if db_path is None:
        _get_batch_status_storage().set_channel_metadata(
            channel_url,
            playlist_id=playlist_id,
            last_checked=last_checked,
            last_full_enumeration=last_full_enumeration,
            video_count_estimate=video_count_estimate,
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_channel_metadata(
            channel_url,
            playlist_id=playlist_id,
            last_checked=last_checked,
            last_full_enumeration=last_full_enumeration,
            video_count_estimate=video_count_estimate,
        )


def upsert_channel(
    channel_url: str, db_path: Path | None = None, **kwargs: str | int | None
) -> None:
    """Upsert channel metadata, updating only provided fields."""
    if db_path is None:
        _get_batch_status_storage().upsert_channel(channel_url, **kwargs)
    else:
        _BatchStatusStorage(db_path=db_path).upsert_channel(channel_url, **kwargs)


def get_pending_by_source(channel_url: str, db_path: Path | None = None) -> list[str]:
    """Get all pending video_ids for a given channel/source."""
    if db_path is None:
        return _get_batch_status_storage().get_pending_by_source(channel_url)
    return _BatchStatusStorage(db_path=db_path).get_pending_by_source(channel_url)


def get_newest_published_for_source(
    channel_url: str, db_path: Path | None = None
) -> str | None:
    """Get the most recent published_at timestamp for a channel/source.

    Used for gap detection. Returns the MAX(published_at) across all
    videos from this source, or None if no videos have published_at set.
    """
    if db_path is None:
        return _get_batch_status_storage().get_newest_published_for_source(channel_url)
    return _BatchStatusStorage(db_path=db_path).get_newest_published_for_source(
        channel_url
    )


def set_status_batch(
    entries: Sequence["BatchEntry"],
    db_path: Path | None = None,
) -> int:
    """Bulk insert/update status for multiple videos — best-effort.

    Each entry is tried individually; malformed entries are skipped without
    rolling back successful ones. Uses busy_timeout to handle writer contention.

    Args:
        entries: Sequence of (video_id, status, source, published_at) tuples.
        db_path: Optional path to a non-default batch_status DB.

    Returns:
        Number of rows inserted/updated.
    """
    if db_path is None:
        return _get_batch_status_storage().set_status_batch(entries)
    return _BatchStatusStorage(db_path=db_path).set_status_batch(entries)


# ---------------------------------------------------------------------------
# nlm_export_state public API
# ---------------------------------------------------------------------------


def get_nlm_export_state(
    composite_id: str, db_path: Path | None = None
) -> dict | None:
    """Get nlm_export_state by composite_id.

    Returns dict with keys: composite_id, notebook_id, batch_key, video_ids,
    content_hash, word_count, nlm_source_id, created_at, updated_at.
    Returns None if not found.
    """
    if db_path is None:
        return _get_batch_status_storage()._get_nlm_export_state(composite_id)
    return _BatchStatusStorage(db_path=db_path)._get_nlm_export_state(composite_id)


def upsert_nlm_export_state(
    composite_id: str,
    batch_key: str,
    video_ids: str,
    content_hash: str,
    word_count: int,
    notebook_id: str | None = None,
    nlm_source_id: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert or update nlm_export_state for a composite.

    Uses BEGIN IMMEDIATE to acquire a write lock and prevent TOCTOU races.
    notebook_id and nlm_source_id are preserved if already set and not provided.

    Args:
        composite_id: Hash of (channel_id, sorted video_ids).
        batch_key: Identifier for the batch run that created this composite.
        video_ids: Pipe-delimited video IDs.
        content_hash: Hash of composite content for idempotency.
        word_count: Total word count of the composite.
        notebook_id: NotebookLM notebook ID (set after successful export).
        nlm_source_id: NotebookLM source ID (set after successful export).
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage()._upsert_nlm_export_state(
            composite_id,
            batch_key,
            video_ids,
            content_hash,
            word_count,
            notebook_id=notebook_id,
            nlm_source_id=nlm_source_id,
        )
    else:
        _BatchStatusStorage(db_path=db_path)._upsert_nlm_export_state(
            composite_id,
            batch_key,
            video_ids,
            content_hash,
            word_count,
            notebook_id=notebook_id,
            nlm_source_id=nlm_source_id,
        )


def get_pending_nlm_exports(db_path: Path | None = None) -> list[dict]:
    """Get all nlm_export_state rows where notebook_id IS NULL (not yet exported).

    These are composites that have been built but not yet successfully
    pushed to NotebookLM.

    Returns list of dicts (same schema as get_nlm_export_state).
    """
    if db_path is None:
        return _get_batch_status_storage()._get_pending_nlm_exports()
    return _BatchStatusStorage(db_path=db_path)._get_pending_nlm_exports()


def get_nlm_exports_by_video(
    video_id: str, db_path: Path | None = None
) -> list[dict]:
    """Get all nlm_export_state rows that contain a given video_id.

    Used to check if a video is already part of a composite.

    Returns list of dicts (same schema as get_nlm_export_state).
    """
    if db_path is None:
        return _get_batch_status_storage()._get_nlm_exports_by_video(video_id)
    return _BatchStatusStorage(db_path=db_path)._get_nlm_exports_by_video(video_id)
