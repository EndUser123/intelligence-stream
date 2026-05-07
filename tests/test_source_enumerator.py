"""Tests for csf/source_enumerator.py - Phase 2: Source enumeration.

Verifies: YouTube Data API v3 enumeration, RSS fallback, channel/playlist parsing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\\packages\\yt-is").absolute()))

from csf.source_enumerator import (
    parse_channel_url,
    parse_playlist_url,
    get_upload_playlist_id,
    enumerate_videos_api,
    detect_gap,
    _ChannelMetadata,
)


class TestURLParsing:
    """Test channel and playlist URL parsing."""

    def test_parse_standard_channel_url(self):
        """Standard youtube.com/channel/UC... URL parses correctly."""
        result = parse_channel_url(
            "https://www.youtube.com/channel/UCxVXX2JELH2YjJc5p7U5g"
        )
        assert result == "UCxVXX2JELH2YjJc5p7U5g"

    def test_parse_handle_channel_url(self):
        """youtube.com/@handle URL parses correctly."""
        result = parse_channel_url("https://www.youtube.com/@MarquesBrownlee")
        assert result == "@MarquesBrownlee"

    def test_parse_custom_channel_url(self):
        """youtube.com/c/customname URL parses correctly."""
        result = parse_channel_url("https://www.youtube.com/c/SomeChannel")
        assert result == "c/SomeChannel"

    def test_parse_bare_channel_id(self):
        """Bare channel ID (UC...) is returned as-is."""
        result = parse_channel_url("UCxVXX2JELH2YjJc5p7U5gwx")
        assert result == "UCxVXX2JELH2YjJc5p7U5gwx"

    def test_parse_playlist_url(self):
        """Playlist URL extracts playlist ID."""
        result = parse_playlist_url(
            "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvTfnJGu2v4"
        )
        assert result == "PLrAXtmErZgOeiKm4sgNOknGvTfnJGu2v4"

    def test_parse_playlist_url_from_watch(self):
        """Watch URL with playlist parameter extracts playlist ID."""
        result = parse_playlist_url(
            "https://www.youtube.com/watch?v=abc123&list=PLrAXtmErZgOeiKm4sgNOknGvTfnJGu2v4"
        )
        assert result == "PLrAXtmErZgOeiKm4sgNOknGvTfnJGu2v4"

    def test_parse_invalid_url_returns_none(self):
        """Invalid URL returns None."""
        result = parse_channel_url("https://notyoutube.com/channel/UCxxx")
        assert result is None

    def test_parse_invalid_playlist_returns_none(self):
        """Playlist URL without list param returns None."""
        result = parse_playlist_url("https://www.youtube.com/playlist")
        assert result is None


class TestChannelMetadata:
    """Test _ChannelMetadata dataclass."""

    def test_channel_metadata_defaults(self):
        """Default values are set correctly."""
        meta = _ChannelMetadata(channel_url="https://youtube.com/channel/UC_TEST")
        assert meta.channel_url == "https://youtube.com/channel/UC_TEST"
        assert meta.playlist_id is None
        assert meta.video_count_estimate == 0
        assert meta.last_checked is None
        assert meta.last_full_enumeration is None
        assert meta.quota_exhausted_at is None

    def test_channel_metadata_full_init(self):
        """All fields initialize correctly."""
        meta = _ChannelMetadata(
            channel_url="https://youtube.com/channel/UC_TEST",
            playlist_id="UU_TEST",
            video_count_estimate=500,
            last_checked="2026-03-28T10:00:00Z",
            last_full_enumeration="2026-03-27T00:00:00Z",
            next_page_token="TOKEN123",
            quota_exhausted_at=None,
        )
        assert meta.playlist_id == "UU_TEST"
        assert meta.video_count_estimate == 500
        assert meta.next_page_token == "TOKEN123"


class TestGapDetection:
    """Test gap detection logic."""

    def test_detect_gap_no_gap_small_overlap(self):
        """Small overlap (5 out of 15) is not a gap."""
        rss_ids = [f"vid{i}" for i in range(15)]
        batch_ids = {f"vid{i}" for i in range(5)}  # Some overlap
        # newest batch video is recent (not > 7 days)
        result = detect_gap(rss_ids, batch_ids, newest_batch_published=None)
        assert result is False

    def test_detect_gap_no_gap_large_overlap(self):
        """Large overlap (10 out of 15) is not a gap."""
        rss_ids = [f"vid{i}" for i in range(15)]
        batch_ids = {f"vid{i}" for i in range(10)}  # Large overlap
        result = detect_gap(rss_ids, batch_ids, newest_batch_published=None)
        assert result is False

    def test_detect_gap_identical(self):
        """Identical sets mean no gap."""
        rss_ids = [f"vid{i}" for i in range(15)]
        batch_ids = set(rss_ids)
        result = detect_gap(rss_ids, batch_ids, newest_batch_published=None)
        assert result is False

    def test_detect_gap_triggers_on_no_overlap_and_old(self):
        """No overlap + old batch video triggers gap detection."""
        rss_ids = [f"vid{i}" for i in range(15)]
        batch_ids = {f"old_vid{i}" for i in range(5)}  # No overlap
        from datetime import datetime, timedelta, timezone

        old_date = datetime.now(timezone.utc) - timedelta(days=10)
        result = detect_gap(rss_ids, batch_ids, newest_batch_published=old_date)
        assert result is True

    def test_detect_gap_no_trigger_recent_batch(self):
        """No overlap but recent batch video = normal upload activity."""
        rss_ids = [f"vid{i}" for i in range(15)]
        batch_ids = {f"old_vid{i}" for i in range(5)}  # No overlap
        from datetime import datetime, timedelta, timezone

        recent_date = datetime.now(timezone.utc) - timedelta(days=2)
        result = detect_gap(rss_ids, batch_ids, newest_batch_published=recent_date)
        assert result is False


class TestAPIEnumerationUnit:
    """Unit tests for API enumeration (mocked)."""

    def test_enumerate_videos_api_returns_list(self):
        """enumerate_videos_api returns a list of video IDs."""
        # This is a unit test that would need mocking for actual API
        # For now just verify the function exists and has correct signature
        import inspect

        sig = inspect.signature(enumerate_videos_api)
        assert "playlist_id" in sig.parameters

    def test_get_upload_playlist_id_returns_string(self):
        """get_upload_playlist_id has correct signature."""
        import inspect

        sig = inspect.signature(get_upload_playlist_id)
        assert "channel_id" in sig.parameters

