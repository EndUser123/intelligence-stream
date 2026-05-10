"""YouTube source enumeration for yt-is pipeline — Phase 2.

Two-tier enumeration strategy:
- Tier 1: RSS (daily monitoring, free, stateless)
- Tier 2: YouTube Data API with publishedAfter cursor (gap resolution)

Uses YOUTUBE_API_KEY from environment for API calls.
"""

import os
import re
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# YouTube Data API endpoint
_YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class ChannelInfo(NamedTuple):
    """Full channel metadata from channels.list API response.

    Returned by get_upload_playlist_id() so callers can capture
    all available fields in a single API call.
    """
    playlist_id: str
    video_count: int
    channel_title: str
    thumbnail_url: str
    subscriber_count: int
    view_count: int
    description: str = ""
    published_at: str = ""
    country: str = ""
    keywords: str = ""      # from brandingSettings.channel.keywords
    custom_url: str = ""    # from snippet.customUrl (e.g. /@MarquesBrownlee)
    topic_categories: str = ""  # from topicDetails.topicCategories (comma-separated)

# RSS feed URL template
_RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# Channel ID pattern (UC...)
_CHANNEL_ID_PATTERN = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")


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
            # Aliases used in P:\\\\\\.env (YT_API_KEY_*, not YOUTUBE_API_KEY*)
            os.environ.get("YT_API_KEY_1"),
            os.environ.get("YT_API_KEY_2"),
            os.environ.get("YT_API_KEY_3"),
            os.environ.get("YT_API_KEY_4"),
            os.environ.get("YT_API_KEY_5"),
        ]
        _YOUTUBE_API_KEYS = [k for k in raw_keys if k]
    assert _YOUTUBE_API_KEYS is not None
    return _YOUTUBE_API_KEYS


@dataclass
class _ApiResult:
    """Structured result from an API call — tracks success, failure reason, and quota state."""

    success: bool
    response: dict | None
    key_index: int
    units_consumed: int  # estimated YouTube API units for this call
    failure_reason: str | None  # None if success, else 'quota_exceeded', 'invalid_key', 'not_found', 'network_error', 'other'


# Module-level per-key quota state
_key_state: dict[int, dict] = {}  # key_index -> {calls_made, units_consumed, exhausted, exhausted_at}
_API_UNIT_ESTIMATE_PER_CALL = 5  # conservative estimate for channels.list with 3 parts


def _init_key_state(num_keys: int) -> None:
    """Initialize quota tracking state for each key."""
    global _key_state
    for i in range(num_keys):
        if i not in _key_state:
            _key_state[i] = {"calls_made": 0, "units_consumed": 0, "exhausted": False, "exhausted_at": None}


def get_quota_status() -> dict[int, dict]:
    """Return current quota state per key index.

    Returns:
        Dict of key_index -> {calls_made, units_consumed, exhausted, exhausted_at}
    """
    keys = _get_api_keys()
    _init_key_state(len(keys))
    return dict(_key_state)


def can_proceed(units_needed: int) -> bool:
    """Check whether enough total quota remains across all keys to proceed.

    Args:
        units_needed: Estimated units required for the operation.

    Returns:
        True if at least one non-exhausted key has enough remaining quota.
    """
    keys = _get_api_keys()
    _init_key_state(len(keys))
    total_exhausted = sum(1 for i in range(len(keys)) if _key_state.get(i, {}).get("exhausted", False))
    if total_exhausted >= len(keys):
        return False  # all keys exhausted
    # Conservative: assume 10K units per key, subtract estimated consumption
    per_key_estimate = 10000
    available = sum(per_key_estimate - _key_state.get(i, {}).get("units_consumed", 0) for i in range(len(keys)))
    return available >= units_needed


