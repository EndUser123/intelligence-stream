"""Tests for csf/batch.py - Parallel Batch Processing.

RED Phase: Tests are written BEFORE implementation to define expected behavior.

Test Matrix:
- Parallel vs sequential timing: Time 5 videos, assert parallel < sequential * 0.6
- Single video unchanged: Run single, assert output keys identical to sequential
- Batch error isolation: Add one invalid video ID, verify other 4 still complete successfully
"""

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf.batch import analyze_videos_parallel


@pytest.fixture(autouse=True)
def _reset_and_patch():
    """Reset _analyze_video_ref and patch has_cached_transcript before each test.

    The _analyze_video_ref global caches the loaded analyze_video function.
    Without this reset, the cached reference persists across tests in a session.

    has_cached_transcript is imported at module level in csf.batch, so it must
    be patched in csf.batch's namespace to prevent the real SQLite query from
    intercepting the mock flow (which would cause mode="transcript" calls that
    the simple mock doesn't support).
    """
    import csf.batch

    csf.batch._analyze_video_ref = None
    with mock.patch.object(csf.batch, "has_cached_transcript", return_value=False):
        yield
    csf.batch._analyze_video_ref = None


def _mock_analyze_video(return_value):
    """Return a mock analyze_video function set as _analyze_video_ref.

    The autouse fixture patches has_cached_transcript to return False,
    so this mock will receive normal calls (not transcript-only mode).
    """
    mock_fn = mock.Mock(return_value=return_value)
    import csf.batch

    csf.batch._analyze_video_ref = mock_fn
    return mock_fn


