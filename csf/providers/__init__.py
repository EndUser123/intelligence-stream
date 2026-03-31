"""Video analysis providers with tiered availability.

Three provider tiers (ordered by quality):
  1. Gemini SDK video passthrough  (API quota, full multi-modal)
  2. OCR + CLIP pipeline            (zero cost, code-on-screen + visual tags)
  3. YouTube transcript only        (zero cost, free API)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from typing_extensions import override


class NonFatalAnalysisError(Exception):
    """Raised by a provider when a non-fatal failure occurs.

    The orchestrator catches this and falls through to the next tier.
    """

    pass


class AnalysisProvider(Protocol):
    """Protocol for video analysis providers.

    Each provider implements analyze(video_id, video_url) and returns
    a VideoAnalysisResult. Non-fatal failures raise NonFatalAnalysisError
    so the orchestrator can fall back to the next tier.
    """

    def analyze(self, video_id: str, video_url: str) -> VideoAnalysisResult:
        """Analyze a video.

        Args:
            video_id: YouTube video ID (11 chars).
            video_url: Full YouTube URL.

        Returns:
            VideoAnalysisResult with all fields populated.

        Raises:
            NonFatalAnalysisError: for non-fatal failures (orchestrator will retry next tier).
        """
        ...


@dataclass(frozen=True, slots=True)
class VideoAnalysisResult:
    """Result of a video analysis operation.

    Attributes:
        title: Video title or "Unknown".
        summary: 2-3 sentence summary.
        key_topics: List of 5 main subjects.
        key_points: List of 3 important takeaways.
        code_snippets: OCR-captured code on screen.
        visual_tags: CLIP-captured visual labels.
        mode: Which provider generated this result
              ("gemini_sdk" | "ocr_clip" | "transcript").
        fallback_reason: Why a lower-quality tier was used, or None if Tier 1 succeeded.
    """

    title: str = "Unknown"
    summary: str = ""
    key_topics: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    code_snippets: list[str] = field(default_factory=list)
    visual_tags: list[str] = field(default_factory=list)
    mode: str = "transcript"
    fallback_reason: str | None = None

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VideoAnalysisResult):
            return NotImplemented
        return (
            self.title == other.title
            and self.summary == other.summary
            and set(self.key_topics) == set(other.key_topics)
            and set(self.key_points) == set(other.key_points)
            and set(self.code_snippets) == set(other.code_snippets)
            and set(self.visual_tags) == set(other.visual_tags)
            and self.mode == other.mode
            and self.fallback_reason == other.fallback_reason
        )


# ---------------------------------------------------------------------------
# Built-in providers (lazy imports to avoid circular dependencies)
# ---------------------------------------------------------------------------


class TranscriptProvider:
    """Tier 3: YouTube transcript only (zero cost, always available)."""

    __slots__ = ()

    def analyze(self, video_id: str, _video_url: str) -> VideoAnalysisResult:
        """Fetch YouTube transcript and summarize via LLM."""
        from csf.summarize import summarize

        try:
            from csf.transcript import fetch_transcript_chain, LanguageConfig

            result = fetch_transcript_chain(video_id, LanguageConfig(prefer_lang="en"))
            transcript_text = result.transcript
        except Exception:
            raise NonFatalAnalysisError(f"Transcript fetch failed for {video_id}")

        if not transcript_text:
            raise NonFatalAnalysisError(f"No transcript available for {video_id}")

        return summarize(
            transcript=transcript_text,
            code_snippets=[],
            visual_tags=[],
        )
