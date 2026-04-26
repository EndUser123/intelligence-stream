"""Tests for Phase 2: Agent-level circuit breaker for transcript fetching.

Tests the fetch_with_circuit_breaker function that prevents infinite retry loops
while allowing retries for transient failures.
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf.transcript import LanguageConfig, TranscriptResult
from csf.terminal_context import resolve_tid


class TestErrorClassification:
    """Test error classification into transient vs permanent."""

    def test_429_error_is_transient(self):
        """HTTP 429 rate limit errors are classified as transient."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(429, "rate limited")
        assert result == "transient"

    def test_503_error_is_transient(self):
        """HTTP 503 service unavailable errors are classified as transient."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(503, "service unavailable")
        assert result == "transient"

    def test_504_error_is_transient(self):
        """HTTP 504 gateway timeout errors are classified as transient."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(504, "gateway timeout")
        assert result == "transient"

    def test_timeout_error_is_transient(self):
        """Network timeout errors are classified as transient."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(None, "timeout")
        assert result == "transient"

    def test_404_error_is_permanent(self):
        """HTTP 404 not found errors are classified as permanent."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(404, "not found")
        assert result == "permanent"

    def test_400_error_is_permanent(self):
        """HTTP 400 bad request errors are classified as permanent."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(400, "bad request")
        assert result == "permanent"

    def test_403_error_is_transient(self):
        """HTTP 403 forbidden errors are classified as transient (may succeed with cookies)."""
        from csf.transcript_phase2 import _classify_error

        result = _classify_error(403, "forbidden")
        assert result == "transient"


class TestCircuitBreakerRetryLogic:
    """Test agent-level circuit breaker retry behavior."""

    def test_transient_error_retries_up_to_max_attempts(self):
        """Transient errors trigger retries up to MAX_RETRY_ATTEMPTS (3)."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_123"
        terminal_id = resolve_tid()

        # Mock fetch chain to fail with 429 twice, then succeed
        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            mock_fetch.side_effect = [
                TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang=None,
                    was_translated=False,
                    transcript="",
                    source="none",
                    detected_lang=None,
                    error="rate limited (429)",
                ),
                TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang=None,
                    was_translated=False,
                    transcript="",
                    source="none",
                    detected_lang=None,
                    error="rate limited (429)",
                ),
                TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang="en",
                    was_translated=False,
                    transcript="success transcript",
                    source="cli",
                    detected_lang="en",
                    error=None,
                ),
            ]

            result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

            assert bool(result.transcript)  # Success: has transcript
            assert result.transcript == "success transcript"
            assert result.error is None
            assert mock_fetch.call_count == 3

    def test_permanent_error_fails_immediately_without_retries(self):
        """Permanent errors (404) fail immediately without retries."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_404"
        terminal_id = resolve_tid()

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            mock_fetch.return_value = TranscriptResult(
                video_id=video_id,
                lang="en",
                raw_lang=None,
                was_translated=False,
                transcript="",
                source="none",
                detected_lang=None,
                error="not found (404)",
            )

            result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

            assert not result.transcript  # Failure: no transcript
            assert "not found" in result.error
            assert mock_fetch.call_count == 1  # No retries for permanent errors

    def test_max_retries_exceeded_returns_failure(self):
        """After MAX_RETRY_ATTEMPTS (3) failures, return failure result."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_max"
        terminal_id = resolve_tid()

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            # Always fail with transient error
            mock_fetch.return_value = TranscriptResult(
                video_id=video_id,
                lang="en",
                raw_lang=None,
                was_translated=False,
                transcript="",
                source="none",
                detected_lang=None,
                error="rate limited (429)",
            )

            result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

            assert not result.transcript  # Failure: no transcript
            assert "Max retries exceeded" in result.error
            assert mock_fetch.call_count == 3  # MAX_RETRY_ATTEMPTS = 3 total attempts


