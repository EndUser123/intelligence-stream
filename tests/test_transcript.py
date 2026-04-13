"""Tests for csf/transcript.py - Full Fallback Chain.

Current chain: ytdlp → ytdlp_ejs → selenium → notebooklm → whisper
"""
from __future__ import annotations

import subprocess
import sys
import time as time_module
from pathlib import Path
from unittest import mock

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

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
    """Test the fallback chain order: ytdlp → ytdlp_ejs → selenium → notebooklm → whisper.

    Free sources (ytdlp) are tried before paid/notebooklm to conserve resources.
    """

    def test_ytdlp_fails_ytdlp_ejs_succeeds(self):
        """ytdlp fails, ytdlp_ejs succeeds as first fallback."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (True, "transcript via ytdlp_ejs", None)
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

    def test_ytdlp_ejs_fails_selenium_succeeds(self):
        """ytdlp and ytdlp_ejs fail, selenium succeeds."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_selenium.return_value = (True, "transcript via selenium", None)
            mock_nlm.return_value = (True, "should not be called", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_selenium.assert_called()
            mock_nlm.assert_not_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "transcript via selenium"
            assert result.source == "selenium"

    def test_selenium_fails_notebooklm_succeeds(self):
        """ytdlp, ytdlp_ejs, selenium fail, notebooklm succeeds."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (True, "transcript via notebooklm", None)
            mock_whisper.return_value = (True, "should not be called", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_ytdlp.assert_called()
            mock_ejs.assert_called()
            mock_selenium.assert_called()
            mock_nlm.assert_called()
            mock_whisper.assert_not_called()
            assert result.transcript == "transcript via notebooklm"
            assert result.source == "notebooklm"

    def test_all_methods_fail_returns_empty_result(self):
        """When all methods fail, returns TranscriptResult with empty transcript."""
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
            mock_ejs.return_value = (False, None, "no cookies")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (False, None, "whisper failed")
            mock_direct.return_value = (False, None, "direct_api failed")

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            assert result.transcript == ""
            assert result.source == "none"
            assert result.last_stage == "direct_api"  # direct_api is last stage reached

    def test_ytdlp_succeeds_free_source_wins(self):
        """ytdlp (free) succeeds — must be returned before later methods."""
        with (
            mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
            mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            # ytdlp (free) succeeds
            mock_ytdlp.return_value = (True, "free transcript via ytdlp", None)
            mock_ejs.return_value = (True, "should not be called", None)
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
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
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
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            # All caption methods fail, whisper succeeds
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
            mock_whisper.return_value = (True, "whisper transcript", None)

            result = fetch_transcript_chain(
                "dQw4w9WgXcQ", LanguageConfig(prefer_lang="en")
            )

            mock_cache_set.assert_called_once()
            call_args = mock_cache_set.call_args[0]
            assert call_args[0] == "dQw4w9WgXcQ"  # video_id
            assert call_args[1] == "en"  # lang
            assert call_args[2] == "whisper"  # source
            assert call_args[3] == "whisper transcript"  # transcript
            assert result.transcript == "whisper transcript"
            assert result.source == "whisper"


class TestJitter:
    """Test random jitter for rate limit avoidance."""

    def test_jitter_in_range(self):
        """Jitter should be between 2.0 and 10.0 seconds (PERF-006: wider range)."""
        jitters = []
        for _ in range(50):
            with (
                mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
                mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
                mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
                mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
                mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
                mock.patch("time.sleep") as mock_sleep,
            ):
                mock_ytdlp.return_value = (False, None, "no captions")
                mock_ejs.return_value = (False, None, "no cookies")
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
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
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
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
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
            mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
            mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
            mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no cookies")
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

    subprocess.call sequence per test: (create, add, list, [content×N], delete)
    The finally block ALWAYS runs delete, so every test needs a delete mock.
    """

    def _mock_run(self, stdout="", returncode=0):
        """Create a mock subprocess.run result."""
        m = mock.MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_auth_failure_returns_auth_failed(self):
        """When _ensure_nlm_auth returns False, no subprocess calls are made."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with mock.patch("csf.transcript._ensure_nlm_auth", return_value=False):
            result = _fetch_via_notebooklm_batch(["vid1", "vid2"])
        assert result["vid1"] == (False, None, "nlm auth failed")
        assert result["vid2"] == (False, None, "nlm auth failed")

    def test_notebook_create_failure_returns_create_failed(self):
        """When notebook create fails, all videos get create error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("error", returncode=1),  # create fails
                self._mock_run("", returncode=0),  # delete (finally block always runs)
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "create failed" in result["vid1"][2]

    def test_parse_notebook_id_failure_returns_parse_failed(self):
        """When notebook ID cannot be parsed, all videos get parse error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            # create succeeds but output has no "ID:" line; delete runs in finally
            mock_run.side_effect = [
                self._mock_run("notebook created\n", returncode=0),  # create
                self._mock_run("", returncode=0),  # delete
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "parse notebook ID failed" in result["vid1"][2]

    def test_source_add_failure_returns_add_source_failed(self):
        """When source add fails, all videos get add-source error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=1),  # add fails → early return
                self._mock_run("", returncode=0),  # delete (finally block)
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "add source failed" in result["vid1"][2]

    def test_source_list_failure_returns_list_failed(self):
        """When source list fails, all videos get list error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add succeeds
                self._mock_run("", returncode=1),  # list fails → early return
                self._mock_run("", returncode=0),  # delete (finally block)
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "source list failed" in result["vid1"][2]

    def test_json_parse_failure_returns_parse_error(self):
        """When source list returns malformed JSON, all videos get parse error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add succeeds
                self._mock_run("not valid json", returncode=0),  # list succeeds but bad JSON
                self._mock_run("", returncode=0),  # delete (finally block)
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "source list parse failed" in result["vid1"][2]

    def test_content_threshold_20_chars_fails(self):
        """Content with 20 chars is discarded (below minimum of 21)."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add succeeds
                self._mock_run(
                    '{"sources":[{"id":"s1","url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}]}',
                    returncode=0,
                ),  # list
                self._mock_run("x" * 20, returncode=0),  # content: 20 chars → fails (< 21)
                self._mock_run("", returncode=0),  # delete (finally block)
            ]
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is False
        assert "source content empty" in result["dQw4w9WgXcQ"][2]

    def test_content_threshold_21_chars_succeeds(self):
        """Content with 21 chars is accepted (minimum threshold is 21)."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add succeeds
                self._mock_run(
                    '{"sources":[{"id":"s1","url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}]}',
                    returncode=0,
                ),  # list
                self._mock_run("x" * 21, returncode=0),  # content: 21 chars → succeeds
                self._mock_run("", returncode=0),  # delete (finally block)
            ]
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is True
        assert result["dQw4w9WgXcQ"][1] == "x" * 21

    def test_300_video_cap(self):
        """When more than 300 videos are passed, only first 300 are processed."""
        from csf.transcript import _fetch_via_notebooklm_batch

        vids = [f"video{i:011d}" for i in range(400)]

        # _ensure_nlm_auth returns False for all → returns auth errors for all batch_ids.
        # The cap is applied BEFORE _ensure_nlm_auth is called, so if we pass 400
        # vids and auth fails, we get 400 auth errors. To test the cap, we let
        # auth succeed and fail at the notebook-create step instead.
        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            # create succeeds, add/list fail → only auth + create are called
            mock_run.side_effect = [
                mock.MagicMock(returncode=1, stdout="", stderr="create failed"),  # create fails
                mock.MagicMock(returncode=0, stdout="", stderr=""),  # delete
            ]
            result = _fetch_via_notebooklm_batch(vids)
        # Should have exactly 300 results (the cap)
        assert len(result) == 300
        # 301st video should not be present
        assert f"video{300:011d}" not in result

    def test_empty_video_list_returns_empty_dict(self):
        """Empty video_ids list returns empty result dict."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with mock.patch("csf.transcript._ensure_nlm_auth", return_value=True):
            result = _fetch_via_notebooklm_batch([])
        assert result == {}

    def test_successful_end_to_end(self):
        """Full happy path: auth → create → add → list → content → delete."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add succeeds
                self._mock_run(
                    '{"sources":[{"id":"s1","url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","title":"Test"}]}',
                    returncode=0,
                ),  # list
                self._mock_run("this is a valid transcript content", returncode=0),  # content
                self._mock_run("", returncode=0),  # delete
            ]
            result = _fetch_via_notebooklm_batch(["dQw4w9WgXcQ"])
        assert result["dQw4w9WgXcQ"][0] is True
        assert result["dQw4w9WgXcQ"][1] == "this is a valid transcript content"
        assert result["dQw4w9WgXcQ"][2] is None

    def test_parse_notebook_id_returns_none(self):
        """When _parse_notebook_id returns None, all videos get parse error."""
        from csf.transcript import _fetch_via_notebooklm_batch

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                self._mock_run("created successfully", returncode=0),  # create — no ID line
                self._mock_run("", returncode=0),  # delete
            ]
            result = _fetch_via_notebooklm_batch(["vid1"])
        assert result["vid1"][0] is False
        assert "parse notebook ID failed" in result["vid1"][2]

    def test_parallel_content_fetch(self):
        """Content fetching uses ThreadPoolExecutor (verify concurrent calls)."""
        from csf.transcript import _fetch_via_notebooklm_batch
        import json
        import threading

        call_times: list[float] = []
        call_lock = threading.Lock()

        original_run = subprocess.run

        def tracking_run(*args, **kwargs):
            with call_lock:
                call_times.append(time_module.time())
            time_module.sleep(0.05)
            return original_run(*args, **kwargs)

        with (
            mock.patch("csf.transcript._ensure_nlm_auth", return_value=True),
            mock.patch("subprocess.run", side_effect=tracking_run),
        ):
            # Use properly-formatted 11-char YouTube video IDs in URLs
            # so _extract_video_id_from_url can match them
            sources = [
                {"id": "s1", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                {"id": "s2", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcR"},
                {"id": "s3", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcS"},
            ]
            list_json = json.dumps({"sources": sources})
            # create + add + list + content×3 + delete = 7 calls
            call_sequence = [
                self._mock_run("notebook created\nNotebook ID: nb-123", returncode=0),  # create
                self._mock_run("", returncode=0),  # add
                self._mock_run(list_json, returncode=0),  # list
                self._mock_run("valid transcript content here", returncode=0),  # content 1
                self._mock_run("valid transcript content here", returncode=0),  # content 2
                self._mock_run("valid transcript content here", returncode=0),  # content 3
                self._mock_run("", returncode=0),  # delete
            ]
            subprocess.run.side_effect = call_sequence
            result = _fetch_via_notebooklm_batch(
                ["dQw4w9WgXcQ", "dQw4w9WgXcR", "dQw4w9WgXcS"]
            )

        # All 3 should succeed
        for vid in ["dQw4w9WgXcQ", "dQw4w9WgXcR", "dQw4w9WgXcS"]:
            assert result[vid][0] is True, f"{vid} failed: {result[vid]}"

        # Verify concurrent execution — content calls (indices 3,4,5) should
        # all start within <0.04s of each other with ThreadPoolExecutor,
        # vs ~0.05s apart if sequential
        if len(call_times) >= 6:
            content_times = call_times[3:6]
            time_span = max(content_times) - min(content_times)
            assert time_span < 0.04, (
                f"Content fetch appears sequential: time span={time_span:.3f}s. "
                f"Expected parallel < 0.04s."
            )


class TestNLMConfig:
    """Tests for NLMConfig singleton."""

    def test_nlm_config_default_values(self):
        """NLMConfig has correct defaults when no env var set."""
        # Reset singleton for clean test state
        import csf.transcript
        csf.transcript._nlm_config = None
        try:
            config = csf.transcript.get_nlm_config()
            assert config.max_sources_per_notebook == 300
            assert config.auth_check_interval == 60.0
            assert config.auth_max_calls_per_window == 10
            assert config.auth_cooldown == 300.0
        finally:
            csf.transcript._nlm_config = None

    def test_nlm_config_env_fallback(self, monkeypatch):
        """YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK env var is used as fallback."""
        import csf.transcript
        csf.transcript._nlm_config = None
        monkeypatch.setenv("YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK", "50")
        try:
            config = csf.transcript.get_nlm_config()
            assert config.max_sources_per_notebook == 50
        finally:
            csf.transcript._nlm_config = None

    def test_nlm_config_override(self):
        """set_nlm_config overrides the singleton for testing."""
        import csf.transcript
        csf.transcript._nlm_config = None
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
            csf.transcript._nlm_config = None


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
            mock.patch("csf.transcript._fetch_via_direct_api"),
            mock.patch("time.sleep"),
        ):
            # All Google-adjacent sources fail; notebooklm succeeds
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
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
            mock.patch("csf.transcript._fetch_via_direct_api"),
            mock.patch("time.sleep"),
        ):
            mock_ytdlp.return_value = (False, None, "no captions")
            mock_ejs.return_value = (False, None, "no captions")
            mock_selenium.return_value = (False, None, "selenium failed")
            mock_nlm.return_value = (False, None, "nlm failed")
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
        mock_transcript = mock.Mock()
        mock_transcript.language_code = "en"
        mock_transcript.is_generated = False
        mock_transcript.fetch.return_value = [{"text": "Hello world"}]

        mock_api = mock.Mock()
        mock_api.list_transcripts.return_value = [mock_transcript]

        with (
            mock.patch("youtube_transcript_api.YouTubeTranscriptApi", return_value=mock_api),
        ):
            from csf.transcript import _fetch_via_direct_api
            success, transcript, error = _fetch_via_direct_api("dQw4w9WgXcQ")
            assert success is True
            assert transcript == "Hello world"
            assert error is None
