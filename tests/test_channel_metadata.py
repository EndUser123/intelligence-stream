"""Tests for csf/batch_status.py channel_metadata table - CHANGE-002.

Verifies: channel_metadata table creation, set_channel_metadata, get_channel_metadata, upsert_channel.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\\\\\\packages\\yt-is").absolute()))

from csf.batch_status import (
    set_channel_metadata,
    get_channel_metadata,
    upsert_channel,
    reset_all,
    _BatchStatusStorage,
)

# Shared DB path for testing
_TEST_DB_PATH = Path(
    "P:\\\\\\.data/yt-is/batch_status/test_channel_metadata.sqlite"
)


class TestChannelMetadataTable:
    """Test channel_metadata table operations."""

    def setup_method(self):
        """Reset status state before each test."""
        reset_all(_TEST_DB_PATH)

    def test_set_and_get_channel_metadata(self):
        """set_channel_metadata stores data and get_channel_metadata retrieves it."""
        channel_url = "https://youtube.com/channel/UC_TEST1"
        set_channel_metadata(
            channel_url,
            playlist_id="PL123456",
            last_checked="2026-03-28T10:00:00Z",
            last_full_enumeration="2026-03-27T10:00:00Z",
            video_count_estimate=150,
            db_path=_TEST_DB_PATH,
        )
        result = get_channel_metadata(channel_url, db_path=_TEST_DB_PATH)
        assert result is not None
        assert result["channel_url"] == "https://www.youtube.com/channel/UC_TEST1"
        assert result["channel_id"] == "UC_TEST1"
        assert result["playlist_id"] == "PL123456"
        assert result["last_checked"] == "2026-03-28T10:00:00Z"
        assert result["last_full_enumeration"] == "2026-03-27T10:00:00Z"
        assert result["video_count_estimate"] == 150

    def test_get_channel_metadata_returns_none_for_unknown(self):
        """Unknown channel_url returns None."""
        result = get_channel_metadata(
            "https://youtube.com/channel/UC_NOTEXIST", db_path=_TEST_DB_PATH
        )
        assert result is None

    def test_upsert_channel_replaces_existing(self):
        """upsert_channel merges with existing data for the same channel_url."""
        channel_url = "https://youtube.com/channel/UC_TEST3"
        upsert_channel(
            channel_url,
            playlist_id="PL_OLD",
            video_count_estimate=100,
            db_path=_TEST_DB_PATH,
        )
        upsert_channel(
            channel_url,
            playlist_id="PL_NEW",
            video_count_estimate=200,
            db_path=_TEST_DB_PATH,
        )
        result = get_channel_metadata(channel_url, db_path=_TEST_DB_PATH)
        assert result is not None
        assert result["playlist_id"] == "PL_NEW"
        assert result["video_count_estimate"] == 200

    def test_upsert_channel_with_optional_fields(self):
        """upsert_channel stores all optional fields."""
        channel_url = "https://youtube.com/channel/UC_TEST4"
        upsert_channel(
            channel_url,
            playlist_id="PL789",
            last_checked="2026-03-28T12:00:00Z",
            last_full_enumeration="2026-03-26T12:00:00Z",
            video_count_estimate=300,
            next_page_token="TOKEN123",
            quota_exhausted_at="2026-03-28T08:00:00Z",
            db_path=_TEST_DB_PATH,
        )
        result = get_channel_metadata(channel_url, db_path=_TEST_DB_PATH)
        assert result is not None
        assert result["next_page_token"] == "TOKEN123"
        assert result["quota_exhausted_at"] == "2026-03-28T08:00:00Z"

    def test_channel_metadata_table_created_on_first_call(self):
        """Table is created automatically on first set_channel_metadata call."""
        # Use a fresh DB path that doesn't exist yet
        fresh_db_path = Path(
            "P:\\\\\\.data/yt-is/batch_status/test_fresh_channel_metadata.sqlite"
        )
        # Clean up if exists
        if fresh_db_path.exists():
            fresh_db_path.unlink()

        channel_url = "https://youtube.com/channel/UC_FRESH"
        set_channel_metadata(
            channel_url,
            playlist_id="PL_FRESH",
            last_checked="2026-03-28T10:00:00Z",
            db_path=fresh_db_path,
        )
        # Should have created the table and stored data
        result = get_channel_metadata(channel_url, db_path=fresh_db_path)
        assert result is not None
        assert result["channel_url"] == "https://www.youtube.com/channel/UC_FRESH"
        assert result["channel_id"] == "UC_FRESH"

        # Clean up
        if fresh_db_path.exists():
            fresh_db_path.unlink()

    def test_set_channel_metadata_insert_or_replace(self):
        """set_channel_metadata uses INSERT OR REPLACE (upsert behavior)."""
        channel_url = "https://youtube.com/channel/UC_TEST6"
        set_channel_metadata(
            channel_url,
            playlist_id="PL_V1",
            last_checked="2026-03-28T10:00:00Z",
            video_count_estimate=50,
            db_path=_TEST_DB_PATH,
        )
        # Replace with new data
        set_channel_metadata(
            channel_url,
            playlist_id="PL_V2",
            last_checked="2026-03-28T11:00:00Z",
            video_count_estimate=75,
            db_path=_TEST_DB_PATH,
        )
        result = get_channel_metadata(channel_url, db_path=_TEST_DB_PATH)
        assert result is not None
        assert result["playlist_id"] == "PL_V2"
        assert result["video_count_estimate"] == 75
        # Only one row should exist
        all_rows = list(
            _BatchStatusStorage(db_path=_TEST_DB_PATH)
            ._get_conn()
            .execute(
                "SELECT * FROM channel_metadata WHERE channel_id = ?", ("UC_TEST6",)
            )
        )
        assert len(all_rows) == 1

    def test_schema_version_migration_pre_existing_table(self):
        """Migration: pre-existing table without schema_version column gets the column added."""
        # Simulate a pre-existing table by manually creating it without schema_version
        storage = _BatchStatusStorage(db_path=_TEST_DB_PATH)
        conn = storage._get_conn()
        # Drop and recreate without schema_version to simulate old schema
        conn.execute("DROP TABLE IF EXISTS channel_metadata")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_metadata (
                channel_url TEXT PRIMARY KEY,
                playlist_id TEXT,
                last_checked TEXT NOT NULL,
                last_full_enumeration TEXT,
                video_count_estimate INTEGER DEFAULT 0,
                next_page_token TEXT,
                quota_exhausted_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        # Now calling set_channel_metadata should detect old schema and add column
        channel_url = "https://youtube.com/channel/UC_TEST7"
        set_channel_metadata(
            channel_url,
            playlist_id="PL_OLD",
            last_checked="2026-03-28T10:00:00Z",
            db_path=_TEST_DB_PATH,
        )

        # Should work without error and have schema_version
        result = get_channel_metadata(channel_url, db_path=_TEST_DB_PATH)
        assert result is not None
        assert result["channel_url"] == "https://www.youtube.com/channel/UC_TEST7"
        assert result["channel_id"] == "UC_TEST7"

        # Verify schema_version column exists
        conn2 = storage._get_conn()
        cursor = conn2.execute("PRAGMA table_info(channel_metadata)")
        columns = {row[1] for row in cursor.fetchall()}
        conn2.close()
        assert "schema_version" in columns

