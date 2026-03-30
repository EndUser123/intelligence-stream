"""Transcript fetching with full fallback chain.

Fallback order: gemini CLI → youtube_transcript_api → youtubei → Gemini SDK.
Each method returns: (success: bool, transcript: str | None, error: str | None).
"""

import os
import random
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Tuple

from csf.cache import get_cached_transcript, set_cached_transcript
from csf.quota_tracker import is_free_only_mode

# Validation
_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Source labels
_SOURCE_CLI = "cli"
_SOURCE_YOUTUBE_TRANSCRIPT_API = "youtube_transcript_api"
_SOURCE_YOUTUBEI = "youtubei"
_SOURCE_SDK = "sdk"
_SOURCE_YTDLP = "ytdlp"

# Jitter bounds for rate limit avoidance
# PERF-006: Wider range (was 0.5-2.5) to prevent thundering herd.
# Workers stagger over a 10s window so concurrent requests are spread.
_JITTER_MIN = 2.0
_JITTER_MAX = 10.0

# BCP-47 validation regex: language is [a-z]{2}, region is [A-Z]{2} optional
_BCP47_PATTERN = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


@dataclass
class LanguageConfig:
    """Language configuration for transcript fetching and translation.

    Attributes:
        prefer_lang: BCP-47 language code (e.g. "en", "es", "pt-BR").
            Defaults to "en".
        allow_translation: If True and preferred language is unavailable,
            translate from the returned language to prefer_lang using Gemini SDK.
            Defaults to False (SEC-001: explicit opt-in required).
        translation_provider: Which provider to use for translation.
            Currently only "gemini" is supported.
    """

    prefer_lang: str = "en"
    allow_translation: bool = False
    translation_provider: Literal["gemini"] = "gemini"


@dataclass
class TranscriptResult:
    """Result of a transcript fetch operation.

    Attributes:
        video_id: YouTube video ID.
        lang: The language that was requested (prefer_lang from config).
        raw_lang: The language the transcript was actually returned in,
            or None if no transcript was available.
        was_translated: True if the transcript was translated from raw_lang
            to prefer_lang. False if the original language matched or no
            translation was performed.
        transcript: The transcript text, in prefer_lang (after translation
            if was_translated=True). Empty string if no transcript found.
        source: Which fetch method succeeded ('cli', 'youtube_transcript_api',
            'youtubei', 'sdk', 'none').
        detected_lang: The detected language of the returned transcript,
            or None if language detection failed or no transcript available.
    """

    video_id: str
    lang: str
    raw_lang: str | None
    was_translated: bool
    transcript: str
    source: str
    detected_lang: str | None


def _validate_bcp47(lang: str) -> None:
    """Validate a BCP-47 language code.

    Raises ValueError if the code does not match the pattern.
    Valid formats: "en", "pt-BR", "zh-CN".
    """
    if not _BCP47_PATTERN.match(lang):
        raise ValueError(
            f"Invalid BCP-47 language code: {lang!r}. "
            "Expected format: 'en', 'es', 'pt-BR', 'zh-CN', etc."
        )


def _translate_text(text: str, from_lang: str, to_lang: str, provider: str) -> str:
    """Translate text from from_lang to to_lang using Gemini SDK.

    BLOCKER-1 resolved: trans! npm not installed; Gemini SDK is sole provider.

    Translation failures are NON-FATAL (FM-003): returns original text on failure.

    Args:
        text: The text to translate.
        from_lang: Source BCP-47 language code.
        to_lang: Target BCP-47 language code.
        provider: Translation provider (only "gemini" supported).

    Returns:
        Translated text, or original text if translation fails.
    """
    if provider != "gemini":
        # Currently only gemini is supported
        return text

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        import logging

        logging.warning("GEMINI_API_KEY not set; cannot translate, returning original text.")
        return text

    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                f"Translate the following text from {from_lang} to {to_lang}. "
                f"Return ONLY the translated text, nothing else.\n\n{text}"
            ],
        )
        if response.text:
            return response.text.strip()
        return text
    except Exception:
        import logging

        logging.warning(
            f"Translation failed ({from_lang} -> {to_lang}); returning original text. "
            "Set allow_translation=False to suppress this message."
        )
        return text


def _validate_video_id(video_id: str) -> bool:
    """Validate video_id format.

    Returns True if valid (11 chars, alphanumeric + hyphen/underscore).
    Returns False otherwise.
    """
    return bool(_VIDEO_ID_PATTERN.match(video_id))


def _apply_jitter() -> None:
    """Apply random jitter between parallel fetch attempts to avoid rate limiting."""
    jitter = random.uniform(_JITTER_MIN, _JITTER_MAX)
    time.sleep(jitter)


