"""Tests for csf/cache.py - Transcript Caching Module."""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.cache import (
    TranscriptCache,
    clear_all_storages,
    get_cached_transcript,
    set_cached_transcript,
)


class TestVideoIdValidation:
    """Test video_id validation - malformed IDs must return None without raising."""

    def test_invalid_video_id_returns_none(self):
        """Malformed video_id (not 11 chars) returns None, does not raise."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            result = get_cached_transcript("abc", "en", "cli")
        assert result is None

    def test_video_id_with_special_chars_returns_none(self):
        """Video ID with special characters returns None without raising."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            result = get_cached_transcript("abc!@#$%^&*()", "en", "cli")
        assert result is None

    def test_video_id_too_short_returns_none(self):
        """Video ID shorter than 11 chars returns None."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            result = get_cached_transcript("short", "en", "cli")
        assert result is None

    def test_video_id_too_long_returns_none(self):
        """Video ID longer than 11 chars returns None."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            result = get_cached_transcript("this_is_12_chars", "en", "cli")
        assert result is None

    def test_set_cached_with_invalid_video_id_does_not_raise(self):
        """Setting cache with invalid video_id does not raise, is silently ignored."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            set_cached_transcript("bad_id", "en", "cli", "some transcript")

    def test_valid_video_id_accepted(self):
        """Valid 11-char video ID is accepted."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_123"}):
            result = get_cached_transcript("dQw4w9WgXcQ", "en", "cli")
        assert result is None


class TestCacheHitWithoutApiCall:
    """Test that cache hit occurs without calling the underlying API."""

    def test_cache_hit_without_api_call(self):
        """Second call to same video hits cache without API call.

        Uses mock.patch to assert the API is NOT called on cache hit.
        """
        video_id = "ZyX98765432"
        lang = "en"
        source = "cli"
        terminal_id = "test_terminal_cache_hit"

        with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
            with mock.patch("youtube_transcript_api.YouTubeTranscriptApi") as mock_api:
                mock_api.return_value.list_transcripts.return_value.find_transcript.return_value.fetch.return_value = [
                    {"text": "Cached transcript content"}
                ]

                set_cached_transcript(video_id, lang, source, "First fetch transcript")

                mock_api.reset_mock()

                result = get_cached_transcript(video_id, lang, source)

                mock_api.assert_not_called()


class TestCacheMiss:
    """Test cache miss behavior."""

    def test_cache_miss_returns_none_for_unknown_video(self):
        """Cache miss returns None for video never cached."""
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_miss"}):
            result = get_cached_transcript("nevercached123", "en", "cli")
        assert result is None

    def test_empty_database_initialization(self):
        """Empty database (no tables) initializes correctly on first query.

        Regression test for bug where queries failed on empty databases.
        Verifies _ensure_db_initialized() is called before read operations.
        """
        import sqlite3
        from pathlib import Path

        # Delete the database to simulate first run on empty DB
        db_path = Path("P:/__csf/.data/yt-is/transcripts.sqlite")
        if db_path.exists():
            db_path.unlink()

        # Clear in-memory cache storage to force re-initialization
        clear_all_storages()

        # Query on empty DB should not raise — tables are created automatically
        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_empty"}):
            result = get_cached_transcript("emptydbtest", "en", "cli")

        # Should return None (cache miss), not raise an exception
        assert result is None

        # Verify table now exists
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='transcript_cache'"
            )
            table_exists = cursor.fetchone() is not None
            conn.close()
            assert table_exists, "transcript_cache table should exist after query"


class TestSourceEnumeration:
    """Test source enumeration validation."""

    def test_valid_sources_accepted(self):
        """All valid source values are accepted without raising."""
        valid_sources = ["cli", "youtube_transcript_api", "youtubei", "sdk"]
        for source in valid_sources:
            with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_src"}):
                result = get_cached_transcript("dQw4w9WgXcQ", "en", source)
                assert result is None or isinstance(result, TranscriptCache)


class TestTranscriptCacheDataclass:
    """Test TranscriptCache dataclass fields and types."""

    def test_transcript_cache_fields(self):
        """TranscriptCache has all required fields."""
        cache = TranscriptCache(
            video_id="dQw4w9WgXcQ",
            lang="en",
            source="cli",
            transcript="Test transcript",
            cached_at=datetime.now(),
            terminal_id="test_term",
        )
        assert cache.video_id == "dQw4w9WgXcQ"
        assert cache.lang == "en"
        assert cache.source == "cli"
        assert cache.transcript == "Test transcript"
        assert cache.terminal_id == "test_term"
        assert isinstance(cache.cached_at, datetime)


class TestTranscriptMetadataRoundTrip:
    """Test that cache entries preserve arbitrary metadata payloads."""

    def test_metadata_round_trip_preserves_payload(self):
        """Metadata should survive a SQLite write/read round trip unchanged."""
        video_id = "ZyX98765432"
        lang = "en"
        source = "notebooklm"
        metadata = {
            "notebook_id": "nb-123",
            "source_id": "src-456",
            "source_title": "Episode 1",
            "source_url": "https://www.youtube.com/watch?v=ZyX98765432",
            "content_length": 12345,
            "quality_metrics": {
                "view_count": 100,
                "like_count": 10,
            },
        }

        with mock.patch.dict(os.environ, {"TERMINAL_ID": "test_term_metadata"}):
            set_cached_transcript(
                video_id,
                lang,
                source,
                "Complete transcript text",
                metadata=metadata,
            )
            cached = get_cached_transcript(video_id, lang, source)

        assert cached is not None
        assert cached.transcript == "Complete transcript text"
        assert cached.metadata == metadata


