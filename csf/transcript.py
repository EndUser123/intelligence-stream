"""Transcript fetching with full fallback chain.

Fallback order:
oEmbed → ytdlp → ytdlp_ejs → direct_api → notebooklm → selenium → whisper.
Each method returns: (success: bool, transcript: str | None, error: str | None).
"""

import glob
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Literal, TYPE_CHECKING

from csf.nlm_config import NLMConfig, get_nlm_config, set_nlm_config
from csf.batch_status import (
    get_source as _get_source_for_video,
    mark_failed as _mark_failed_video,
    set_negative_cache as _set_negative_cache,
)
from csf.batch_scheduler import BatchScheduler
from csf.cache import set_cached_transcript
from csf.csf_logging import log_action
from csf.youtube_auth import get_browser_cookies

if TYPE_CHECKING:
    from csf.nlm_scraper import NLMIndustrialScraper


# Module-level singleton — avoids repeated _recover_stale_attempting() +
# PRAGMA wal_checkpoint overhead when many 429s/successes fire under concurrency.
_scheduler: BatchScheduler | None = None

_NEGATIVE_CACHE_SOFT_TTL_SECONDS = 24 * 3600
_NEGATIVE_CACHE_TERMINAL_TTL_SECONDS = 3650 * 24 * 3600


def _get_scheduler() -> BatchScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BatchScheduler()
    return _scheduler


# Module-level NLM scraper singleton — one terminal-local staging notebook
# reused across all _fetch_via_notebooklm calls within this process.
_nlm_scraper: "NLMIndustrialScraper | None" = None


def _get_nlm_scraper() -> "NLMIndustrialScraper":
    global _nlm_scraper
    if _nlm_scraper is None:
        _ensure_nlm_auth()
        from csf.nlm_scraper import NLMIndustrialScraper

        _nlm_scraper = NLMIndustrialScraper(headless=True, browser_cfg=get_nlm_config())
    else:
        # Refresh auth check on every call to catch mid-session expiry
        _ensure_nlm_auth()
    return _nlm_scraper


# Validation
_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Source labels
_SOURCE_CLI = "cli"
_SOURCE_YOUTUBE_TRANSCRIPT_API = "youtube_transcript_api"
_SOURCE_YOUTUBEI = "youtubei"
_SOURCE_SDK = "sdk"
_SOURCE_YTDLP = "ytdlp"
_SOURCE_YTDLP_EJS = "ytdlp_ejs"
_SOURCE_WHISPER = "whisper"
_SOURCE_SELENIUM = "selenium"
_SOURCE_NLM = "notebooklm"
_SOURCE_EXTERNAL = "external"
_SOURCE_DIRECT_API = "direct_api"

# Source-stage versioning for transcript provenance
# When NotebookLM changes its JSON response structure, source_stage increments.
# Re-fetches with higher source_stage win over stale lower-stage content.
STAGE_VERSION_YTDLP = 1
STAGE_VERSION_EJS = 1
STAGE_VERSION_SELENIUM = 1
STAGE_VERSION_NOTEBOOKLM = 1
STAGE_VERSION_DIRECT_API = 2

# Pluggable external transcript provider hook — called after all built-in
# stages (yt-dlp → cookies → Selenium → NLM) have failed.
# Set via register_external_transcript_provider().
# Signature: (video_id: str, lang: str) -> tuple[bool, str | None, str | None]
# Returns: (success, transcript_text, error)
_external_provider: Callable[[str, str], tuple[bool, str | None, str | None]] | None = None


def register_external_transcript_provider(provider: Callable[[str, str], tuple[bool, str | None, str | None]]) -> None:
    """Register an external transcript provider as the final fallback.

    The provider is called after all built-in stages fail.
    It must have signature: (video_id: str, lang: str)
    -> tuple[bool, str | None, str | None]  (success, transcript, error)

    Args:
        provider: A callable that takes (video_id, lang) and returns
            (success: bool, transcript: str | None, error: str | None).
            Return (False, None, error) on failure.
    """
    global _external_provider
    _external_provider = provider

# Jitter bounds for rate limit avoidance
# PERF-006: Wider range (was 0.5-2.5) to prevent thundering herd.
# Workers stagger over a 10s window so concurrent requests are spread.
_JITTER_MIN = 2.0
_JITTER_MAX = 10.0

# BCP-47 validation regex: language is [a-z]{2}, region is [A-Z]{2} optional
_BCP47_PATTERN = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")

# Per-source circuit breaker state
import threading

_consecutive_429: dict[str, int] = {}
_source_cooldown_until: dict[str, float] = {}
_circuit_lock = threading.Lock()

_CIRCUIT_OPEN_THRESHOLD = 3  # consecutive 429s before skipping source
_COOLDOWN_SECONDS = 300  # 5 minutes
_BACKOFF_BASE = 2  # jitter multiplier per consecutive 429
_MAX_BACKOFF_MULTIPLIER = 32  # cap jitter at 32x to prevent pathological sleeps

# Minimum transcript content length in characters (accepted at 21 chars, rejected below)
_NLM_MIN_CONTENT_CHARS = 21

# Whisper fallback — set YTIS_WHISPER_ENABLED=false to disable
_WHISPER_ENABLED: bool | None = None  # lazily loaded from env

# Whisper audio download prefers broad selectors so we do not fail valid
# videos just because a particular extension is unavailable.
_WHISPER_AUDIO_FORMATS: tuple[str, ...] = (
    "bestaudio/best",
    "bestaudio",
    "best",
)

# Cookie file cache - avoid repeated Firefox cookies.sqlite extraction per video
_cookie_cache: dict[str, str | int | float] = {}  # {path: str, refcount: int, expiry: float}
_cookie_lock = threading.Lock()
COOKIE_CACHE_TTL = 300  # 5 minutes


# AuthRateLimiter — per-process singleton for call-frequency tracking
_auth_rate_limiter_lock = threading.Lock()
_auth_rate_limiter: "AuthRateLimiter | None" = None


class AuthRateLimiter:
    """Tracks NLM auth call frequency and enforces cooldown on failures.

    Thread-safe per-process singleton. Blocks after auth_max_calls_per_window
    calls within auth_check_interval seconds. Triggers auth_cooldown seconds
    cooldown after 3 consecutive --force login failures.

    Fail-closed on lock error: if lock acquisition fails, is_allowed() returns
    False and the call is blocked.
    """

    def __init__(self) -> None:
        self._call_timestamps: list[float] = []
        self._cooldown_until: float = 0.0
        self._consecutive_failures: int = 0
        self._lock = threading.Lock()

    def _is_in_cooldown(self) -> bool:
        """Return True if currently in cooldown period."""
        return time.monotonic() < self._cooldown_until

    def is_allowed(self) -> bool:
        """Return True if auth call is allowed. Fail-closed on lock error."""
        try:
            acquired = self._lock.acquire(timeout=0.1)
        except Exception:
            # Fail-closed: block the call and log error
            logging.error("[AuthRateLimiter] lock acquisition failed — blocking call")
            return False
        if not acquired:
            logging.error("[AuthRateLimiter] could not acquire lock — blocking call")
            return False
        try:
            if self._is_in_cooldown():
                return False
            config = get_nlm_config()
            now = time.monotonic()
            window_start = now - config.auth_check_interval
            self._call_timestamps = [ts for ts in self._call_timestamps if ts > window_start]
            if len(self._call_timestamps) >= config.auth_max_calls_per_window:
                return False
            return True
        finally:
            self._lock.release()

    def record_call(self) -> None:
        """Record an auth call timestamp. Thread-safe."""
        with self._lock:
            self._call_timestamps.append(time.monotonic())

    def record_auth_failure(self) -> None:
        """Record a --force login failure. Triggers cooldown after 3 consecutive."""
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._cooldown_until = time.monotonic() + get_nlm_config().auth_cooldown
                logging.warning(
                    f"[AuthRateLimiter] 3 consecutive auth failures — entering "
                    f"{get_nlm_config().auth_cooldown}s cooldown"
                )

    def record_auth_success(self) -> None:
        """Reset failure counter on successful --force login."""
        with self._lock:
            self._consecutive_failures = 0

    def remaining(self) -> int:
        """Return the number of auth calls remaining in the current window.

        Returns 0 if in cooldown or if the window is exhausted.
        Thread-safe.
        """
        with self._lock:
            if self._is_in_cooldown():
                return 0
            config = get_nlm_config()
            now = time.monotonic()
            window_start = now - config.auth_check_interval
            active = [ts for ts in self._call_timestamps if ts > window_start]
            return max(0, config.auth_max_calls_per_window - len(active))


