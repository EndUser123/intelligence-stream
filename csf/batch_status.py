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
from typing import Literal
from collections.abc import Sequence
from dataclasses import dataclass, asdict

# Type alias for batch entries - use dataclass for extensibility
@dataclass
class BatchEntry:
    video_id: str
    status: Literal["pending", "complete", "failed"]
    source: str | None = None
    published_at: str | None = None
    has_captions: bool | None = None
    title: str | None = None
    description: str | None = None
    channel_id: str | None = None
    thumbnail: str | None = None
    duration: int | None = None
    privacy_status: str | None = None
    upload_status: str | None = None
    is_live_content: bool | None = None
    unavailable_reason: str | None = None
    last_stage: str | None = None  # Which fetch stage succeeded
    failure_reason: str | None = None  # Why it failed

    def to_tuple(self) -> tuple:
        """Convert to tuple for backward compatibility."""
        return (
            self.video_id,
            self.status,
            self.source,
            self.published_at,
            self.has_captions,
            self.title,
            self.description,
            self.channel_id,
            self.thumbnail,
            self.duration,
            self.privacy_status,
            self.upload_status,
            self.is_live_content,
            self.unavailable_reason,
            self.last_stage,
            self.failure_reason,
        )

# Status values
_STATUS_PENDING = "pending"
_STATUS_COMPLETE = "complete"
_STATUS_FAILED = "failed"

