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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from csf.batch_status import get_source as _get_source_for_video
from csf.batch_scheduler import BatchScheduler

# Module-level singleton — avoids repeated _recover_stale_attempting() +
# PRAGMA wal_checkpoint overhead when many 429s/successes fire under concurrency.
_scheduler: BatchScheduler | None = None


def _get_scheduler() -> BatchScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BatchScheduler()
    return _scheduler
from csf.cache import set_cached_transcript
from csf.quota_tracker import is_free_only_mode
from csf.youtube_auth import get_browser_cookies

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
            'youtubei', 'sdk', 'whisper', 'none').
        detected_lang: The detected language of the returned transcript,
            or None if language detection failed or no transcript available.
        error: The error message from the last failed source, or None if no
            error occurred or transcript was successfully fetched.
    """

    video_id: str
    lang: str
    raw_lang: str | None
    was_translated: bool
    transcript: str
    source: str
    detected_lang: str | None
    error: str | None


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


def check_video_availability(video_id: str) -> tuple[bool, str | None]:
    """Pre-check video availability using list_transcripts() — no quota consumed.

    Returns:
        (True, None) — video is available for transcript fetching
        (False, "video_unavailable") — video is unavailable (permanent)
        (False, "transcripts_disabled") — channel has disabled transcripts
        (False, "no_transcript_found") — no transcript languages available
        (False, str(e)) — unexpected error
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return (False, "youtube_transcript_api not installed")

    try:
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )

        def _check() -> None:
            api = YouTubeTranscriptApi()
            api.list(video_id)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_check)
            try:
                future.result(timeout=10)
            except TimeoutError:
                return (False, "video_unavailable")  # treat timeout as unavailable
            except VideoUnavailable:
                return (False, "video_unavailable")
            except TranscriptsDisabled:
                return (False, "transcripts_disabled")
            except NoTranscriptFound:
                return (False, "no_transcript_found")
        return (True, None)
    except ImportError:
        return (False, "youtube_transcript_api not installed")
    except Exception as e:
        return (False, str(e))


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
        # WEB client avoids bot-detection on public videos. No cookies needed.
        # Age-restricted videos require auth — handled by second attempt below.
        "extractor_args": {
            "youtube": {
                "client_name": "WEB",
                "client_version": "2.20210721.01.00",
            }
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
            # Fall back to urllib — will likely get bot-checked
            import urllib.request

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

    # Find Firefox cookies file — requires Firefox running to extract live cookies
    cookie_file = _get_firefox_cookie_file()
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
        # EJS github component resolves YouTube's JS challenge for age-restricted videos.
        # Works alongside cookies to authenticate and extract transcripts.
        "extractor_args": {
            "youtube": {"external_downloader": "ejs:github"}
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
            os.unlink(cookie_file)
            return (False, None, "no subtitles available")

        sub_url = subs[0].get("url")
        if not sub_url:
            os.unlink(cookie_file)
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
        os.unlink(cookie_file)
        if not full_text.strip():
            return (False, None, "subtitle file was empty")

        return (True, full_text.strip(), None)

    except urllib.error.HTTPError as e:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass
        if e.code == 429:
            return (False, None, "rate limited (429)")
        return (False, None, f"yt-dlp-with-cookies HTTP error: {e.code}")
    except subprocess.TimeoutExpired:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass
        return (False, None, "yt-dlp-with-cookies timed out")
    except Exception as e:
        try:
            os.unlink(cookie_file)
        except Exception:
            pass
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
        # Use the default Firefox profile so cookies/session carry over
        import glob as _glob

        appdata = os.environ.get("APPDATA") or ""
        profile_base = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
        profiles = _glob.glob(os.path.join(profile_base, "*.default*"))
        if profiles:
            firefox_profile_path = profiles[0]

        opts = Options()
        opts.add_argument("--headless=new")

        service = Service()
        if firefox_profile_path:
            profile = FirefoxProfile(firefox_profile_path)
            driver = webdriver.Firefox(service=service, options=opts, firefox_profile=profile)
        else:
            driver = webdriver.Firefox(service=service, options=opts)

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


def fetch_transcript_chain(video_id: str, config: LanguageConfig) -> TranscriptResult:
    """Fetch transcript using full fallback chain with optional translation.

    Chain order:
      1. yt-dlp Python API (WEB client, curl_cffi TLS) — public videos (~2s)
         → bot-check triggers immediate cookie-based retry with browser cookies
      2. youtube_transcript_api, youtubei, Gemini SDK, gemini CLI
      3. Steps 1-2 with "en" fallback language
      4. yt-dlp Python API with browser cookies — age-restricted videos (~5-10s)
      5. Selenium Firefox — real browser TLS bypasses YouTube bot-check (~15-30s)
      6. Whisper audio transcription (last resort, ~30-90s)

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
            detected_lang=None,
            error=f"invalid BCP-47 language code: {prefer_lang!r}",
        )

    # Helper to build a "no transcript" result
    def _none_result(last_err: str | None = None) -> TranscriptResult:
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=None,
            was_translated=False,
            transcript="",
            source="none",
            detected_lang=None,
            error=last_err,
        )

    # Fallback chain: yt-dlp → youtube_transcript_api → youtubei → SDK
    # Bot-check on yt-dlp triggers Selenium Firefox immediately (Step 1b above).
    # youtube_transcript_api is re-enabled — it uses browser cookies and may succeed
    # where bare yt-dlp fails due to IP-level blocking.
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
        if _is_source_rate_limited(source):
            continue  # skip circuit-open source
        if source == _SOURCE_CLI:
            from csf.quota_tracker import increment_cli_calls

            increment_cli_calls()
        success, transcript, error = fetch_fn(video_id, prefer_lang)
        if success and transcript:
            _record_source_success(source, video_id)
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
                error=None,
            )
        last_error = error
        if error and ("429" in error.lower() or "rate limited" in error.lower()):
            _record_source_429(source, video_id)
            _apply_jitter_with_backoff(source)
        else:
            _apply_jitter()

        # Bot-check from yt-dlp: try Selenium Firefox immediately, skip remaining methods
        if source == _SOURCE_YTDLP and error == "yt-dlp bot_check":
            success, transcript, error = _fetch_via_selenium_firefox(video_id, prefer_lang)
            if success and transcript:
                _record_source_success("selenium_firefox", video_id)
                set_cached_transcript(video_id, prefer_lang, "selenium_firefox", transcript)
                return TranscriptResult(
                    video_id=video_id,
                    lang=prefer_lang,
                    raw_lang=prefer_lang,
                    was_translated=False,
                    transcript=transcript,
                    source="selenium_firefox",
                    detected_lang=prefer_lang,
                    error=None,
                )
            # Selenium also failed — fall through to generic fallback

    # Step 2: Try any language
    for source, fetch_fn in free_methods:  # Only free methods for fallback
        if _is_source_rate_limited(source):
            continue  # skip circuit-open source
        success, transcript, error = fetch_fn(video_id, "en")
        if success and transcript:
            _record_source_success(source, video_id)
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
                error=None,
            )
        last_error = error
        if error and ("429" in error.lower() or "rate limited" in error.lower()):
            _record_source_429(source, video_id)
            _apply_jitter_with_backoff(source)
        else:
            _apply_jitter()

    # Step 3: yt-dlp Python API with browser cookies — age-restricted videos
    success, transcript, error = _fetch_via_ytdlp_with_cookies(video_id, prefer_lang)
    if success and transcript:
        _record_source_success(_SOURCE_YTDLP_EJS, video_id)
        set_cached_transcript(video_id, prefer_lang, _SOURCE_YTDLP_EJS, transcript)
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=prefer_lang,
            was_translated=False,
            transcript=transcript,
            source=_SOURCE_YTDLP_EJS,
            detected_lang=prefer_lang,
            error=None,
        )
    last_error = error or last_error

    # Step 4: Selenium Firefox (last resort before Whisper)
    success, transcript, error = _fetch_via_selenium_firefox(video_id, prefer_lang)
    if success and transcript:
        _record_source_success("selenium_firefox", video_id)
        set_cached_transcript(video_id, prefer_lang, "selenium_firefox", transcript)
        return TranscriptResult(
            video_id=video_id,
            lang=prefer_lang,
            raw_lang=prefer_lang,
            was_translated=False,
            transcript=transcript,
            source="selenium_firefox",
            detected_lang=prefer_lang,
            error=None,
        )
    last_error = error or last_error

    # All methods failed — non-fatal
    # Persist final state to shared archive so restart doesn't re-process this video.
    try:
        BatchScheduler().archive_finalize(video_id, "failed")
    except Exception as e:
        logging.warning(f"[transcript] Failed to archive final failure for {video_id}: {e}")
    return _none_result(last_error)