def _get_auth_rate_limiter() -> AuthRateLimiter:
    """Return the AuthRateLimiter per-process singleton."""
    global _auth_rate_limiter
    with _auth_rate_limiter_lock:
        if _auth_rate_limiter is None:
            _auth_rate_limiter = AuthRateLimiter()
        return _auth_rate_limiter


# CookieFreshnessTracker — per-process singleton for active cookie probe
_cookie_freshness_tracker_lock = threading.Lock()
_cookie_freshness_tracker: "CookieFreshnessTracker | None" = None


class CookieFreshnessTracker:
    """Tracks cookie freshness using active probe, not just TTL.

    TTL (300s) is a fast-path optimization. When TTL expires, an active
    `nlm login --check` probe (30s timeout) is the authoritative check.
    On probe timeout or failure, invalidate() is called to force re-auth.
    """

    def __init__(self) -> None:
        self._last_check: float = 0.0
        self._ttl: float = 300.0
        self._lock = threading.Lock()

    def is_fresh(self) -> bool:
        """Return True if cookie is fresh (TTL not expired or active probe passes).

        If TTL has expired, runs `nlm login --check` (30s timeout) as authoritative.
        On probe failure or timeout, calls invalidate() and returns False.
        """
        with self._lock:
            if time.monotonic() - self._last_check <= self._ttl:
                return True

        # TTL expired — run active probe
        try:
            check = subprocess.run(
                ["nlm", "login", "--check"],
                capture_output=True, timeout=30,
            )
            if check.returncode == 0:
                with self._lock:
                    self._last_check = time.monotonic()
                return True
            # Probe failed — invalidate and fall through
            self.invalidate()
            return False
        except subprocess.TimeoutExpired:
            logging.warning("[CookieFreshnessTracker] probe timed out after 30s — invalidating")
            self.invalidate()
            return False
        except Exception:
            self.invalidate()
            return False

    def invalidate(self) -> None:
        """Force re-auth on next _ensure_nlm_auth call."""
        with self._lock:
            self._last_check = 0.0


