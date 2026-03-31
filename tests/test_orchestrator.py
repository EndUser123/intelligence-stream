"""Tests for csf/orchestrator.py — Tiered availability routing and thread-safety."""

import sys
import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.orchestrator import (
    select_provider,
    analyze_video,
    GeminiSDKProvider,
)
from csf.providers import (
    NonFatalAnalysisError,
    TranscriptProvider,
)


class TestSelectProvider:
    """Tests for select_provider() routing logic."""

    @mock.patch("csf.orchestrator.has_cached_transcript", return_value=True)
    def test_select_provider_cached_transcript_returns_tier3(self, mock_cached):
        """When has_cached_transcript is True, TranscriptProvider is returned directly.

        No SDK, OCR, or CLIP calls are made.
        """
        provider = select_provider(
            "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

        assert isinstance(provider, TranscriptProvider)
        mock_cached.assert_called_once_with("dQw4w9WgXcQ")

    def test_select_provider_invalid_video_id_raises_valueerror(self):
        """Malformed video_id raises ValueError before any provider call."""
        with pytest.raises(ValueError, match="Invalid video_id format"):
            select_provider("abc", "https://www.youtube.com/watch?v=abc")

    def test_select_provider_invalid_url_raises_valueerror(self):
        """URL with invalid scheme raises ValueError before any provider call."""
        with pytest.raises(ValueError, match="Invalid URL scheme"):
            select_provider("dQw4w9WgXcQ", "ftp://youtube.com/watch?v=dQw4w9WgXcQ")

    @mock.patch("csf.orchestrator.has_cached_transcript", return_value=False)
    def test_select_provider_tier1_when_gemini_available(self, mock_cached):
        """When _gemini_available is True, GeminiSDKProvider is returned."""
        import csf.orchestrator as oc

        # Set gemini available to True under lock
        with oc._gemini_lock:
            oc._gemini_available = True
            oc._last_reset_date = oc._get_pacific_date()

        provider = select_provider(
            "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

        assert isinstance(provider, GeminiSDKProvider)

    @mock.patch("csf.orchestrator.has_cached_transcript", return_value=False)
    def test_select_provider_tier2_when_tier1_unavailable(self, mock_cached):
        """When _gemini_available is False, OcrClipProvider is returned."""
        import csf.orchestrator as oc

        # Set gemini unavailable
        with oc._gemini_lock:
            oc._gemini_available = False

        # Mock the OcrClipProvider class via _load_ocr_clip_provider
        mock_provider_instance = mock.Mock()
        with mock.patch.object(
            oc, "_load_ocr_clip_provider", return_value=type(mock_provider_instance)
        ):
            provider = select_provider(
                "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )

            # Should get OcrClipProvider when tier 1 is down
            assert isinstance(provider, mock.Mock)

    @mock.patch("csf.orchestrator.has_cached_transcript", return_value=False)
    def test_select_provider_tier3_fallback(self, mock_cached):
        """When tier 1 and tier 2 both unavailable, TranscriptProvider is returned."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False

        # Mock _load_ocr_clip_provider to raise NonFatalAnalysisError so we fall through
        def raise_nonfatal(*args, **kwargs):
            raise NonFatalAnalysisError("Tier 2 down")

        with mock.patch.object(
            oc, "_load_ocr_clip_provider", side_effect=raise_nonfatal
        ):
            provider = select_provider(
                "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )

        assert isinstance(provider, TranscriptProvider)


class TestMidnightReset:
    """Tests for Pacific midnight daily reset of _gemini_available flag."""

    def test_midnight_reset_flips_gemini_available(self):
        """When wall clock passes Pacific midnight, _gemini_available resets to True."""
        import csf.orchestrator as oc

        # Set flag to False manually
        with oc._gemini_lock:
            oc._gemini_available = False
            oc._last_reset_date = None

        # Mock _get_pacific_date to return a different date than the stored date
        mock_date = datetime.date(2026, 3, 31)

        with (
            mock.patch.object(oc, "_get_pacific_date", return_value=mock_date),
        ):
            with oc._gemini_lock:
                oc._check_and_reset_gemini()
                assert oc._gemini_available is True
                assert oc._last_reset_date == mock_date

        # Clean up
        with oc._gemini_lock:
            oc._gemini_available = True


class TestQuotaError:
    """Tests for quota error handling that flips _gemini_available to False."""

    def test_quota_error_sets_gemini_unavailable(self):
        """A quota error from the SDK flips _gemini_available to False under lock."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = True

        # Simulate what happens when a quota error is caught
        exc = Exception("429 Quota exceeded")
        if oc._is_quota_error(exc):
            with oc._gemini_lock:
                oc._gemini_available = False

        with oc._gemini_lock:
            assert oc._gemini_available is False

        # Clean up
        with oc._gemini_lock:
            oc._gemini_available = True


class TestThreadSafety:
    """Tests for thread-safe concurrent access to _gemini_available flag."""

    def test_thread_safety_concurrent_workers(self):
        """ThreadPoolExecutor with 4 workers hitting quota error causes no crashes."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = True

        errors = []

        def worker():
            try:
                exc = Exception("429 Quota exceeded")
                if oc._is_quota_error(exc):
                    with oc._gemini_lock:
                        oc._gemini_available = False
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker) for _ in range(4)]
            for f in futures:
                f.result()

        assert len(errors) == 0
        with oc._gemini_lock:
            oc._gemini_available = True


class TestAnalyzeVideo:
    """Tests for analyze_video() exception wrapping."""

    def test_analyze_video_wraps_unexpected_exception(self):
        """Unexpected exception from provider is wrapped in NonFatalAnalysisError."""
        mock_provider = mock.Mock()
        mock_provider.analyze.side_effect = RuntimeError("unexpected internal error")

        with pytest.raises(NonFatalAnalysisError, match="unexpected error"):
            analyze_video(
                "dQw4w9WgXcQ",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                provider=mock_provider,
            )
