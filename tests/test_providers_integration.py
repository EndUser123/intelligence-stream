"""Integration tests for csf/providers/ — cross-tier error propagation."""

import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf.providers import (
    VideoAnalysisResult,
    NonFatalAnalysisError,
    TranscriptProvider,
)
from csf.providers.ocr_clip_provider import OcrClipProvider
from csf.orchestrator import analyze_video, select_provider


class TestProviderSelection:
    """Tests for select_provider() — verifies correct provider is selected per tier."""

    def test_select_provider_returns_transcript_when_gemini_unavailable(self):
        """Tier 1 (Gemini) unavailable, OCR unavailable → select_provider returns TranscriptProvider (Tier 3)."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False

        try:
            with (
                mock.patch.object(oc, "_check_and_reset_gemini"),
                mock.patch.object(
                    oc, "_get_gemini_analyze", side_effect=RuntimeError("no gemini")
                ),
                mock.patch.object(
                    oc,
                    "_load_ocr_clip_provider",
                    side_effect=RuntimeError("OCR not available"),
                ),
            ):
                provider = select_provider(
                    "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )
                assert isinstance(provider, TranscriptProvider)
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True

    def test_select_provider_returns_ocr_clip_when_gemini_unavailable_and_ocr_available(
        self,
    ):
        """Tier 1 unavailable, OCR available → select_provider returns OcrClipProvider (Tier 2)."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False

        try:
            with (
                mock.patch.object(oc, "_check_and_reset_gemini"),
                mock.patch.object(oc, "_load_ocr_clip_provider") as mock_load,
                mock.patch.object(oc, "has_cached_transcript", return_value=False),
            ):
                mock_load.return_value = OcrClipProvider
                provider = select_provider(
                    "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )
                assert isinstance(provider, OcrClipProvider)
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True


class TestTierIntegration:
    """Integration tests for tier behavior — NonFatalAnalysisError propagation."""

    def test_analyze_video_with_gemini_nonfatal_propagates(self):
        """NonFatalAnalysisError from GeminiSDKProvider.analyze propagates to caller."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = True

        try:
            with (
                mock.patch.object(oc, "has_cached_transcript", return_value=False),
                mock.patch.object(
                    oc.GeminiSDKProvider,
                    "analyze",
                    side_effect=NonFatalAnalysisError("Tier 1 quota error"),
                ),
            ):
                with pytest.raises(NonFatalAnalysisError, match="Tier 1 quota error"):
                    analyze_video(
                        "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                    )
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True

    def test_analyze_video_with_ocr_nonfatal_propagates(self):
        """NonFatalAnalysisError from OcrClipProvider.analyze propagates to caller."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False  # Skip Tier 1

        try:
            with (
                mock.patch.object(oc, "_check_and_reset_gemini"),
                mock.patch.object(oc, "has_cached_transcript", return_value=False),
                mock.patch.object(
                    oc,
                    "_load_ocr_clip_provider",
                    return_value=OcrClipProvider,
                ),
                mock.patch.object(
                    OcrClipProvider,
                    "analyze",
                    side_effect=NonFatalAnalysisError("Tier 2 OCR unavailable"),
                ),
            ):
                with pytest.raises(
                    NonFatalAnalysisError, match="Tier 2 OCR unavailable"
                ):
                    analyze_video(
                        "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                    )
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True

    def test_analyze_video_with_transcript_provider_returns_result(self):
        """TranscriptProvider (Tier 3) returns a valid result."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False

        try:
            with (
                mock.patch.object(oc, "_check_and_reset_gemini"),
                mock.patch.object(
                    oc,
                    "_load_ocr_clip_provider",
                    side_effect=RuntimeError("OCR not available"),
                ),
                mock.patch.object(
                    TranscriptProvider,
                    "analyze",
                    return_value=VideoAnalysisResult(
                        title="Test Title",
                        summary="Test summary",
                        key_topics=["topic1"],
                        key_points=["point1"],
                        code_snippets=[],
                        visual_tags=[],
                        mode="transcript",
                    ),
                ),
            ):
                result = analyze_video(
                    "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )
                assert result.title == "Test Title"
                assert result.mode == "transcript"
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True

    def test_ocr_clip_provider_empty_transcript_raises_nonfatal(self):
        """OcrClipProvider raises NonFatalAnalysisError when transcript is empty."""
        import csf.orchestrator as oc

        with oc._gemini_lock:
            oc._gemini_available = False

        try:
            with (
                mock.patch(
                    "csf.transcript.fetch_transcript_chain",
                    return_value=mock.Mock(transcript="", segments=[]),
                ),
                mock.patch(
                    "csf.providers.ocr_clip_provider.summarize",
                    return_value=mock.Mock(
                        title="Test",
                        summary="",
                        key_topics=[],
                        key_points=[],
                        code_snippets=[],
                        visual_tags=[],
                        mode="ocr_clip",
                    ),
                ),
            ):
                provider = OcrClipProvider()
                with pytest.raises(NonFatalAnalysisError):
                    provider.analyze(
                        "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                    )
        finally:
            with oc._gemini_lock:
                oc._gemini_available = True