def _get_cookie_freshness_tracker() -> CookieFreshnessTracker:
    """Return the CookieFreshnessTracker per-process singleton."""
    global _cookie_freshness_tracker
    with _cookie_freshness_tracker_lock:
        if _cookie_freshness_tracker is None:
            _cookie_freshness_tracker = CookieFreshnessTracker()
        return _cookie_freshness_tracker


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
        source: Which fetch method succeeded ('ytdlp', 'ytdlp_ejs', 'selenium',
            'notebooklm', 'direct_api', 'none').
        source_stage: Versioned provenance tag. None means pre-versioning era
            (records from before this field existed). Higher values indicate
            more recent source format versions. Stage versions: ytdlp/ejs/selenium/
            notebooklm=1, direct_api=2.
        detected_lang: The detected language of the returned transcript,
            or None if language detection failed or no transcript available.
        error: The error message from the last failed source, or None if no
            error occurred or transcript was successfully fetched.
        last_stage: Which stage in the chain was reached ('ytdlp', 'ytdlp_ejs',
            'selenium', 'notebooklm', 'direct_api'). None on success — the
            successful source is in the `source` field.
        failure_reason: Classified reason for final failure ('region_block',
            'no_transcript', 'quota_exceeded', 'auth_failed', 'captcha',
            'unavailable', 'timeout', 'unknown'). None if not yet determined
            or if transcript was successfully fetched.
    """

    video_id: str
    lang: str
    raw_lang: str | None
    was_translated: bool
    transcript: str
    source: str
    source_stage: int | None = None
    detected_lang: str | None = None
    error: str | None = None
    last_stage: str | None = None
    failure_reason: str | None = None
    # YouTube engagement + content quality signals (populated during transcript fetch)
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    duration: int | None = None
    video_title: str | None = None
    video_description: str | None = None


def _extract_video_metadata(info: dict) -> dict:
    """Pull engagement and content-quality fields from a yt-dlp info dict.

    yt-dlp's extract_info returns a full video metadata dict on every call.
    Capturing it here avoids re-fetching for quality metrics.

    Returns a flat dict with only populated fields.
    """
    if not info:
        return {}
    return {
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "duration": info.get("duration"),
        "title": info.get("title"),
        "description": info.get("description"),
    }


def build_transcript_cache_metadata(
    result: TranscriptResult, extra: dict[str, object] | None = None
) -> dict[str, object]:
    """Build a lossless metadata payload for the transcript cache."""
    metadata = {field.name: getattr(result, field.name, None) for field in fields(TranscriptResult)}
    metadata.pop("transcript", None)
    metadata["transcript_chars"] = len(result.transcript)
    if extra:
        metadata.update(extra)
    return metadata


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

        logging.warning(
            "GEMINI_API_KEY not set; cannot translate, returning original text."
        )
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


def _is_source_rate_limited(source: str) -> bool:
    """Return True if source is in circuit-open cooldown."""
    return (
        source in _source_cooldown_until
        and time.monotonic() < _source_cooldown_until[source]
    )


def _record_source_429(source: str, video_id: str | None = None) -> None:
    """Record a 429 for a source. Opens circuit after threshold.

    Also writes cross-terminal cooldown state to BatchScheduler when video_id is provided.
    """
    with _circuit_lock:
        _consecutive_429[source] = _consecutive_429.get(source, 0) + 1
        count = _consecutive_429[source]
    if count >= _CIRCUIT_OPEN_THRESHOLD:
        with _circuit_lock:
            _source_cooldown_until[source] = time.monotonic() + _COOLDOWN_SECONDS
        import logging

        logging.warning(
            f"[transcript] Circuit breaker OPEN for '{source}' "
            f"({count} consecutive 429s, cooldown={_COOLDOWN_SECONDS}s)"
        )
    # Cross-terminal cooldown: resolve channel URL and record in shared SQLite.
    # COMP-001: _record_source_429 is called with method tokens (e.g. _SOURCE_WHISPER='whisper')
    # but BatchScheduler expects channel_url as PRIMARY KEY. Resolve via get_source(video_id).
    if video_id is not None:
        channel_url = _get_source_for_video(video_id)
        if channel_url is not None:
            try:
                _get_scheduler().record_429(channel_url)
            except Exception as e:
                logging.warning(f"[transcript] Cross-terminal sync failed: {e}")


def _record_source_success(source: str, video_id: str | None = None) -> None:
    """Reset 429 counter on any success. Clears cross-terminal channel cooldown."""
    with _circuit_lock:
        _consecutive_429[source] = 0
    # Cross-terminal cooldown clear: resolve channel URL and clear in shared SQLite.
    if video_id is not None:
        channel_url = _get_source_for_video(video_id)
        if channel_url is not None:
            try:
                _get_scheduler().record_success(channel_url)
            except Exception as e:
                logging.warning(f"[transcript] Cross-terminal sync failed: {e}")


def _apply_jitter_with_backoff(source: str) -> None:
    """Apply jitter with backoff multiplier based on consecutive failures, capped at MAX."""
    with _circuit_lock:
        count = _consecutive_429.get(source, 0)
    multiplier = (
        min(_BACKOFF_BASE**count, _MAX_BACKOFF_MULTIPLIER) if count > 0 else 1.0
    )
    jitter = random.uniform(_JITTER_MIN, _JITTER_MAX) * multiplier
    time.sleep(jitter)


def _fetch_via_gemini_cli(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None]:
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
        proc.communicate()
        return (False, None, "gemini CLI timed out after 300s")
    except Exception as e:
        return (False, None, f"gemini CLI error: {e}")

    if proc.returncode != 0:
        return (False, None, f"gemini CLI failed: {stderr.strip()}")

    return (True, stdout.strip(), None)


def _fetch_via_youtube_transcript_api(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None]:
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
) -> tuple[bool, str | None, str | None]:
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

    def _fetch() -> tuple[bool, str | None, str | None]:
        try:
            client = youtubei.get_client()
            video = client.get_video(video_id)
            transcript_data = video.get_transcript()
            if transcript_data is None:
                return (False, None, "No transcript available")
            text = " ".join(item["text"] for item in transcript_data)
            return (True, text, None)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate limit" in msg:
                return (False, None, "youtubei rate limited (429)")
            return (False, None, f"youtubei error: {e}")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=60)
    except TimeoutError:
        return (False, None, "youtubei timeout (>15s)")


def _fetch_via_ytdlp(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None, dict]:
    """Fetch transcript using yt-dlp Python API with Chrome TLS impersonation.

    Uses yt-dlp's Python API (not CLI subprocess) with WEB client + curl-cffi
    Chrome impersonation to bypass YouTube's TLS fingerprinting bot detection.
    The "Sign in to confirm you're not a bot" error is a TLS handshake rejection —
    curl-cffi makes the request look like Chrome, bypassing it.

    Falls back gracefully if curl-cffi is not installed.

    Returns:
        (success, transcript, error, info_dict) — info_dict contains video metadata
        (view_count, like_count, comment_count, duration, title, description) on success.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts: dict = {
        "skip_download": True,
        "writeautomaticsubs": True,
        "writesubtitles": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "json3",
        "quiet": True,
        "no_warnings": True,
        # Rate limiting: humanize requests to avoid detection
        "sleep_interval": 15,
        "max_sleep_interval": 60,
        # Retry logic with exponential backoff
        "extractor_retries": 5,
        "fragment_retries": 10,
        "ignoreerrors": False,
        # WEB client avoids bot-detection on public videos. No cookies needed.
        # Age-restricted videos require auth — handled by second attempt below.
        "extractor_args": {
            "youtube": {
                "client_name": "WEB",
                "client_version": "2.20210721.01.00",
                "player_client": "web",
                # User region for geolocation context
                "UACountry": "CA",
            }
        },
        # HTTP headers to mimic browser requests
        "http_headers": {
            "Referer": "https://www.youtube.com/",
            "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
        },
    }

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        # Get subtitle entries from automatic_captions (prefer) or subtitles
        subs = (
            info.get("automatic_captions", {}).get(lang)
            or info.get("subtitles", {}).get(lang)
            or info.get("automatic_captions", {}).get("en")
            or info.get("subtitles", {}).get("en")
        )

        if not subs or len(subs) == 0:
            return (False, None, "no subtitles available")

        sub_url = subs[0].get("url")
        if not sub_url:
            return (False, None, "no subtitle URL in yt-dlp response")

        # Fetch the timedtext JSON3 using curl-cffi with Chrome impersonation.
        # This is the actual HTTP call — curl-cffi bypasses TLS fingerprinting.
        try:
            from curl_cffi import requests as curl_requests

            resp = curl_requests.get(
                sub_url,
                impersonate="chrome",
                timeout=30,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            data = json.loads(resp.content.decode("utf-8"))
        except ImportError:
            # Fall back to urllib.request (module-level import) — will likely get bot-checked
            req = urllib.request.Request(
                sub_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))

        # Parse timedtext JSON3 format into plain text
        # JSON3 format: {"events": [{"segs": [{"utf8": "text"}, ...]}, ...]}
        text_parts = []
        for event in data.get("events", []):
            for seg in event.get("segs", []):
                text = seg.get("utf8", "").strip()
                if text:
                    text_parts.append(text)
            # Add newline between subtitle blocks for readability
            if event.get("segs"):
                text_parts.append("\n")

        full_text = " ".join(t for t in text_parts if t != "\n")
        if not full_text.strip():
            return (False, None, "subtitle file was empty", {})

        return (True, full_text.strip(), None, info)

    except urllib.error.HTTPError as e:
        if e.code == 429:
            return (False, None, "rate limited (429)", {})
        return (False, None, f"yt-dlp HTTP error: {e.code}", {})
    except subprocess.TimeoutExpired:
        return (False, None, "yt-dlp timed out", {})
    except Exception as e:
        err_str = str(e).lower()
        if "429" in err_str or "too many requests" in err_str:
            return (False, None, "rate limited (429)", {})
        if "no subtitles" in err_str or "does not have any subtitles" in err_str:
            return (False, None, "no subtitles available", {})
        if "sign in to confirm" in err_str or "not a bot" in err_str:
            # Bot-check triggered — try age-restricted approach with cookies + default extractor.
            # This is a second attempt inside the same function rather than a separate method.
            return _fetch_via_ytdlp_with_cookies(video_id, lang)
        return (False, None, f"yt-dlp error: {e}", {})