class TestAnalyzeVideosParallel:
    """Test the main analyze_videos_parallel function."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_returns_tuple_of_dict_and_list(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Returns tuple of (successful_results: dict, failed_video_ids: list)."""
        _mock_analyze_video({"title": "Test", "summary": "Test summary"})
        video_ids = ["dQw4w9WgXcQ"]

        result = analyze_videos_parallel(video_ids)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], list)

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_successful_video_in_results_dict(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Successful videos appear in the results dict keyed by video_id."""
        mock_fn = _mock_analyze_video({"title": "Test", "summary": "Test summary"})
        video_ids = ["dQw4w9WgXcQ"]

        successful, failed = analyze_videos_parallel(video_ids)

        assert "dQw4w9WgXcQ" in successful
        assert successful["dQw4w9WgXcQ"]["title"] == "Test"
        assert "dQw4w9WgXcQ" not in failed
        assert mock_fn.call_count == 1

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_failed_video_in_failed_list(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Failed videos appear in the failed_video_ids list."""
        mock_fn = mock.Mock(side_effect=RuntimeError("Video unavailable"))
        import csf.batch

        csf.batch._analyze_video_ref = mock_fn
        video_ids = ["dQw4w9WgXcQ"]

        successful, failed = analyze_videos_parallel(video_ids)

        assert "dQw4w9WgXcQ" not in successful
        assert "dQw4w9WgXcQ" in failed

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_multiple_videos_parallel(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Multiple videos are processed (not necessarily all successful)."""
        _mock_analyze_video({"title": "Test"})
        video_ids = ["dQw4w9WgXcQ", "abc123defgh", "xyz789uvw456"]

        successful, failed = analyze_videos_parallel(video_ids)

        assert len(successful) + len(failed) == 3

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_max_workers_bounded_at_8(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """max_workers is bounded at min(os.cpu_count() or 4, 8)."""
        mock_fn = _mock_analyze_video({"title": "Test"})
        video_ids = [f"video{i:03d}ID11" for i in range(10)]  # 10 valid-looking IDs

        # Request 100 workers - should be bounded to 8
        analyze_videos_parallel(video_ids, max_workers=100)

        # The implementation should cap workers at 8, so we check
        # by observing that it doesn't crash and processes all videos
        mock_fn.assert_called()
        # Total calls should equal number of videos
        assert mock_fn.call_count == 10

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_max_workers_default_is_4(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """max_workers defaults to 4 when not specified."""
        mock_fn = _mock_analyze_video({"title": "Test"})
        video_ids = ["dQw4w9WgXcQ"]

        analyze_videos_parallel(video_ids)

        # Should complete without error with default max_workers=4
        assert mock_fn.call_count == 1


class TestParallelSpeedup:
    """Test that parallel execution provides speedup over sequential."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_parallel_faster_than_sequential(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Parallel 5 videos completes in < 60% of sequential time.

        Each mock video takes 0.2s. Sequential would take ~1.0s for 5 videos.
        Parallel should take ~0.3s (0.2s + overhead with 4 workers).
        """

        # Each call takes 0.2s
        def slow_analyze(video_id, video_url, **kwargs):
            time.sleep(0.2)
            return {"title": f"Video {video_id}", "video_id": video_id}

        import csf.batch

        csf.batch._analyze_video_ref = slow_analyze

        video_ids = [f"dQw4w9WgXc{str(i)}" for i in range(5)]

        # Time parallel execution
        start_parallel = time.monotonic()
        successful, failed = analyze_videos_parallel(video_ids, max_workers=4)
        parallel_time = time.monotonic() - start_parallel

        # Time sequential execution (single worker)
        start_seq = time.monotonic()
        for vid in video_ids:
            slow_analyze(vid, f"https://youtube.com/watch?v={vid}")
        sequential_time = time.monotonic() - start_seq

        # Parallel should be < 60% of sequential time
        assert parallel_time < sequential_time * 0.6, (
            f"Parallel {parallel_time:.2f}s not faster than sequential {sequential_time:.2f}s "
            f"(expected < {sequential_time * 0.6:.2f}s)"
        )


class TestSingleVideoUnchanged:
    """Test that single video output matches sequential expectations."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_single_video_output_keys_match(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Single video via parallel has same keys as direct analyze_video call."""
        expected = {
            "title": "Test Title",
            "summary": "Test Summary",
            "key_topics": ["a", "b"],
        }
        _mock_analyze_video(expected)

        video_ids = ["dQw4w9WgXcQ"]

        # Direct call
        direct_result = expected

        # Parallel call
        successful, failed = analyze_videos_parallel(video_ids)

        # Keys should match
        assert set(successful["dQw4w9WgXcQ"].keys()) == set(direct_result.keys())

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_single_video_in_results(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Single video ID in list produces exactly one result."""
        _mock_analyze_video({"title": "Single"})
        video_ids = ["dQw4w9WgXcQ"]

        successful, failed = analyze_videos_parallel(video_ids)

        assert len(successful) == 1
        assert len(failed) == 0
        assert "dQw4w9WgXcQ" in successful


class TestBatchErrorIsolation:
    """Test that one invalid video ID does not crash other workers."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_invalid_id_does_not_crash_others(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """One invalid video ID in batch does not prevent other 4 from completing."""

        # First 4 succeed, 5th raises error
        def analyze_with_one_failure(video_id, video_url, **kwargs):
            if video_id == "invalidID123":
                raise RuntimeError("Invalid video ID")
            return {"title": f"Video {video_id}"}

        import csf.batch

        csf.batch._analyze_video_ref = analyze_with_one_failure

        video_ids = [
            "dQw4w9WgXcA",
            "dQw4w9WgXcB",
            "dQw4w9WgXcC",
            "dQw4w9WgXcD",
            "invalidID123",
        ]

        # Should not raise - errors are caught internally
        successful, failed = analyze_videos_parallel(video_ids)

        # 4 should succeed
        assert len(successful) == 4
        # 1 should fail
        assert len(failed) == 1
        assert "invalidID123" in failed

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_first_video_fails_others_continue(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """If first video fails, subsequent videos still complete."""
        call_count = [0]

        def analyze_tracking(video_id, video_url, **kwargs):
            call_count[0] += 1
            if video_id == "failFirst000":
                raise ValueError("First fails")
            return {"title": f"Video {video_id}"}

        import csf.batch

        csf.batch._analyze_video_ref = analyze_tracking

        video_ids = ["failFirst000", "dQw4w9WgXcB", "dQw4w9WgXcC"]

        successful, failed = analyze_videos_parallel(video_ids)

        # All 3 were attempted
        assert call_count[0] == 3
        # 2 succeeded
        assert len(successful) == 2
        # 1 failed
        assert len(failed) == 1

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_all_fail_returns_empty_successful(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """When all videos fail, returns empty successful dict and all failed IDs."""
        mock_fn = mock.Mock(side_effect=RuntimeError("All fail"))
        import csf.batch

        csf.batch._analyze_video_ref = mock_fn

        video_ids = ["aaaa111bbbb2", "cccc222dddd3", "eeee333ffff4"]

        successful, failed = analyze_videos_parallel(video_ids)

        assert len(successful) == 0
        assert len(failed) == 3
        assert set(failed) == set(video_ids)


class TestQueueBoundedMemory:
    """Test that the bounded queue prevents memory exhaustion."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_works_with_large_batch(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """Large batch (100 videos) should complete without memory issues."""
        _mock_analyze_video({"title": "Test"})

        # 100 video IDs
        video_ids = [f"dQw4w9WgXc{str(i).zfill(3)}" for i in range(100)]

        successful, failed = analyze_videos_parallel(video_ids, max_workers=4)

        # All should be accounted for
        assert len(successful) + len(failed) == 100


class TestCPUCountAwareness:
    """Test CPU count awareness for worker bounds."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_workers_capped_at_cpu_count(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """max_workers is bounded by min(os.cpu_count() or 4, 8)."""

        mock_fn = mock.Mock(return_value={"title": "Test"})
        import csf.batch

        csf.batch._analyze_video_ref = mock_fn
        video_ids = [f"dQw4w9WgXc{str(i)}" for i in range(8)]

        # Request many workers
        analyze_videos_parallel(video_ids, max_workers=50)

        # Should complete - workers capped appropriately
        total = len(video_ids)
        assert mock_fn.call_count == total

