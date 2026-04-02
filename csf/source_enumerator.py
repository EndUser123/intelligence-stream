"""YouTube source enumeration for intelligence-stream pipeline — Phase 2.

Three-tier enumeration strategy:
- Tier 1: RSS (daily monitoring, free, stateless)
- Tier 2: YouTube Data API with publishedAfter cursor (gap resolution)
- Tier 3: yt-dlp --flat-playlist (full enumeration fallback)

Uses YOUTUBE_API_KEY from environment for API calls.
"""

import os
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# YouTube Data API endpoint
_YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# RSS feed URL template
_RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# Channel ID pattern (UC...)
_CHANNEL_ID_PATTERN = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")

# Threshold constants from ADR
_GAP_TRIGGER_RSS_COUNT = 15  # Minimum RSS videos to trigger gap detection
_GAP_TRIGGER_DAYS_OLD = 7  # Newest batch video must be > 7 days old


@dataclass
class _ChannelMetadata:
    """Metadata for a tracked YouTube channel.

    Stored in the channel_metadata table of batch_status.sqlite.
    """

    channel_url: str
    playlist_id: str | None = None
    last_checked: str | None = None
    last_full_enumeration: str | None = None
    video_count_estimate: int = 0
    next_page_token: str | None = None
    quota_exhausted_at: str | None = None


_YOUTUBE_API_KEYS: list[str] | None = None


def _get_api_keys() -> list[str]:
    """Get all available YouTube Data API keys, ordered by priority.

    Returns:
        List of API keys (primary first), filtered to non-empty values.
    """
    global _YOUTUBE_API_KEYS
    if _YOUTUBE_API_KEYS is None:
        raw_keys: list[str | None] = [
            os.environ.get("YOUTUBE_API_KEY"),
            os.environ.get("YOUTUBE_API_KEY_2"),
            os.environ.get("YOUTUBE_API_KEY_3"),
            os.environ.get("YOUTUBE_API_KEY_4"),
            os.environ.get("YOUTUBE_API_KEY_5"),
            # Aliases used in P:/.env (YT_API_KEY_*, not YOUTUBE_API_KEY*)
            os.environ.get("YT_API_KEY_1"),
            os.environ.get("YT_API_KEY_2"),
            os.environ.get("YT_API_KEY_3"),
            os.environ.get("YT_API_KEY_4"),
            os.environ.get("YT_API_KEY_5"),
        ]
        _YOUTUBE_API_KEYS = [k for k in raw_keys if k]
    assert _YOUTUBE_API_KEYS is not None
    return _YOUTUBE_API_KEYS


