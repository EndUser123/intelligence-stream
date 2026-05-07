"""Tests for csf/transcript.py - Full Fallback Chain.

Current chain: oEmbed → ytdlp → ytdlp_ejs → direct_api → notebooklm → selenium → whisper
"""
from __future__ import annotations

import subprocess
import sys
import urllib.error
import time as time_module
from pathlib import Path
from unittest import mock

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\\packages\\yt-is").absolute()))

from csf.transcript import LanguageConfig, TranscriptResult, fetch_transcript_chain


class TestVideoIdValidation:
    """Test video_id validation - malformed IDs must return empty TranscriptResult."""

    def test_invalid_video_id_returns_empty_result(self):
        result = fetch_transcript_chain("abc", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""
        assert result.source == "none"

    def test_video_id_with_special_chars_returns_empty_result(self):
        result = fetch_transcript_chain("abc!@#$%^&*()", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_video_id_too_short_returns_empty_result(self):
        result = fetch_transcript_chain("short", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_video_id_too_long_returns_empty_result(self):
        result = fetch_transcript_chain("this_is_12_chars", LanguageConfig())
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""

    def test_valid_video_id_accepted(self):
        """Valid 11-char video ID is accepted and fetch is attempted."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            # ytdlp succeeds (free source wins)
            mock_ytdlp.return_value = (True, "transcript text", None)
            mock_ejs.return_value = (True, "should not be called", None)
            mock_selenium.return_value = (True, "should not be called", None)
            mock_nlm.return_value = (True, "should not be called", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert isinstance(result, TranscriptResult)
            assert result.transcript == "transcript text"
            assert result.source == "ytdlp"


class TestFallbackChain:
    """Test the fallback chain order: ytdlp → ytdlp_ejs → direct_api → notebooklm → selenium → whisper.

    Free sources (ytdlp) are tried before paid/notebooklm to conserve resources.
    """

    def test_oembed_unavailable_short_circuits_chain(self, monkeypatch):
        """oEmbed 404 should stop the chain before expensive transcript probes."""
        monkeypatch.setenv("YTIS_OEMBED_ENABLED", "1")
        http_error = urllib.error.HTTPError(
            "https://www.youtube.com/oembed",
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )
        with (
            mock.patch("csf.transcript.urllib.request.urlopen", side_effect=http_error),
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._get_scheduler") as mock_scheduler,
            mock.patch("csf.transcript.log_action") as mock_log,
        ):
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_not_called()
            mock_ejs.assert_not_called()
            mock_nlm.assert_not_called()
            mock_selenium.assert_not_called()
            mock_whisper.assert_not_called()
            mock_direct.assert_not_called()
            mock_scheduler.return_value.archive_finalize.assert_called_once()
            assert result.transcript == ""
            assert result.source == "none"
            assert result.last_stage == "oembed"
            assert result.failure_reason == "unavailable"
            oembed_events = [
                call.args[1]
                for call in mock_log.call_args_list
                if call.args[0] == "transcript_oembed_checked"
            ]
            assert oembed_events
            assert oembed_events[0]["ok"] is False
            failure_events = [
                call.args[1]
                for call in mock_log.call_args_list
                if call.args[0] == "transcript_chain_failed"
            ]
            assert failure_events
            assert failure_events[0]["last_stage"] == "oembed"
            assert failure_events[0]["failure_reason"] == "unavailable"

    def test_direct_api_terminal_failure_short_circuits_later_stages(self):
        """direct_api unavailable should stop before Selenium/Whisper."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._get_scheduler") as mock_scheduler,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_direct.return_value = (False, None, "direct_api unavailable: removed by uploader")
            mock_nlm.return_value = (False, None, "should not be called")
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_nlm.assert_not_called()
            mock_selenium.assert_not_called()
            mock_whisper.assert_not_called()
            mock_scheduler.return_value.archive_finalize.assert_called_once()
            assert result.transcript == ""
            assert result.last_stage == "direct_api"
            assert result.failure_reason == "unavailable"

    def test_all_methods_fail_emits_final_stage_summary(self):
        """When the full chain fails, emit a compact final-stage summary."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._get_scheduler") as mock_scheduler,
            mock.patch("csf.transcript.log_action") as mock_log,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_whisper.return_value = (False, None, "whisper failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="en"),
                skip_notebooklm=True,
            )

            mock_scheduler.return_value.archive_finalize.assert_called_once()
            assert result.transcript == ""
            assert result.last_stage == "whisper"
            failure_events = [
                call.args[1]
                for call in mock_log.call_args_list
                if call.args[0] == "transcript_chain_failed"
            ]
            assert failure_events
            assert failure_events[-1]["last_stage"] == "whisper"
            assert failure_events[-1]["failure_reason"] == "unknown"

    def test_ytdlp_fails_ytdlp_ejs_succeeds(self):
        """ytdlp fails, ytdlp_ejs succeeds as first fallback."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (True, "transcript via ytdlp_ejs", None)
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            # Later methods should NOT be called
            mock_selenium.return_value = (True, "should not be called", None)
            mock_nlm.return_value = (True, "should not be called", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_selenium.assert_not_called()
            mock_nlm.assert_not_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "transcript via ytdlp_ejs"
            assert result.source == "ytdlp_ejs"

    def test_ytdlp_ejs_fails_notebooklm_succeeds(self):
        """ytdlp and ytdlp_ejs fail, notebooklm succeeds (now 3rd in chain)."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_nlm.return_value = (True, "transcript via notebooklm", None)
            mock_selenium.return_value = (True, "should not be called", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_nlm.assert_called()
            mock_selenium.assert_not_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "transcript via notebooklm"
            assert result.source == "notebooklm"

    def test_notebooklm_fails_selenium_succeeds(self):
        """ytdlp, ytdlp_ejs, notebooklm fail, selenium succeeds (now 4th in chain)."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_selenium.return_value = (True, "transcript via selenium", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_nlm.assert_called()
            mock_selenium.assert_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "transcript via selenium"
            assert result.source == "selenium"

    def test_all_methods_fail_returns_empty_result(self):
        """When all methods fail, returns TranscriptResult with empty transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            assert result.transcript == ""
            assert result.source == "none"
            assert result.last_stage == "whisper"

    def test_ytdlp_succeeds_free_source_wins(self):
        """ytdlp (free) succeeds — must be returned before later methods."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            # ytdlp (free) succeeds
            mock_ytdlp.return_value = (True, "free transcript via ytdlp", None)
            mock_ejs.return_value = (True, "should not be called", None)
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (True, "should not be called", None)
            mock_nlm.return_value = (True, "should not be called", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_not_called()
            mock_selenium.assert_not_called()
            mock_nlm.assert_not_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "free transcript via ytdlp"
            assert result.source == "ytdlp"

    def test_whisper_called_as_last_resort(self):
        """whisper is called only after all caption methods fail."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "transcript via whisper", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_selenium.assert_called()
            mock_nlm.assert_called()
            mock_whisper.assert_called()
            assert result.transcript == "transcript via whisper"
            assert result.source == "whisper"


class TestCacheIntegration:
    """Test cache integration - set_cached_transcript called after successful fetch."""

    def test_result_cached_after_successful_fetch(self):
        """After successful fetch, set_cached_transcript is called with correct args."""
        with (
            mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            # All caption methods fail, whisper succeeds
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "whisper transcript", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_cache_set.assert_called_once()
            call_args = mock_cache_set.call_args
            assert call_args.args[0] == "dQw4w9WgXcQ"  # video_id
            assert call_args.args[1] == "en"  # lang
            assert call_args.args[2] == "whisper"  # source
            assert call_args.args[3] == "whisper transcript"  # transcript
            metadata = call_args.kwargs["metadata"]
            assert metadata["source"] == "whisper"
            assert metadata["lang"] == "en"
            assert metadata["transcript_chars"] == len("whisper transcript")
            assert result.transcript == "whisper transcript"
            assert result.source == "whisper"

    def test_translated_result_is_cached_after_successful_fetch(self):
        """When translation is enabled, the cached transcript should be the translated text."""
        with (
            mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._translate_text") as mock_translate,
            mock.patch("time.sleep"),
        ):
            # First language attempt fails, second succeeds, then translation applies.
            mock_ytdlp.side_effect = [
                (False, None, "no captions"),
                (True, "hello world", None),
            ]
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")
            mock_translate.return_value = "hola mundo"

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="es", allow_translation=True),
            )

            mock_cache_set.assert_called_once()
            call_args = mock_cache_set.call_args
            assert call_args.args[0] == "dQw4w9WgXcQ"
            assert call_args.args[1] == "es"
            assert call_args.args[2] == "ytdlp"
            assert call_args.args[3] == "hola mundo"
            metadata = call_args.kwargs["metadata"]
            assert metadata["source"] == "ytdlp"
            assert metadata["lang"] == "es"
            assert metadata["was_translated"] is True
            assert metadata["transcript_chars"] == len("hola mundo")
            assert result.transcript == "hola mundo"
            assert result.was_translated is True
            assert result.raw_lang == "en"
            assert result.source == "ytdlp"

    def test_unknown_language_fallback_does_not_pretend_to_be_english(self):
        """The lang=None fallback should preserve unknown language metadata and skip translation."""
        with (
            mock.patch("csf.transcript.set_cached_transcript") as mock_cache_set,
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._translate_text") as mock_translate,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.side_effect = [
                (False, None, "no captions"),
                (False, None, "no captions"),
                (True, "bonjour monde", None),
            ]
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="fr", allow_translation=True),
            )

            mock_translate.assert_not_called()
            mock_cache_set.assert_called_once()
            call_args = mock_cache_set.call_args
            assert call_args.args[0] == "dQw4w9WgXcQ"
            assert call_args.args[1] == "fr"
            assert call_args.args[2] == "ytdlp"
            assert call_args.args[3] == "bonjour monde"
            metadata = call_args.kwargs["metadata"]
            assert metadata["source"] == "ytdlp"
            assert metadata["lang"] == "fr"
            assert metadata["raw_lang"] is None
            assert metadata["was_translated"] is False
            assert result.transcript == "bonjour monde"
            assert result.was_translated is False
            assert result.raw_lang is None
            assert result.detected_lang is None
            assert result.source == "ytdlp"


class TestWhisperEmptyClassification:
    """Tests for Whisper empty-result classification and messaging."""

    def test_whisper_empty_result_mentions_likely_music_or_silence(self):
        from csf.transcript import _summarize_whisper_empty_result

        class DummySegment:
            def __init__(self, no_speech_prob: float, text: str = "") -> None:
                self.no_speech_prob = no_speech_prob
                self.text = text

        message = _summarize_whisper_empty_result(
            [DummySegment(0.94), DummySegment(0.91)]
        )
        assert "likely music or silence" in message
        assert "max_no_speech_prob=0.94" in message

    def test_whisper_music_hint_is_treated_as_no_transcript(self):
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (
                False,
                None,
                "whisper no speech detected (likely music or silence; segments=2, max_no_speech_prob=0.94)",
            )

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            assert result.transcript == ""
            assert result.source == "none"
            assert result.last_stage == "whisper"
            assert result.failure_reason == "no_transcript"
            assert "likely music or silence" in result.error

    def test_whisper_music_hint_sets_negative_cache(self):
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._set_negative_cache") as mock_negative_cache,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (
                False,
                None,
                "whisper no speech detected (likely music or silence; segments=2, max_no_speech_prob=0.94)",
            )

            result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

            assert result.transcript == ""
            assert result.failure_reason == "no_transcript"
            mock_negative_cache.assert_called_once()
            args, kwargs = mock_negative_cache.call_args
            assert args[0] == "dQw4w9WgXcQ"
            assert args[1] == "no_transcript"
            assert kwargs["last_stage"] == "whisper"


class TestWhisperAdmission:
    """Tests for pre-Whisper admission filtering."""

    def test_whisper_skips_obvious_music_title_without_calling_whisper(self):
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="en"),
                admission_metadata={
                    "title": "Official Audio",
                    "description": "track",
                    "duration": 10,
                },
            )

            assert mock_whisper.call_count == 0
            assert result.source == "none"
            assert result.last_stage == "whisper_admission"
            assert result.failure_reason == "no_transcript"

    def test_short_speech_like_clip_still_reaches_whisper(self):
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "short spoken transcript", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="en"),
                admission_metadata={
                    "title": "Quick Interview",
                    "description": "a tiny spoken clip",
                    "duration": 10,
                },
            )

            assert mock_whisper.call_count == 1
            assert result.transcript == "short spoken transcript"
            assert result.source == "whisper"

    def test_live_item_skips_whisper_as_terminal(self):
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ",
                LanguageConfig(prefer_lang="en"),
                admission_metadata={
                    "title": "Live Stream Replay",
                    "description": "watch live",
                    "upload_status": "live_stream",
                    "is_live_content": True,
                },
            )

            assert mock_whisper.call_count == 0
            assert result.source == "none"
            assert result.last_stage == "whisper_admission"
            assert result.failure_reason == "unavailable"


class TestJitter:
    """Test random jitter for rate limit avoidance."""

    def test_jitter_in_range(self):
        """Jitter should be between 2.0 and 10.0 seconds (PERF-006: wider range)."""
        jitters = []
        for _ in range(20):
            with (
                mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
                mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
                mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
                mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
                mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
                mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
                mock.patch("time.sleep") as mock_sleep,
            ):
                mock_ytdlp.return_value = (False, None, "no captions")
                mock_ejs.return_value = (False, None, "no cookies")
                mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
                mock_selenium.return_value = (False, None, "selenium failed")
                mock_nlm.return_value = (False, None, "nlm failed")
                mock_whisper.return_value = (True, "transcript", None)

                fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))

                for call in mock_sleep.call_args_list:
                    jitters.append(call[0][0])

        assert len(jitters) > 0, "No jitter was applied"
        for jitter in jitters:
            assert 2.0 <= jitter <= 10.0, f"Jitter {jitter} out of range [2.0, 10.0]"


class TestReturnType:
    """Test that return type is always TranscriptResult."""

    def test_returns_transcript_result(self):
        """Result is a TranscriptResult."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "transcript", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert isinstance(result, TranscriptResult)

    def test_success_returns_transcript(self):
        """On success, TranscriptResult contains transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "whisper text", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.transcript == "whisper text"
            assert result.source == "whisper"

    def test_all_fail_returns_empty_result(self):
        """On failure, TranscriptResult has empty transcript."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_direct.return_value = (False, None, "direct_api no_transcript: subtitles disabled")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.transcript == ""
            assert result.source == "none"


class TestFetchViaNotebooklmBatch:
    """Unit tests for _fetch_via_notebooklm_batch error paths.

    _fetch_via_notebooklm_batch delegates to nlm_batch.process_industrial_batch,
    which creates an NLMBatchIngestor, calls create_batch_notebook → extract_transcripts → cleanup.
    We mock NLMBatchIngestor methods to test each failure path.
    """

    def _mock_ingestor(self, create_fn=None, extract_fn=None):
        """Create a mock NLMBatchIngestor with configurable methods."""
        ingestor = mock.MagicMock()
        ingestor.create_batch_notebook = create_fn or mock.MagicMock(return_value="nb-123")
        ingestor.extract_transcripts = extract_fn or mock.MagicMock(return_value={})
        ingestor.cleanup = mock.MagicMock()
        return ingestor

    def test_auth_failure_returns_auth_failed(self):
        """When _ensure_nlm_auth returns False, NLMBatchIngestor still gets created but fails."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value=None),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1", "vid2"])
        assert result["vid1"][0] is False
        assert result["vid2"][0] is False

    def test_notebook_create_failure_returns_create_failed(self):
        """When notebook create fails, all videos get 'Notebook failed' error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value=None),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "Notebook failed" in result["vid1"][2]

    def test_parse_notebook_id_failure_returns_parse_failed(self):
        """When notebook create returns empty string, all videos get Notebook failed."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value=""),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "Notebook failed" in result["vid1"][2]

    def test_source_add_failure_returns_add_source_failed(self):
        """When notebook created but no transcripts extracted, videos get failure."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"vid1": (False, None, "Fetch failed for s1")}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False

    def test_source_list_failure_returns_list_failed(self):
        """When extract_transcripts returns List failed error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"vid1": (False, None, "List failed")}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "List failed" in result["vid1"][2]

    def test_json_parse_failure_returns_parse_error(self):
        """When extract_transcripts returns Parse failed error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"vid1": (False, None, "Parse failed")}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "Parse failed" in result["vid1"][2]

    def test_content_threshold_short_fails(self):
        """Content below 100 chars is discarded by NLMBatchIngestor (min threshold)."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"dQw4w9WgXcQ": (False, None, "Fetch failed for s1")}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is False

    def test_content_above_threshold_succeeds(self):
        """Content above 100 chars is accepted by NLMBatchIngestor."""
        from csf.transcript import _fetch_via_notebooklm_batch

        long_text = "x" * 200
        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"dQw4w9WgXcQ": (True, long_text, None)}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is True
        assert result["dQw4w9WgXcQ"][1] == long_text

    def test_batch_processes_all_passed_videos(self):
        """All videos passed to process_industrial_batch are returned."""
        from csf.transcript import _fetch_via_notebooklm_batch

        vids = [f"video{i:011d}" for i in range(10)]

        fail_results = {vid: (False, None, "Notebook failed") for vid in vids}
        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value=None),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(vids)
        # All 10 videos should get failure results
        assert len(result) == 10
        for vid in vids:
            assert result[vid][0] is False

    def test_empty_video_list_returns_empty_dict(self):
        """Empty video_ids list returns empty result dict."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor()
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch([])
        assert result == {}

    def test_successful_end_to_end(self):
        """Full happy path: create → extract → cleanup."""
        from csf.transcript import _fetch_via_notebooklm_batch

        transcript = "x" * 200
        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value={"dQw4w9WgXcQ": (True, transcript, None)}),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is True
        assert result["dQw4w9WgXcQ"][1] == transcript
        assert result["dQw4w9WgXcQ"][2] is None
        ingestor.cleanup.assert_called_once()

    def test_notebook_create_returns_false(self):
        """When create_batch_notebook returns falsy, all videos get Notebook failed."""
        from csf.transcript import _fetch_via_notebooklm_batch

        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value=""),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "Notebook failed" in result["vid1"][2]

    def test_multi_video_batch(self):
        """Multiple videos are passed through to extract_transcripts."""
        from csf.transcript import _fetch_via_notebooklm_batch

        vids = ["dQw4w9WgXcQ", "dQw4w9WgXcR", "dQw4w9WgXcS"]
        extract_results = {vid: (True, "x" * 200, None) for vid in vids}
        ingestor = self._mock_ingestor(
            create_fn=mock.MagicMock(return_value="nb-123"),
            extract_fn=mock.MagicMock(return_value=extract_results),
        )
        with mock.patch("csf.nlm_batch.NLMBatchIngestor", return_value=ingestor):
            result = _fetch_via_notebooklm_batch(vids)

        for vid in vids:
            assert result[vid][0] is True, f"{vid} failed: {result[vid]}"
        ingestor.cleanup.assert_called_once()


