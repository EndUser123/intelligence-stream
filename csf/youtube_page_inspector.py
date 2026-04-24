"""Direct YouTube watch-page inspection helpers.

These helpers fetch a YouTube watch page and classify the playback state from
the embedded ytInitialPlayerResponse payload. They are used as an auxiliary
signal for failure classification when NotebookLM or transcript probes fail.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

_YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
_YOUTUBE_PAGE_TIMEOUT_S = float(os.getenv("YTIS_YOUTUBE_WATCH_PAGE_TIMEOUT_S", "12"))
_YOUTUBE_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "identity",
    "Referer": "https://www.youtube.com/",
}
_YTDLP_WATCH_TIMEOUT_S = float(os.getenv("YTIS_YTDLP_WATCH_TIMEOUT_S", "20"))


def _extract_renderer_text(node: Any) -> str | None:
    if node is None:
        return None
    if isinstance(node, str):
        text = node.strip()
        return text or None
    if isinstance(node, dict):
        if isinstance(node.get("simpleText"), str):
            text = node["simpleText"].strip()
            if text:
                return text
        runs = node.get("runs")
        if isinstance(runs, list):
            parts: list[str] = []
            for run in runs:
                if isinstance(run, dict):
                    text = str(run.get("text") or "").strip()
                    if text:
                        parts.append(text)
            joined = " ".join(parts).strip()
            if joined:
                return joined
        for key in ("text", "label"):
            value = node.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
    return None


def _extract_balanced_json_object(text: str, marker: str) -> str | None:
    marker_index = text.find(marker)
    if marker_index < 0:
        return None
    start_index = text.find("{", marker_index)
    if start_index < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start_index, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_index : idx + 1]
    return None


def extract_yt_initial_player_response(html: str) -> dict[str, Any]:
    """Extract the embedded ytInitialPlayerResponse JSON object from HTML."""
    json_blob = _extract_balanced_json_object(html, "ytInitialPlayerResponse")
    if not json_blob:
        raise ValueError("ytInitialPlayerResponse not found")
    parsed = json.loads(json_blob)
    if not isinstance(parsed, dict):
        raise ValueError("ytInitialPlayerResponse was not a JSON object")
    return parsed


def classify_youtube_watch_page(player: dict[str, Any]) -> dict[str, Any]:
    """Classify the playback state from a parsed watch-page payload."""
    playability = player.get("playabilityStatus")
    if not isinstance(playability, dict):
        playability = {}
    video_details = player.get("videoDetails")
    if not isinstance(video_details, dict):
        video_details = {}

    status = str(playability.get("status") or "UNKNOWN").strip() or "UNKNOWN"
    reason = _extract_renderer_text(playability.get("reason"))
    error_screen = playability.get("errorScreen")
    if not isinstance(error_screen, dict):
        error_screen = {}
    renderer = error_screen.get("playerErrorMessageRenderer")
    if not isinstance(renderer, dict):
        renderer = {}
    subreason = _extract_renderer_text(renderer.get("subreason"))
    title = _extract_renderer_text(video_details.get("title"))
    is_live_content_raw = video_details.get("isLiveContent")
    is_live_content = bool(is_live_content_raw) if is_live_content_raw is not None else None

    reason_blob = " ".join(part for part in (reason, subreason) if part).lower()

    classification = "unavailable"
    if status == "OK":
        classification = "ok"
    elif status == "LIVE_STREAM_OFFLINE":
        if any(
            marker in reason_blob
            for marker in (
                "begin in a few moments",
                "will begin",
                "not yet",
                "starts soon",
            )
        ):
            classification = "not_yet_live"
        elif "ended" in reason_blob or "over" in reason_blob or "concluded" in reason_blob:
            classification = "ended_live"
        else:
            classification = "live_stream_offline"
    elif "removed by the uploader" in reason_blob or "removed by owner" in reason_blob:
        classification = "removed_by_owner"
    elif "private video" in reason_blob or "this video is private" in reason_blob:
        classification = "private"
    elif "sign in to confirm your age" in reason_blob or "age-restricted" in reason_blob or "age restricted" in reason_blob:
        classification = "age_restricted"
    elif "not available in your country" in reason_blob or "geo" in reason_blob:
        classification = "geo_block"
    elif "login required" in reason_blob:
        classification = "login_required"
    elif "video unavailable" in reason_blob or "unavailable" in reason_blob or "not available" in reason_blob:
        classification = "unavailable"

    return {
        "classification": classification,
        "available": classification == "ok",
        "status": status,
        "reason": reason,
        "subreason": subreason,
        "is_live_content": is_live_content,
        "title": title,
    }


def classify_ytdlp_watch_info(info: dict[str, Any]) -> dict[str, Any]:
    """Classify yt-dlp JSON metadata into a coarse availability bucket."""
    availability = str(info.get("availability") or "").strip().lower() or None
    live_status = str(info.get("live_status") or "").strip().lower() or None
    title = _extract_renderer_text(info.get("title"))
    was_live_raw = info.get("was_live")
    was_live = bool(was_live_raw) if was_live_raw is not None else None
    is_live_raw = info.get("is_live")
    is_live = bool(is_live_raw) if is_live_raw is not None else None

    classification = "unknown"
    if availability in {"private"}:
        classification = "private"
    elif availability in {"needs_auth", "subscriber_only"}:
        classification = "login_required"
    elif availability in {"premium_only"}:
        classification = "premium_only"
    elif availability in {"unavailable", "deleted"}:
        classification = "unavailable"
    elif live_status in {"is_live"} or is_live is True:
        classification = "live"
    elif live_status in {"post_live"} or was_live is True:
        classification = "ended_live"
    elif live_status in {"is_upcoming", "upcoming"}:
        classification = "not_yet_live"
    elif availability in {"public", "unlisted"} or live_status in {"not_live", None}:
        classification = "ok"
    elif availability:
        classification = availability

    return {
        "classification": classification,
        "available": classification == "ok",
        "availability": availability,
        "live_status": live_status,
        "was_live": was_live,
        "is_live": is_live,
        "title": title,
    }


def inspect_youtube_watch_page_via_ytdlp(video_id: str, *, timeout_s: float = _YTDLP_WATCH_TIMEOUT_S) -> dict[str, Any]:
    """Fetch and classify a YouTube watch page using yt-dlp JSON output."""
    video_id = str(video_id or "").strip()
    url = _YOUTUBE_WATCH_URL.format(video_id=video_id)
    started_at = time.time()
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return {
            "video_id": video_id,
            "url": url,
            "available": False,
            "classification": "error",
            "availability": None,
            "live_status": None,
            "was_live": None,
            "is_live": None,
            "title": None,
            "stdout": "",
            "stderr": "yt-dlp not found on PATH",
            "returncode": None,
            "checked_at_epoch": started_at,
            "elapsed_s": round(time.time() - started_at, 3),
        }

    proc = subprocess.run(
        [ytdlp, "-J", "--skip-download", "--no-playlist", url],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode == 0:
        try:
            payload = json.loads(stdout)
            if not isinstance(payload, dict):
                raise ValueError("yt-dlp JSON payload was not an object")
            classified = classify_ytdlp_watch_info(payload)
            classified.update(
                {
                    "video_id": video_id,
                    "url": url,
                    "stdout": stdout[:4000],
                    "stderr": stderr[:4000],
                    "returncode": proc.returncode,
                    "checked_at_epoch": started_at,
                    "elapsed_s": round(time.time() - started_at, 3),
                }
            )
            return classified
        except Exception as e:
            return {
                "video_id": video_id,
                "url": url,
                "available": False,
                "classification": "error",
                "availability": None,
                "live_status": None,
                "was_live": None,
                "is_live": None,
                "title": None,
                "stdout": stdout[:4000],
                "stderr": stderr[:4000],
                "returncode": proc.returncode,
                "checked_at_epoch": started_at,
                "elapsed_s": round(time.time() - started_at, 3),
                "error": str(e),
            }

    stderr_blob = f"{stdout}\n{stderr}".lower()
    classification = "error"
    if "removed by the uploader" in stderr_blob:
        classification = "removed_by_owner"
    elif "private video" in stderr_blob or "is private" in stderr_blob:
        classification = "private"
    elif "not available in your country" in stderr_blob or "geo" in stderr_blob:
        classification = "geo_block"
    elif "begin in a few moments" in stderr_blob or "will begin" in stderr_blob or "not yet live" in stderr_blob:
        classification = "not_yet_live"
    elif "video unavailable" in stderr_blob or "unavailable" in stderr_blob or "deleted" in stderr_blob:
        classification = "unavailable"
    elif "age-restricted" in stderr_blob or "age restricted" in stderr_blob or "confirm your age" in stderr_blob:
        classification = "age_restricted"
    elif "login required" in stderr_blob or "sign in" in stderr_blob:
        classification = "login_required"
    elif proc.returncode == 0:
        classification = "ok"

    return {
        "video_id": video_id,
        "url": url,
        "available": classification == "ok",
        "classification": classification,
        "availability": None,
        "live_status": None,
        "was_live": None,
        "is_live": None,
        "title": None,
        "stdout": stdout[:4000],
        "stderr": stderr[:4000],
        "returncode": proc.returncode,
        "checked_at_epoch": started_at,
        "elapsed_s": round(time.time() - started_at, 3),
    }


def inspect_youtube_watch_page(video_id: str, *, timeout_s: float = _YOUTUBE_PAGE_TIMEOUT_S) -> dict[str, Any]:
    """Fetch and classify a public YouTube watch page when possible."""
    video_id = str(video_id or "").strip()
    url = _YOUTUBE_WATCH_URL.format(video_id=video_id)
    started_at = time.time()
    req = urllib.request.Request(url, headers=_YOUTUBE_PAGE_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            http_status = getattr(resp, "status", 200)
            html = resp.read().decode("utf-8", "replace")
        player = extract_yt_initial_player_response(html)
        classified = classify_youtube_watch_page(player)
        classified.update(
            {
                "video_id": video_id,
                "url": url,
                "http_status": http_status,
                "checked_at_epoch": started_at,
                "elapsed_s": round(time.time() - started_at, 3),
            }
        )
        return classified
    except urllib.error.HTTPError as e:
        status = "http_error"
        if e.code in {401, 403, 404, 410}:
            status = "unavailable"
        elif e.code == 429:
            status = "rate_limited"
        return {
            "video_id": video_id,
            "url": url,
            "available": False,
            "classification": status,
            "status": status,
            "reason": f"HTTP {e.code}",
            "subreason": None,
            "is_live_content": None,
            "title": None,
            "http_status": e.code,
            "checked_at_epoch": started_at,
            "elapsed_s": round(time.time() - started_at, 3),
            "error": str(e),
        }
    except Exception as e:
        return {
            "video_id": video_id,
            "url": url,
            "available": False,
            "classification": "error",
            "status": "error",
            "reason": None,
            "subreason": None,
            "is_live_content": None,
            "title": None,
            "http_status": None,
            "checked_at_epoch": started_at,
            "elapsed_s": round(time.time() - started_at, 3),
            "error": str(e),
        }