def _api_request(endpoint: str, params: dict, record_quota: bool = True, unit_cost: int | None = None) -> dict | None:
    """Make a YouTube Data API request with automatic key failover and quota tracking.

    Args:
        endpoint: API endpoint (e.g., 'channels', 'playlistItems')
        params: Query parameters including 'key' for API key
        record_quota: If True, update per-key quota state on each call.
        unit_cost: Override per-call unit cost. Defaults to _API_UNIT_ESTIMATE_PER_CALL.
            Use 1 for lightweight calls (snippet only), 5 for full channel calls.

    Returns:
        JSON response dict or None on error.
    """
    keys = _get_api_keys()
    if not keys:
        return None

    if record_quota:
        _init_key_state(len(keys))

    url = f"{_YOUTUBE_API_BASE}/{endpoint}"
    last_error: str | None = None
    cost = unit_cost if unit_cost is not None else _API_UNIT_ESTIMATE_PER_CALL

    for key_idx, api_key in enumerate(keys):
        if record_quota and _key_state.get(key_idx, {}).get("exhausted", False):
            continue  # skip exhausted keys

        all_params = {**params, "key": api_key}
        query = urllib.parse.urlencode(all_params)

        try:
            req = urllib.request.Request(f"{url}?{query}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                if record_quota:
                    _key_state[key_idx]["calls_made"] += 1
                    _key_state[key_idx]["units_consumed"] += cost
                import json

                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            if e.code == 404:
                if record_quota:
                    _key_state[key_idx]["calls_made"] += 1
                    _key_state[key_idx]["units_consumed"] += cost
                return None
            if e.code == 400 and "expired" in body.lower():
                last_error = f"key expired (HTTP 400)"
                continue
            if e.code == 403 or e.code == 429:
                last_error = f"quota exceeded (HTTP {e.code})"
                if record_quota:
                    _key_state[key_idx]["exhausted"] = True
                    from datetime import datetime, timezone

                    _key_state[key_idx]["exhausted_at"] = datetime.now(timezone.utc).isoformat()
                continue
            if record_quota:
                _key_state[key_idx]["calls_made"] += 1
                _key_state[key_idx]["units_consumed"] += cost
            return None
        except Exception:
            if record_quota:
                _key_state[key_idx]["calls_made"] += 1
                _key_state[key_idx]["units_consumed"] += cost
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


def get_upload_playlist_id(channel_id: str) -> ChannelInfo | None:
    """Get the uploads playlist ID and full metadata for a channel using YouTube Data API.

    Two-tier lookup strategy (quota-efficient):
    - Tier 1: channels.list(part=snippet) — 1 unit. Validates handle and extracts
      channel title, thumbnail, and customUrl.
    - Tier 2: channels.list(part=contentDetails,statistics) — 5 units. Gets uploads
      playlist ID and video counts. Only called if Tier 1 succeeds.

    All available metadata is captured and returned as ChannelInfo.

    Args:
        channel_id: Channel identifier (UC..., @handle, c/name, user/name)

    Returns:
        ChannelInfo (playlist_id, video_count, channel_title, thumbnail_url,
        subscriber_count, view_count) or None if channel not found.
    """
    # Determine part and id-type based on channel_id format
    if channel_id.startswith("UC"):
        id_param_key = "id"
        id_param_val = channel_id
    elif channel_id.startswith("@"):
        id_param_key = "forHandle"
        id_param_val = channel_id
    elif channel_id.startswith("c/") or channel_id.startswith("user/"):
        name = channel_id.split("/", 1)[1]
        id_param_key = "forUsername"
        id_param_val = name
    else:
        return None

    # Tier 1: snippet + brandingSettings + topicDetails (1 unit total) — validates handle,
    # captures title, customUrl, thumbnail, keywords, topicCategories, and channel identity
    tier1_params = {id_param_key: id_param_val, "part": "snippet,brandingSettings,topicDetails"}
    result_t1 = _api_request("channels", tier1_params, unit_cost=1)
    if not result_t1 or "items" not in result_t1 or len(result_t1["items"]) == 0:
        return None

    try:
        item_t1 = result_t1["items"][0]
        snippet_t1 = item_t1.get("snippet", {})
        branding_t1 = item_t1.get("brandingSettings", {})
        thumbnails_t1 = snippet_t1.get("thumbnails", {})
        tier1_custom_url = snippet_t1.get("customUrl", "")
        tier1_title = snippet_t1.get("title", "")
        tier1_thumbnail = (
            thumbnails_t1.get("default", {}).get("url", "")
            or thumbnails_t1.get("medium", {}).get("url", "")
            or thumbnails_t1.get("high", {}).get("url", "")
        )
        # Extract channel identity keywords from brandingSettings (channels self-declare)
        branding_channel = branding_t1.get("channel", {})
        tier1_keywords = branding_channel.get("keywords", "") or ""
        # Extract topicCategories from topicDetails (YouTube's FreeBase topic assignments)
        topic_details = item_t1.get("topicDetails", {})
        tier1_topic_categories = ",".join(topic_details.get("topicCategories", []) or [])
        # Extract channel ID from Tier 1 response (channel IDs always start with UC)
        tier1_channel_id = item_t1.get("id", "") or snippet_t1.get("channelId", "")
    except (KeyError, ValueError, TypeError):
        return None

    # Tier 2: full metadata call (5 units) — only if Tier 1 succeeded
    tier2_params = {id_param_key: id_param_val, "part": "contentDetails,statistics"}
    result_t2 = _api_request("channels", tier2_params, unit_cost=5)
    if not result_t2 or "items" not in result_t2 or len(result_t2["items"]) == 0:
        return None

    try:
        item_t2 = result_t2["items"][0]
        playlist_id = item_t2["contentDetails"]["relatedPlaylists"]["uploads"]
        stats = item_t2.get("statistics", {})
        snippet_t2 = item_t2.get("snippet", {})

        return ChannelInfo(
            playlist_id=playlist_id,
            video_count=int(stats.get("videoCount", 0) or 0),
            channel_title=tier1_title or snippet_t2.get("title", ""),
            thumbnail_url=tier1_thumbnail,
            subscriber_count=int(stats.get("subscriberCount", 0) or 0),
            view_count=int(stats.get("viewCount", 0) or 0),
            description=snippet_t1.get("description", ""),
            published_at=snippet_t1.get("publishedAt", ""),
            country=snippet_t1.get("country", ""),
            keywords=tier1_keywords,
            custom_url=tier1_custom_url,
            topic_categories=tier1_topic_categories,
        )
    except (KeyError, ValueError, TypeError):
        return None


def get_video_count(channel_id: str) -> int | None:
    """Get the total video count for a channel using YouTube Data API.

    Uses channels.list API with statistics projection.

    Args:
        channel_id: Channel identifier (UC..., @handle, c/name, user/name)

    Returns:
        Total video count or None if not found.
    """
    # Determine part and id-type based on channel_id format
    if channel_id.startswith("UC"):
        # Direct channel ID
        params = {"part": "statistics", "id": channel_id}
    elif channel_id.startswith("@"):
        # Handle
        params = {"part": "statistics", "forHandle": channel_id}
    elif channel_id.startswith("c/") or channel_id.startswith("user/"):
        # Custom URL or user URL
        name = channel_id.split("/", 1)[1]
        params = {"part": "statistics", "forUsername": name}
    else:
        return None

    result = _api_request("channels", params)
    if not result or "items" not in result or len(result["items"]) == 0:
        return None

    try:
        return int(result["items"][0]["statistics"].get("videoCount", 0))
    except (KeyError, ValueError, TypeError):
        return None


def resolve_to_uc_channel_id(channel_identifier: str) -> str | None:
    """Resolve a channel identifier (@handle, c/custom, user/name) to UC channel ID.

    Uses YouTube Data API channels.list with id projection.

    Args:
        channel_identifier: Channel identifier (@handle, c/name, user/name, or UC...)

    Returns:
        UC channel ID (e.g., "UCxxxxxxxxxxxxxxxxxx") or None if not found.
    """
    # If already UC format, return as-is
    if channel_identifier.startswith("UC"):
        return channel_identifier

    # Determine API parameters based on format
    if channel_identifier.startswith("@"):
        # Handle
        params = {"part": "id", "forHandle": channel_identifier}
    elif channel_identifier.startswith("c/"):
        # Custom URL
        name = channel_identifier.split("/", 1)[1]
        params = {"part": "id", "forUsername": name}
    elif channel_identifier.startswith("user/"):
        # User URL
        name = channel_identifier.split("/", 1)[1]
        params = {"part": "id", "forUsername": name}
    else:
        return None

    result = _api_request("channels", params)
    if not result or "items" not in result or len(result["items"]) == 0:
        return None

    try:
        return result["items"][0]["id"]
    except (KeyError, IndexError):
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
        Tuple of (list of video dicts with id/title/publishedAt/has_captions, next_page_token or None)
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

            # Filter out truly non-playable videos
            # Keep memberOnly videos - they should be tracked as unavailable
            # Keep scheduled live streams - they'll become available later
            privacy = status.get("privacyStatus", "public")
            upload = status.get("uploadStatus", "")
            if privacy == "private":
                continue
            if upload in ("deleted", "failed"):
                continue

            # Check if video is unavailable for transcript download
            unavailable_reason = None
            if privacy == "memberOnly":
                unavailable_reason = "member_only"
            elif upload == "scheduled":
                unavailable_reason = "scheduled_live"
            elif upload == "processing":
                unavailable_reason = "processing"

            # Capture additional metadata from API
            content_details = item.get("contentDetails", {})
            live_details = status.get("liveBroadcastDetails", {})

            video = {
                "video_id": snippet["resourceId"]["videoId"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "channel_id": snippet.get("channelId", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnail": snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                "duration": content_details.get("duration", 0),  # seconds
                "has_captions": content_details.get("caption", False),
                "caption": content_details.get("caption", ""),  # caption track info
                "privacy_status": status.get("privacyStatus", "public"),
                "upload_status": status.get("uploadStatus", ""),
                "is_live_content": live_details.get("isLiveContent", False),
                "unavailable_reason": unavailable_reason,
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

    Returns exactly 15 most recent video IDs (YouTube RSS limit).

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
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015"
        }

        video_ids = []
        for entry in root.findall(".//atom:entry", ns):
            # YouTube video ID is in the yt:videoId element
            video_id_elem = entry.find("yt:videoId", ns)
            if video_id_elem is not None and video_id_elem.text:
                video_ids.append(video_id_elem.text)

        return video_ids
    except Exception:
        return []


def enumerate_full_playlist(playlist_id: str, max_videos: int = 20000) -> list[dict]:
    """Fully enumerate all videos in a playlist using nextPageToken pagination.

    Used for deep discovery fallback (Phase 2). Unlike RSS (limit 15), this
    walks the entire playlist (up to max_videos) to ensure no videos were missed
    during downtime.

    Args:
        playlist_id: Playlist ID to enumerate (usually UU... for uploads)
        max_videos: Safety cap to prevent runaway API usage (default 20,000)

    Returns:
        List of video dicts with id, title, published_at, has_captions, etc.
    """
    all_videos = []
    page_token = None

    while True:
        videos, next_token = enumerate_videos_api(playlist_id, page_token=page_token)
        if not videos:
            break

        all_videos.extend(videos)

        if not next_token or len(all_videos) >= max_videos:
            break

        page_token = next_token

    return all_videos


def detect_gap(
    rss_ids: list[str],
    all_video_ids: set[str],
    newest_batch_published: datetime | None = None,
) -> bool:
    """Detect whether a channel has a gap requiring API gap resolution.

    Gap is triggered when RSS shows videos that don't exist in local database
    AND the newest batch video is older than 7 days (stale data suggesting
    missed videos). Recent batch activity means no gap — just new uploads.

    Args:
        rss_ids: Video IDs from RSS feed (exactly 15 videos)
        all_video_ids: ALL video IDs in database for this channel (pending, complete, failed)
        newest_batch_published: Timestamp of newest video in batch, or None if unknown

    Returns:
        True if gap resolution is needed, False otherwise.
    """
    if not rss_ids:
        return False

    rss_set = set(rss_ids)
    overlap = rss_set & all_video_ids

    if overlap:
        return False

    # No overlap — check if batch is stale enough to indicate a gap
    if newest_batch_published is not None:
        age = datetime.now(timezone.utc) - newest_batch_published
        if age < timedelta(days=7):
            return False

    return True


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