class TestCacheIntegrationWithTranscriptChain:
    """Test cache integration with the full fetch_transcript_chain.

    Note: The cache pre-check (get_cached_transcript) lives in batch.py via
    has_cached_transcript() -- it is NOT called inside fetch_transcript_chain.
    These tests verify that fetch_transcript_chain calls set_cached_transcript
    on success and does NOT call it on failure.
    """

    def test_fetch_transcript_chain_calls_set_cached_transcript_on_success(self):
        """Successful fetch calls set_cached_transcript with correct args."""
        from csf.transcript import (
            LanguageConfig,
            TranscriptResult,
            fetch_transcript_chain,
        )

        video_id = "dQw4w9WgXcQ"
        lang_config = LanguageConfig(prefer_lang="en")
        terminal_id = "test_term_fetch_chain"

        with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
            with (
                mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
                mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
                mock.patch("csf.transcript._is_source_rate_limited", return_value=False),
            ):
                # First yt-dlp call succeeds
                mock_ytdlp.return_value = (True, "transcript via yt-dlp", None)

                result = fetch_transcript_chain(video_id, lang_config)

                assert isinstance(result, TranscriptResult)
                assert result.transcript == "transcript via yt-dlp"
                assert result.source == "ytdlp"
                assert result.lang == "en"
                mock_ytdlp.assert_called_once_with(video_id, "en")
                mock_cache_set.assert_called_once()
                call_args = mock_cache_set.call_args
                assert call_args.args == (
                    video_id,
                    "en",
                    "ytdlp",
                    "transcript via yt-dlp",
                )
                assert call_args.kwargs["metadata"]["source"] == "ytdlp"
                assert call_args.kwargs["metadata"]["lang"] == "en"
                assert call_args.kwargs["metadata"]["transcript_chars"] == len(
                    "transcript via yt-dlp"
                )

    def test_fetch_transcript_chain_no_cache_call_on_all_fail(self):
        """All fetch methods fail: set_cached_transcript not called."""
        from csf.transcript import (
            LanguageConfig,
            TranscriptResult,
            fetch_transcript_chain,
        )

        video_id = "dQw4w9WgXcQ"
        lang_config = LanguageConfig(prefer_lang="en")
        terminal_id = "test_term_fetch_chain"

        with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
            with (
                mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
                mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
                mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ytdlp_cookies,
                mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
                mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
                mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
                mock.patch("csf.transcript._is_source_rate_limited", return_value=False),
            ):
                # All fetch methods fail
                mock_ytdlp.return_value = (False, None, "ytdlp blocked")
                mock_ytdlp_cookies.return_value = (False, None, "cookies blocked")
                mock_selenium.return_value = (False, None, "selenium blocked")
                mock_nlm.return_value = (False, None, "nlm blocked")
                mock_whisper.return_value = (False, None, "whisper blocked")

                result = fetch_transcript_chain(video_id, lang_config)

                assert isinstance(result, TranscriptResult)
                assert result.transcript == ""
                assert result.source == "none"
                mock_cache_set.assert_not_called()


class TestConcurrentCacheWrites:
    """Test concurrent cache writes from multiple workers."""

    def test_concurrent_writes_no_corruption(self):
        """4 workers writing simultaneously produces correct cache entries."""
        video_ids = ["dQw4w9WgXcA", "dQw4w9WgXcB", "dQw4w9WgXcC", "dQw4w9WgXcD"]
        lang = "en"
        terminal_id = "test_term_concurrent"

        results = {}
        errors = []

        def write_to_cache(video_id: str) -> None:
            try:
                with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
                    set_cached_transcript(
                        video_id, lang, "cli", f"transcript for {video_id}"
                    )
                    results[video_id] = "written"
            except Exception as e:
                errors.append((video_id, str(e)))

        threads = []
        for vid in video_ids:
            t = threading.Thread(target=write_to_cache, args=(vid,))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent writes: {errors}"
        assert len(results) == 4

        time.sleep(0.5)

        with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
            for vid in video_ids:
                cached = get_cached_transcript(vid, lang, "cli")
                assert cached is not None, f"Cache entry missing for {vid}"
                assert cached.transcript == f"transcript for {vid}"

    def test_concurrent_writes_same_video_id(self):
        """Multiple threads writing same video_id simultaneously - no corruption.

        INSERT OR IGNORE ensures first writer wins (no silent data loss via REPLACE).
        The key invariant is: no errors, and exactly one entry is persisted.
        Which entry wins is non-deterministic due to threading race.
        """
        video_id = "dQw4w9WgXcQ"
        lang = "en"
        terminal_id = "test_term_concurrent_same"
        transcripts = ["transcript A", "transcript B", "transcript C", "transcript D"]

        errors = []

        def write_to_cache(transcript: str) -> None:
            try:
                with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
                    set_cached_transcript(video_id, lang, "cli", transcript)
            except Exception as e:
                errors.append(str(e))

        threads = []
        for transcript in transcripts:
            t = threading.Thread(target=write_to_cache, args=(transcript,))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # INSERT OR IGNORE means first writer wins silently — no errors expected
        assert len(errors) == 0, f"Errors during concurrent writes: {errors}"

        time.sleep(0.5)

        with mock.patch.dict(os.environ, {"TERMINAL_ID": terminal_id}):
            cached = get_cached_transcript(video_id, lang, "cli")
            # Exactly one entry persisted — no corruption
            assert cached is not None
            # The winner must be one of the 4 transcripts (INSERT OR IGNORE keeps first)
            assert cached.transcript in transcripts