# Default DB path — separate from transcript cache and quota DBs
_DEFAULT_DB_DIR = Path("P:/__csf/.data/yt-is")
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
                published_at TEXT,
                has_captions INTEGER
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
        # Migrate existing DBs that predate the has_captions column
        try:
            conn.execute("SELECT has_captions FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN has_captions INTEGER")
        # Migrate additional metadata columns
        try:
            conn.execute("SELECT title FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN title TEXT")
        try:
            conn.execute("SELECT description FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN description TEXT")
        try:
            conn.execute("SELECT channel_id FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN channel_id TEXT")
        try:
            conn.execute("SELECT thumbnail FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN thumbnail TEXT")
        try:
            conn.execute("SELECT duration FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN duration INTEGER DEFAULT 0")
        try:
            conn.execute("SELECT privacy_status FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN privacy_status TEXT DEFAULT 'public'")
        try:
            conn.execute("SELECT upload_status FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN upload_status TEXT")
        try:
            conn.execute("SELECT is_live_content FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN is_live_content INTEGER DEFAULT 0")
        try:
            conn.execute("SELECT unavailable_reason FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN unavailable_reason TEXT")
        # Migrate existing DBs that predate last_stage column (which stage succeeded)
        try:
            conn.execute("SELECT last_stage FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN last_stage TEXT")
        # Migrate existing DBs that predate failure_reason column (why it failed)
        try:
            conn.execute("SELECT failure_reason FROM analysis_status LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE analysis_status ADD COLUMN failure_reason TEXT")
        # Index for get_pending_by_source queries (source, status) — avoids full table scan
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_status_source_status ON analysis_status(source, status)"
        )
        # Checkpoint WAL to prevent unbounded WAL file growth (matches cache.py pattern)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # Migrate existing DBs that predate download_archive and channel_cooldown tables.
        # Uses try/except on a column unique to channel_cooldown to detect absence.
        try:
            conn.execute("SELECT cooldown_until FROM channel_cooldown LIMIT 1")
        except sqlite3.OperationalError:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS download_archive (
                    video_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('success', 'failed', 'skipped', 'attempting')),
                    source TEXT,
                    attempted_at REAL NOT NULL,
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
            pass  # Column already absent or SQLite version doesn't support DROP COLUMN
        conn.close()
        self._ensure_nlm_export_state()
        self._ensure_channel_metadata()
        self._ensure_provider_score()
        self._ensure_channel_blocklist()

    def _ensure_provider_score(self) -> None:
        """Create or migrate provider_score table for failure-aware routing."""
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_score (
                channel_url TEXT NOT NULL,
                provider TEXT NOT NULL,
                successes INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0,
                last_result TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel_url, provider)
            )
            """
        )
        conn.close()

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
                schema_version INTEGER DEFAULT 1,
                -- Full metadata from channels.list API (contentDetails + statistics + snippet)
                channel_title TEXT,
                thumbnail_url TEXT,
                subscriber_count INTEGER,
                view_count INTEGER
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
        # Migration for full metadata columns (channel_title, thumbnail_url, subscriber_count, view_count)
        for col, col_type in [
            ("channel_title", "TEXT"),
            ("thumbnail_url", "TEXT"),
            ("subscriber_count", "INTEGER"),
            ("view_count", "INTEGER"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM channel_metadata LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE channel_metadata ADD COLUMN {col} {col_type}")
        # Migration for extended metadata columns (description, published_at, country)
        for col, col_type in [
            ("description", "TEXT"),
            ("published_at", "TEXT"),
            ("country", "TEXT"),
        ]:
            try:
                conn.execute(f"SELECT {col} FROM channel_metadata LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute(f"ALTER TABLE channel_metadata ADD COLUMN {col} {col_type}")
        # Migration for topic_categories (topicDetails.topicCategories from YouTube API)
        try:
            conn.execute("SELECT topic_categories FROM channel_metadata LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE channel_metadata ADD COLUMN topic_categories TEXT")
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

    # ---------------------------------------------------------------------------
    # provider_score — failure-aware routing
    # ---------------------------------------------------------------------------

    def _record_provider_result(
        self, channel_url: str, provider: str, success: bool
    ) -> None:
        """Record a provider result for a channel.

        Uses BEGIN IMMEDIATE to prevent TOCTOU races with concurrent writers.
        """
        self._ensure_provider_score()
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(timezone.utc).isoformat()
            if success:
                conn.execute(
                    """
                    INSERT INTO provider_score (channel_url, provider, successes, failures, last_result, updated_at)
                    VALUES (?, ?, 1, 0, 'success', ?)
                    ON CONFLICT(channel_url, provider) DO UPDATE SET
                        successes = successes + 1,
                        last_result = 'success',
                        updated_at = excluded.updated_at
                    """,
                    (channel_url, provider, now),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO provider_score (channel_url, provider, successes, failures, last_result, updated_at)
                    VALUES (?, ?, 0, 1, 'failure', ?)
                    ON CONFLICT(channel_url, provider) DO UPDATE SET
                        failures = failures + 1,
                        last_result = 'failure',
                        updated_at = excluded.updated_at
                    """,
                    (channel_url, provider, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_provider_scores(
        self, channel_url: str
    ) -> dict[str, tuple[int, int]]:
        """Get (successes, failures) per provider for a channel.

        Returns {provider: (successes, failures)}. Unknown providers omitted.
        """
        self._ensure_provider_score()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT provider, successes, failures FROM provider_score WHERE channel_url = ?",
            (channel_url,),
        )
        rows = cursor.fetchall()
        conn.close()
        return {row[0]: (row[1], row[2]) for row in rows}

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
        last_stage: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Set status for a video_id with current timestamp and optional source/published_at.

        Uses BEGIN IMMEDIATE to acquire a write lock and prevent TOCTOU races
        between reading the existing source/published_at and writing the new row.

        Args:
            video_id: The YouTube video ID.
            status: One of 'pending', 'complete', 'failed'.
            source: Optional channel URL or source identifier.
            published_at: Optional ISO timestamp of video publish date.
            last_stage: Which fetch stage succeeded ('ytdlp', 'ytdlp_ejs', 'selenium', 'notebooklm').
            failure_reason: Why the video failed ('region_block', 'no_transcript', 'quota_exceeded', etc.).
        """
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Preserve existing source, published_at, last_stage, failure_reason if not provided
            if source is None or published_at is None or last_stage is None or failure_reason is None:
                cursor = conn.execute(
                    "SELECT source, published_at, last_stage, failure_reason FROM analysis_status WHERE video_id = ?",
                    (video_id,),
                )
                row = cursor.fetchone()
                if row:
                    if source is None:
                        source = row[0]
                    if published_at is None:
                        published_at = row[1]
                    if last_stage is None:
                        last_stage = row[2]
                    if failure_reason is None:
                        failure_reason = row[3]
            conn.execute(
                "INSERT OR REPLACE INTO analysis_status (video_id, status, updated_at, source, published_at, last_stage, failure_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (video_id, status, now, source, published_at, last_stage, failure_reason),
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
        channel_title: str | None = None,
        thumbnail_url: str | None = None,
        subscriber_count: int | None = None,
        view_count: int | None = None,
        description: str | None = None,
        published_at: str | None = None,
        country: str | None = None,
        topic_categories: str | None = None,
        keywords: str | None = None,
        custom_url: str | None = None,
    ) -> None:
        """Set channel metadata for channel_url (insert or replace)."""
        self._ensure_channel_metadata()
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO channel_metadata (channel_url, playlist_id, last_checked, last_full_enumeration, video_count_estimate, next_page_token, quota_exhausted_at, schema_version, channel_title, thumbnail_url, subscriber_count, view_count, description, published_at, country, topic_categories, keywords, custom_url) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel_url,
                playlist_id,
                last_checked or now,
                last_full_enumeration,
                video_count_estimate,
                next_page_token,
                quota_exhausted_at,
                channel_title,
                thumbnail_url,
                subscriber_count,
                view_count,
                description,
                published_at,
                country,
                topic_categories,
                keywords,
                custom_url,
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
            # Read existing within the same transaction — all columns including extended metadata
            cursor = conn.execute(
                "SELECT channel_url, playlist_id, video_count_estimate, last_checked, "
                "last_full_enumeration, next_page_token, quota_exhausted_at, "
                "channel_title, thumbnail_url, subscriber_count, view_count, "
                "description, published_at, country, topic_categories "
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
                    "channel_title": None,
                    "thumbnail_url": None,
                    "subscriber_count": None,
                    "view_count": None,
                    "description": None,
                    "published_at": None,
                    "country": None,
                    "keywords": None,
                    "custom_url": None,
                    "topic_categories": None,
                }
                vals.update(kwargs)
                conn.execute(
                    "INSERT INTO channel_metadata "
                    "(channel_url, playlist_id, video_count_estimate, last_checked, "
                    "last_full_enumeration, next_page_token, quota_exhausted_at, "
                    "channel_title, thumbnail_url, subscriber_count, view_count, "
                    "description, published_at, country, keywords, custom_url, "
                    "topic_categories, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        vals["channel_url"],
                        vals["playlist_id"],
                        vals["video_count_estimate"],
                        vals["last_checked"],
                        vals["last_full_enumeration"],
                        vals["next_page_token"],
                        vals["quota_exhausted_at"],
                        vals["channel_title"],
                        vals["thumbnail_url"],
                        vals["subscriber_count"],
                        vals["view_count"],
                        vals["description"],
                        vals["published_at"],
                        vals["country"],
                        vals["keywords"],
                        vals["custom_url"],
                        vals["topic_categories"],
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
                    "channel_title": row[7],
                    "thumbnail_url": row[8],
                    "subscriber_count": row[9],
                    "view_count": row[10],
                    "description": row[11],
                    "published_at": row[12],
                    "country": row[13],
                    "topic_categories": row[14],
                }
                for key in (
                    "playlist_id",
                    "video_count_estimate",
                    "last_checked",
                    "last_full_enumeration",
                    "next_page_token",
                    "quota_exhausted_at",
                    "channel_title",
                    "thumbnail_url",
                    "subscriber_count",
                    "view_count",
                    "description",
                    "published_at",
                    "country",
                    "keywords",
                    "custom_url",
                    "topic_categories",
                ):
                    if key in kwargs:
                        existing[key] = kwargs[key]
                # last_checked always updated to now when upsert is called
                existing["last_checked"] = now
                conn.execute(
                    "UPDATE channel_metadata SET "
                    "playlist_id=?, video_count_estimate=?, last_checked=?, "
                    "last_full_enumeration=?, next_page_token=?, quota_exhausted_at=?, "
                    "channel_title=?, thumbnail_url=?, subscriber_count=?, view_count=?, "
                    "description=?, published_at=?, country=?, keywords=?, custom_url=?, "
                    "topic_categories=? "
                    "WHERE channel_url=?",
                    (
                        existing["playlist_id"],
                        existing["video_count_estimate"],
                        existing["last_checked"],
                        existing["last_full_enumeration"],
                        existing["next_page_token"],
                        existing["quota_exhausted_at"],
                        existing["channel_title"],
                        existing["thumbnail_url"],
                        existing["subscriber_count"],
                        existing["view_count"],
                        existing["description"],
                        existing["published_at"],
                        existing["country"],
                        existing.get("keywords"),
                        existing.get("custom_url"),
                        existing.get("topic_categories"),
                        channel_url,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
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

    # ---------------------------------------------------------------------------
    # channel blocklist
    # ---------------------------------------------------------------------------

    def _ensure_channel_blocklist(self) -> None:
        """Create channel_blocklist table if it doesn't exist."""
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_blocklist (
                channel_url TEXT PRIMARY KEY,
                blocked_at TEXT NOT NULL
            )
            """
        )
        conn.close()

    def block_channel(self, channel_url: str) -> None:
        """Add a channel to the blocklist."""
        self._ensure_channel_blocklist()
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO channel_blocklist (channel_url, blocked_at) VALUES (?, ?)",
            (channel_url, now),
        )
        conn.commit()
        conn.close()

    def unblock_channel(self, channel_url: str) -> bool:
        """Remove a channel from the blocklist. Returns True if it was blocked."""
        self._ensure_channel_blocklist()
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM channel_blocklist WHERE channel_url = ? RETURNING channel_url",
            (channel_url,),
        )
        deleted = cursor.fetchone() is not None
        conn.commit()
        conn.close()
        return deleted

    def is_channel_blocked(self, channel_url: str) -> bool:
        """Check if a channel is on the blocklist."""
        self._ensure_channel_blocklist()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM channel_blocklist WHERE channel_url = ?",
            (channel_url,),
        )
        exists = cursor.fetchone() is not None
        conn.close()
        return exists

    def get_all_blocked_channels(self) -> list[tuple[str, str]]:
        """Return all blocked channels as (channel_url, blocked_at) tuples."""
        self._ensure_channel_blocklist()
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT channel_url, blocked_at FROM channel_blocklist ORDER BY blocked_at DESC"
        )
        rows = cursor.fetchall()
        conn.close()
        return rows

    def delete_channel(self, channel_url: str) -> bool:
        """Delete a channel and all its video entries. Returns True if deleted."""
        self._ensure_channel_metadata()
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM analysis_status WHERE source = ?", (channel_url,))
            conn.execute("DELETE FROM channel_metadata WHERE channel_url = ?", (channel_url,))
            conn.commit()
            deleted = True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return deleted

    def get_entries_for_source(
        self, channel_url: str
    ) -> list[tuple[str, str, bool | None]]:
        """Get all entries for a channel/source.

        Returns list of (video_id, status, has_captions) tuples.
        Used by csf-transcript-fetch to avoid re-enumerating via yt-dlp.
        """
        # Normalize URL for query consistency
        channel_url = _normalize_channel_url(channel_url)
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT video_id, status, has_captions FROM analysis_status WHERE source = ?",
            (channel_url,),
        )
        rows = cursor.fetchall()
        conn.close()
        return [(r[0], r[1], r[2]) for r in rows]

    def set_status_batch(self, entries: Sequence[BatchEntry]) -> int:
        """Bulk insert/update status for multiple videos — best-effort.

        Uses a regular BEGIN (not IMMEDIATE) so readers are not blocked.
        Each entry is wrapped in try/except: if one fails, the others still
        succeed. Use busy_timeout PRAGMA to handle writer-writer contention.

        Args:
            entries: List of BatchEntry dataclass objects.

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
            for entry in entries:
                try:
                    # Handle both tuple (backward compat) and dataclass
                    if isinstance(entry, tuple):
                        # Unpack tuple for backward compatibility
                        video_id, status, source, published_at, has_captions, *rest = entry
                        # Set optional fields from rest if available
                        title = rest[0] if len(rest) > 0 else None
                        description = rest[1] if len(rest) > 1 else None
                        channel_id = rest[2] if len(rest) > 2 else None
                        thumbnail = rest[3] if len(rest) > 3 else None
                        duration = rest[4] if len(rest) > 4 else None
                        privacy_status = rest[5] if len(rest) > 5 else None
                        upload_status = rest[6] if len(rest) > 6 else None
                        is_live_content = rest[7] if len(rest) > 7 else None
                        unavailable_reason = rest[8] if len(rest) > 8 else None
                    else:
                        # Use dataclass fields
                        video_id = entry.video_id
                        status = entry.status
                        source = entry.source
                        published_at = entry.published_at
                        has_captions = entry.has_captions
                        title = entry.title
                        description = entry.description
                        channel_id = entry.channel_id
                        thumbnail = entry.thumbnail
                        duration = entry.duration
                        privacy_status = entry.privacy_status
                        upload_status = entry.upload_status
                        is_live_content = entry.is_live_content
                        unavailable_reason = entry.unavailable_reason
                        last_stage = entry.last_stage
                        failure_reason = entry.failure_reason

                    # Preserve existing values if not provided
                    if (source is None or published_at is None or has_captions is None
                            or last_stage is None or failure_reason is None):
                        row = conn.execute(
                            "SELECT source, published_at, has_captions, last_stage, failure_reason FROM analysis_status WHERE video_id = ?",
                            (video_id,),
                        ).fetchone()
                        if row:
                            if source is None:
                                source = row[0]
                            if published_at is None:
                                published_at = row[1]
                            if has_captions is None:
                                has_captions = row[2]
                            if last_stage is None:
                                last_stage = row[3]
                            if failure_reason is None:
                                failure_reason = row[4]

                    conn.execute(
                        "INSERT OR REPLACE INTO analysis_status "
                        "(video_id, status, updated_at, source, published_at, has_captions, "
                        "title, description, channel_id, thumbnail, duration, privacy_status, upload_status, "
                        "is_live_content, unavailable_reason, last_stage, failure_reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            video_id, status, now, source, published_at, has_captions,
                            title, description, channel_id, thumbnail, duration,
                            privacy_status, upload_status, is_live_content, unavailable_reason,
                            last_stage, failure_reason,
                        ),
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
    last_stage: str | None = None,
    failure_reason: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Set status for a video_id with current timestamp and optional source/published_at.

    Args:
        video_id: The YouTube video ID.
        status: One of 'pending', 'complete', 'failed'.
        source: Optional channel URL or source identifier for attribution.
        published_at: Optional ISO timestamp of video publish date (for gap detection).
        last_stage: Which fetch stage succeeded ('ytdlp', 'ytdlp_ejs', 'selenium', 'notebooklm').
        failure_reason: Why the video failed ('region_block', 'no_transcript', 'quota_exceeded', etc.).
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, status, source=source, published_at=published_at,
            last_stage=last_stage, failure_reason=failure_reason,
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, status, source=source, published_at=published_at,
            last_stage=last_stage, failure_reason=failure_reason,
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
    last_stage: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Mark video_id as successfully analyzed, optionally attributing a source.

    Args:
        video_id: The YouTube video ID.
        source: Optional channel URL or source identifier for attribution.
        published_at: Optional ISO timestamp of video publish date (for gap detection).
        last_stage: Which fetch stage succeeded ('ytdlp', 'ytdlp_ejs', 'selenium', 'notebooklm').
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, _STATUS_COMPLETE, source=source, published_at=published_at,
            last_stage=last_stage,
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, _STATUS_COMPLETE, source=source, published_at=published_at,
            last_stage=last_stage,
        )


