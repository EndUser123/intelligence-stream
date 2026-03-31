"""RED phase test for TASK-004: fetch_transcript_chain returns TranscriptResult."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from unittest import mock

from csf.transcript import TranscriptResult, fetch_transcript_chain, LanguageConfig


def test_returns_transcript_result_type():
    """fetch_transcript_chain returns TranscriptResult (not 3-tuple)."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch(
            "csf.transcript._fetch_via_youtube_transcript_api",
            return_value=(True, "english transcript", None),
        ),
        mock.patch("csf.transcript._fetch_via_youtubei", return_value=(False, None, "fail")),
        mock.patch("csf.transcript._fetch_via_sdk", return_value=(False, None, "fail")),
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "no captions")
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain("dQw4w9WgXcQ", LanguageConfig(prefer_lang="en"))
        assert isinstance(result, TranscriptResult), (
            f"Expected TranscriptResult, got {type(result)}"
        )
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.transcript == "english transcript"
        assert result.was_translated is False
        assert result.lang == "en"
        assert result.raw_lang == "en"


def test_translation_triggered_when_any_lang_fallback_and_allow_translation():
    """When any-language fallback returns non-English and allow_translation=True, translate."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        # prefer_lang 'pt-BR' fails; any-lang 'en' returns Spanish text
        mock.patch(
            "csf.transcript._fetch_via_youtube_transcript_api",
            side_effect=[
                (False, None, "no pt-BR transcript"),  # prefer_lang fails
                (True, "texto espanol", None),  # any-lang returns Spanish
            ],
        ),
        mock.patch(
            "csf.transcript._fetch_via_youtubei",
            return_value=(False, None, "unavailable"),
        ),
        mock.patch(
            "csf.transcript._fetch_via_sdk",
            return_value=(False, None, "unavailable"),
        ),
        mock.patch(
            "csf.transcript._translate_text",
            return_value="translated to portuguese",
        ),
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "no captions")
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="pt-BR", allow_translation=True),
        )
        assert isinstance(result, TranscriptResult)
        assert result.was_translated is True
        assert result.raw_lang == "en"  # any-lang was 'en'
        assert result.transcript == "translated to portuguese"


def test_no_translation_when_allow_translation_false():
    """When non-preferred language returned but allow_translation=False, no translation."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch(
            "csf.transcript._fetch_via_youtube_transcript_api",
            return_value=(True, "texto espanol", None),
        ),
        mock.patch("csf.transcript._fetch_via_youtubei", return_value=(False, None, "fail")),
        mock.patch("csf.transcript._fetch_via_sdk", return_value=(False, None, "fail")),
        mock.patch("csf.transcript._translate_text") as mock_translate,
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "no captions")
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en", allow_translation=False),
        )
        assert isinstance(result, TranscriptResult)
        assert result.was_translated is False
        mock_translate.assert_not_called()


def test_all_methods_fail_returns_empty_transcript():
    """When all methods fail, returns TranscriptResult with empty transcript."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch(
            "csf.transcript._fetch_via_youtube_transcript_api",
            return_value=(False, None, "no transcript"),
        ),
        mock.patch(
            "csf.transcript._fetch_via_youtubei",
            return_value=(False, None, "no transcript"),
        ),
        mock.patch(
            "csf.transcript._fetch_via_sdk",
            return_value=(False, None, "no transcript"),
        ),
        mock.patch(
            "csf.transcript._fetch_via_gemini_cli",
            return_value=(False, None, "no transcript"),
        ),
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("csf.transcript.is_free_only_mode", return_value=False),
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "ytdlp failed")
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en"),
        )
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""
        assert result.source == "none"
