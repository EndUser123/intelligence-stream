"""Tests for csf/batch.py - PROC-01: Batch progress visibility.

RED Phase: Tests are written BEFORE implementation to define expected behavior.
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf.batch import analyze_videos_parallel


@pytest.fixture(autouse=True)
def _reset_and_patch():
    """Reset _analyze_video_ref and patch has_cached_transcript before each test.

    The _analyze_video_ref global caches the result of _get_analyze_video() after
    the first call. Without this reset, tests that run after the first call to
    _get_analyze_video() in a session would use the cached real function instead
    of the patched mock.

    has_cached_transcript is imported at module level in csf.batch, so it must
    be patched in csf.batch's namespace to prevent the real SQLite query.
    """
    import csf.batch

    csf.batch._analyze_video_ref = None
    with mock.patch.object(csf.batch, "has_cached_transcript", return_value=False):
        yield
    csf.batch._analyze_video_ref = None


def _make_mock_analyze_video(return_value):
    mock_fn = mock.Mock()
    mock_fn.return_value = return_value
    return mock_fn


class TestBatchProgressVisibility:
    """Test --progress flag shows real-time counts during batch processing."""

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_progress_callback_receives_updates(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Progress callback is called with counts after each video completes."""
        progress_updates = []

        def on_progress(pending, done, failed, cached):
            progress_updates.append(
                {"pending": pending, "done": done, "failed": failed, "cached": cached}
            )

        mock_get_analyze.return_value = _make_mock_analyze_video(
            {"title": "Test", "summary": "Test summary"}
        )
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB", "dQw4w9WgXcC"]

        _successful, _failed = analyze_videos_parallel(
            video_ids, max_workers=2, progress_callback=on_progress, force=False
        )

        # Should have at least one progress update
        assert len(progress_updates) > 0
        # Total accounted for should equal total videos
        for u in progress_updates:
            assert u["pending"] + u["done"] + u["failed"] == len(video_ids)

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_progress_callback_receives_pending_decrement(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Pending count decrements as videos complete."""
        pending_values = []

        def on_progress(pending, done, failed, cached):
            pending_values.append(pending)

        mock_get_analyze.return_value = _make_mock_analyze_video(
            {"title": "Test", "summary": "Test summary"}
        )
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB", "dQw4w9WgXcC"]

        analyze_videos_parallel(video_ids, max_workers=2, progress_callback=on_progress)

        # Pending should decrease monotonically from initial to 0
        assert pending_values[-1] == 0
        # Each subsequent pending value should be <= the previous
        for i in range(1, len(pending_values)):
            assert pending_values[i] <= pending_values[i - 1]

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_failed_video_increments_failed_count(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Failed videos are counted in the failed bucket."""
        failed_counts = []

        def on_progress(pending, done, failed, cached):
            failed_counts.append(failed)

        mock_fn = mock.Mock()

        # 2 succeed, 1 fails
        def analyze_with_one_failure(video_id, _video_url, **kwargs):
            if video_id == "dQw4w9WgXcB":
                raise RuntimeError("Failed")
            return {"title": "Test"}

        mock_get_analyze.return_value = analyze_with_one_failure
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB", "dQw4w9WgXcC"]

        analyze_videos_parallel(video_ids, max_workers=2, progress_callback=on_progress)

        # Final failed count should be 1
        assert any(f == 1 for f in failed_counts)

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_cached_count_tracks_cached_videos(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Cached count increments for successfully analyzed videos."""
        cached_counts = []

        def on_progress(pending, done, failed, cached):
            cached_counts.append(cached)

        mock_get_analyze.return_value = _make_mock_analyze_video(
            {"title": "Test", "summary": "Test summary"}
        )
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB"]

        analyze_videos_parallel(video_ids, max_workers=2, progress_callback=on_progress)

        # Final cached count should be 2
        assert cached_counts[-1] == 2

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_no_callback_no_error(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Without progress_callback, no error is raised."""
        mock_get_analyze.return_value = _make_mock_analyze_video(
            {"title": "Test", "summary": "Test summary"}
        )
        video_ids = ["dQw4w9WgXcQ"]

        # Should not raise
        successful, failed = analyze_videos_parallel(video_ids)
        assert len(successful) == 1

    @mock.patch("csf.batch._get_analyze_video")
    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_done_count_includes_successful_only(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete, mock_get_analyze
    ):
        """Done count only includes successfully analyzed videos."""
        done_values = []

        def on_progress(pending, done, failed, cached):
            done_values.append(done)

        mock_fn = mock.Mock()

        # 1 succeeds, 1 fails
        def analyze_with_one_failure(video_id, _video_url, **kwargs):
            if video_id == "dQw4w9WgXcB":
                raise RuntimeError("Failed")
            return {"title": "Test"}

        mock_get_analyze.return_value = analyze_with_one_failure
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB"]

        analyze_videos_parallel(video_ids, max_workers=2, progress_callback=on_progress)

        # Done count should never exceed 1
        assert all(d <= 1 for d in done_values)