def _api_request(endpoint: str, params: dict) -> dict | None:
    """Make a YouTube Data API request with automatic key failover.

    Tries each available key in order; on 403/429 (quota exceeded), tries next.
    On 404, returns None immediately (resource genuinely not found).

    Args:
        endpoint: API endpoint (e.g., 'channels', 'playlistItems')
        params: Query parameters including 'key' for API key

    Returns:
        JSON response dict or None on error.
    """
    keys = _get_api_keys()
    if not keys:
        return None

    url = f"{_YOUTUBE_API_BASE}/{endpoint}"
    last_error: str | None = None

    for api_key in keys:
        all_params = {**params, "key": api_key}
        query = urllib.parse.urlencode(all_params)

        try:
            req = urllib.request.Request(f"{url}?{query}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                import json

                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 404:
                return None
            # 400 with "expired" = bad key, try next key
            if e.code == 400 and "expired" in body.lower():
                last_error = f"key expired (HTTP 400)"
                continue  # try next key
            if e.code == 403 or e.code == 429:
                last_error = f"quota exceeded (HTTP {e.code})"
                continue  # try next key
            return None
        except Exception:
            return None

    import logging

    logging.warning(f"YouTube API all keys quota exceeded: {last_error}")
    return None


def parse_channel_url(url: str) -> str | None:
    """Parse a YouTube channel URL and return the channel identifier.

    Supports:
    - https://www.youtube.com/channel/UCxxxx (channel ID returned as-is)
    - https://www.youtube.com/@handle (@handle returned as-is for API resolution)
    - https://www.youtube.com/c/customname (custom URL returned as-is)
    - Bare UCxxxx channel ID (returned as-is)

    Args:
        url: Channel URL or bare channel ID

    Returns:
        Channel identifier (channel ID, @handle, or c/name) or None if invalid.
    """
    if not url:
        return None

    # Bare channel ID
    if _CHANNEL_ID_PATTERN.match(url):
        return url

    # Remove trailing slash
    url = url.rstrip("/")

    # Only accept youtube.com domains (not notyoutube.com, etc.)
    # Negative lookbehind ensures youtube.com is not preceded by alphanumeric
    if not re.search(r"(?<![a-zA-Z0-9])youtube\.com/", url):
        return None

    # Channel URL pattern
    channel_match = re.search(r"/channel/([a-zA-Z0-9_-]+)", url)
    if channel_match:
        return channel_match.group(1)

    # Handle pattern (@username)
    handle_match = re.search(r"/@([a-zA-Z0-9_-]+)", url)
    if handle_match:
        return f"@{handle_match.group(1)}"

    # Custom URL pattern
    custom_match = re.search(r"/c/([a-zA-Z0-9_-]+)", url)
    if custom_match:
        return f"c/{custom_match.group(1)}"

    # User URL pattern
    user_match = re.search(r"/user/([a-zA-Z0-9_-]+)", url)
    if user_match:
        return f"user/{user_match.group(1)}"

    return None


def parse_playlist_url(url: str) -> str | None:
    """Parse a YouTube playlist URL and return the playlist ID.

    Supports:
    - https://www.youtube.com/playlist?list=PLxxxx
    - https://www.youtube.com/watch?v=...&list=PLxxxx

    Args:
        url: Playlist URL or bare playlist ID

    Returns:
        Playlist ID or None if not found.
    """
    if not url:
        return None

    # Bare playlist ID (starts with PL, UL, etc.)
    if re.match(r"^PL[a-zA-Z0-9_-]+$", url):
        return url

    # Remove trailing slash
    url = url.rstrip("/")

    # Extract from query parameter
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    if "list" in query_params:
        return query_params["list"][0]

    return None


# Video ID pattern (11-character alphanumeric + -_)
_VIDEO_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{11}$")


def parse_video_url(url: str) -> str | None:
    """Parse a YouTube watch URL and return the video ID.

    Supports:
    - https://www.youtube.com/watch?v=xxxxx
    - https://www.youtube.com/watch?v=xxxxx&list=... (list param ignored)
    - Bare 11-char video ID

    Args:
        url: Watch URL or bare video ID

    Returns:
        Video ID or None if not found.
    """
    if not url:
        return None

    # Bare video ID
    if _VIDEO_ID_PATTERN.match(url):
        return url

    # Must contain youtube.com/watch
    if "youtube.com/watch" not in url:
        return None

    url = url.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    if "v" in query_params:
        vid = query_params["v"][0]
        if _VIDEO_ID_PATTERN.match(vid):
            return vid

    return None


def get_upload_playlist_id(channel_id: str) -> str | None:
    """Get the uploads playlist ID for a channel using YouTube Data API.

    Uses channels.list API with contentDetails projection.

    Args:
        channel_id: Channel identifier (UC..., @handle, c/name, user/name)

    Returns:
        Upload playlist ID (starts with UU) or None if not found.
    """
    # Determine part and id-type based on channel_id format
    if channel_id.startswith("UC"):
        # Direct channel ID
        params = {"part": "contentDetails", "id": channel_id}
    elif channel_id.startswith("@"):
        # Handle
        params = {"part": "contentDetails", "forHandle": channel_id}
    elif channel_id.startswith("c/") or channel_id.startswith("user/"):
        # Custom URL or user URL
        name = channel_id.split("/", 1)[1]
        params = {"part": "contentDetails", "forUsername": name}
    else:
        return None

    result = _api_request("channels", params)
    if not result or "items" not in result or len(result["items"]) == 0:
        return None

    try:
        return result["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except KeyError:
        return None


def enumerate_videos_api(
    playlist_id: str,
    max_results: int = 50,
    page_token: str | None = None,
    published_after: str | None = None,
) -> tuple[list[dict], str | None]:
    """Enumerate videos from a playlist using YouTube Data API.

    Args:
        playlist_id: Playlist ID (UU... for uploads)
        max_results: Max results per page (default 50, API max)
        page_token: Next page token for pagination
        published_after: ISO timestamp to filter videos published after

    Returns:
        Tuple of (list of video dicts with id/title/publishedAt, next_page_token or None)
    """
    params: dict[str, str | int] = {
        "part": "snippet,status,contentDetails",
        "playlistId": playlist_id,
        "maxResults": min(max_results, 50),
    }

    if page_token:
        params["pageToken"] = page_token

    if published_after:
        # YouTube Data API requires RFC 3339 format: '2026-03-28T00:00:00Z'
        # Handle both Z-suffix and +00:00 offset formats
        normalized = published_after.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
            params["publishedAfter"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # Fallback: treat as raw string if already RFC 3339
            params["publishedAfter"] = published_after

    result = _api_request("playlistItems", params)
    if not result or "items" not in result:
        return [], None

    videos = []
    for item in result["items"]:
        try:
            snippet = item["snippet"]
            status = item.get("status", {})

            # Filter non-playable videos at enumeration time (fail-fast)
            privacy = status.get("privacyStatus", "public")
            upload = status.get("uploadStatus", "")
            if privacy in ("private", "memberOnly"):
                continue
            if upload in ("deleted", "failed", "processing"):
                continue

            video = {
                "video_id": snippet["resourceId"]["videoId"],
                "title": snippet.get("title", ""),
                "published_at": snippet.get("publishedAt", ""),
                "has_captions": item.get("contentDetails", {}).get("caption", False),
            }
            videos.append(video)
        except KeyError:
            continue

    next_token = result.get("nextPageToken")
    return videos, next_token


def enumerate_full(playlist_id: str) -> list[dict]:
    """Fully enumerate all videos in a playlist via pagination.

    Used for initial import. Uses nextPageToken pagination.

    Args:
        playlist_id: Playlist ID to enumerate

    Returns:
        List of video dicts with id/title/publishedAt
    """
    all_videos = []
    page_token = None

    while True:
        videos, next_token = enumerate_videos_api(playlist_id, page_token=page_token)
        all_videos.extend(videos)

        if not next_token:
            break

        page_token = next_token

        # Safety limit to prevent infinite loops
        if len(all_videos) > 10000:
            break

    return all_videos


def enumerate_recent(
    playlist_id: str,
    published_after: str,
    max_iterations: int = 20,
) -> list[dict]:
    """Enumerate videos published after a timestamp using publishedAfter cursor.

    Used for gap resolution. Fetches 50 at a time until overlap is found.

    Args:
        playlist_id: Playlist ID to enumerate
        published_after: ISO timestamp (lower bound)
        max_iterations: Max API calls to bound quota usage (default 20 = 1000 videos)

    Returns:
        List of video dicts with id/title/publishedAt
    """
    all_videos = []
    cursor = published_after

    for _ in range(max_iterations):
        videos, _ = enumerate_videos_api(playlist_id, published_after=cursor)
        if not videos:
            break

        all_videos.extend(videos)

        # Use oldest video's publishedAt as new cursor for next page
        cursor = videos[-1]["published_at"]

        # If we got fewer than 50, we've reached the end
        if len(videos) < 50:
            break

    return all_videos


def check_rss(channel_id: str) -> list[str]:
    """Check RSS feed for recent videos from a channel.

    Returns up to ~15-20 most recent video IDs.

    Args:
        channel_id: YouTube channel ID (UC...)

    Returns:
        List of recent video IDs from RSS feed.
    """
    if not _CHANNEL_ID_PATTERN.match(channel_id):
        return []

    url = _RSS_TEMPLATE.format(channel_id=channel_id)

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_content = resp.read().decode("utf-8")
    except Exception:
        return []

    # Parse YouTube RSS namespace
    try:
        root = ET.fromstring(xml_content)
        ns = {"yt": "http://www.youtube.com/xml/schemas/2015"}

        video_ids = []
        for entry in root.findall(".//entry"):
            # YouTube video ID is in the yt:videoId element
            video_id_elem = entry.find("yt:videoId", ns)
            if video_id_elem is not None and video_id_elem.text:
                video_ids.append(video_id_elem.text)

        return video_ids
    except Exception:
        return []


def detect_gap(
    rss_ids: list[str],
    batch_status_ids: set[str],
    newest_batch_published: datetime | None,
) -> bool:
    """Detect whether a channel has a gap requiring API gap resolution.

    Gap is triggered when:
    - RSS returns >= 15 non-overlapping video IDs
    - AND newest batch video is > 7 days old

    Args:
        rss_ids: Video IDs from RSS feed
        batch_status_ids: Video IDs already in batch_status for this channel
        newest_batch_published: datetime of the newest video already processed

    Returns:
        True if gap resolution is needed, False otherwise.
    """
    if len(rss_ids) < _GAP_TRIGGER_RSS_COUNT:
        return False

    # Check for overlap
    rss_set = set(rss_ids)
    overlap = rss_set & batch_status_ids

    # No overlap = potential gap
    if not overlap:
        if newest_batch_published is None:
            # No existing videos = this is initial import, not a gap
            return False

        now = datetime.now(timezone.utc)
        age = now - newest_batch_published

        # Gap only if batch is stale
        return age > timedelta(days=_GAP_TRIGGER_DAYS_OLD)

    # Has overlap = normal processing
    return False


def get_pending_by_source(channel_url: str) -> list[str]:
    """Get all pending video IDs for a given source (channel_url).

    Uses batch_status.get_pending_by_source().

    Args:
        channel_url: The channel URL to query

    Returns:
        List of pending video IDs.
    """
    from csf.batch_status import get_pending_by_source

    return get_pending_by_source(channel_url)