def _fetch_via_ytdlp_with_cookies(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None, dict]:
    """Second-attempt transcript fetch with browser cookies for age-restricted videos.

    Called by _fetch_via_ytdlp when bot-check fires on the WEB client approach.
    Uses the default yt-dlp extractor (not WEB client) with Firefox browser cookies.
    Falls back gracefully if cookies are unavailable or extraction fails.

    Returns:
        (success, transcript, error, info_dict) — info_dict has video metadata on success.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # Get cached cookie file (or create new one) with reference counting
    cookie_file = _get_cookie_file()
    if not cookie_file:
        return (False, None, "no firefox cookie file")

    ydl_opts: dict = {
        "skip_download": True,
        "writeautomaticsubs": True,
        "writesubtitles": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "json3",
        "quiet": True,
        "no_warnings": True,
        "cookiefile": cookie_file,
        # Rate limiting: even more conservative with cookies (account-level risk)
        "sleep_interval": 20,
        "max_sleep_interval": 90,
        # Retry logic with exponential backoff
        "extractor_retries": 5,
        "fragment_retries": 10,
        "ignoreerrors": False,
        # EJS github component resolves YouTube's JS challenge for age-restricted videos.
        # Works alongside cookies to authenticate and extract transcripts.
        "extractor_args": {
            "youtube": {
                "external_downloader": "ejs:github",
                "player_client": "web",
                # User region for geolocation context
                "UACountry": "CA",
            }
        },
        # HTTP headers to mimic browser requests
        "http_headers": {
            "Referer": "https://www.youtube.com/",
            "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
        },
    }

    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        subs = (
            info.get("automatic_captions", {}).get(lang)
            or info.get("subtitles", {}).get(lang)
            or info.get("automatic_captions", {}).get("en")
            or info.get("subtitles", {}).get("en")
        )

        if not subs or len(subs) == 0:
            _release_cookie_file(cookie_file)
            return (False, None, "no subtitles available", {})

        sub_url = subs[0].get("url")
        if not sub_url:
            _release_cookie_file(cookie_file)
            return (False, None, "no subtitle URL in yt-dlp response", {})

        # Fetch subtitle URL with curl_cffi Chrome impersonation
        try:
            from curl_cffi import requests as curl_requests

            resp = curl_requests.get(
                sub_url,
                impersonate="chrome",
                timeout=30,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            data = json.loads(resp.content.decode("utf-8"))
        except ImportError:
            req = urllib.request.Request(
                sub_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))

        text_parts = []
        for event in data.get("events", []):
            for seg in event.get("segs", []):
                t = seg.get("utf8", "").strip()
                if t:
                    text_parts.append(t)
            if event.get("segs"):
                text_parts.append("\n")

        full_text = " ".join(t for t in text_parts if t != "\n")
        _release_cookie_file(cookie_file)
        if not full_text.strip():
            return (False, None, "subtitle file was empty", {})

        return (True, full_text.strip(), None, info)

    except urllib.error.HTTPError as e:
        _release_cookie_file(cookie_file)
        if e.code == 429:
            return (False, None, "rate limited (429)", {})
        return (False, None, f"yt-dlp-with-cookies HTTP error: {e.code}", {})
    except subprocess.TimeoutExpired:
        _release_cookie_file(cookie_file)
        return (False, None, "yt-dlp-with-cookies timed out", {})
    except Exception as e:
        _release_cookie_file(cookie_file)
        err_str = str(e).lower()
        if "429" in err_str or "too many requests" in err_str:
            return (False, None, "rate limited (429)", {})
        if "sign in" in err_str or "age" in err_str or "login" in err_str:
            return (False, None, "age-restricted or requires login", {})
        return (False, None, f"yt-dlp-with-cookies error: {e}", {})


def _get_firefox_cookie_file() -> str | None:
    """Export live Firefox YouTube cookies to a temp Netscape cookie file.

    Copies cookies.sqlite from the live Firefox profile to bypass Windows
    file locking, then exports YouTube/Google/Googlevideo cookies to
    Netscape format. The caller is responsible for deleting the temp file.

    Returns:
        Path to temp cookie file, or None if Firefox is not running / no cookies found.
    """
    appdata = os.environ.get("APPDATA") or ""
    profile_base = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
    profiles = glob.glob(os.path.join(profile_base, "*.default*"))
    if not profiles:
        return None

    # Prefer the release profile (has active YouTube session)
    release = next((p for p in profiles if "release" in p), profiles[0])
    cookie_db = os.path.join(release, "cookies.sqlite")
    if not os.path.exists(cookie_db):
        return None

    tmp_db = tempfile.mktemp(suffix=".sqlite")
    try:
        shutil.copy2(cookie_db, tmp_db)
    except Exception:
        return None

    try:
        conn = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT host, name, value, path, expiry, isSecure FROM moz_cookies "
            'WHERE host LIKE "%youtube.com" OR host LIKE "%google.com" OR host LIKE "%googlevideo.com"'
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        os.unlink(tmp_db)
        return None

    if not rows:
        os.unlink(tmp_db)
        return None

    cookie_file = tempfile.mktemp(suffix=".txt")
    try:
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for row in rows:
                h, n, v, p, exp, sec = (
                    row["host"],
                    row["name"],
                    row["value"],
                    row["path"],
                    row["expiry"],
                    row["isSecure"],
                )
                flag = "TRUE" if h.startswith(".") else "FALSE"
                p = p or "/"
                sec_str = "TRUE" if sec else "FALSE"
                v = v.replace("\n", "%0A")
                f.write(f"{h}\t{flag}\t{p}\t{sec_str}\t{exp}\t{n}\t{v}\n")
        return cookie_file
    except Exception:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass
        os.unlink(tmp_db)
        return None
    finally:
        try:
            os.unlink(tmp_db)
        except Exception:
            pass


def _get_cookie_file() -> str | None:
    """Get cached cookie file with reference counting.

    Returns cached cookie file if available and valid, otherwise generates
    a new one. Uses reference counting to ensure the file is not deleted
    while still in use by concurrent requests.

    Returns:
        Cookie file path, or None if unavailable.
    """
    global _cookie_cache

    with _cookie_lock:
        # Check cache validity
        if _cookie_cache:
            path = _cookie_cache.get("path")
            expiry = _cookie_cache.get("expiry", 0)
            if path and os.path.exists(path) and time.time() < expiry:
                _cookie_cache["refcount"] = _cookie_cache.get("refcount", 0) + 1
                return path
            else:
                # Cleanup stale cache
                _cleanup_cookie_cache()

        # Generate new cookie file using existing function
        cookie_file = _get_firefox_cookie_file()
        if cookie_file:
            _cookie_cache = {
                "path": cookie_file,
                "refcount": 1,
                "expiry": time.time() + COOKIE_CACHE_TTL
            }
        return cookie_file


def _release_cookie_file(cookie_file: str) -> None:
    """Release reference to cached cookie file.

    Decrements reference count; cleans up cookie file when refcount reaches zero.

    Args:
        cookie_file: Path to the cookie file being released.
    """
    global _cookie_cache

    with _cookie_lock:
        if _cookie_cache.get("path") == cookie_file:
            _cookie_cache["refcount"] = _cookie_cache.get("refcount", 1) - 1
            if _cookie_cache["refcount"] <= 0:
                _cleanup_cookie_cache()


def _cleanup_cookie_cache() -> None:
    """Cleanup cached cookie file and reset cache.

    Deletes the cookie file if it exists and resets the module-level cache.
    Logs a warning if deletion fails (instead of silently ignoring).
    """
    global _cookie_cache

    path = _cookie_cache.get("path")
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception as e:
            logging.warning(f"Failed to cleanup cookie file {path}: {e}")
    _cookie_cache = {}


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


def _fetch_via_sdk(video_id: str, lang: str) -> tuple[bool, str | None, str | None]:
    """Fetch transcript using Gemini SDK as last resort."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return (False, None, "GEMINI_API_KEY not set")

    try:
        from google import genai
    except ImportError:
        return (False, None, "google-genai not installed")

    def _fetch() -> tuple[bool, str | None, str | None]:
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    f"Get the transcript for YouTube video {video_id} in language {lang}"
                ],
            )
            text = response.text.strip() if response.text else ""
            return (True, text, None)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg:
                return (False, None, "SDK rate limited (429)")
            return (False, None, f"SDK error: {e}")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            return future.result(timeout=60)
    except TimeoutError:
        return (False, None, "SDK timeout (>60s)")


