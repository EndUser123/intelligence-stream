"""Tests for csf/providers/ocr_clip_provider.py — OCR/CLIP provider implementation."""

import sys
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\\packages\\yt-is").absolute()))

from csf.providers import VideoAnalysisResult, NonFatalAnalysisError
from csf.providers.ocr_clip_provider import OcrClipProvider


def _mock_summarize_result(_transcript, code_snippets, visual_tags):
    """Return a VideoAnalysisResult matching what summarize() would return."""
    return VideoAnalysisResult(
        title="Test Title",
        summary="Test summary",
        key_topics=["topic1"],
        key_points=["point1"],
        code_snippets=list(code_snippets),
        visual_tags=list(visual_tags),
        mode="ocr_clip",
        fallback_reason=None,
    )


class TestOcrClipProvider:
    """Tests for OcrClipProvider.analyze() — OCR/CLIP pipeline.

    Note: ocr_clip_provider.py uses direct imports (from csf.X import Y), so all
    mocks must patch the binding site (csf.providers.ocr_clip_provider.Y), not the
    source module (csf.X.Y).
    """

    def test_empty_transcript_raises_nonfatal(self):
        """Empty transcript string raises NonFatalAnalysisError."""
        with (
            mock.patch(
                "csf.transcript.fetch_transcript_chain",
                return_value=mock.Mock(transcript="", segments=[]),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.summarize",
                side_effect=_mock_summarize_result,
            ),
        ):
            provider = OcrClipProvider()
            with pytest.raises(NonFatalAnalysisError):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )

    def test_valid_transcript_returns_video_analysis_result(self):
        """Valid transcript returns VideoAnalysisResult with ocr_clip mode."""
        with (
            mock.patch(
                "csf.transcript.fetch_transcript_chain",
                return_value=mock.Mock(transcript="This is a transcript", segments=[]),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.extract_frames",
                return_value=[Path("/tmp/fake.jpg")],
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.extract_code_snippets",
                return_value=["def hello():", "x = 1"],
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.tag_frames",
                return_value=["code screenshot", "slide"],
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.summarize",
                side_effect=_mock_summarize_result,
            ),
        ):
            provider = OcrClipProvider()
            result = provider.analyze(
                "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )

            assert isinstance(result, VideoAnalysisResult)
            assert result.mode == "ocr_clip"
            assert result.code_snippets == ["def hello():", "x = 1"]
            assert result.visual_tags == ["code screenshot", "slide"]

    def test_ocr_fails_raises_nonfatal(self):
        """OCR failure with empty transcript raises NonFatalAnalysisError."""
        with (
            mock.patch(
                "csf.transcript.fetch_transcript_chain",
                return_value=mock.Mock(transcript="", segments=[]),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.extract_frames",
                side_effect=RuntimeError("FFmpeg error"),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.summarize",
                side_effect=_mock_summarize_result,
            ),
        ):
            provider = OcrClipProvider()
            # Empty transcript → NonFatalAnalysisError regardless of extract_frames
            with pytest.raises(NonFatalAnalysisError):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                )

    def test_clip_fails_returns_partial_with_empty_visual_tags(self):
        """CLIP failure returns partial result with empty visual_tags."""
        with (
            mock.patch(
                "csf.transcript.fetch_transcript_chain",
                return_value=mock.Mock(transcript="This is a transcript", segments=[]),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.extract_frames",
                return_value=[Path("/tmp/fake.jpg")],
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.extract_code_snippets",
                return_value=["def hello():", "x = 1"],
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.tag_frames",
                side_effect=RuntimeError("CLIP error"),
            ),
            mock.patch(
                "csf.providers.ocr_clip_provider.summarize",
                side_effect=_mock_summarize_result,
            ),
        ):
            provider = OcrClipProvider()
            result = provider.analyze(
                "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )

            assert isinstance(result, VideoAnalysisResult)
            assert result.code_snippets == ["def hello():", "x = 1"]
            assert result.visual_tags == []

