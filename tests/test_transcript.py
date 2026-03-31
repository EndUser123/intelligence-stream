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
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_cli.return_value = (True, "transcript text", None)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            mock_ytdlp.return_value = (False, None, "no captions")
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
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
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (True, "transcript via youtubei", None)
            mock_ytdlp.return_value = (False, None, "no captions")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_yt_api.assert_called_once()
            mock_youtubei.assert_called_once()
            assert result.transcript == "transcript via youtubei"
            assert result.source == "youtubei"

    def test_fallback_to_sdk_when_all_blocked(self):
        """When methods 1-3 are blocked, SDK fallback is attempted."""
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (True, "transcript via SDK", None)
            mock_ytdlp.return_value = (False, None, "no captions")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            assert result.transcript == "transcript via SDK"
            assert result.source == "sdk"

    def test_all_methods_fail_returns_empty_result(self):
        """When all methods fail, returns TranscriptResult with empty transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt_api.return_value = (False, None, "API failed")
            mock_youtubei.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (False, None, "SDK failed")
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_whisper.return_value = (False, None, "whisper failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            assert result.transcript == ""
            assert result.source == "none"

    def test_free_source_tried_before_paid_cli(self):
        """Free sources (youtube_transcript_api) must be tried BEFORE paid CLI.

        When both CLI and youtube_transcript_api succeed, the FREE source
        result must be returned to conserve paid API quota.
        TECH-01 fix: Fallback order must put free sources first.
        """
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            # Both succeed — free source should win
            mock_cli.return_value = (True, "paid transcript", None)
            mock_yt_api.return_value = (True, "free transcript", None)
            mock_youtubei.return_value = (True, "free transcript", None)
            mock_sdk.return_value = (False, None, "unavailable")
            mock_ytdlp.return_value = (False, None, "no captions")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

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
            mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            # Free sources fail, SDK fails, CLI succeeds (last resort)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            mock_cli.return_value = (True, "fresh transcript", None)
            mock_ytdlp.return_value = (False, None, "no captions")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

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
                mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
                mock.patch(
                    "csf.transcript._fetch_via_youtube_transcript_api"
                ) as mock_yt,
                mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
                mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
                mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
                mock.patch("time.sleep") as mock_sleep,
                mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            ):
                mock_cli.return_value = (False, None, "fail")
                mock_yt.return_value = (False, None, "fail")
                mock_yi.return_value = (False, None, "fail")
                mock_sdk.return_value = (
                    True,
                    "transcript",
                    None,
                )  # Success at last method
                mock_ytdlp.return_value = (False, None, "no captions")

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
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_cli.return_value = (True, "transcript", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert isinstance(result, TranscriptResult)

    def test_success_returns_transcript(self):
        """On success, TranscriptResult contains transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api"
            ) as mock_yt_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_youtubei,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            # Free sources fail, CLI succeeds (last in free-first chain)
            mock_yt_api.return_value = (False, None, "free source unavailable")
            mock_youtubei.return_value = (False, None, "free source unavailable")
            mock_sdk.return_value = (False, None, "free source unavailable")
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_cli.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.transcript == "transcript text"
            assert result.source == "cli"

    def test_all_fail_returns_empty_result(self):
        """On failure, TranscriptResult has empty transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch("csf.transcript._fetch_via_youtube_transcript_api") as mock_yt,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            mock_cli.return_value = (False, None, "CLI failed")
            mock_yt.return_value = (False, None, "API failed")
            mock_yi.return_value = (False, None, "youtubei failed")
            mock_sdk.return_value = (False, None, "SDK failed")
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_whisper.return_value = (False, None, "whisper failed")
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.transcript == ""
            assert result.source == "none"


class TestCircuitBreaker:
    """Test per-source circuit breaker for 429 rate-limit handling.

    Each fetch_transcript_chain call runs TWO loops: Step 1 (prefer_lang) and
    Step 2 (any language, free_methods only). Since time.sleep is mocked to
    zero delay, cooldown never expires between steps. Each source's counter
    therefore increments TWICE per call (once per step), until its circuit opens.
    """

    def _reset_circuit_state(self):
        """Reset all circuit breaker module state before each test."""
        import csf.transcript as t

        t._consecutive_429.clear()
        t._source_cooldown_until.clear()

    def test_circuit_opens_after_three_consecutive_429s(self):
        """After 3 total 429s from a source, circuit opens and source is skipped."""
        self._reset_circuit_state()
        import csf.transcript as t

        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_youtube_transcript_api") as mock_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            rate_limit = (False, None, "rate limited (429)")
            mock_ytdlp.return_value = rate_limit
            mock_api.return_value = rate_limit
            mock_yi.return_value = rate_limit
            mock_sdk.return_value = rate_limit
            mock_cli.return_value = rate_limit
            mock_whisper.return_value = (False, None, "whisper not available")

            video_id = "dQw4w9WgXcQ"

            # Call 1: Step1: all 4 sources 429 (ytdlp=1), Step2: all 4 sources 429 (ytdlp=2)
            # No circuit opens yet (threshold=3)
            fetch_transcript_chain(video_id, LanguageConfig(prefer_lang="en"))
            assert (
                t._consecutive_429.get("ytdlp") == 2
            ), f"Expected ytdlp=2 after 1 call, got {t._consecutive_429.get('ytdlp')}"

            # Call 2: ytdlp=3 (opens circuit), api/youtubei/sdk also=3 (open circuits)
            # ytdlp IS called (cooldown not yet set from Call 1)
            fetch_transcript_chain(video_id, LanguageConfig(prefer_lang="en"))
            assert t._consecutive_429.get("ytdlp") == 3, "ytdlp should reach 3"
            assert (
                t._is_source_rate_limited("ytdlp") is True
            ), "ytdlp circuit should be open"

    def test_success_resets_only_that_source_counter(self):
        """Success from a source resets only that source's counter, not other sources."""
        self._reset_circuit_state()
        import csf.transcript as t

        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_youtube_transcript_api") as mock_api,
            mock.patch("csf.transcript._fetch_via_youtubei") as mock_yi,
            mock.patch("csf.transcript._fetch_via_sdk") as mock_sdk,
            mock.patch("csf.transcript._fetch_via_gemini_cli") as mock_cli,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            video_id = "dQw4w9WgXcQ"

            # ytdlp: 429 (Step1 + Step2 = 2 increments), others: non-429
            mock_ytdlp.return_value = (False, None, "rate limited (429)")
            mock_api.return_value = (False, None, "some other error")
            mock_yi.return_value = (False, None, "some other error")
            mock_sdk.return_value = (False, None, "some other error")
            mock_cli.return_value = (False, None, "some other error")
            mock_whisper.return_value = (False, None, "whisper not available")

            fetch_transcript_chain(video_id, LanguageConfig(prefer_lang="en"))
            assert t._consecutive_429.get("ytdlp") == 2
            assert t._consecutive_429.get("youtube_transcript_api") is None

            # ytdlp succeeds — should reset only ytdlp counter
            mock_ytdlp.return_value = (True, "ytdlp transcript", None)
            fetch_transcript_chain(video_id, LanguageConfig(prefer_lang="en"))

            assert (
                t._consecutive_429.get("ytdlp") == 0
            ), "ytdlp counter should reset on success"
            assert (
                t._consecutive_429.get("youtube_transcript_api") is None
            ), "Other source counters should NOT be reset"

    def test_backoff_multiplier_grows_exponentially(self):
        """Backoff multiplier is 2^count per consecutive failure, capped at 32x.

        Directly tests _apply_jitter_with_backoff with sequential counts since the
        full chain can't isolate a single source (all 4 sources return 429 each step).
        """
        self._reset_circuit_state()
        import csf.transcript as t

        with (
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("csf.transcript.random.uniform", return_value=6.0),
        ):
            multipliers_seen = []

            def capture_sleep(seconds):
                # multiplier = seconds / 6.0 (mock uniform returns 6.0)
                multipliers_seen.append(round(seconds / 6.0, 2))

            mock_sleep.side_effect = capture_sleep

            # count=1 → 2^1=2x, count=2 → 2^2=4x, count=3 → 2^3=8x
            for count in [1, 2, 3]:
                t._consecutive_429["ytdlp"] = count
                t._apply_jitter_with_backoff("ytdlp")

            assert (
                multipliers_seen[0] == 2.0
            ), f"Expected 2.0, got {multipliers_seen[0]}"
            assert (
                multipliers_seen[1] == 4.0
            ), f"Expected 4.0, got {multipliers_seen[1]}"
            assert (
                multipliers_seen[2] == 8.0
            ), f"Expected 8.0, got {multipliers_seen[2]}"

    def test_backoff_capped_at_32x(self):
        """Multiplier stops growing at 32x regardless of consecutive failures.

        Directly tests _apply_jitter_with_backoff with a manually-set high count,
        since the full fetch_transcript_chain can't reach 32x (circuit opens on
        Call 1 and stays open indefinitely when time.monotonic doesn't advance).
        """
        self._reset_circuit_state()
        import csf.transcript as t

        with (
            mock.patch("time.sleep") as mock_sleep,
            mock.patch("csf.transcript.random.uniform", return_value=6.0),
        ):
            max_multiplier_seen = 0.0

            def track_max(seconds):
                nonlocal max_multiplier_seen
                multiplier = seconds / 6.0
                if multiplier > max_multiplier_seen:
                    max_multiplier_seen = round(multiplier, 2)

            mock_sleep.side_effect = track_max

            # Test with counts that would produce > 32x without capping
            for count in [5, 6, 7, 8, 9, 10]:
                t._consecutive_429["ytdlp"] = count
                t._apply_jitter_with_backoff("ytdlp")

            # Multiplier should be capped at 32
            assert (
                max_multiplier_seen == 32.0
            ), f"Multiplier should cap at 32x, got {max_multiplier_seen}"

    def test_is_source_rate_limited_during_cooldown(self):
        """_is_source_rate_limited returns True during cooldown window, False after."""
        self._reset_circuit_state()
        import csf.transcript as t

        with mock.patch("time.monotonic", return_value=1000.0):
            assert t._is_source_rate_limited("ytdlp") is False

            t._source_cooldown_until["ytdlp"] = 1300.0

            assert t._is_source_rate_limited("ytdlp") is True

            with mock.patch("time.monotonic", return_value=1301.0):
                assert t._is_source_rate_limited("ytdlp") is False

    def test_concurrent_workers_increment_counter_atomically(self):
        """ThreadPoolExecutor workers increment _consecutive_429 atomically via threading.Lock."""
        self._reset_circuit_state()
        import csf.transcript as t
        from concurrent.futures import ThreadPoolExecutor

        with (
            mock.patch(
                "csf.transcript._fetch_via_ytdlp",
                return_value=(False, None, "rate limited (429)"),
            ),
            mock.patch(
                "csf.transcript._fetch_via_youtube_transcript_api",
                return_value=(False, None, "rate limited (429)"),
            ),
            mock.patch(
                "csf.transcript._fetch_via_youtubei",
                return_value=(False, None, "rate limited (429)"),
            ),
            mock.patch(
                "csf.transcript._fetch_via_sdk",
                return_value=(False, None, "rate limited (429)"),
            ),
            mock.patch(
                "csf.transcript._fetch_via_gemini_cli",
                return_value=(False, None, "rate limited (429)"),
            ),
            mock.patch(
                "csf.transcript._fetch_via_whisper",
                return_value=(False, None, "whisper not available"),
            ),
            mock.patch("csf.transcript.is_free_only_mode", return_value=False),
            mock.patch("time.sleep"),
        ):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(
                        fetch_transcript_chain,
                        "dQw4w9WgXcQ",
                        LanguageConfig(prefer_lang="en"),
                    )
                    for _ in range(4)
                ]
                for f in futures:
                    f.result()

            # Counter should be >= 3 after 4 concurrent calls (via threading.Lock protection)
            assert (
                (t._consecutive_429.get("ytdlp") or 0) >= 3
            ), f"Counter should be >= 3 after 4 concurrent calls, got {t._consecutive_429.get('ytdlp')}"
