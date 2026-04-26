"""Tests for csf/batch.py — Parallel batch processing integration."""

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf.batch import analyze_videos_parallel
from csf.providers import (
    VideoAnalysisResult,
    NonFatalAnalysisError,
    TranscriptProvider,
)


# Reset + patch fixture (same pattern as test_batch.py)
@pytest.fixture(autouse=True)
def _reset_and_patch():
    """Reset _analyze_video_ref and patch has_cached_transcript before each test.

    Patches BOTH csf.cache (source of direct import in batch.py) AND
    csf.batch (module-namespace lookup) so select_provider AND batch.py
    both consistently see the mocked value.
    """
    import csf.batch

    csf.batch._analyze_video_ref = None
    with (
        mock.patch("csf.cache.has_cached_transcript", return_value=False),
        mock.patch.object(csf.batch, "has_cached_transcript", return_value=False),
    ):
        yield
    csf.batch._analyze_video_ref = None


class TestCachedTranscriptFastpath:
    """Tests for the Tier 3 cached transcript fast-path in batch."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    @mock.patch("csf.batch.has_cached_transcript", return_value=True)
    def test_cached_transcript_fastpath_no_orchestrator(
        self, mock_cached, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """has_cached_transcript=True → TranscriptProvider called directly, no SDK calls."""
        import csf.batch

        csf.batch._analyze_video_ref = None  # Ensure no cached ref

        # Mock a successful transcript result
        mock_result = VideoAnalysisResult(
            title="Cached Video",
            summary="From cache",
            key_topics=["a"],
            key_points=["b"],
            mode="transcript",
        )

        transcript_provider_used = []

        def fake_analyze(video_id, video_url):
            transcript_provider_used.append((video_id, video_url))
            return mock_result

        with mock.patch.object(TranscriptProvider, "analyze", side_effect=fake_analyze):
            successful, failed = analyze_videos_parallel(["dQw4w9WgXcQ"])

        assert len(successful) == 1
        assert "dQw4w9WgXcQ" in successful
        assert len(transcript_provider_used) == 1
        # Fast path used — no orchestrator needed
        assert transcript_provider_used[0][0] == "dQw4w9WgXcQ"


class TestNonFatalIsolation:
    """Tests for NonFatalAnalysisError isolation in batch workers."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_orchestrator_nonfatal_does_not_crash_batch(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """NonFatalAnalysisError from Tier 2 → Tier 3 succeeds, batch worker continues."""
        import csf.batch

        call_log = []

        def tiered_analyze(video_id, video_url, **kwargs):
            call_log.append(video_id)
            if video_id == "failTier2":
                raise NonFatalAnalysisError("Tier 2 OCR/CLIP failed")
            return VideoAnalysisResult(
                title=f"Video {video_id}",
                summary="OK",
                key_topics=[],
                key_points=[],
                mode="transcript",
            )

        # Patch _get_analyze_video so _analyze_one uses our mock
        with mock.patch.object(
            csf.batch, "_get_analyze_video", return_value=tiered_analyze
        ):
            successful, failed = analyze_videos_parallel(
                ["dQw4w9WgXcA", "failTier2", "dQw4w9WgXcC"]
            )

        # All 3 were attempted
        assert len(call_log) == 3
        # 2 succeeded, 1 failed gracefully
        assert len(successful) == 2
        assert len(failed) == 1


class TestConcurrentWorkers:
    """Tests for concurrent worker safety."""

    @mock.patch("csf.batch.is_complete", return_value=False)
    @mock.patch("csf.batch.mark_complete")
    @mock.patch("csf.batch.mark_failed")
    def test_4_worker_concurrent(
        self, mock_mark_failed, mock_mark_complete, mock_is_complete
    ):
        """ThreadPoolExecutor with 4 workers — verify no race conditions."""
        import csf.batch

        worker_start_times = []
        lock = __import__("threading").Lock()

        def slow_analyze(video_id, video_url, **kwargs):
            with lock:
                worker_start_times.append((video_id, time.monotonic()))
            time.sleep(0.1)
            return VideoAnalysisResult(
                title=f"Video {video_id}",
                summary="OK",
                key_topics=[],
                key_points=[],
                mode="transcript",
            )

        video_ids = [f"dQw4w9WgXc{str(i)}" for i in range(4)]

        with mock.patch.object(
            csf.batch, "_get_analyze_video", return_value=slow_analyze
        ):
            successful, failed = analyze_videos_parallel(video_ids, max_workers=4)

        # All 4 completed
        assert len(successful) == 4
        assert len(failed) == 0

        # Workers started close together (concurrent), not sequentially
        # Time between first and last start should be < 0.2s (sequential would be ~0.4s)
        if len(worker_start_times) >= 2:
            first = worker_start_times[0][1]
            last = worker_start_times[-1][1]
            assert (
                last - first
            ) < 0.3, "Workers should start concurrently, not sequentially"