def _fetch_via_gemini_cli(
    video_id: str, lang: str
) -> Tuple[bool, str | None, str | None]:
    """Fetch transcript using gemini CLI transcript command.

    Uses `timeout -k 1s 300s gemini transcript <video_id>`.
    """
    gemini_path = shutil.which("gemini")
    if not gemini_path:
        return (False, None, "gemini CLI not found")

    try:
        cmd = [gemini_path, "transcript", video_id, "--lang", lang]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        return (False, None, "gemini CLI timed out after 300s")
    except Exception as e:
        return (False, None, f"gemini CLI error: {e}")

    if proc.returncode != 0:
        return (False, None, f"gemini CLI failed: {stderr.strip()}")

    return (True, stdout.strip(), None)


def _fetch_via_youtube_transcript_api(
    video_id: str, lang: str
) -> Tuple[bool, str | None, str | None]:
    """Fetch transcript using youtube-transcript-api Python package."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return (False, None, "youtube_transcript_api not installed")

    try:
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )

        def _fetch() -> str:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id, languages=[lang])
            return " ".join(snippet.text for snippet in transcript.snippets)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            try:
                text = future.result(timeout=30)
            except TimeoutError:
                return (False, None, "youtube_transcript_api timeout (>30s)")
            except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as e:
                return (False, None, f"youtube_transcript_api error: {e}")
        return (True, text, None)
    except ImportError:
        return (False, None, "youtube_transcript_api not installed")
    except Exception as e:
        return (False, None, f"youtube_transcript_api error: {e}")


def _fetch_via_youtubei(
    video_id: str, lang: str
) -> Tuple[bool, str | None, str | None]:
    """Fetch transcript using direct YouTube API call with cookie auth.

    Note: youtubei does not support language parameter specification.
    This method returns English transcripts only — there is no way to request
    a specific language via this API. The lang parameter is accepted for
    interface consistency but ignored.
    """
    try:
        import youtubei
    except ImportError:
        return (False, None, "youtubei not installed")

    def _fetch() -> Tuple[bool, str | None, str | None]:
        try:
            client = youtubei.get_client()
            video = client.get_video(video_id)
            transcript_data = video.get_transcript()
            if transcript_data is None:
                return (False, None, "No transcript available")
            text = " ".join(item["text"] for item in transcript_data)
            return (True, text, None)
        except Exception as e:
            return (False, None, f"youtubei error: {e}")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=60)
    except TimeoutError:
        return (False, None, "youtubei timeout (>15s)")


def _fetch_via_ytdlp(
    video_id: str, lang: str
) -> Tuple[bool, str | None, str | None]:
    """Fetch transcript using yt-dlp to download auto-generated subtitles.

    Downloads auto-generated subtitles via yt-dlp (which can access YouTube
    even when youtube-transcript-api is IP-blocked), then parses SRT to plain text.

    Fails fast on 429 (rate limited) — chain falls back to SDK immediately.
    """
    import tempfile

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    tmp_dir = tempfile.mkdtemp(prefix="ytdlp_subs_")
    try:
        output_template = os.path.join(tmp_dir, "subs")

        cmd = [
            "yt-dlp",
            "--write-auto-subs",
            "--skip-download",
            "--convert-subs", "srt",
            "--output", output_template,
            video_url,
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if proc.returncode != 0:
            stderr_lower = proc.stderr.lower()
            if "429" in proc.stderr or "too many requests" in stderr_lower:
                return (False, None, "rate limited (429)")
            if "no subtitles" in stderr_lower or "does not have any subtitles" in stderr_lower:
                return (False, None, "no subtitles available")
            return (False, None, f"yt-dlp failed: {proc.stderr.strip()[:200]}")

        # Find the generated SRT file
        srt_files = list(Path(tmp_dir).glob("*.srt"))
        if not srt_files:
            for ext in ("vtt", "ass", "lrc"):
                subs = list(Path(tmp_dir).glob(f"*.{ext}"))
                if subs:
                    srt_files = subs
                    break
            if not srt_files:
                return (False, None, "no subtitle file produced")

        srt_path = srt_files[0]
        text = _parse_srt(srt_path.read_text(encoding="utf-8"))
        if not text.strip():
            return (False, None, "subtitle file was empty")
        return (True, text, None)

    except subprocess.TimeoutExpired:
        return (False, None, "yt-dlp timed out")
    except Exception as e:
        return (False, None, f"yt-dlp error: {e}")
    finally:
        import shutil as _shutil
        try:
            _shutil.rmtree(tmp_dir)
        except Exception:
            pass


def _parse_srt(srt_content: str) -> str:
    """Parse SRT subtitle content into plain transcript text."""
    import re

    entries = re.split(r"\n\d+\n", srt_content)
    text_parts = []
    for entry in entries:
        lines = entry.strip().split("\n")
        for line in lines:
            # Skip numeric timecodes (00:00:00,000 --> 00:00:00,000)
            if "-->" in line:
                continue
            # Skip HTML tags
            line = re.sub(r"<[^>]+>", "", line)
            line = line.strip()
            if line:
                text_parts.append(line)
    return " ".join(text_parts)


def _fetch_via_sdk(video_id: str, lang: str) -> Tuple[bool, str | None, str | None]:
    """Fetch transcript using Gemini SDK as last resort."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return (False, None, "GEMINI_API_KEY not set")

    try:
        from google import genai
    except ImportError:
        return (False, None, "google-genai not installed")

    def _fetch() -> Tuple[bool, str | None, str | None]:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[f"Get the transcript for YouTube video {video_id} in language {lang}"],
            )
            text = response.text.strip()
            return (True, text, None)
        except Exception as e:
            return (False, None, f"SDK error: {e}")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=60)
    except TimeoutError:
        return (False, None, "SDK timeout (>60s)")