def mark_failed(
    video_id: str,
    published_at: str | None = None,
    failure_reason: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Mark video_id as failed (retry allowed on restart).

    Args:
        video_id: The YouTube video ID.
        published_at: Optional ISO timestamp of video publish date.
        failure_reason: Why the video failed ('region_block', 'no_transcript', 'quota_exceeded', etc.).
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage().set_status(
            video_id, _STATUS_FAILED, published_at=published_at,
            failure_reason=failure_reason,
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_status(
            video_id, _STATUS_FAILED, published_at=published_at,
            failure_reason=failure_reason,
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
    channel_title: str | None = None,
    thumbnail_url: str | None = None,
    subscriber_count: int | None = None,
    view_count: int | None = None,
    description: str | None = None,
    published_at: str | None = None,
    country: str | None = None,
    topic_categories: str | None = None,
    keywords: str | None = None,
    custom_url: str | None = None,
) -> None:
    """Set channel metadata for channel_url (insert or replace)."""
    if db_path is None:
        _get_batch_status_storage().set_channel_metadata(
            channel_url,
            playlist_id=playlist_id,
            last_checked=last_checked,
            last_full_enumeration=last_full_enumeration,
            video_count_estimate=video_count_estimate,
            channel_title=channel_title,
            thumbnail_url=thumbnail_url,
            subscriber_count=subscriber_count,
            view_count=view_count,
            description=description,
            published_at=published_at,
            country=country,
            topic_categories=topic_categories,
            keywords=keywords,
            custom_url=custom_url,
        )
    else:
        _BatchStatusStorage(db_path=db_path).set_channel_metadata(
            channel_url,
            playlist_id=playlist_id,
            last_checked=last_checked,
            last_full_enumeration=last_full_enumeration,
            video_count_estimate=video_count_estimate,
            channel_title=channel_title,
            thumbnail_url=thumbnail_url,
            subscriber_count=subscriber_count,
            view_count=view_count,
            description=description,
            published_at=published_at,
            country=country,
            topic_categories=topic_categories,
            keywords=keywords,
            custom_url=custom_url,
        )


def _normalize_channel_url(url: str) -> str:
    """Normalize channel URL to remove /channel/ prefix for consistency.

    Ensures @handle URLs are stored as https://www.youtube.com/@handle
    without the /channel/ prefix.
    """
    if "/channel/@" in url:
        return url.replace("/channel/@", "/@")
    return url


def upsert_channel(
    channel_url: str, db_path: Path | None = None, **kwargs: str | int | None
) -> None:
    """Upsert channel metadata, updating only provided fields."""
    # Normalize URL before storing
    channel_url = _normalize_channel_url(channel_url)
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


# ---------------------------------------------------------------------------
# channel blocklist public API
# ---------------------------------------------------------------------------


def block_channel(channel_url: str, db_path: Path | None = None) -> None:
    """Add a channel to the blocklist."""
    if db_path is None:
        _get_batch_status_storage().block_channel(channel_url)
    else:
        _BatchStatusStorage(db_path=db_path).block_channel(channel_url)


def unblock_channel(channel_url: str, db_path: Path | None = None) -> bool:
    """Remove a channel from the blocklist. Returns True if it was blocked."""
    if db_path is None:
        return _get_batch_status_storage().unblock_channel(channel_url)
    return _BatchStatusStorage(db_path=db_path).unblock_channel(channel_url)


def is_channel_blocked(channel_url: str, db_path: Path | None = None) -> bool:
    """Check if a channel is on the blocklist."""
    if db_path is None:
        return _get_batch_status_storage().is_channel_blocked(channel_url)
    return _BatchStatusStorage(db_path=db_path).is_channel_blocked(channel_url)


def get_all_blocked_channels(db_path: Path | None = None) -> list[tuple[str, str]]:
    """Return all blocked channels as (channel_url, blocked_at) tuples."""
    if db_path is None:
        return _get_batch_status_storage().get_all_blocked_channels()
    return _BatchStatusStorage(db_path=db_path).get_all_blocked_channels()


def delete_channel(channel_url: str, db_path: Path | None = None) -> bool:
    """Delete a channel and all its video entries. Returns True if deleted."""
    if db_path is None:
        return _get_batch_status_storage().delete_channel(channel_url)
    return _BatchStatusStorage(db_path=db_path).delete_channel(channel_url)


def get_entries_for_source(
    channel_url: str, db_path: Path | None = None
) -> list[tuple[str, str, bool | None]]:
    """Get all entries for a channel/source.

    Returns list of (video_id, status, has_captions) tuples.
    Used by csf-transcript-fetch to avoid re-enumerating via yt-dlp.
    """
    if db_path is None:
        return _get_batch_status_storage().get_entries_for_source(channel_url)
    return _BatchStatusStorage(db_path=db_path).get_entries_for_source(channel_url)


def set_status_batch(
    entries: Sequence["BatchEntry"],
    db_path: Path | None = None,
) -> int:
    """Bulk insert/update status for multiple videos — best-effort.

    Each entry is tried individually; malformed entries are skipped without
    rolling back successful ones. Uses busy_timeout to handle writer contention.

    Args:
        entries: Sequence of (video_id, status, source, published_at, has_captions) tuples.
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


def get_nlm_export_state(composite_id: str, db_path: Path | None = None) -> dict | None:
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


def get_nlm_exports_by_video(video_id: str, db_path: Path | None = None) -> list[dict]:
    """Get all nlm_export_state rows that contain a given video_id.

    Used to check if a video is already part of a composite.

    Returns list of dicts (same schema as get_nlm_export_state).
    """
    if db_path is None:
        return _get_batch_status_storage()._get_nlm_exports_by_video(video_id)
    return _BatchStatusStorage(db_path=db_path)._get_nlm_exports_by_video(video_id)


# ---------------------------------------------------------------------------
# provider_score public API — failure-aware routing
# ---------------------------------------------------------------------------

def record_provider_result(
    channel_url: str,
    provider: str,
    success: bool,
    db_path: Path | None = None,
) -> None:
    """Record a provider result for a channel, supporting failure-aware routing.

    Args:
        channel_url: The channel (source) this video belongs to.
        provider: Provider name ('gemini_sdk', 'ocr_clip', 'transcript').
        success: True if the provider succeeded, False if it fell through.
        db_path: Optional path to a non-default batch_status DB.
    """
    if db_path is None:
        _get_batch_status_storage()._record_provider_result(channel_url, provider, success)
    else:
        _BatchStatusStorage(db_path=db_path)._record_provider_result(channel_url, provider, success)


def get_provider_scores(
    channel_url: str, db_path: Path | None = None
) -> dict[str, tuple[int, int]]:
    """Get success/failure counts for each provider for a channel.

    Args:
        channel_url: The channel (source) to look up.
        db_path: Optional path to a non-default batch_status DB.

    Returns:
        Dict mapping provider name -> (successes, failures).
        Providers with no record return (0, 0).
    """
    if db_path is None:
        return _get_batch_status_storage()._get_provider_scores(channel_url)
    return _BatchStatusStorage(db_path=db_path)._get_provider_scores(channel_url)
