"""Availability routing layer for tiered video analysis system.

Routing priority: Tier 3 (cached transcript) → Tier 1 (Gemini SDK) →
Tier 2 (OCR/CLIP) → Tier 3 (transcript fallback).

Thread-safe _gemini_available flag with per-process reset at Pacific midnight.

Failure-aware routing: select_provider() uses per-channel success/failure
history to route around channels where certain providers consistently fail,
maximizing the probability of getting a high-quality result on the first try.
"""

from __future__ import annotations

import threading
import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from collections.abc import Callable

from csf.providers import (
    NonFatalAnalysisError,
    VideoAnalysisResult,
    TranscriptProvider,
)
from csf.cache import has_cached_transcript
from csf.transcript import _VIDEO_ID_PATTERN

# ---------------------------------------------------------------------------
# Module-level thread-safe Gemini availability state
# ---------------------------------------------------------------------------

_gemini_available: bool = True
_last_reset_date: datetime.date | None = None
_gemini_lock = threading.Lock()


def _get_pacific_date() -> datetime.date:
    """Return the current date in Pacific Time (UTC-8 or UTC-7 depending on DST)."""
    # Start from UTC timestamp
    utc_now = datetime.datetime.now(datetime.UTC)
    # Pacific timezone: UTC-8 (PST) or UTC-7 (PDT) depending on DST
    # DST starts second Sunday in March, ends first Sunday in November
    pacific_offset = -7 if _is_dst(utc_now) else -8
    pacific_tz = datetime.timezone(datetime.timedelta(hours=pacific_offset))
    pacific_now = utc_now.astimezone(pacific_tz)
    return pacific_now.date()


def _is_dst(utc_now: datetime.datetime) -> bool:
    """Return True if the given UTC datetime is during Pacific DST."""
    # DST: second Sunday in March 2am local → first Sunday in November 2am local
    # Approximate: March through November
    if utc_now.month < 3 or utc_now.month > 11:
        return False
    if utc_now.month > 3 and utc_now.month < 11:
        return True
    # March: DST starts second Sunday
    if utc_now.month == 3:
        # Find second Sunday
        first_day = datetime.date(utc_now.year, 3, 1)
        first_sunday = first_day + datetime.timedelta(
            days=(6 - first_day.weekday()) % 7
        )
        second_sunday = first_sunday + datetime.timedelta(days=7)
        return utc_now.date() >= second_sunday
    # November: DST ends first Sunday
    if utc_now.month == 11:
        first_day = datetime.date(utc_now.year, 11, 1)
        first_sunday = first_day + datetime.timedelta(
            days=(6 - first_day.weekday()) % 7
        )
        return utc_now.date() < first_sunday
    return False


def _check_and_reset_gemini() -> None:
    """Check if daily reset is needed and flip _gemini_available to True if so.

    Must be called under _gemini_lock.
    """
    global _gemini_available, _last_reset_date
    today = _get_pacific_date()
    if _last_reset_date is None or _last_reset_date < today:
        _gemini_available = True
        _last_reset_date = today


# ---------------------------------------------------------------------------
# Gemini SDK provider (Tier 1)
# ---------------------------------------------------------------------------

# Lazy reference to gemini_video_analyze
_gemini_video_analyze_ref: Callable[..., Any] | None = None


