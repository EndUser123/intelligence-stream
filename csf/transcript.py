"""Transcript fetching with full fallback chain.

Fallback order: gemini CLI → youtube_transcript_api → youtubei → Gemini SDK.
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from csf.batch_status import get_source as _get_source_for_video
from csf.batch_scheduler import BatchScheduler
from csf.cache import set_cached_transcript
from csf.youtube_auth import get_browser_cookies

if TYPE_CHECKING:
    from csf.nlm_scraper import NLMIndustrialScraper


# Module-level singleton — avoids repeated _recover_stale_attempting() +
# PRAGMA wal_checkpoint overhead when many 429s/successes fire under concurrency.
_scheduler: BatchScheduler | None = None


def _get_scheduler() -> BatchScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BatchScheduler()
    return _scheduler


# Module-level NLM scraper singleton — one terminal-local staging notebook
# reused across all _fetch_via_notebooklm calls within this process.
_nlm_scraper: "NLMIndustrialScraper | None" = None


def _ensure_nlm_auth() -> bool:
    """Verify nlm CLI authentication is valid, re-authenticating if expired.

    Returns True if auth is valid (or was just refreshed).
    """
    import subprocess

    check = subprocess.run(
        ["nlm", "login", "--check"], capture_output=True, text=True
    )
    if check.returncode == 0:
        return True

    # Auth expired — re-authenticate (auto-launches Chrome headless)
    print("[transcript] NLM auth expired, re-authenticating...")
    login = subprocess.run(["nlm", "login"], capture_output=True, text=True)
    if login.returncode != 0:
        print(f"[transcript] Re-auth failed: {login.stderr}")
        return False
    return True


def _get_nlm_scraper() -> "NLMIndustrialScraper":
    global _nlm_scraper
    if _nlm_scraper is None:
        _ensure_nlm_auth()
        from csf.nlm_scraper import NLMIndustrialScraper

        _nlm_scraper = NLMIndustrialScraper(headless=True)
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
_external_provider: callable | None = None


def register_external_transcript_provider(provider: callable) -> None:
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

# NLMConfig singleton — replaces module-level _NLM_MAX_SOURCES_PER_NOTEBOOK lazy env read
_nlm_config_lock = threading.Lock()
_nlm_config: "NLMConfig | None" = None


@dataclass(frozen=True)
class NLMConfig:
    """Runtime configuration for NLM (NotebookLM) operations.

    Attributes:
        max_sources_per_notebook: Maximum YouTube sources per batch notebook.
            Standard: ~50, Pro: ~300, Ultra: ~600. Default 300.
        auth_check_interval: Seconds between auth check calls. Default 60.0.
        auth_max_calls_per_window: Max auth calls per window before blocking.
            Default 10.
        auth_cooldown: Seconds to block after consecutive auth failures.
            Default 300.0.
    """

    max_sources_per_notebook: int = 300
    auth_check_interval: float = 60.0
    auth_max_calls_per_window: int = 10
    auth_cooldown: float = 300.0


def get_nlm_config() -> NLMConfig:
    """Return the NLMConfig singleton, initializing from env var on first access.

    Thread-safe. Falls back to YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK env var if
    singleton is uninitialized.
    """
    global _nlm_config
    with _nlm_config_lock:
        if _nlm_config is None:
            _nlm_config = NLMConfig(
                max_sources_per_notebook=int(
                    os.environ.get("YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK", "300")
                )
            )
        return _nlm_config


def set_nlm_config(config: NLMConfig) -> None:
    """Set the NLMConfig singleton (for testing override). Thread-safe."""
    global _nlm_config
    with _nlm_config_lock:
        _nlm_config = config


# Minimum transcript content length in characters (accepted at 21 chars, rejected below)
_NLM_MIN_CONTENT_CHARS = 21

# Whisper fallback — set YTIS_WHISPER_ENABLED=false to disable
_WHISPER_ENABLED: bool | None = None  # lazily loaded from env

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


def _fetch_via_ytdlp(video_id: str, lang: str) -> tuple[bool, str | None, str | None]:
    """Fetch transcript using yt-dlp Python API with Chrome TLS impersonation.

    Uses yt-dlp's Python API (not CLI subprocess) with WEB client + curl-cffi
    Chrome impersonation to bypass YouTube's TLS fingerprinting bot detection.
    The "Sign in to confirm you're not a bot" error is a TLS handshake rejection —
    curl-cffi makes the request look like Chrome, bypassing it.

    Falls back gracefully if curl-cffi is not installed.
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
            return (False, None, "subtitle file was empty")

        return (True, full_text.strip(), None)

    except urllib.error.HTTPError as e:
        if e.code == 429:
            return (False, None, "rate limited (429)")
        return (False, None, f"yt-dlp HTTP error: {e.code}")
    except subprocess.TimeoutExpired:
        return (False, None, "yt-dlp timed out")
    except Exception as e:
        err_str = str(e).lower()
        if "429" in err_str or "too many requests" in err_str:
            return (False, None, "rate limited (429)")
        if "no subtitles" in err_str or "does not have any subtitles" in err_str:
            return (False, None, "no subtitles available")
        if "sign in to confirm" in err_str or "not a bot" in err_str:
            # Bot-check triggered — try age-restricted approach with cookies + default extractor.
            # This is a second attempt inside the same function rather than a separate method.
            return _fetch_via_ytdlp_with_cookies(video_id, lang)
        return (False, None, f"yt-dlp error: {e}")


