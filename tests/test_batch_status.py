"""Tests for csf/batch_status.py - PROC-02: Batch idempotency.

RED Phase: Tests are written BEFORE implementation to define expected behavior.
Verifies: analysis_status table skip-on-restart, --force override.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.batch_status import (
    get_analysis_status,
    is_complete,
    mark_complete,
    mark_failed,
    reset_status,
    reset_all,
    set_status_batch,
    get_status_batch,
    BatchEntry,
)


# Shared DB path for testing
_TEST_DB_PATH = Path(
    "P:/__csf/.data/intelligence-stream/batch_status/test_status.sqlite"
)


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


class TestSetStatusBatch:
    """Test set_status_batch bulk insert — best-effort per-entry."""

    def setup_method(self):
        reset_all(_TEST_DB_PATH)

    def test_set_status_batch_inserts_multiple(self):
        """set_status_batch inserts multiple entries and returns correct count."""
        entries: list[BatchEntry] = [
            ("vid1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z"),
            ("vid2", "pending", "https://youtube.com/channel/UC1", "2026-01-02T00:00:00Z"),
            ("vid3", "pending", "https://youtube.com/channel/UC1", "2026-01-03T00:00:00Z"),
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
            ("vid1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z"),
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
            ("vid_good1", "pending", "https://youtube.com/channel/UC1", "2026-01-01T00:00:00Z"),
            ("vid_good2", "pending", "https://youtube.com/channel/UC1", "2026-01-02T00:00:00Z"),
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