class TestNLMConfig:
    """Tests for NLMConfig singleton."""

    def test_nlm_config_default_values(self):
        """NLMConfig has correct defaults when no env var set."""
        # Reset singleton for clean test state
        import csf.transcript
        from csf import nlm_config
        nlm_config.reset_nlm_config()
        try:
            config = csf.transcript.get_nlm_config()
            assert config.max_sources_per_notebook == 300
            assert config.auth_check_interval == 60.0
            assert config.auth_max_calls_per_window == 10
            assert config.auth_cooldown == 300.0
        finally:
            nlm_config.reset_nlm_config()

    def test_nlm_config_env_fallback(self, monkeypatch):
        """YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK env var is used as fallback."""
        import csf.transcript
        from csf import nlm_config
        nlm_config.reset_nlm_config()
        monkeypatch.setenv("YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK", "50")
        try:
            config = csf.transcript.get_nlm_config()
            assert config.max_sources_per_notebook == 50
        finally:
            nlm_config.reset_nlm_config()

    def test_nlm_config_override(self):
        """set_nlm_config overrides the singleton for testing."""
        import csf.transcript
        from csf import nlm_config
        nlm_config.reset_nlm_config()
        try:
            new_config = csf.transcript.NLMConfig(
                max_sources_per_notebook=100,
                auth_check_interval=30.0,
                auth_max_calls_per_window=5,
                auth_cooldown=60.0,
            )
            csf.transcript.set_nlm_config(new_config)
            config = csf.transcript.get_nlm_config()
            assert config.max_sources_per_notebook == 100
            assert config.auth_check_interval == 30.0
            assert config.auth_max_calls_per_window == 5
            assert config.auth_cooldown == 60.0
        finally:
            nlm_config.reset_nlm_config()