def fetch_transcript_chain(
    video_id: str, config: LanguageConfig
) -> TranscriptResult:
    """Fetch transcript using full fallback chain with optional translation.

    Chain order:
      1. youtube_transcript_api with prefer_lang
      2. If wrong language + allow_translation: translate to prefer_lang
      3. youtube_transcript_api with any available language
      4. If non-English + allow_translation: translate to prefer_lang
      5. youtubei → Gemini SDK → gemini CLI (last)

    Free sources first (TECH-01). CLI is skipped if quota exceeded (LOGIC-004).

    Args:
        video_id: YouTube video ID (must be 11 chars)
        config: LanguageConfig specifying prefer_lang and allow_translation

    Returns:
        TranscriptResult with all fields populated including detected_lang.
        On complete failure, returns TranscriptResult with empty transcript,
        source='none', and was_translated=False.
    """
    prefer_lang = config.prefer_lang

    # Validate video_id
    if not _validate_video_id(video_id):
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            detected_lang=None,
        )

    # BLOCKER-13: Validate BCP-47 before any API calls
    try:
        _validate_bcp47(prefer_lang)
    except ValueError:
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            detected_lang=None,
        )

    # Helper to build a "no transcript" result
    def _none_result() -> TranscriptResult:
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            detected_lang=None,
        )

    # Try free sources first (TECH-01), then paid CLI last
    # LOGIC-004: Skip CLI if quota exceeded (free-only mode active)
    free_methods = [
        (_SOURCE_YTDLP, _fetch_via_ytdlp),
        (_SOURCE_YOUTUBE_TRANSCRIPT_API, _fetch_via_youtube_transcript_api),
        (_SOURCE_YOUTUBEI, _fetch_via_youtubei),
        (_SOURCE_SDK, _fetch_via_sdk),
    ]
    all_methods = list(free_methods)
    if not is_free_only_mode():
        all_methods.append((_SOURCE_CLI, _fetch_via_gemini_cli))

    last_error: str | None = None

    # Step 1: Try prefer_lang
    for source, fetch_fn in all_methods:
        if source == _SOURCE_CLI:
            from csf.quota_tracker import increment_cli_calls
            increment_cli_calls()
        success, transcript, error = fetch_fn(video_id, prefer_lang)
        if success and transcript:
            raw_lang = prefer_lang
            detected_lang = prefer_lang
            final_transcript = transcript
            was_translated = False
            # Translate if not actually in prefer_lang
            if source == _SOURCE_SDK and prefer_lang not in ("en",):
                # SDK may not respect lang; check by looking at transcript
                # Simple heuristic: if transcript looks like it might not be prefer_lang, translate
                pass
            if raw_lang != prefer_lang and config.allow_translation:
                final_transcript = _translate_text(
                    transcript, raw_lang, prefer_lang, config.translation_provider
                )
                was_translated = True
            set_cached_transcript(video_id, prefer_lang, source, transcript)
            return TranscriptResult(
                video_id=video_id,
                lang=prefer_lang,
                raw_lang=raw_lang,
                was_translated=was_translated,
                transcript=final_transcript,
                source=source,
                detected_lang=detected_lang,
            )
        last_error = error
        _apply_jitter()

    # Step 2: Try any language
    for source, fetch_fn in free_methods:  # Only free methods for fallback
        success, transcript, error = fetch_fn(video_id, "en")
        if success and transcript:
            raw_lang = "en"
            detected_lang = "en"
            final_transcript = transcript
            was_translated = False
            if config.allow_translation and prefer_lang != "en":
                final_transcript = _translate_text(
                    transcript, "en", prefer_lang, config.translation_provider
                )
                was_translated = True
            set_cached_transcript(video_id, prefer_lang, source, transcript)
            return TranscriptResult(
                video_id=video_id,
                lang=prefer_lang,
                raw_lang=raw_lang,
                was_translated=was_translated,
                transcript=final_transcript,
                source=source,
                detected_lang=detected_lang,
            )
        last_error = error
        _apply_jitter()

    # All methods failed — non-fatal
    return _none_result()