def _fetch_via_ytdlp_with_cookies(
    video_id: str, lang: str
) -> tuple[bool, str | None, str | None]:
    """Second-attempt transcript fetch with browser cookies for age-restricted videos.

    Called by _fetch_via_ytdlp when bot-check fires on the WEB client approach.
    Uses the default yt-dlp extractor (not WEB client) with Firefox browser cookies.
    Falls back gracefully if cookies are unavailable or extraction fails.
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
            return (False, None, "no subtitles available")

        sub_url = subs[0].get("url")
        if not sub_url:
            _release_cookie_file(cookie_file)
            return (False, None, "no subtitle URL in yt-dlp response")

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
            return (False, None, "subtitle file was empty")

        return (True, full_text.strip(), None)

    except urllib.error.HTTPError as e:
        _release_cookie_file(cookie_file)
        if e.code == 429:
            return (False, None, "rate limited (429)")
        return (False, None, f"yt-dlp-with-cookies HTTP error: {e.code}")
    except subprocess.TimeoutExpired:
        _release_cookie_file(cookie_file)
        return (False, None, "yt-dlp-with-cookies timed out")
    except Exception as e:
        _release_cookie_file(cookie_file)
        err_str = str(e).lower()
        if "429" in err_str or "too many requests" in err_str:
            return (False, None, "rate limited (429)")
        if "sign in" in err_str or "age" in err_str or "login" in err_str:
            return (False, None, "age-restricted or requires login")
        return (False, None, f"yt-dlp-with-cookies error: {e}")


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
        # Download audio only via yt-dlp
        cmd = [
            "yt-dlp",
            *get_browser_cookies("firefox"),
            "-f",
            "bestaudio[ext=m4a]",
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
        if proc.returncode != 0:
            stderr_lower = proc.stderr.lower()
            if "429" in proc.stderr or "too many requests" in stderr_lower:
                return (False, None, "audio download rate limited (429)")
            if "not found" in stderr_lower or "video unavailable" in stderr_lower:
                return (False, None, "video unavailable for audio download")
            return (False, None, f"audio download failed: {proc.stderr.strip()[:200]}")

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
        text = " ".join(segment.text for segment in segments)
        if not text.strip():
            return (False, None, "whisper produced empty transcript")
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
            rate_limiter.record_call()
            return True
    except Exception:
        pass

    # 4. Auth expired — auto-recover with force login
    try:
        rate_limiter.record_call()
        login = subprocess.run(
            ["nlm", "login", "--force"],
            capture_output=True, timeout=120,
        )
        if login.returncode == 0:
            rate_limiter.record_auth_success()
            return True
        # Only --force failures count toward cooldown trigger
        rate_limiter.record_auth_failure()
        return False
    except Exception:
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
    try:
        import youtube_transcript_api
    except ImportError:
        logging.warning("[_fetch_via_direct_api] youtube_transcript_api not installed")
        return (False, None, "no_transcript")

    try:
        api = youtube_transcript_api.YouTubeTranscriptApi()
        # List available transcripts to find a non-generated English one first
        transcripts = api.list_transcripts(video_id)
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
        return (False, None, f"direct_api error: {e}")


def fetch_transcript_chain(video_id: str, config: LanguageConfig) -> TranscriptResult:
    """Fetch transcript using yt-dlp → Selenium → NotebookLM fallback chain.

    Chain order:
      1. yt-dlp (WEB client, curl_cffi TLS) — High Fidelity, Fastest Local
      2. NotebookLM Industrial (Cloud) — High Fidelity, Cleanest Data, Best for Backlog
      3. Selenium Firefox — Dirty Scraper (Polluted with page noise), Slow
      4. Whisper — Audio Fallback

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
        (_SOURCE_NLM, _fetch_via_notebooklm, STAGE_VERSION_NOTEBOOKLM),
        (_SOURCE_SELENIUM, _fetch_via_selenium_firefox, STAGE_VERSION_SELENIUM),
        (_SOURCE_WHISPER, _fetch_via_whisper, None),  # audio fallback — no captions needed
        (_SOURCE_DIRECT_API, _fetch_via_direct_api, STAGE_VERSION_DIRECT_API),
    ]

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

                set_cached_transcript(video_id, prefer_lang, source, final_transcript)
                return TranscriptResult(
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
            last_error = error
        # direct_api uses different signature (no lang arg)
        elif source == _SOURCE_DIRECT_API:
            success, transcript, error = fetch_fn(video_id)
            if success and transcript:
                _record_source_success(source, video_id)
                set_cached_transcript(video_id, prefer_lang, source, transcript)
                return TranscriptResult(
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
            last_error = error
        else:
            for lang in lang_fallbacks:
                # Use "en" as placeholder when lang is None (yt-dlp will use its default)
                try_lang = lang if lang is not None else "en"

                success, transcript, error = fetch_fn(video_id, try_lang)
                if success and transcript:
                    _record_source_success(source, video_id)

                    # Determine actual language and whether translation is needed
                    raw_lang = lang if lang is not None else "en"
                    detected_lang = raw_lang
                    final_transcript = transcript
                    was_translated = False

                    # Translate if raw_lang != prefer_lang and translation is enabled
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
                        source_stage=stage,
                        detected_lang=detected_lang,
                        error=None,
                        last_stage=source,
                        failure_reason=None,
                    )

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
            set_cached_transcript(video_id, prefer_lang, _SOURCE_EXTERNAL, transcript)
            return TranscriptResult(
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
        last_error = error

    # All methods failed — non-fatal
    failure_reason = _classify_failure(last_error, last_stage_reached or "")
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