def _get_gemini_analyze() -> Callable[..., Any]:
    """Get the gemini_video_analyze function, loading it lazily once from bin/csf-analyze."""
    global _gemini_video_analyze_ref
    if _gemini_video_analyze_ref is None:
        import importlib.util
        from importlib.machinery import SourceFileLoader

        bin_path = str(Path(__file__).parent.parent / "bin" / "csf-analyze")
        loader = SourceFileLoader("csf_analyze", bin_path)
        spec = importlib.util.spec_from_loader("csf_analyze", loader)
        if spec is None:
            raise RuntimeError("Could not load csf-analyze module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _gemini_video_analyze_ref = module.gemini_video_analyze
    return _gemini_video_analyze_ref


class GeminiSDKProvider:
    """Tier 1: Gemini SDK video passthrough (full multi-modal analysis)."""

    __slots__ = ()

    def analyze(self, video_id: str, video_url: str) -> VideoAnalysisResult:
        """Analyze video using Gemini SDK with true video URL passthrough."""
        gemini_analyze = _get_gemini_analyze()
        try:
            raw_result = gemini_analyze(video_id, video_url)
        except Exception as e:
            # On quota error, mark Gemini unavailable for rest of this process
            if _is_quota_error(e):
                with _gemini_lock:
                    global _gemini_available
                    _gemini_available = False
            raise NonFatalAnalysisError(f"Gemini SDK failed for {video_id}: {e}") from e

        # Map raw dict result to VideoAnalysisResult
        return VideoAnalysisResult(
            title=raw_result.get("title", "Unknown"),
            summary=raw_result.get("summary", ""),
            key_topics=raw_result.get("key_topics", []),
            key_points=raw_result.get("key_points", []),
            code_snippets=[],
            visual_tags=[],
            mode="gemini_sdk",
            fallback_reason=raw_result.get("fallback_reason"),
        )


def _is_quota_error(exc: Exception) -> bool:
    """Return True if this exception represents a quota exhaustion (429) error."""
    msg = str(exc).lower()
    if "429" in msg or "quota" in msg or "resourceexhausted" in msg:
        return True
    # Also check for chained exceptions
    if exc.__cause__ is not None:
        return _is_quota_error(exc.__cause__)
    return False


# ---------------------------------------------------------------------------
# OCR/CLIP provider (Tier 2) — lazy import to avoid circular dependencies
# ---------------------------------------------------------------------------


def _load_ocr_clip_provider() -> Any:
    """Load and return the OCR/CLIP provider class lazily to avoid circular imports."""
    from csf.providers.ocr_clip_provider import OcrClipProvider as OCP

    return OCP


# ---------------------------------------------------------------------------
# Transcript provider (Tier 3)
# ---------------------------------------------------------------------------

# Re-use TranscriptProvider from csf.providers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_provider(
    video_id: str, video_url: str, channel_url: str | None = None
) -> Any:
    """Select and return the best available analysis provider.

    Failure-aware routing: if per-channel success/failure history is available
    for this channel_url, providers are reordered to try the most reliable one
    first (highest success rate). Circuit-breaker logic still applies to
    Gemini availability.

    Default priority (no history):
      1. Cached transcript exists → TranscriptProvider directly (zero cost)
      2. Gemini SDK available → GeminiSDKProvider
      3. OCR/CLIP available → OcrClipProvider
      4. Fallback → TranscriptProvider

    Args:
        video_id: YouTube video ID (must be 11 chars, alphanumeric + hyphen/underscore).
        video_url: Full YouTube URL (must be http or https).
        channel_url: Optional channel URL for failure-aware routing. If None,
            provider order falls back to default priority.

    Returns:
        An AnalysisProvider instance.

    Raises:
        ValueError: if video_id format or video_url scheme is invalid.
    """
    # Validate video_id format
    if not _VIDEO_ID_PATTERN.match(video_id):
        raise ValueError(
            f"Invalid video_id format: {video_id!r}. "
            "Expected 11-character YouTube video ID (alphanumeric, hyphen, underscore)."
        )

    # Validate video_url scheme
    parsed = urlparse(video_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Invalid URL scheme: {parsed.scheme!r}. "
            "video_url must use http or https scheme."
        )

    # Tier 3 optimization: if transcript is already cached, skip orchestration
    # and return TranscriptProvider directly (zero cost, deterministic)
    if has_cached_transcript(video_id):
        return TranscriptProvider()

    # Check and reset Gemini availability on every call
    with _gemini_lock:
        _check_and_reset_gemini()
        gemini_available = _gemini_available

    # Build candidate list in default quality order (highest first)
    tier1 = ("gemini_sdk", "GeminiSDKProvider")
    tier2 = ("ocr_clip", "OcrClipProvider")
    tier3 = ("transcript", "TranscriptProvider")

    candidates = [tier1[0], tier2[0], tier3[0]]

    # Failure-aware reordering: if we have channel history, sort by success rate
    # Higher success rate (succeeded/(succeeded+failed)) = try first
    if channel_url:
        try:
            from csf.batch_status import get_provider_scores

            scores = get_provider_scores(channel_url)
            if scores:
                # Sort candidates by success rate desc; unknown providers last
                def success_rate(provider_name: str) -> float:
                    if provider_name not in scores:
                        return -1.0  # unknown = try last
                    successes, failures = scores[provider_name]
                    total = successes + failures
                    if total == 0:
                        return -1.0
                    return successes / total

                candidates = sorted(candidates, key=success_rate, reverse=True)
        except Exception:
            pass  # Never let routing data affect availability

    # Instantiate providers in sorted order, skipping unavailable ones
    for provider_name in candidates:
        if provider_name == "gemini_sdk" and not gemini_available:
            continue
        if provider_name == "ocr_clip":
            try:
                OcrClipProvider = _load_ocr_clip_provider()
                return OcrClipProvider()
            except Exception:
                continue
        if provider_name == "gemini_sdk" and gemini_available:
            return GeminiSDKProvider()
        if provider_name == "transcript":
            return TranscriptProvider()

    # Should never reach here (TranscriptProvider is always available)
    return TranscriptProvider()


def analyze_video(
    video_id: str, video_url: str, provider: Any | None = None
) -> VideoAnalysisResult:
    """Analyze a video using the selected provider or orchestrator's default selection.

    Args:
        video_id: YouTube video ID (11 chars).
        video_url: Full YouTube URL.
        provider: Optional pre-selected provider instance. If None, select automatically.

    Returns:
        VideoAnalysisResult from the provider.

    Raises:
        ValueError: if video_id format or video_url scheme is invalid.
        NonFatalAnalysisError: if all provider tiers fail (propagated from provider).
    """
    if provider is None:
        provider = select_provider(video_id, video_url)
    assert provider is not None  # for pyright type narrowing

    try:
        return provider.analyze(video_id, video_url)
    except Exception as exc:
        # Wrap unexpected exceptions in NonFatalAnalysisError so callers can
        # distinguish fatal vs non-fatal failures
        raise NonFatalAnalysisError(
            f"Provider {provider.__class__.__name__} raised unexpected error: {exc}"
        ) from exc
