"""Tests for csf/transcript.py - Full Fallback Chain.

Updated for TASK-011: Breaking API change from (bool, str, str) 3-tuple
to TranscriptResult return type.
"""

import sys
from pathlib import Path
from unittest import mock

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.transcript import LanguageConfig, TranscriptResult, fetch_transcript_chain


class TestVideoIdValidation:
    """Test video_id validation - malformed IDs must return empty TranscriptResult."""

    def test_invalid_video_id_returns_empty_result(self):
        """Malformed video_id (not 11 chars) returns empty TranscriptResult without raising."""
        result = fetch_transcript_chain("abc", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""
        assert result.source == "none"

    def test_video_id_with_special_chars_returns_empty_result(self):
        """Video ID with special characters returns empty result."""
        result = fetch_transcript_chain("abc!@#$%^&*()", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_video_id_too_short_returns_empty_result(self):
        """Video ID shorter than 11 chars returns empty result."""
        result = fetch_transcript_chain("short", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_video_id_too_long_returns_empty_result(self):
        """Video ID longer than 11 chars returns empty result."""
        result = fetch_transcript_chain("this_is_12_chars", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_valid_video_id_accepted(self):
        """Valid 11-char video ID is accepted and fetch is attempted."""
        with (
            mock.patch("csf.transcript.get_cached_transcript", return_value=None),
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
        ):
            mock_cli.return_value = (True, "transcript text", None)
            # Mock free sources to not interfere (free-first: yt_api tried first)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))
            assert isinstance(result, TranscriptResult)
            assert result.transcript == "transcript text"
            assert result.source == "cli"
            assert result.was_translated is False


class TestFallbackChain:
    """Test the fallback chain order: youtube_transcript_api → youtubei → SDK → CLI.

    TECH-01: Free sources are tried before paid CLI to conserve API quota.
    """

    def test_fallback_to_youtubei_when_api_blocked(self):
        """When youtube_transcript_api is blocked, youtubei is attempted."""
        with (
            mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
        ):
            mock_cache.return_value = None  # Cache miss
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (True, "transcript via youtubei", None)

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            mock_yt_api.assert_called_once()
            mock_youtubei.assert_called_once()
            assert result.transcript == "transcript via youtubei"
            assert result.source == "youtubei"

    def test_fallback_to_sdk_when_all_blocked(self):
        """When methods 1-3 are blocked, SDK fallback is attempted."""
        with (
            mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
        ):
            mock_cache.return_value = None  # Cache miss
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (True, "transcript via SDK", None)

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            assert result.transcript == "transcript via SDK"
            assert result.source == "sdk"

    def test_all_methods_fail_returns_empty_result(self):
        """When all methods fail, returns TranscriptResult with empty transcript."""
        with (
            mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
        ):
            mock_cache.return_value = None  # Cache miss
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (False, None, "SDK failed")

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            assert result.transcript == ""
            assert result.source == "none"

    def test_free_source_tried_before_paid_cli(self):
        """Free sources (youtube_transcript_api) must be tried BEFORE paid CLI.

        When both CLI and youtube_transcript_api succeed, the FREE source
        result must be returned to conserve paid API quota.
        TECH-01 fix: Fallback order must put free sources first.
        """
        with (
            mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
        ):
            mock_cache.return_value = None  # Cache miss
            # Both succeed — free source should win
            mock_cli.return_value = (True, "paid transcript", None)
            mock_yt_api.return_value = (True, "free transcript", None)

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            # Free source MUST be called and return free transcript
            mock_yt_api.assert_called_once()
            assert result.transcript == "free transcript"
            assert result.source == "youtube_transcript_api"
            # CLI must NOT be called when free source succeeds
            mock_cli.assert_not_called()


class TestCacheIntegration:
    """Test cache integration - set_cached_transcript called after successful fetch."""

    def test_result_cached_after_successful_fetch(self):
        """After successful fetch, set_cached_transcript is called with correct args."""
        with (
            mock.patch("csf.transcript.get_cached_transcript", return_value=None),
            mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
        ):
            # Free sources fail, SDK fails, CLI succeeds (last resort)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            mock_cli.return_value = (True, "fresh transcript", None)

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            mock_cache_set.assert_called_once()
            call_args = mock_cache_set.call_args[0]
            assert call_args[0] == "dQw4w9WgXcQ"  # video_id
            assert call_args[1] == "en"  # lang
            assert call_args[2] == "cli"  # source
            assert call_args[3] == "fresh transcript"  # transcript
            assert result.transcript == "fresh transcript"
            assert result.source == "cli"


class TestJitter:
    """Test random jitter for rate limit avoidance."""

    def test_jitter_in_range(self):
        """Jitter should be between 2.0 and 10.0 seconds (PERF-006: wider range)."""
        jitters = []
        for _ in range(100):
            with (
                mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
                mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
                mock.patch(
                    "csf.transcript._fetch_via_youtube_transcript_api"
                ) as mock_yt,
                mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
                mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
                mock.patch("time.sleep") as mock_sleep,
            ):
                mock_cache.return_value = None  # Cache miss
                mock_cli.return_value = (False, None, "fail")
                mock_yt.return_value = (False, None, "fail")
                mock_yi.return_value = (False, None, "fail")
                mock_sdk.return_value = (
                    True,
                    "transcript",
                    None,
                )  # Success at last method

                fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

                # Collect all jitter values from time.sleep calls
                for call in mock_sleep.call_args_list:
                    jitters.append(call[0][0])

        assert len(jitters) > 0, "No jitter was applied"
        for jitter in jitters:
            assert 2.0 <= jitter <= 10.0, f"Jitter {jitter} out of range [2.0, 10.0]"


class TestReturnType:
    """Test that return type is always TranscriptResult."""

    def test_returns_transcript_result(self):
        """Result is a TranscriptResult."""
        with mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli:
            mock_cli.return_value = (True, "transcript", None)
            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))
            assert isinstance(result, TranscriptResult)

    def test_success_returns_transcript(self):
        """On success, TranscriptResult contains transcript."""
        with (
            mock.patch("csf.transcript.get_cached_transcript", return_value=None),
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
        ):
            # Free sources fail, CLI succeeds (last in free-first chain)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            mock_cli.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))
            assert result.transcript == "transcript text"
            assert result.source == "cli"

    def test_all_fail_returns_empty_result(self):
        """On failure, TranscriptResult has empty transcript."""
        with (
            mock.patch("csf.transcript.get_cached_transcript") as mock_cache,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch("csf.transcript._fetch_via_youtube_transcript_api") as mock_yt,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
        ):
            mock_cache.return_value = None  # Cache miss
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt.return_value = (False, None, "API failed")
            mock_yi.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (False, None, "SDK failed")
            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))
            assert result.transcript == ""
            assert result.source == "none"