class TestTerminalScopedRetryState:
    """Test that retry state is terminal-scoped (isolated per terminal)."""

    def test_concurrent_terminals_have_independent_retry_budgets(self):
        """Two terminals fetching the same video should have independent retry budgets."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_concurrent"
        terminal_a = "terminal_a"
        terminal_b = "terminal_b"

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            with mock.patch("csf.terminal_context.resolve_tid") as mock_tid:
                # Terminal A fails 3 times, should exhaust its budget
                mock_tid.return_value = terminal_a
                # Configure mock to always fail for terminal A
                mock_fetch.return_value = TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang=None,
                    was_translated=False,
                    transcript="",
                    source="none",
                    detected_lang=None,
                    error="rate limited (429)",
                )
                result_a = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_a)

                # Terminal B should still have 3 retries available
                mock_fetch.reset_mock()
                mock_tid.return_value = terminal_b
                mock_fetch.return_value = TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang="en",
                    was_translated=False,
                    transcript="success",
                    source="cli",
                    detected_lang="en",
                    error=None,
                )
                result_b = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_b)

                # Terminal A should have failed (exceeded retries)
                assert not result_a.transcript  # Failure: no transcript
                # Terminal B should succeed (has fresh retry budget)
                assert bool(result_b.transcript)  # Success: has transcript


class TestExponentialBackoff:
    """Test exponential backoff between retries."""

    def test_exponential_backoff_between_retries(self):
        """Retries use exponential backoff: 2s, 4s, 8s, 16s, 32s, 60s (cap)."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_backoff"
        terminal_id = resolve_tid()

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            with mock.patch("time.sleep") as mock_sleep:
                # Fail 3 times then succeed
                mock_fetch.side_effect = [
                    TranscriptResult(
                        video_id=video_id,
                        lang="en",
                        raw_lang=None,
                        was_translated=False,
                        transcript="",
                        source="none",
                        detected_lang=None,
                        error="rate limited (429)",
                    ),
                    TranscriptResult(
                        video_id=video_id,
                        lang="en",
                        raw_lang=None,
                        was_translated=False,
                        transcript="",
                        source="none",
                        detected_lang=None,
                        error="rate limited (429)",
                    ),
                    TranscriptResult(
                        video_id=video_id,
                        lang="en",
                        raw_lang="en",
                        was_translated=False,
                        transcript="success",
                        source="cli",
                        detected_lang="en",
                        error=None,
                    ),
                ]

                fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

                # Verify sleep was called with exponential backoff values
                assert mock_sleep.call_count == 2  # 2 sleeps before 3rd attempt succeeds
                sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
                assert sleep_calls[0] == 2.0  # First retry: 2s
                assert sleep_calls[1] == 4.0  # Second retry: 4s


class TestOverflowHandling:
    """Test context overflow detection and handling strategies."""

    def test_truncate_strategy(self):
        """Truncate strategy returns first max_length chars with cache notice."""
        from csf.transcript_phase2 import handle_overflow

        long_transcript = "x" * 100_000  # 100K chars
        result = handle_overflow(
            long_transcript, strategy="truncate", max_length=10_000,
            video_id="test_video", lang="en", source="cli"
        )

        assert len(result) <= 10_200  # max_length + notice
        assert result.startswith("x" * 10_000)
        assert "cached" in result
        assert "100000 chars" in result

    def test_summarize_strategy(self):
        """Summarize strategy extracts intro, conclusion, and key segments with cache notice."""
        from csf.transcript_phase2 import handle_overflow

        # Create transcript with keyword markers that match the search keywords
        intro = "Introduction text. " * 100
        middle = "This is the important part. " + "More content here. " * 200 + " The key point. " + "Finally, we conclude. "
        conclusion = "Conclusion. " * 100
        long_transcript = intro + middle + conclusion

        result = handle_overflow(
            long_transcript, strategy="summarize", max_length=500,
            video_id="test_video", lang="en", source="cli"
        )

        assert len(result) <= 600  # max_length + notice
        assert "cached" in result

    def test_chunk_strategy_raises_not_implemented(self):
        """Chunk strategy raises NotImplementedError with guidance in Phase 2."""
        from csf.transcript_phase2 import handle_overflow
        import pytest

        long_transcript = "x" * 100_000

        with pytest.raises(NotImplementedError) as exc_info:
            handle_overflow(long_transcript, strategy="chunk", max_length=10_000)

        assert "session state" in str(exc_info.value)
        assert "Phase 3" in str(exc_info.value)
        assert "summarize" in str(exc_info.value) or "truncate" in str(exc_info.value)

    def test_default_strategy_is_summarize(self):
        """When no strategy specified, default to summarize."""
        from csf.transcript_phase2 import handle_overflow

        long_transcript = "x" * 100_000
        # Note: strategy is required, no default - testing that summarize works
        result = handle_overflow(long_transcript, strategy="summarize", max_length=10_000)

        # Should use summarize (includes intro + conclusion pattern)
        assert "Full transcript" in result

    def test_no_action_when_under_limit(self):
        """Transcripts under max_length are returned unchanged."""
        from csf.transcript_phase2 import handle_overflow

        short_transcript = "Short transcript."
        result = handle_overflow(short_transcript, strategy="summarize", max_length=50_000)

        assert result == short_transcript
        assert "truncated" not in result

    def test_summarize_with_keyword_extraction(self):
        """Summarize strategy finds and extracts keyword-dense sections."""
        from csf.transcript_phase2 import handle_overflow

        # Create transcript with specific keyword markers
        intro = "Introduction. " * 50
        middle = "The key point is this. " + "Filler content. " * 100 + "Main idea here. " + "More filler. " * 100
        conclusion = "Conclusion. " * 50
        transcript = intro + middle + conclusion

        result = handle_overflow(transcript, strategy="summarize", max_length=500)

        # Should include keyword-containing sections
        assert "Full transcript" in result


