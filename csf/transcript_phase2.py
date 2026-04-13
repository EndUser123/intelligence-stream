"""Phase 2: Agent-level circuit breaker and overflow handling for transcript fetching.

This module provides:
1. Agent-level circuit breaker with terminal-scoped retry state
2. Context overflow detection and handling
3. Error classification for transient vs permanent failures
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from csf.retry_queue import get_retry_entry
from csf.terminal_context import resolve_tid
from csf.transcript import LanguageConfig, TranscriptResult, fetch_transcript_chain
from csf.cache import set_cached_transcript

logger = logging.getLogger(__name__)

# Constants
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0]  # Exponential backoff (cap at 60s)

# Error classification table
TRANSIENT_ERRORS = {429, 503, 504}
PERMANENT_ERRORS = {400, 404}


def _classify_error(http_code: int | None, error_message: str) -> Literal["transient", "permanent"]:
    """Classify an error as transient or permanent.

    Args:
        http_code: HTTP status code (if applicable).
        error_message: Error message content.

    Returns:
        "transient" if the error should trigger retries, "permanent" if not.
    """
    # Check for timeout in error message
    if http_code is None and "timeout" in error_message.lower():
        return "transient"

    # Classify by HTTP code
    if http_code in TRANSIENT_ERRORS:
        return "transient"
    if http_code in PERMANENT_ERRORS:
        return "permanent"

    # 403 is special: may succeed with cookies (geo-restriction, age-gate)
    if http_code == 403:
        return "transient"

    # Default: treat unknown errors as permanent to avoid infinite loops
    return "permanent"


@dataclass
class CircuitBreakerConfig:
    """Configuration for the agent-level circuit breaker.

    Attributes:
        max_retry_attempts: Maximum number of retry attempts (default: 3).
        retry_backoff: List of backoff delays in seconds (exponential).
        terminal_id: Terminal identifier for state isolation.
    """

    max_retry_attempts: int = MAX_RETRY_ATTEMPTS
    retry_backoff: list[float] = field(default_factory=lambda: RETRY_BACKOFF.copy())
    terminal_id: str | None = None

    def __post_init__(self):
        if self.terminal_id is None:
            self.terminal_id = resolve_tid()


def fetch_with_circuit_breaker(
    video_id: str, config: LanguageConfig, terminal_id: str
) -> TranscriptResult:
    """Fetch transcript with agent-level circuit breaker to prevent infinite retry loops.

    Wraps the existing fetch_transcript_chain() with retry logic that:
    - Distinguishes transient errors (rate limit, timeout) from permanent errors (404, invalid ID)
    - Uses exponential backoff between retries
    - Maintains terminal-scoped retry state for multi-terminal isolation

    Args:
        video_id: YouTube video ID (11 characters).
        config: Language configuration for the fetch.
        terminal_id: Terminal identifier for state isolation.

    Returns:
        TranscriptResult with success=True if fetch succeeded, success=False otherwise.
    """
    # Use config directly - LanguageConfig is already defined in transcript.py

    # Check if we've already exhausted retries for this video+terminal
    retry_entry = get_retry_entry(video_id)
    if retry_entry and retry_entry.retry_count >= MAX_RETRY_ATTEMPTS:
        return TranscriptResult(
            video_id=video_id,
            lang=config.prefer_lang if hasattr(config, "prefer_lang") else "en",
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            detected_lang=None,
            error=f"Max retries exceeded (terminal: {terminal_id})",
        )

    # Attempt fetch with retries
    for attempt in range(MAX_RETRY_ATTEMPTS):
        result = fetch_transcript_chain(video_id, config)

        # Success - cache full transcript, then apply overflow handling
        if result.transcript:
            # Cache the full transcript before chopping
            set_cached_transcript(video_id, result.lang, result.source, result.transcript)

            # Apply overflow handling with summarize strategy (default)
            result.transcript = handle_overflow(
                result.transcript,
                strategy="summarize",
                max_length=MAX_TRANSCRIPT_LENGTH,
                video_id=video_id,
                lang=result.lang,
                source=result.source
            )
            return result

        # Check if error is permanent - no retries
        error_type = _classify_error_from_result(result)
        if error_type == "permanent":
            logger.warning(
                f"[CircuitBreaker] Permanent error for {video_id} "
                f"(terminal: {terminal_id}, attempt: {attempt + 1}): {result.error}"
            )
            return result

        # Transient error - log and retry with backoff
        if attempt < MAX_RETRY_ATTEMPTS - 1:
            backoff = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            logger.info(
                f"[CircuitBreaker] Transient error for {video_id} "
                f"(terminal: {terminal_id}, attempt: {attempt + 1}): {result.error}. "
                f"Retrying in {backoff}s..."
            )
            time.sleep(backoff)

    # All retries exhausted
    return TranscriptResult(
        video_id=video_id,
        lang=config.prefer_lang if hasattr(config, "prefer_lang") else "en",
        raw_lang=None,
        was_translated=False,
        transcript="",
        source="none",
        detected_lang=None,
        error=f"Max retries exceeded (tried {MAX_RETRY_ATTEMPTS} times, terminal: {terminal_id})",
    )


def _classify_error_from_result(result: TranscriptResult) -> Literal["transient", "permanent"]:
    """Classify error from TranscriptResult as transient or permanent.

    Args:
        result: TranscriptResult from fetch attempt.

    Returns:
        "transient" if error should trigger retries, "permanent" if not.
    """
    if not result.error:
        return "permanent"

    error_lower = result.error.lower()

    # Check for timeout
    if "timeout" in error_lower:
        return "transient"

    # Extract HTTP code from error message if present
    import re

    # Match patterns like: "http error 429", "status 429", "rate limited (429)", "error:429"
    http_code_match = re.search(r"(?:http error:? |status |(?:\(|:)\s*)(\d{3})", result.error, re.IGNORECASE)
    if http_code_match:
        http_code = int(http_code_match.group(1))
        return _classify_error(http_code, result.error)

    # Default: treat unknown errors as permanent
    return "permanent"


# Phase 2: Overflow handling (TASK-002)
MAX_TRANSCRIPT_LENGTH = 50_000
OverflowStrategy = Literal["truncate", "summarize", "chunk"]


def handle_overflow(
    transcript: str,
    strategy: OverflowStrategy,
    max_length: int = MAX_TRANSCRIPT_LENGTH,
    video_id: str | None = None,
    lang: str = "en",
    source: str = "unknown"
) -> str:
    """Handle transcripts that exceed context limits.

    Args:
        transcript: Full transcript text.
        strategy: Overflow strategy ("truncate", "summarize", "chunk").
        max_length: Maximum length for output.
        video_id: YouTube video ID for cache notice (optional).
        lang: Language code for cache notice (default: "en").
        source: Transcript source for cache notice (default: "unknown").

    Returns:
        Processed transcript within max_length, with notice about full version.
    """
    if len(transcript) <= max_length:
        return transcript

    # Log overflow event
    logger.info(
        f"[Overflow] Transcript overflow detected: {len(transcript)} chars "
        f"-> using strategy '{strategy}' with max_length={max_length}"
    )

    if strategy == "truncate":
        return _truncate_transcript(transcript, max_length, video_id, lang, source)
    elif strategy == "summarize":
        return _summarize_transcript(transcript, max_length, video_id, lang, source)
    elif strategy == "chunk":
        return _chunk_transcript_phase2(transcript, max_length)
    else:
        # Default to summarize for unknown strategy
        return _summarize_transcript(transcript, max_length, video_id, lang, source)


def _truncate_transcript(
    transcript: str, max_length: int, video_id: str | None = None,
    lang: str = "en", source: str = "unknown"
) -> str:
    """Truncate transcript to max_length with cache notice.

    Simple length cutoff - fastest but loses content.
    """
    cache_notice = _get_cache_notice(video_id, lang, source, len(transcript))
    return transcript[:max_length] + f"\n\n{cache_notice}"


def _summarize_transcript(
    transcript: str, max_length: int, video_id: str | None = None,
    lang: str = "en", source: str = "unknown"
) -> str:
    """Summarize transcript by extracting key segments.

    Combines intro (first 10%), conclusion (last 20%), and middle keyword-dense sections.
    """
    intro_end = len(transcript) // 10
    conclusion_start = len(transcript) - (len(transcript) // 5)
    middle = transcript[intro_end:conclusion_start]

    # Find keyword-dense sections in middle
    keywords = ["important", "key", "main", "conclusion", "finally"]
    key_segments = []
    for kw in keywords:
        if f" {kw} " in middle.lower():
            # Extract sentence around keyword
            idx = middle.lower().find(f" {kw} ")
            if idx != -1:
                start = max(0, idx - 50)
                end = min(len(middle), idx + 100)
                key_segments.append(middle[start:end])

    summary = transcript[:intro_end] + "\n\n".join(key_segments) + transcript[conclusion_start:]
    cache_notice = _get_cache_notice(video_id, lang, source, len(transcript))
    return f"{summary[:max_length]}\n\n{cache_notice}"


def _get_cache_notice(video_id: str | None, lang: str, source: str, original_length: int) -> str:
    """Generate cache-aware notice for truncated/summarized transcripts.

    Args:
        video_id: YouTube video ID (if available).
        lang: Language code.
        source: Transcript source.
        original_length: Original transcript length in characters.

    Returns:
        Notice message pointing to cache.
    """
    if video_id:
        return f"[Full transcript cached: {video_id} / {lang} / {source} - {original_length} chars]"
    else:
        # Fallback for when video_id not available
        return f"[Full transcript: {original_length} chars - cached locally]"


def _chunk_transcript_phase2(transcript: str, max_length: int) -> str:
    """Chunk strategy - Phase 2 behavior.

    Phase 2 raises NotImplementedError with clear guidance.
    Full chunking with session state is deferred to Phase 3.
    """
    raise NotImplementedError(
        "Chunk strategy requires session state mechanism - scheduled for Phase 3. "
        "Use strategy='summarize' or strategy='truncate' as alternatives."
    )
