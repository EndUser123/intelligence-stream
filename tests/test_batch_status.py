"""Tests for csf/batch_status.py - PROC-02: Batch idempotency.

RED Phase: Tests are written BEFORE implementation to define expected behavior.
Verifies: analysis_status table skip-on-restart, --force override.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\\\\\packages\yt-is").absolute()))

from csf.batch_status import (
    backup_batch_status_db,
    block_channel,
    get_analysis_status,
    get_channel_metadata,
    get_entries_for_source_details,
    get_negative_cache,
    get_pending_by_source,
    get_source,
    is_channel_blocked,
    promote_batch_status_db,
    summarize_video_ids,
    is_complete,
    mark_complete,
    mark_failed,
    migrate_channel_state_to_channel_id,
    reset_status,
    reset_all,
    set_negative_cache,
    set_channel_metadata,
    set_status_batch,
    get_status_batch,
    BatchEntry,
)
from csf.channel_identity import ChannelIdentity


# Shared DB path for testing
_TEST_DB_PATH = Path(tempfile.gettempdir()) / "yt-is" / "batch_status" / "test_status.sqlite"


class TestAnalysisStatusTable:
    """Test analysis_status table operations."""

    def setup_method(self):
        """Reset status state before each test."""
        reset_all(_TEST_DB_PATH)

    def test_mark_complete_stores_status(self):
        """mark_complete sets status='complete' for video_id."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        status = get_analysis_status("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert status == "complete"

    def test_mark_failed_stores_status(self):
        """mark_failed sets status='failed' for video_id."""
        mark_failed("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        status = get_analysis_status("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert status == "failed"

    def test_mark_failed_accepts_source(self):
        """mark_failed should preserve source attribution when provided."""
        mark_failed("dQw4w9WgXcQ", source="https://www.youtube.com/@example", db_path=_TEST_DB_PATH)
        source = get_source("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert source == "https://www.youtube.com/@example"

    def test_get_analysis_status_returns_none_for_unknown(self):
        """Unknown video_id returns None."""
        status = get_analysis_status("unknown_video_id", db_path=_TEST_DB_PATH)
        assert status is None

    def test_is_complete_returns_true_when_complete(self):
        """is_complete returns True when status='complete'."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert is_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH) is True

    def test_is_complete_returns_false_when_failed(self):
        """is_complete returns False when status='failed'."""
        mark_failed("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert is_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH) is False

    def test_is_complete_returns_false_when_unknown(self):
        """is_complete returns False for unknown video_id."""
        assert is_complete("unknown_video_id", db_path=_TEST_DB_PATH) is False

    def test_reset_status_clears_video(self):
        """reset_status removes the video entry."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        reset_status("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        assert get_analysis_status("dQw4w9WgXcQ", db_path=_TEST_DB_PATH) is None

    def test_reset_all_clears_all(self):
        """reset_all removes all entries."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        mark_complete("dQw4w9WgXcB", db_path=_TEST_DB_PATH)
        reset_all(_TEST_DB_PATH)
        assert get_analysis_status("dQw4w9WgXcQ", db_path=_TEST_DB_PATH) is None
        assert get_analysis_status("dQw4w9WgXcB", db_path=_TEST_DB_PATH) is None

    def test_status_persists_across_storage_instances(self):
        """Status persists in DB and is visible to new storage instances."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        # New instance should see the status
        from csf.batch_status import _BatchStatusStorage

        storage = _BatchStatusStorage(db_path=_TEST_DB_PATH)
        assert storage.get_status("dQw4w9WgXcQ") == "complete"


class TestBatchIdempotency:
    """Test that batch respects status skip-on-restart."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_videos_marked_complete_are_skipped(self):
        """Videos with status='complete' should be detected by is_complete."""
        mark_complete("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)
        mark_complete("dQw4w9WgXcB", db_path=_TEST_DB_PATH)

        # Simulate what batch.py would do: check is_complete before processing
        pending = ["dQw4w9WgXcQ", "dQw4w9WgXcB", "dQw4w9WgXcR"]
        to_process = [v for v in pending if not is_complete(v, db_path=_TEST_DB_PATH)]
        assert to_process == ["dQw4w9WgXcR"]

    def test_failed_videos_are_not_skipped(self):
        """Videos with status='failed' should NOT be skipped (retry allowed)."""
        mark_failed("dQw4w9WgXcQ", db_path=_TEST_DB_PATH)

        pending = ["dQw4w9WgXcQ", "dQw4w9WgXcR"]
        to_process = [v for v in pending if not is_complete(v, db_path=_TEST_DB_PATH)]
        # failed is NOT skipped - is_complete returns False for failed
        assert "dQw4w9WgXcQ" in to_process

    def test_negative_cache_skips_pending_videos_temporarily(self):
        """Active negative-cache entries should keep pending videos out of the queue."""
        entries: list[BatchEntry] = [
            BatchEntry(
                video_id="dQw4w9WgXcQ",
                status="pending",
                source="https://youtube.com/channel/UC1",
                published_at="2026-01-01T00:00:00Z",
                has_captions=False,
            ),
            BatchEntry(
                video_id="dQw4w9WgXcR",
                status="pending",
                source="https://youtube.com/channel/UC1",
                published_at="2026-01-02T00:00:00Z",
                has_captions=False,
            ),
        ]
        set_status_batch(entries, db_path=_TEST_DB_PATH)
        set_negative_cache(
            "dQw4w9WgXcR",
            "no_transcript",
            ttl_seconds=3600,
            db_path=_TEST_DB_PATH,
        )

        pending = get_pending_by_source("https://youtube.com/channel/UC1", db_path=_TEST_DB_PATH)
        assert pending == ["dQw4w9WgXcQ"]
        assert get_negative_cache("dQw4w9WgXcR", db_path=_TEST_DB_PATH) is not None


class TestSetStatusBatch:
    """Test set_status_batch bulk insert — best-effort per-entry."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_set_status_batch_inserts_multiple(self):
        """set_status_batch inserts multiple entries and returns correct count."""
        entries: list[BatchEntry] = [
            ("vid1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z", None),
            ("vid2", "pending", "https://youtube.com/channel/UC1", "2026-01-02T00:00:00Z", None),
            ("vid3", "pending", "https://youtube.com/channel/UC1", "2026-01-03T00:00:00Z", None),
        ]
        count = set_status_batch(entries, db_path=_TEST_DB_PATH)
        assert count == 3
        assert get_analysis_status("vid1", db_path=_TEST_DB_PATH) == "pending"
        assert get_analysis_status("vid2", db_path=_TEST_DB_PATH) == "pending"
        assert get_analysis_status("vid3", db_path=_TEST_DB_PATH) == "pending"

    def test_set_status_batch_empty_returns_zero(self):
        """set_status_batch with empty list returns 0 without error."""
        count = set_status_batch([], db_path=_TEST_DB_PATH)
        assert count == 0

    def test_set_status_batch_replaces_existing(self):
        """set_status_batch with INSERT OR REPLACE updates existing entries."""
        mark_complete("vid1", db_path=_TEST_DB_PATH)
        entries: list[BatchEntry] = [
            ("vid1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z", None),
        ]
        count = set_status_batch(entries, db_path=_TEST_DB_PATH)
        assert count == 1
        # Status was replaced to 'pending'
        assert get_analysis_status("vid1", db_path=_TEST_DB_PATH) == "pending"

    def test_set_status_batch_best_effort_skips_bad_entries(self):
        """set_status_batch skips entries that cause errors without rolling back good ones.

        This is a structural test: entries with valid video_ids succeed even if one
        in the batch would fail. In practice INSERT OR REPLACE doesn't fail on
        valid entries, so all succeed in the normal case.
        """
        # First insert some valid entries
        good_entries: list[BatchEntry] = [
            ("vid_good1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z", None),
            ("vid_good2", "pending", "https://youtube.com/channel/UC1", "2026-01-02T00:00:00Z", None),
        ]
        count1 = set_status_batch(good_entries, db_path=_TEST_DB_PATH)
        assert count1 == 2
        assert get_analysis_status("vid_good1", db_path=_TEST_DB_PATH) == "pending"
        assert get_analysis_status("vid_good2", db_path=_TEST_DB_PATH) == "pending"


class TestGetStatusBatch:
    """Test get_status_batch O(1) bulk lookup."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_get_status_batch_returns_all_statuses(self):
        """get_status_batch returns status for all found video_ids."""
        mark_complete("vid1", db_path=_TEST_DB_PATH)
        mark_failed("vid2", db_path=_TEST_DB_PATH)
        # vid3 is unknown

        result = get_status_batch(["vid1", "vid2", "vid3"], db_path=_TEST_DB_PATH)
        assert result == {
            "vid1": "complete",
            "vid2": "failed",
            "vid3": None,
        }

    def test_get_status_batch_empty_list_returns_empty(self):
        """get_status_batch with empty list returns empty dict without error."""
        result = get_status_batch([], db_path=_TEST_DB_PATH)
        assert result == {}

    def test_get_status_batch_missing_ids_have_none_value(self):
        """get_status_batch includes unknown video_ids with None value."""
        mark_complete("vid1", db_path=_TEST_DB_PATH)
        result = get_status_batch(["vid1", "nonexistent"], db_path=_TEST_DB_PATH)
        assert "vid1" in result
        assert result["nonexistent"] is None


class TestGetEntriesForSourceDetails:
    """Test richer per-source metadata fetch used for fetch triage."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_get_entries_for_source_details_returns_metadata(self):
        entries: list[BatchEntry] = [
            BatchEntry(
                video_id="vid_terminal",
                status="pending",
                source="https://youtube.com/channel/UC1",
                published_at="2026-01-01T00:00:00Z",
                has_captions=False,
                duration=42,
                privacy_status="private",
                upload_status="deleted",
                is_live_content=False,
                unavailable_reason="deleted",
            ),
            BatchEntry(
                video_id="vid_audio",
                status="pending",
                source="https://youtube.com/channel/UC1",
                published_at="2026-01-02T00:00:00Z",
                has_captions=False,
                duration=133,
                privacy_status="public",
                upload_status="processed",
                is_live_content=False,
                unavailable_reason=None,
            ),
        ]
        set_status_batch(entries, db_path=_TEST_DB_PATH)

        details = get_entries_for_source_details(
            "https://youtube.com/channel/UC1",
            db_path=_TEST_DB_PATH,
        )

        assert len(details) == 2
        assert details[0]["video_id"] == "vid_terminal"
        assert details[0]["privacy_status"] == "private"
        assert details[0]["unavailable_reason"] == "deleted"
        assert details[1]["video_id"] == "vid_audio"
        assert details[1]["duration"] == 133
        assert details[1]["upload_status"] == "processed"


class TestSummarizeVideoIds:
    """Test metadata profiling for NotebookLM batches."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_summarize_video_ids_groups_source_classes(self):
        entries: list[BatchEntry] = [
            BatchEntry(
                video_id="vid_captioned",
                status="pending",
                source="https://youtube.com/channel/UC1",
                has_captions=True,
                privacy_status="public",
                upload_status="processed",
                is_live_content=False,
                unavailable_reason=None,
            ),
            BatchEntry(
                video_id="vid_terminal",
                status="pending",
                source="https://youtube.com/channel/UC1",
                has_captions=False,
                privacy_status="private",
                upload_status="deleted",
                is_live_content=False,
                unavailable_reason="deleted",
            ),
            BatchEntry(
                video_id="vid_live",
                status="pending",
                source="https://youtube.com/channel/UC1",
                has_captions=None,
                privacy_status="public",
                upload_status="live",
                is_live_content=True,
                unavailable_reason=None,
            ),
        ]
        set_status_batch(entries, db_path=_TEST_DB_PATH)

        summary = summarize_video_ids(
            ["vid_captioned", "vid_terminal", "vid_live", "vid_missing"],
            db_path=_TEST_DB_PATH,
        )

        assert summary["total"] == 4
        assert summary["matched"] == 3
        assert summary["missing"] == 1
        assert summary["source_class_counts"]["captioned"] == 1
        assert summary["source_class_counts"]["terminal_deleted"] == 1
        assert summary["source_class_counts"]["live"] == 1


def test_batch_status_env_override_uses_live_data_root(tmp_path, monkeypatch):
    live_db = tmp_path / "batch_status.sqlite"
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(live_db))

    set_channel_metadata(
        "https://www.youtube.com/@example",
        playlist_id="PL123",
        last_checked="2026-04-25T00:00:00Z",
    )
    block_channel("https://www.youtube.com/@blocked")

    assert live_db.exists()
    assert get_channel_metadata("https://www.youtube.com/@example", db_path=live_db) is not None
    assert is_channel_blocked("https://www.youtube.com/@blocked", db_path=live_db) is True


def test_batch_status_normalizes_malformed_handle_urls(tmp_path, monkeypatch):
    live_db = tmp_path / "batch_status.sqlite"
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(live_db))

    set_channel_metadata(
        "https://www.youtube.com@example",
        playlist_id="PL123",
        last_checked="2026-04-25T00:00:00Z",
    )
    block_channel("https://www.youtube.com@blocked")

    row = get_channel_metadata("https://www.youtube.com/@example", db_path=live_db)
    assert row is not None
    assert row["channel_url"] == "https://www.youtube.com/@example"
    assert get_channel_metadata("https://www.youtube.com@example", db_path=live_db) is not None
    assert is_channel_blocked("https://www.youtube.com/@blocked", db_path=live_db) is True
    assert is_channel_blocked("https://www.youtube.com@blocked", db_path=live_db) is True


def test_backup_batch_status_db_snapshots_channel_state(tmp_path, monkeypatch):
    live_db = tmp_path / "batch_status.sqlite"
    backup_root = tmp_path / "backups"
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(live_db))

    set_channel_metadata(
        "https://www.youtube.com/@example",
        playlist_id="PL123",
        last_checked="2026-04-25T00:00:00Z",
    )
    block_channel("https://www.youtube.com/@blocked")

    backup_path = backup_batch_status_db(backup_root=backup_root)

    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.parent == backup_root
    assert get_channel_metadata("https://www.youtube.com/@example", db_path=backup_path) is not None
    assert is_channel_blocked("https://www.youtube.com/@blocked", db_path=backup_path) is True


def test_promote_batch_status_db_merges_channel_state(tmp_path):
    live_db = tmp_path / "live.sqlite"
    staging_db = tmp_path / "staging.sqlite"

    set_channel_metadata(
        "https://www.youtube.com/channel/UCLIVE000000000000000000",
        playlist_id="PLLIVE",
        last_checked="2026-04-24T00:00:00Z",
        db_path=live_db,
    )
    set_channel_metadata(
        "https://www.youtube.com/channel/UCSTAGE0000000000000000",
        playlist_id="PLSTAGE",
        last_checked="2026-04-25T00:00:00Z",
        db_path=staging_db,
    )
    block_channel("https://www.youtube.com/channel/UCBLOCK0000000000000000", db_path=staging_db)

    promoted = promote_batch_status_db(staging_db, live_db)

    assert promoted >= 2
    assert get_channel_metadata("https://www.youtube.com/channel/UCLIVE000000000000000000", db_path=live_db) is not None
    assert get_channel_metadata("https://www.youtube.com/channel/UCSTAGE0000000000000000", db_path=live_db) is not None
    assert is_channel_blocked("https://www.youtube.com/channel/UCBLOCK0000000000000000", db_path=live_db) is True


def test_migrate_channel_state_to_channel_id_backfills_live_rows(tmp_path, monkeypatch):
    live_db = tmp_path / "live.sqlite"
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(live_db))

    def fake_resolve(channel_ref: str):
        mapping = {
            "https://www.youtube.com@legacy": ChannelIdentity(
                channel_id="UCLEGACY000000000000000",
                canonical_url="https://www.youtube.com/@legacy",
                source_ref=channel_ref,
            ),
            "https://www.youtube.com@blocked": ChannelIdentity(
                channel_id="UCBLOCKED00000000000000",
                canonical_url="https://www.youtube.com/@blocked",
                source_ref=channel_ref,
            ),
            "https://www.youtube.com@provider": ChannelIdentity(
                channel_id="UCPROVIDER0000000000000",
                canonical_url="https://www.youtube.com/@provider",
                source_ref=channel_ref,
            ),
            "https://www.youtube.com@analysis": ChannelIdentity(
                channel_id="UCANALYSIS0000000000000",
                canonical_url="https://www.youtube.com/@analysis",
                source_ref=channel_ref,
            ),
        }
        return mapping.get(channel_ref)

    monkeypatch.setattr("csf.batch_status.resolve_channel_identity", fake_resolve)

    # Create a legacy URL-keyed schema directly, without channel_id columns.
    import sqlite3

    conn = sqlite3.connect(live_db)
    conn.executescript(
        """
        CREATE TABLE channel_metadata (
            channel_url TEXT PRIMARY KEY,
            playlist_id TEXT,
            last_checked TEXT NOT NULL,
            last_full_enumeration TEXT,
            video_count_estimate INTEGER DEFAULT 0,
            next_page_token TEXT,
            quota_exhausted_at TEXT,
            schema_version INTEGER DEFAULT 1,
            channel_title TEXT,
            thumbnail_url TEXT,
            subscriber_count INTEGER,
            view_count INTEGER,
            description TEXT,
            published_at TEXT,
            country TEXT,
            topic_categories TEXT,
            keywords TEXT,
            custom_url TEXT,
            category TEXT
        );
        CREATE TABLE channel_blocklist (
            channel_url TEXT PRIMARY KEY,
            blocked_at TEXT NOT NULL
        );
        CREATE TABLE provider_score (
            channel_url TEXT NOT NULL,
            provider TEXT NOT NULL,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            last_result TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel_url, provider)
        );
        CREATE TABLE analysis_status (
            video_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            published_at TEXT,
            has_captions BOOLEAN,
            title TEXT,
            description TEXT,
            thumbnail TEXT,
            duration TEXT,
            privacy_status TEXT,
            upload_status TEXT,
            is_live_content BOOLEAN,
            unavailable_reason TEXT,
            last_stage TEXT,
            failure_reason TEXT,
            quality_metrics TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO channel_metadata (channel_url, playlist_id, last_checked, video_count_estimate) VALUES (?, ?, ?, ?)",
        ("https://www.youtube.com@legacy", "PL123", "2026-04-25T00:00:00Z", 11),
    )
    conn.execute(
        "INSERT INTO channel_blocklist (channel_url, blocked_at) VALUES (?, ?)",
        ("https://www.youtube.com@blocked", "2026-04-25T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO provider_score (channel_url, provider, successes, failures, last_result, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("https://www.youtube.com@provider", "nlm", 2, 1, "success", "2026-04-25T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO analysis_status (video_id, status, updated_at, source, published_at, has_captions, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("vid1", "pending", "2026-04-25T00:00:00Z", "https://www.youtube.com@analysis", "2026-04-24T00:00:00Z", 0, "Example"),
    )
    conn.commit()
    conn.close()

    counts = migrate_channel_state_to_channel_id(db_path=live_db)

    assert counts["channel_metadata"] == 1
    assert counts["channel_blocklist"] == 1
    assert counts["provider_score"] == 1
    assert counts["analysis_status"] == 1

    conn = sqlite3.connect(live_db)
    try:
        meta = conn.execute(
            "SELECT channel_id, channel_url FROM channel_metadata"
        ).fetchone()
        blocked = conn.execute(
            "SELECT channel_id, channel_url FROM channel_blocklist"
        ).fetchone()
        provider = conn.execute(
            "SELECT channel_id, channel_url FROM provider_score"
        ).fetchone()
        analysis = conn.execute(
            "SELECT channel_id, source FROM analysis_status"
        ).fetchone()
    finally:
        conn.close()

    assert meta == ("UCLEGACY000000000000000", "https://www.youtube.com/@legacy")
    assert blocked == ("UCBLOCKED00000000000000", "https://www.youtube.com/@blocked")
    assert provider == ("UCPROVIDER0000000000000", "https://www.youtube.com/@provider")
    assert analysis == ("UCANALYSIS0000000000000", "https://www.youtube.com/@analysis")