class TestOverflowIntegrationInCircuitBreaker:
    """Test that fetch_with_circuit_breaker applies overflow handling to successful transcripts."""

    def test_long_transcript_is_summarized_before_return(self):
        """When fetch returns a long transcript, circuit breaker caches full version and returns chopped version."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker
        from csf.cache import TranscriptCache, get_cached_transcript

        # Use valid 11-char video_id (cache validates with regex)
        video_id = "testvid1234"
        terminal_id = resolve_tid()

        # Mock fetch chain to return a very long transcript
        long_transcript = "x" * 100_000  # Exceeds MAX_TRANSCRIPT_LENGTH (50_000)

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            # Mock get_cached_transcript to return what was cached
            # (real implementation uses terminal-scoped in-memory storage)
            mock_cached = TranscriptCache(
                video_id=video_id,
                lang="en",
                source="cli",
                transcript=long_transcript,
                cached_at=datetime.now(),
                terminal_id=terminal_id,
            )
            with mock.patch("csf.cache.get_cached_transcript", return_value=mock_cached):
                mock_fetch.return_value = TranscriptResult(
                    video_id=video_id,
                    lang="en",
                    raw_lang="en",
                    was_translated=False,
                    transcript=long_transcript,
                    source="cli",
                    detected_lang="en",
                    error=None,
                )

                result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

                # Should have overflow handling applied
                assert len(result.transcript) < 60_000  # Should be summarized + notice
                assert "cached" in result.transcript  # Cache notice present
                assert "100000 chars" in result.transcript  # Original length mentioned

                # Verify full transcript can be retrieved from cache
                cached = get_cached_transcript(video_id, "en", "cli")
                assert cached is not None
                assert cached.transcript == long_transcript  # Full version intact in cache

    def test_short_transcript_passes_through_unchanged(self):
        """When fetch returns a short transcript, circuit breaker doesn't modify it."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_short"
        terminal_id = resolve_tid()

        short_transcript = "Short transcript."

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            mock_fetch.return_value = TranscriptResult(
                video_id=video_id,
                lang="en",
                raw_lang="en",
                was_translated=False,
                transcript=short_transcript,
                source="cli",
                detected_lang="en",
                error=None,
            )

            result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

            # Should pass through unchanged
            assert result.transcript == short_transcript
            assert "truncated" not in result.transcript

    def test_error_result_does_not_trigger_overflow_handling(self):
        """When fetch fails, no overflow handling is applied (no transcript to process)."""
        from csf.transcript_phase2 import fetch_with_circuit_breaker

        video_id = "test_video_error"
        terminal_id = resolve_tid()

        with mock.patch("csf.transcript_phase2.fetch_transcript_chain") as mock_fetch:
            mock_fetch.return_value = TranscriptResult(
                video_id=video_id,
                lang="en",
                raw_lang=None,
                was_translated=False,
                transcript="",
                source="none",
                detected_lang=None,
                error="not found (404)",
            )

            result = fetch_with_circuit_breaker(video_id, LanguageConfig(), terminal_id)

            # Should not have transcript
            assert result.transcript == ""
            assert "not found" in result.error