class TestTranscriptSourceStage:
    """Tests for source_stage versioning."""

    def test_source_stage_populated_for_ytdlp(self):
        """ytdlp success returns source_stage=1."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies"),
            mock.patch("csf.transcript._fetch_via_selenium_firefox"),
            mock.patch("csf.transcript._fetch_via_notebooklm"),
            mock.patch("csf.transcript._fetch_via_whisper"),
            mock.patch("csf.transcript._fetch_via_direct_api"),
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.source_stage == 1
            assert result.source == "ytdlp"

    def test_source_stage_populated_for_notebooklm(self):
        """notebooklm success returns source_stage=1."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper"),
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("time.sleep"),
        ):
            # All Google-adjacent sources fail; notebooklm succeeds
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_direct.return_value = (False, None, "direct_api failed")
            mock_nlm.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.source_stage == 1
            assert result.source == "notebooklm"

    def test_source_stage_populated_for_direct_api(self):
        """direct_api success returns source_stage=2."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("time.sleep"),
        ):
            # All earlier sources fail; direct_api succeeds
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")
            mock_direct.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.source_stage == 2
            assert result.source == "direct_api"

    def test_source_stage_none_for_whisper(self):
        """whisper success returns source_stage=None (stage version not assigned)."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_direct.return_value = (False, None, "direct_api failed")
            mock_whisper.return_value = (True, "transcript text", None)
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.source_stage is None
            assert result.source == "whisper"

    def test_source_stage_none_on_failure(self):
        """All-methods-fail returns source_stage=None."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("csf.transcript._fetch_via_direct_api") as mock_direct,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")
            mock_direct.return_value = (False, None, "direct_api failed")
            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )
            assert result.source_stage is None
            assert result.source == "none"


class TestAuthRateLimiter:
    """Tests for AuthRateLimiter."""

    def test_auth_rate_limiter_blocks_after_threshold(self):
        """is_allowed() returns False after auth_max_calls_per_window exceeded."""
        import csf.transcript
        csf.transcript._auth_rate_limiter = None  # reset singleton
        try:
            csf.transcript.set_nlm_config(
                csf.transcript.NLMConfig(
                    max_sources_per_notebook=300,
                    auth_check_interval=60.0,
                    auth_max_calls_per_window=3,
                    auth_cooldown=300.0,
                )
            )
            limiter = csf.transcript._get_auth_rate_limiter()
            # First 3 calls should be allowed
            assert limiter.is_allowed() is True
            limiter.record_call()
            assert limiter.is_allowed() is True
            limiter.record_call()
            assert limiter.is_allowed() is True
            limiter.record_call()
            # 4th call should be blocked
            assert limiter.is_allowed() is False
        finally:
            csf.transcript._auth_rate_limiter = None

    def test_auth_rate_limiter_cooldown_trigger(self):
        """3 consecutive auth failures trigger cooldown."""
        import csf.transcript
        csf.transcript._auth_rate_limiter = None
        try:
            csf.transcript.set_nlm_config(
                csf.transcript.NLMConfig(
                    max_sources_per_notebook=300,
                    auth_check_interval=60.0,
                    auth_max_calls_per_window=10,
                    auth_cooldown=300.0,
                )
            )
            limiter = csf.transcript._get_auth_rate_limiter()
            # Simulate 3 auth failures
            limiter.record_auth_failure()
            assert limiter._consecutive_failures == 1
            limiter.record_auth_failure()
            assert limiter._consecutive_failures == 2
            limiter.record_auth_failure()
            assert limiter._consecutive_failures == 3
            # Should now be in cooldown
            assert limiter._is_in_cooldown() is True
            # is_allowed should also return False during cooldown
            assert limiter.is_allowed() is False
        finally:
            csf.transcript._auth_rate_limiter = None

    def test_auth_rate_limiter_success_resets_failures(self):
        """Successful --force login resets consecutive failure counter."""
        import csf.transcript
        csf.transcript._auth_rate_limiter = None
        try:
            limiter = csf.transcript._get_auth_rate_limiter()
            limiter.record_auth_failure()
            limiter.record_auth_failure()
            assert limiter._consecutive_failures == 2
            limiter.record_auth_success()
            assert limiter._consecutive_failures == 0
        finally:
            csf.transcript._auth_rate_limiter = None

    def test_auth_rate_limiter_remaining_returns_count(self):
        """remaining() returns the number of calls left in the current window."""
        import csf.transcript
        csf.transcript._auth_rate_limiter = None
        try:
            csf.transcript.set_nlm_config(
                csf.transcript.NLMConfig(
                    max_sources_per_notebook=300,
                    auth_check_interval=60.0,
                    auth_max_calls_per_window=3,
                    auth_cooldown=300.0,
                )
            )
            limiter = csf.transcript._get_auth_rate_limiter()
            assert limiter.remaining() == 3
            limiter.record_call()
            assert limiter.remaining() == 2
            limiter.record_call()
            assert limiter.remaining() == 1
            limiter.record_call()
            assert limiter.remaining() == 0
            # Next is_allowed should be False
            assert limiter.is_allowed() is False
        finally:
            csf.transcript._auth_rate_limiter = None


class TestCookieFreshnessTracker:
    """Tests for CookieFreshnessTracker."""

    def test_cookie_freshness_ttl_fast_path(self):
        """is_fresh() returns True within TTL without calling probe."""
        import csf.transcript
        csf.transcript._cookie_freshness_tracker = None
        try:
            tracker = csf.transcript._get_cookie_freshness_tracker()
            tracker._last_check = time_module.monotonic()  # just set it to now
            # No probe should be called since TTL not expired
            with mock.patch("subprocess.run") as mock_run:
                result = tracker.is_fresh()
                assert result is True
                mock_run.assert_not_called()
        finally:
            csf.transcript._cookie_freshness_tracker = None

    def test_cookie_freshness_probe_on_expired_ttl(self):
        """is_fresh() calls nlm login --check when TTL expired."""
        import csf.transcript
        csf.transcript._cookie_freshness_tracker = None
        try:
            tracker = csf.transcript._get_cookie_freshness_tracker()
            tracker._last_check = 0.0  # TTL expired
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=0)
                result = tracker.is_fresh()
                assert result is True
                mock_run.assert_called_once()
                call_args = mock_run.call_args[0][0]
                assert call_args == ["nlm", "login", "--check"]
        finally:
            csf.transcript._cookie_freshness_tracker = None

    def test_cookie_freshness_invalidate(self):
        """invalidate() sets _last_check to 0.0 forcing re-auth."""
        import csf.transcript
        csf.transcript._cookie_freshness_tracker = None
        try:
            tracker = csf.transcript._get_cookie_freshness_tracker()
            tracker._last_check = time_module.monotonic()
            tracker.invalidate()
            assert tracker._last_check == 0.0
        finally:
            csf.transcript._cookie_freshness_tracker = None

    def test_cookie_freshness_probe_failure_invalidates(self):
        """Probe failure calls invalidate() and returns False."""
        import csf.transcript
        csf.transcript._cookie_freshness_tracker = None
        try:
            tracker = csf.transcript._get_cookie_freshness_tracker()
            tracker._last_check = 0.0
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(returncode=1)
                result = tracker.is_fresh()
                assert result is False
                assert tracker._last_check == 0.0
        finally:
            csf.transcript._cookie_freshness_tracker = None


class TestNlmAuthLogging:
    """_ensure_nlm_auth should emit explicit auth-state markers."""

    def test_auth_check_logs_ok(self):
        """A clean --check result should log an auth-ok marker."""
        import csf.transcript

        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "Auth valid")

        with mock.patch("subprocess.run", side_effect=mock_run):
            with mock.patch("csf.transcript.log_action") as mock_log:
                assert csf.transcript._ensure_nlm_auth() is True

        mock_log.assert_called_once()
        assert mock_log.call_args.args[0] == "nlm_auth_checked"
        assert mock_log.call_args.args[1]["component"] == "transcript"

    def test_auth_refresh_logs_refreshed(self):
        """A refresh path should log an auth-refreshed marker."""
        import csf.transcript

        def mock_run(cmd, **kwargs):
            if cmd == ["nlm", "login", "--check"]:
                return subprocess.CompletedProcess(cmd, 1, "", "Auth expired")
            if cmd == ["nlm", "login"]:
                return subprocess.CompletedProcess(cmd, 0, "", "OK")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("subprocess.run", side_effect=mock_run):
            with mock.patch("csf.transcript.log_action") as mock_log:
                assert csf.transcript._ensure_nlm_auth() is True

        assert [c.args[0] for c in mock_log.call_args_list] == [
            "nlm_login_started",
            "nlm_login_completed",
            "nlm_auth_refreshed",
        ]

    def test_auth_refresh_uses_profile_env(self):
        """Profile-aware auth refresh should stay on the active NotebookLM profile."""
        import csf.transcript

        calls: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd == ["nlm", "login", "--check", "--profile", "ytis-pro-worker-01"]:
                return mock.MagicMock(returncode=1, stdout="", stderr="Auth expired")
            if cmd == ["nlm", "login", "--force", "--profile", "ytis-pro-worker-01"]:
                return mock.MagicMock(returncode=0, stdout="", stderr="OK")
            return mock.MagicMock(returncode=0, stdout="", stderr="")

        with mock.patch.dict(csf.transcript.os.environ, {"NOTEBOOKLM_PROFILE": "ytis-pro-worker-01"}):
            with mock.patch("subprocess.run", side_effect=mock_run):
                with mock.patch("csf.transcript.log_action"):
                    assert csf.transcript._ensure_nlm_auth() is True

        assert calls[:2] == [
            ["nlm", "login", "--check", "--profile", "ytis-pro-worker-01"],
            ["nlm", "login", "--force", "--profile", "ytis-pro-worker-01"],
        ]


class TestDirectApiFallback:
    """Tests for _fetch_via_direct_api fallback."""

    def test_direct_api_import_error_returns_no_transcript(self):
        """youtube_transcript_api unavailable returns (False, None, 'no_transcript')."""
        from csf.transcript import _fetch_via_direct_api
        with mock.patch("builtins.__import__") as mock_import:
            mock_import.side_effect = ImportError("No module named 'youtube_transcript_api'")
            success, transcript, error = _fetch_via_direct_api("dQw4w9WgXcQ")
            assert success is False
            assert transcript is None
            assert error == "no_transcript"

    def test_direct_api_success_returns_transcript(self):
        """direct_api returns (True, transcript, None) on success."""
        import sys
        # Must be >= 21 chars (strictly greater than _NLM_MIN_CONTENT_CHARS=20)
        mock_transcript = mock.Mock()
        mock_transcript.language_code = "en"
        mock_transcript.is_generated = False
        mock_transcript.fetch.return_value = [{"text": "This is a valid transcript with sufficient length"}]

        mock_api = mock.Mock()
        mock_api.fetch.return_value.fetch.return_value = [
            {"text": "This is a valid transcript with sufficient length"}
        ]

        mock_ytapi = mock.Mock()
        mock_ytapi.YouTubeTranscriptApi.return_value = mock_api

        # Pre-load the mock into sys.modules before importing
        sys.modules["youtube_transcript_api"] = mock_ytapi
        from csf.transcript import _fetch_via_direct_api
        success, transcript, error = _fetch_via_direct_api("dQw4w9WgXcQ")
        assert success is True
        assert transcript == "This is a valid transcript with sufficient length"
        assert error is None

    def test_direct_api_failure_is_summarized(self):
        """direct_api failure text should be concise and reason-coded."""
        import sys

        mock_api = mock.Mock()
        mock_api.fetch.side_effect = Exception(
            "Could not retrieve a transcript for the video https://www.youtube.com/watch?v=dQw4w9WgXcQ! "
            "Subtitles are disabled for this video"
        )

        mock_ytapi = mock.Mock()
        mock_ytapi.YouTubeTranscriptApi.return_value = mock_api

        sys.modules["youtube_transcript_api"] = mock_ytapi
        try:
            from csf.transcript import _fetch_via_direct_api

            success, transcript, error = _fetch_via_direct_api("dQw4w9WgXcQ")
            assert success is False
            assert transcript is None
            assert error == "direct_api no_transcript: subtitles disabled"
        finally:
            sys.modules.pop("youtube_transcript_api", None)


class TestWhisperFallback:
    """Tests for _fetch_via_whisper fallback."""

    def test_whisper_retries_broader_audio_formats_when_first_selector_fails(self):
        """Whisper should retry with broader audio selectors and a JS runtime before giving up."""
        from pathlib import Path
        import sys
        import tempfile as pytemp

        from csf.transcript import _fetch_via_whisper

        calls: list[list[str]] = []

        def mock_run(cmd, **kwargs):
            calls.append(list(cmd))
            output_base = cmd[cmd.index("--output") + 1]
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    "",
                    "Requested format is not available",
                )

            Path(f"{output_base}.mp3").write_text("fake audio")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with pytemp.TemporaryDirectory() as tmp_dir:
            with (
                mock.patch("tempfile.mkdtemp", return_value=tmp_dir),
                mock.patch("csf.transcript.get_browser_cookies", return_value=[]),
                mock.patch("subprocess.run", side_effect=mock_run),
            ):
                fake_segment = mock.Mock()
                fake_segment.text = "hello from whisper"
                fake_segment.no_speech_prob = 0.01
                fake_model = mock.Mock()
                fake_model.transcribe.return_value = ([fake_segment], None)

                sys.modules["faster_whisper"] = mock.Mock(WhisperModel=mock.Mock(return_value=fake_model))
                try:
                    success, transcript, error = _fetch_via_whisper("dQw4w9WgXcQ", "en")
                finally:
                    sys.modules.pop("faster_whisper", None)

        assert success is True
        assert transcript == "hello from whisper"
        assert error is None
        assert len(calls) == 2
        assert "--js-runtimes" in calls[0]
        assert calls[0][calls[0].index("--js-runtimes") + 1] == "node"
        assert calls[0][calls[0].index("-f") + 1] == "bestaudio/best"
        assert calls[1][calls[1].index("-f") + 1] == "bestaudio"