def _fetch_via_whisper(video_id: str, lang: str) -> tuple[bool, str | None, str | None]:
    """Transcribe audio using faster-whisper as final fallback.

    Downloads audio via yt-dlp then transcribes with faster-whisper.
    Used only after all caption-based sources fail — it is slow (~30-90s)
    but can transcribe any video that has audio available.

    Args:
        video_id: YouTube video ID.
        lang: Target language code (used only as hint; faster-whisper
            auto-detects if not in known languages).

    Returns:
        (success, transcript, error).
    """
    import tempfile

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    tmp_dir = tempfile.mkdtemp(prefix="whisper_audio_")
    audio_path = os.path.join(tmp_dir, "audio")
    try:
        # Download audio only via yt-dlp. Use broad selectors so we still
        # capture videos that do not expose an m4a stream.
        last_audio_error: str | None = None
        js_runtime_args = ["--js-runtimes", "node"] if shutil.which("node") else []
        for audio_format in _WHISPER_AUDIO_FORMATS:
            cmd = [
                "yt-dlp",
                *get_browser_cookies("firefox"),
                *js_runtime_args,
                "-f",
                audio_format,
                "--extract-audio",
                "--audio-format",
                "mp3",
                "--output",
                audio_path,
                video_url,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode == 0:
                break

            stderr_lower = (proc.stderr or "").lower()
            if "429" in proc.stderr or "too many requests" in stderr_lower:
                return (False, None, "audio download rate limited (429)")
            if "not found" in stderr_lower or "video unavailable" in stderr_lower:
                return (False, None, "video unavailable for audio download")

            last_audio_error = f"audio download failed: {proc.stderr.strip()[:200]}"
            if any(
                hint in stderr_lower
                for hint in (
                    "requested format is not available",
                    "format is not available",
                    "no such format",
                    "no formats available",
                )
            ):
                continue

            if any(
                hint in stderr_lower
                for hint in (
                    "sign in to confirm",
                    "not a bot",
                    "challenge solving failed",
                )
            ):
                continue

            return (False, None, last_audio_error)
        else:
            return (False, None, last_audio_error or "audio download failed")

        # Find the downloaded audio file
        audio_files = list(Path(tmp_dir).glob("*.mp3"))
        if not audio_files:
            return (False, None, "no audio file produced")

        audio_file = str(audio_files[0])

        # Run faster-whisper transcription
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return (False, None, "faster-whisper not installed")

        # Use medium model for better accuracy; falls back automatically
        model = WhisperModel("medium", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            audio_file, language=lang if lang != "en" else None
        )
        segments = list(segments)
        text = " ".join(segment.text for segment in segments)
        if not text.strip():
            return (False, None, _summarize_whisper_empty_result(segments))
        return (True, text.strip(), None)

    except subprocess.TimeoutExpired:
        return (False, None, "audio download timed out (>300s)")
    except Exception as e:
        return (False, None, f"whisper transcription error: {e}")
    finally:
        import shutil as _shutil

        try:
            _shutil.rmtree(tmp_dir)
        except Exception:
            pass


def _summarize_whisper_empty_result(segments: list[object]) -> str:
    """Describe an empty Whisper result with a conservative speech-vs-music hint.

    We cannot prove that the audio is music, but faster-whisper exposes
    per-segment `no_speech_prob`. When that is high across the returned
    segments, the audio was likely silence, music, or otherwise speech-free.
    """

    no_speech_probs: list[float] = []
    for segment in segments:
        try:
            prob = getattr(segment, "no_speech_prob", None)
        except Exception:
            prob = None
        if prob is not None:
            try:
                no_speech_probs.append(float(prob))
            except (TypeError, ValueError):
                continue

    max_no_speech_prob = max(no_speech_probs) if no_speech_probs else None
    segment_count = len(segments)

    if max_no_speech_prob is not None and max_no_speech_prob >= 0.75:
        return (
            "whisper no speech detected (likely music or silence; "
            f"segments={segment_count}, max_no_speech_prob={max_no_speech_prob:.2f})"
        )
    if max_no_speech_prob is not None:
        return (
            "whisper produced empty transcript "
            f"(segments={segment_count}, max_no_speech_prob={max_no_speech_prob:.2f})"
        )
    return f"whisper produced empty transcript (segments={segment_count})"


def _fetch_via_selenium_firefox(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None]:
    """Fetch transcript using Selenium-driven Firefox with real browser TLS.

    This is a fallback that bypasses YouTube's TLS fingerprinting bot detection
    by running an actual Firefox browser with your real browser session (cookies).
    It is slow (~15-30s per video) but reliable when yt-dlp fails due to bot detection.

    Args:
        video_id: YouTube video ID.
        lang: BCP-47 language code (currently unused — Firefox returns
            the transcript in whatever language YouTube provides, usually en).

    Returns:
        (success, transcript_text, error)
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
        from selenium.webdriver.common.by import By
    except ImportError:
        return (False, None, "selenium not installed")

    firefox_profile_path = None
    try:
        # Use dedicated download profile (ProfileForDownloading) with YouTube login
        import glob as _glob

        appdata = os.environ.get("APPDATA") or ""
        profile_base = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
        # Prefer dedicated download profile, fall back to first non-default profile
        profiles = _glob.glob(os.path.join(profile_base, "*.Profile 1*"))
        if not profiles:
            # Fallback: use any profile that's not the default/release
            all_profiles = _glob.glob(os.path.join(profile_base, "*"))
            profiles = [p for p in all_profiles if ".default" not in os.path.basename(p)]
        opts = Options()
        opts.add_argument("--headless=new")

        # Don't use existing profile - it conflicts with Selenium's preference setting
        # For age-restricted videos requiring cookies, use yt-dlp with cookies instead
        driver = webdriver.Firefox(service=Service(), options=opts)

        try:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            driver.get(video_url)
            time.sleep(3)

            # Scroll down to expose the transcript button, then click it via JS
            driver.execute_script("window.scrollBy(0, 400)")
            time.sleep(0.5)

            transcript_clicked = False
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                aria_label = btn.get_attribute("aria-label") or ""
                if "transcript" in aria_label.lower():
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.3)
                    driver.execute_script("arguments[0].click();", btn)
                    transcript_clicked = True
                    time.sleep(3)
                    break

            if not transcript_clicked:
                return (False, None, "transcript button not found")

            # Extract all transcript text from the rendered page
            body_text = driver.find_element(By.TAG_NAME, "body").text

            if not body_text or len(body_text) < 20:
                return (False, None, "transcript panel was empty")

            return (True, body_text, None)

        finally:
            driver.quit()

    except Exception as e:
        return (False, None, f"selenium error: {e}")


def _ensure_nlm_auth() -> bool:
    """Check NLM auth and auto-recover if expired.

    Integration: AuthRateLimiter gate + CookieFreshnessTracker probe + nlm login.
    Cooldown trigger is split: only --force login failures count toward the
    3-failure cooldown. A --check probe failure followed by successful --force
    recovery does NOT count as a failure.
    """
    rate_limiter = _get_auth_rate_limiter()

    # 1. AuthRateLimiter gate — block if rate limit exceeded or in cooldown
    if not rate_limiter.is_allowed():
        logging.warning("[_ensure_nlm_auth] blocked by AuthRateLimiter")
        return False

    freshness = _get_cookie_freshness_tracker()

    # 2. CookieFreshnessTracker — if stale, force re-auth
    if not freshness.is_fresh():
        logging.info("[_ensure_nlm_auth] cookie stale, forcing re-auth")

    # 3. Run --check probe (for freshness tracker to record success)
    try:
        check = subprocess.run(
            ["nlm", "login", "--check"],
            capture_output=True, timeout=30,
        )
        if check.returncode == 0:
            log_action("nlm_auth_checked", {"component": "transcript", "status": "ok"})
            rate_limiter.record_call()
            return True
    except Exception:
        pass

    # 4. Auth expired — auto-recover with force login
    try:
        rate_limiter.record_call()
        login_started = time.perf_counter()
        log_action(
            "nlm_login_started",
            {"component": "transcript", "mode": "force", "status": "started"},
        )
        login = subprocess.run(
            ["nlm", "login", "--force"],
            capture_output=True, timeout=120,
        )
        login_elapsed = round(time.perf_counter() - login_started, 3)
        if login.returncode == 0:
            log_action(
                "nlm_login_completed",
                {
                    "component": "transcript",
                    "mode": "force",
                    "status": "ok",
                    "elapsed_s": login_elapsed,
                },
            )
            log_action("nlm_auth_refreshed", {"component": "transcript", "status": "ok"})
            rate_limiter.record_auth_success()
            return True
        # Only --force failures count toward cooldown trigger
        log_action(
            "nlm_login_failed",
            {
                "component": "transcript",
                "mode": "force",
                "status": "failed",
                "elapsed_s": login_elapsed,
                "returncode": login.returncode,
            },
        )
        log_action(
            "nlm_auth_failed",
            {"component": "transcript", "status": "refresh_failed"},
        )
        rate_limiter.record_auth_failure()
        return False
    except Exception:
        login_elapsed = round(time.perf_counter() - login_started, 3) if "login_started" in locals() else None
        log_action(
            "nlm_login_failed",
            {
                "component": "transcript",
                "mode": "force",
                "status": "exception",
                "elapsed_s": login_elapsed,
            },
        )
        log_action(
            "nlm_auth_failed",
            {"component": "transcript", "status": "refresh_exception"},
        )
        rate_limiter.record_auth_failure()
        return False


def _parse_notebook_id(output: str) -> str | None:
    """Parse notebook ID from nlm notebook create output."""
    for line in output.strip().split("\n"):
        if "ID:" in line:
            return line.split("ID: ")[-1].strip()
    return None


def _extract_video_id_from_url(url: str) -> str | None:
    """Extract video ID from YouTube URL."""
    import re
    match = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else None


def _fetch_via_notebooklm_batch(
    video_ids: list[str],
) -> dict[str, tuple[bool, str | None, str | None]]:
    """Fetch transcripts for multiple videos using Industrial NLM batch ingest.

    Uses NLMBatchIngestor (parallel nlm source content CLI) for ~18K v/hr,
    falling back to the Selenium scraper if that path is unavailable.

    Args:
        video_ids: List of YouTube video IDs (11 chars each)

    Returns:
        dict mapping video_id -> (success, transcript_text, error)
    """
    from csf.nlm_batch import process_industrial_batch

    return process_industrial_batch(video_ids)


def _fetch_via_notebooklm(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None]:
    """Fetch transcript using terminal-local staging notebook.

    Reuses a single persistent staging notebook across calls within the
    same process, clearing and recreating when the 300-source limit is
    approached.
    """
    scraper = _get_nlm_scraper()
    results = scraper.scrape_with_staging([video_id])
    success, transcript, error = results.get(
        video_id, (False, None, "scraper returned no result")
    )
    return (success, transcript, error)


def _fetch_via_direct_api(video_id: str) -> tuple[bool, str | None, str | None]:
    """Fetch transcript using youtube-transcript-api directly (non-Google fallback).

    This is the final fallback after all Google-adjacent sources (yt-dlp,
    Selenium, NotebookLM) have failed. youtube-transcript-api scrapes YouTube
    captions directly and may succeed where Google's ecosystem fails.

    Returns:
        (success, transcript_text, error)
    """
    def _summarize_direct_api_error(error: Exception | str) -> str:
        raw = str(error).strip()
        low = raw.lower()
        if "subtitles are disabled" in low or "no subtitles" in low:
            return "direct_api no_transcript: subtitles disabled"
        if "removed by the uploader" in low:
            return "direct_api unavailable: removed by uploader"
        if "not available in your country" in low or "geo" in low:
            return "direct_api unavailable: not available in your country"
        if "unplayable" in low or "video unavailable" in low or "private video" in low:
            return "direct_api unavailable: video unavailable"
        if "transcript could not be retrieved" in low or "no transcript available" in low:
            return "direct_api no_transcript: transcript unavailable"
        if "could not retrieve a transcript" in low:
            return "direct_api no_transcript: could not retrieve transcript"
        if "youtube transcript api" in low and "error" in low:
            return "direct_api error: youtube transcript api failure"
        if "transcript" in low and "not" in low:
            return f"direct_api no_transcript: {raw}"
        if "429" in low or "rate limit" in low or "quota" in low:
            return f"direct_api quota_exceeded: {raw}"
        return f"direct_api error: {raw}"

    try:
        import youtube_transcript_api
    except ImportError:
        logging.warning("[_fetch_via_direct_api] youtube_transcript_api not installed")
        return (False, None, "no_transcript")

    try:
        api = youtube_transcript_api.YouTubeTranscriptApi()
        api_type = type(api)
        # Prefer the installed API shape: list(video_id) returns a TranscriptList.
        # Older/newer releases have used slightly different names here, so we
        # gracefully adapt rather than pinning the whole fallback path to one
        # package version.
        if callable(getattr(api_type, "list_transcripts", None)):
            transcripts = api.list_transcripts(video_id)
        elif callable(getattr(api_type, "list", None)):
            transcripts = api.list(video_id)
        else:
            fetched = api.fetch(video_id, languages=["en"])
            transcript_text = " ".join(
                segment["text"] for segment in fetched.fetch()
            )
            if len(transcript_text) >= _NLM_MIN_CONTENT_CHARS:
                return (True, transcript_text, None)
            return (False, None, "no_transcript")

        # List available transcripts to find a non-generated English one first
        for transcript in transcripts:
            # Prefer English, non-generated
            if transcript.language_code == "en" and not transcript.is_generated:
                text_parts = []
                for segment in transcript.fetch():
                    text_parts.append(segment["text"])
                transcript_text = " ".join(text_parts)
                if len(transcript_text) >= _NLM_MIN_CONTENT_CHARS:
                    return (True, transcript_text, None)
        # Fallback: any available non-generated transcript
        for transcript in transcripts:
            if not transcript.is_generated:
                text_parts = []
                for segment in transcript.fetch():
                    text_parts.append(segment["text"])
                transcript_text = " ".join(text_parts)
                if len(transcript_text) >= _NLM_MIN_CONTENT_CHARS:
                    return (True, transcript_text, None)
        return (False, None, "no_transcript")
    except Exception as e:
        return (False, None, _summarize_direct_api_error(e))


def _persist_terminal_failure(video_id: str, error: str | None, last_stage: str | None) -> None:
    """Persist an early terminal/unavailable result so future scans skip it."""
    source = None
    try:
        source = _get_source_for_video(video_id)
    except Exception:
        source = None
    try:
        _get_scheduler().archive_finalize(video_id, "failed", None, error)
    except Exception as e:
        logging.warning(f"[transcript] Failed to archive terminal failure for {video_id}: {e}")
    try:
        _mark_failed_video(video_id, source=source, failure_reason="unavailable")
    except Exception as e:
        logging.warning(f"[transcript] Failed to mark terminal failure for {video_id}: {e}")
    try:
        _set_negative_cache(
            video_id,
            "unavailable",
            source=source,
            last_stage=last_stage,
            ttl_seconds=_NEGATIVE_CACHE_TERMINAL_TTL_SECONDS,
        )
    except Exception as e:
        logging.warning(f"[transcript] Failed to set terminal negative cache for {video_id}: {e}")


def _record_soft_negative(
    video_id: str,
    reason: str,
    *,
    last_stage: str | None,
    error: str | None,
) -> None:
    """Record a temporary negative cache entry without permanently failing the video."""
    source = None
    try:
        source = _get_source_for_video(video_id)
    except Exception:
        source = None
    try:
        _get_scheduler().archive_finalize(video_id, "failed", None, error)
    except Exception as e:
        logging.warning(f"[transcript] Failed to archive soft failure for {video_id}: {e}")
    try:
        _set_negative_cache(
            video_id,
            reason,
            source=source,
            last_stage=last_stage,
            ttl_seconds=_NEGATIVE_CACHE_SOFT_TTL_SECONDS,
        )
    except Exception as e:
        logging.warning(f"[transcript] Failed to set soft negative cache for {video_id}: {e}")


def _probe_oembed(video_id: str) -> tuple[bool, str | None]:
    """Cheap reachability probe for obvious unavailable/private/removed videos."""
    oembed_url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "format": "json",
        }
    )
    req = urllib.request.Request(
        oembed_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if getattr(resp, "status", 200) == 200:
                return (True, None)
            return (False, f"oembed unavailable: HTTP {getattr(resp, 'status', 'unknown')}")
    except urllib.error.HTTPError as e:
        if e.code in {401, 403, 404, 410}:
            return (False, f"oembed unavailable: HTTP {e.code}")
        if e.code == 429:
            return (False, "oembed rate limited (429)")
        return (False, f"oembed error: HTTP {e.code}")
    except Exception as e:
        return (False, f"oembed error: {e}")


def _log_transcript_chain_event(action: str, video_id: str, **data: object) -> None:
    """Emit a compact transcript-chain trace event."""
    payload = {"component": "transcript", "video_id": video_id}
    payload.update({key: value for key, value in data.items() if value is not None})
    log_action(action, payload)


def fetch_transcript_chain(
    video_id: str,
    config: LanguageConfig,
    *,
    skip_notebooklm: bool = False,
) -> TranscriptResult:
    """Fetch transcript using yt-dlp → Selenium → NotebookLM fallback chain.

    Chain order:
      1. oEmbed reachability probe — cheap early skip for removed/private videos
      2. yt-dlp (WEB client, curl_cffi TLS) — High Fidelity, Fastest Local
      3. direct_api — cheap terminal/no-transcript discriminator
      4. NotebookLM Industrial (Cloud) — High Fidelity, Cleanest Data, Best for Backlog
      5. Selenium Firefox — Dirty Scraper (Polluted with page noise), Slow
      6. Whisper — Audio Fallback

    Args:
        video_id: YouTube video ID (must be 11 chars)
        config: LanguageConfig specifying prefer_lang and allow_translation
        skip_notebooklm: If True, skip the NotebookLM stage and fall back to
            Selenium, Whisper, and direct API only.

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
            source_stage=None,
            detected_lang=None,
            error="invalid video_id format",
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
            source_stage=None,
            detected_lang=None,
            error=f"invalid BCP-47 language code: {prefer_lang!r}",
            last_stage=None,
            failure_reason="invalid_config",
        )

    chain_started_at = time.perf_counter()

    def _classify_failure(error: str | None, stage: str) -> str:
        """Classify error string into structured failure reason."""
        if not error:
            return "unknown"
        err_lower = error.lower()
        if "429" in err_lower or "rate limit" in err_lower or "quota" in err_lower:
            return "quota_exceeded"
        if "region" in err_lower or "not available" in err_lower or "geo" in err_lower:
            return "region_block"
        if "auth" in err_lower or "login" in err_lower or "credential" in err_lower:
            return "auth_failed"
        if "captcha" in err_lower or "bot detection" in err_lower:
            return "captcha"
        if "timeout" in err_lower or "timed out" in err_lower:
            return "timeout"
        if "no transcript" in err_lower or "transcript unavailable" in err_lower:
            return "no_transcript"
        if "no speech detected" in err_lower or "likely music or silence" in err_lower:
            return "no_transcript"
        if "whisper produced empty transcript" in err_lower:
            return "no_transcript"
        if "unavailable" in err_lower or "deleted" in err_lower or "private" in err_lower:
            return "unavailable"
        if "not found" in err_lower or "404" in err_lower:
            return "unavailable"
        return "unknown"

    # Helper to build a "no transcript" result
    def _none_result(last_err: str | None = None, last_stage: str | None = None) -> TranscriptResult:
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            source_stage=None,
            detected_lang=None,
            error=last_err,
            last_stage=last_stage,
            failure_reason=_classify_failure(last_err, last_stage or ""),
        )

    def _archive_failed_result(
        last_err: str | None, last_stage: str | None
    ) -> TranscriptResult:
        failure_reason = _classify_failure(last_err, last_stage or "")
        _log_transcript_chain_event(
            "transcript_chain_failed",
            video_id,
            last_stage=last_stage,
            failure_reason=failure_reason,
            error=last_err,
            elapsed_s=round(time.perf_counter() - chain_started_at, 3),
        )
        if failure_reason == "unavailable":
            _persist_terminal_failure(video_id, last_err, last_stage)
        else:
            _record_soft_negative(
                video_id,
                failure_reason,
                last_stage=last_stage,
                error=last_err,
            )
        return _none_result(last_err, last_stage)

    oembed_enabled = os.getenv("YTIS_OEMBED_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if oembed_enabled:
        oembed_started_at = time.perf_counter()
        oembed_ok, oembed_error = _probe_oembed(video_id)
        _log_transcript_chain_event(
            "transcript_oembed_checked",
            video_id,
            enabled=True,
            ok=oembed_ok,
            error=oembed_error,
            elapsed_s=round(time.perf_counter() - oembed_started_at, 3),
        )
        if not oembed_ok and oembed_error:
            oembed_error_lower = oembed_error.lower()
            if "oembed unavailable" in oembed_error_lower:
                _log_transcript_chain_event(
                    "transcript_chain_failed",
                    video_id,
                    last_stage="oembed",
                    failure_reason="unavailable",
                    error=oembed_error,
                    elapsed_s=round(time.perf_counter() - chain_started_at, 3),
                )
                _persist_terminal_failure(video_id, oembed_error, "oembed")
                return _none_result(oembed_error, "oembed")

    # Language fallback order: prefer_lang → en → None (any available)
    lang_fallbacks: list[str | None] = [prefer_lang]
    if prefer_lang != "en":
        lang_fallbacks.append("en")
    lang_fallbacks.append(None)  # Any available language

    last_error: str | None = None
    last_stage_reached: str | None = None

    # Methods to try: yt-dlp (WEB) → yt-dlp with cookies → NotebookLM → Selenium → Whisper → direct_api
    methods_to_try = [
        (_SOURCE_YTDLP, _fetch_via_ytdlp, STAGE_VERSION_YTDLP),
        (_SOURCE_YTDLP_EJS, _fetch_via_ytdlp_with_cookies, STAGE_VERSION_EJS),
        (_SOURCE_DIRECT_API, _fetch_via_direct_api, STAGE_VERSION_DIRECT_API),
        (_SOURCE_SELENIUM, _fetch_via_selenium_firefox, STAGE_VERSION_SELENIUM),
        (_SOURCE_WHISPER, _fetch_via_whisper, None),  # audio fallback — no captions needed
    ]
    if not skip_notebooklm:
        methods_to_try.insert(3, (_SOURCE_NLM, _fetch_via_notebooklm, STAGE_VERSION_NOTEBOOKLM))

    for source, fetch_fn, stage in methods_to_try:
        if _is_source_rate_limited(source):
            continue  # skip circuit-open source
        # Skip whisper if disabled via env var
        if source == _SOURCE_WHISPER:
            global _WHISPER_ENABLED
            if _WHISPER_ENABLED is None:
                _WHISPER_ENABLED = os.getenv("YTIS_WHISPER_ENABLED", "true").lower() == "true"
            if not _WHISPER_ENABLED:
                continue

        last_stage_reached = source  # Track the last stage we actually tried

        # NLM ignores lang — call once without lang iteration (no language filtering)
        if source == _SOURCE_NLM:
            success, transcript, error = fetch_fn(video_id, "en")
            if success and transcript:
                _record_source_success(source, video_id)
                # NLM always returns English (NotebookLM extracts from YouTube source)
                raw_lang = "en"
                detected_lang = raw_lang
                final_transcript = transcript
                was_translated = False

                # Translate if prefer_lang is not English and translation is enabled
                if raw_lang != prefer_lang and config.allow_translation:
                    final_transcript = _translate_text(
                        transcript, raw_lang, prefer_lang, config.translation_provider
                    )
                    was_translated = True

                result = TranscriptResult(
                    video_id=video_id,
                    lang=prefer_lang,
                    raw_lang=raw_lang,
                    was_translated=was_translated,
                    transcript=final_transcript,
                    source=source,
                    source_stage=stage,
                    detected_lang=detected_lang,
                    error=None,
                    last_stage=source,
                    failure_reason=None,
                )
                set_cached_transcript(
                    video_id,
                    prefer_lang,
                    source,
                    final_transcript,
                    metadata=build_transcript_cache_metadata(result),
                )
                return result
            last_error = error
        # direct_api uses different signature (no lang arg)
        elif source == _SOURCE_DIRECT_API:
            success, transcript, error = fetch_fn(video_id)
            if success and transcript:
                _record_source_success(source, video_id)
                result = TranscriptResult(
                    video_id=video_id,
                    lang=prefer_lang,
                    raw_lang=prefer_lang,
                    was_translated=False,
                    transcript=transcript,
                    source=source,
                    source_stage=stage,
                    detected_lang=prefer_lang,
                    error=None,
                    last_stage=source,
                    failure_reason=None,
                )
                set_cached_transcript(
                    video_id,
                    prefer_lang,
                    source,
                    transcript,
                    metadata=build_transcript_cache_metadata(result),
                )
                return result
            last_error = error
            if error and (
                "unavailable" in error.lower()
                or "removed" in error.lower()
                or "private" in error.lower()
            ):
                _log_transcript_chain_event(
                    "transcript_chain_failed",
                    video_id,
                    last_stage=source,
                    failure_reason="unavailable",
                    error=error,
                    elapsed_s=round(time.perf_counter() - chain_started_at, 3),
                )
                _persist_terminal_failure(video_id, error, source)
                return _none_result(error, source)
        else:
            for lang in lang_fallbacks:
                # Use "en" as placeholder when lang is None (yt-dlp will use its default)
                try_lang = lang if lang is not None else "en"

                result = fetch_fn(video_id, try_lang)
                # Normalize 3-tuple (NotebookLM, Selenium, Whisper, direct_api) vs
                # 4-tuple (yt-dlp, yt-dlp-with-cookies) which carries video metadata
                if len(result) == 4:
                    success, transcript, error, info_dict = result
                else:
                    success, transcript, error = result
                    info_dict = {}

                if success and transcript:
                    _record_source_success(source, video_id)

                    # Extract engagement metrics from yt-dlp info dict when available
                    video_metadata = _extract_video_metadata(info_dict)

                    # Determine actual language and whether translation is needed.
                    # When lang is None we only know the transcript came from the
                    # generic fallback, so keep the language unknown instead of
                    # pretending it is English.
                    raw_lang = lang
                    detected_lang = raw_lang
                    final_transcript = transcript
                    was_translated = False

                    # Only translate when the source language is known.
                    if raw_lang is not None and raw_lang != prefer_lang and config.allow_translation:
                        final_transcript = _translate_text(
                            transcript, raw_lang, prefer_lang, config.translation_provider
                        )
                        was_translated = True

                    result = TranscriptResult(
                        video_id=video_id,
                        lang=prefer_lang,
                        raw_lang=raw_lang,
                        was_translated=was_translated,
                        transcript=final_transcript,
                        source=source,
                        source_stage=stage,
                        detected_lang=detected_lang,
                        error=None,
                        last_stage=source,
                        failure_reason=None,
                        view_count=video_metadata.get("view_count"),
                        like_count=video_metadata.get("like_count"),
                        comment_count=video_metadata.get("comment_count"),
                        duration=video_metadata.get("duration"),
                        video_title=video_metadata.get("title"),
                        video_description=video_metadata.get("description"),
                    )
                    set_cached_transcript(
                        video_id,
                        prefer_lang,
                        source,
                        final_transcript,
                        metadata=build_transcript_cache_metadata(
                            result,
                            extra={"yt_dlp_info_dict": info_dict},
                        ),
                    )
                    return result

                last_error = error
                if error and ("429" in error.lower() or "rate limited" in error.lower()):
                    _record_source_429(source, video_id)
                    _apply_jitter_with_backoff(source)
                    # Break out of lang loop on rate limit, try next method
                    break
                else:
                    _apply_jitter()
                    # Try next language fallback

    # External provider hook — last chance before giving up
    if _external_provider is not None:
        last_stage_reached = _SOURCE_EXTERNAL
        success, transcript, error = _external_provider(video_id, prefer_lang)
        if success and transcript:
            result = TranscriptResult(
                video_id=video_id,
                lang=prefer_lang,
                raw_lang=prefer_lang,
                was_translated=False,
                transcript=transcript,
                source=_SOURCE_EXTERNAL,
                source_stage=None,
                detected_lang=prefer_lang,
                error=None,
                last_stage=_SOURCE_EXTERNAL,
                failure_reason=None,
            )
            set_cached_transcript(
                video_id,
                prefer_lang,
                _SOURCE_EXTERNAL,
                transcript,
                metadata=build_transcript_cache_metadata(result),
            )
            return result
        last_error = error

    # All methods failed — non-fatal
    failure_reason = _classify_failure(last_error, last_stage_reached or "")
    _log_transcript_chain_event(
        "transcript_chain_failed",
        video_id,
        last_stage=last_stage_reached,
        failure_reason=failure_reason,
        error=last_error,
        elapsed_s=round(time.perf_counter() - chain_started_at, 3),
    )
    # Persist final state to shared archive so restart doesn't re-process this video.
    try:
        _get_scheduler().archive_finalize(video_id, "failed", None, last_error)
    except Exception as e:
        logging.warning(f"[transcript] Failed to archive final failure for {video_id}: {e}")
    return TranscriptResult(
        video_id=video_id,
        lang=prefer_lang,
        raw_lang=None,
        was_translated=False,
        transcript="",
        source="none",
        source_stage=None,
        detected_lang=None,
        error=last_error,
        last_stage=last_stage_reached,
        failure_reason=failure_reason,
    )
