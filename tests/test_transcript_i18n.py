"""Integration tests for transcript language parameterization (i18n).

Covers:
- LanguageConfig defaults and field types
- TranscriptResult fields including detected_lang
- was_translated flag behavior
- Translation opt-in/opt-out
- Non-fatal translation degradation
- Import smoke test

Acceptance: All tests pass via `pytest tests/test_transcript_i18n.py`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from unittest import mock
import pytest

from csf.transcript import (
    LanguageConfig,
    TranscriptResult,
    fetch_transcript_chain,
    _translate_text,
    _validate_bcp47,
)


# =============================================================================
# Import smoke test
# =============================================================================


def test_import_smoke_test():
    """Import smoke test: LanguageConfig and TranscriptResult are importable."""
    from csf.transcript import LanguageConfig, TranscriptResult

    cfg = LanguageConfig()
    assert cfg.prefer_lang == "en"
    assert cfg.allow_translation is False
    result = TranscriptResult(
        video_id="dQw4w9WgXcQ",
        lang="en",
        raw_lang="en",
        was_translated=False,
        transcript="test",
        source="none",
        detected_lang="en",
        error=None,
    )
    assert result.video_id == "dQw4w9WgXcQ"


# =============================================================================
# LanguageConfig defaults
# =============================================================================


def test_language_config_defaults():
    """LanguageConfig has correct defaults."""
    cfg = LanguageConfig()
    assert cfg.prefer_lang == "en"
    assert cfg.allow_translation is False
    assert cfg.translation_provider == "gemini"


def test_language_config_custom():
    """LanguageConfig accepts custom values."""
    cfg = LanguageConfig(prefer_lang="es", allow_translation=True)
    assert cfg.prefer_lang == "es"
    assert cfg.allow_translation is True
    assert cfg.translation_provider == "gemini"


# =============================================================================
# TranscriptResult fields
# =============================================================================


def test_transcript_result_fields():
    """TranscriptResult has all required fields."""
    result = TranscriptResult(
        video_id="dQw4w9WgXcQ",
        lang="pt-BR",
        raw_lang="en",
        was_translated=True,
        transcript="translated texto",
        source="cli",
        detected_lang="en",
        error=None,
    )
    assert result.video_id == "dQw4w9WgXcQ"
    assert result.lang == "pt-BR"
    assert result.raw_lang == "en"
    assert result.was_translated is True
    assert result.transcript == "translated texto"
    assert result.source == "cli"
    assert result.detected_lang == "en"
    assert result.error is None


def test_transcript_result_detected_lang_none():
    """TranscriptResult detected_lang can be None on failure."""
    result = TranscriptResult(
        video_id="dQw4w9WgXcQ",
        lang="en",
        raw_lang=None,
        was_translated=False,
        transcript="",
        source="none",
        detected_lang=None,
        error="no transcript",
    )
    assert result.transcript == ""
    assert result.source == "none"
    assert result.detected_lang is None
    assert result.error == "no transcript"


# =============================================================================
# BCP-47 validation
# =============================================================================


class TestBCP47Validation:
    """BLOCKER-13: Invalid BCP-47 codes must raise ValueError before any API call."""

    def test_valid_two_letter_code(self):
        _validate_bcp47("en")
        _validate_bcp47("es")
        _validate_bcp47("zh")

    def test_valid_with_region(self):
        _validate_bcp47("pt-BR")
        _validate_bcp47("zh-CN")

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("eng")

    def test_invalid_region_code_raises(self):
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("en-us")

    def test_numeric_code_raises(self):
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("123")

    def test_empty_code_raises(self):
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("")


# =============================================================================
# was_translated flag behavior
# =============================================================================


def test_was_translated_false_when_no_translation():
    """was_translated=False when no translation performed."""
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
        mock_nlm.return_value = (True, "english transcript", None)
        mock_whisper.return_value = (True, "should not be called", None)
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en", allow_translation=True),
        )
        assert result.was_translated is False
        assert result.transcript == "english transcript"


def test_was_translated_true_when_translation_occurs():
    """was_translated=True when non-preferred language returned and allow_translation=True."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
        mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
        mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
        mock.patch(
            "csf.transcript._translate_text",
            return_value="translated to portuguese",
        ),
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "no captions")
        mock_ejs.return_value = (False, None, "no cookies")
        mock_selenium.return_value = (False, None, "selenium failed")
        # NLM called once with "en" (lang loop skipped) — returns English text,
        # then translation to pt-BR happens inside the NLM branch
        mock_nlm.return_value = (True, "english transcript text", None)
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="pt-BR", allow_translation=True),
        )
        assert result.was_translated is True
        assert result.raw_lang == "en"
        assert result.transcript == "translated to portuguese"


def test_no_translation_when_allow_translation_false():
    """Translation not called when allow_translation=False."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
        mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
        mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
        mock.patch("csf.transcript._translate_text") as mock_translate,
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "no captions")
        mock_ejs.return_value = (False, None, "no cookies")
        mock_selenium.return_value = (False, None, "selenium failed")
        mock_nlm.return_value = (True, "texto espanol", None)
        mock_whisper.return_value = (True, "should not be called", None)
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en", allow_translation=False),
        )
        assert result.was_translated is False
        mock_translate.assert_not_called()


# =============================================================================
# Non-fatal translation degradation
# =============================================================================


def test_translate_text_non_fatal_on_failure():
    """Translation failure returns original text (non-fatal per FM-003)."""
    mock_client = mock.MagicMock()
    mock_client.models.generate_content.side_effect = Exception("Gemini API error")

    with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
        with mock.patch("google.genai.Client", return_value=mock_client):
            result = _translate_text("texto original", "es", "en", "gemini")
            assert result == "texto original"


def test_translate_text_success():
    """_translate_text returns translated string on success."""
    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.text = "texto traducido"
    mock_client.models.generate_content.return_value = mock_response

    with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
        with mock.patch("google.genai.Client", return_value=mock_client):
            result = _translate_text("texto original", "es", "en", "gemini")
            assert result == "texto traducido"


# =============================================================================
# Graceful degradation
# =============================================================================


def test_all_methods_fail_returns_empty_transcript():
    """When all methods fail, returns TranscriptResult with empty transcript."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
        mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
        mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "ytdlp failed")
        mock_ejs.return_value = (False, None, "ejs failed")
        mock_selenium.return_value = (False, None, "selenium failed")
        mock_nlm.return_value = (False, None, "nlm failed")
        mock_whisper.return_value = (False, None, "whisper failed")
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en"),
        )
        assert isinstance(result, TranscriptResult)
        assert result.transcript == ""
        assert result.source == "none"


# =============================================================================
# Return type
# =============================================================================


def test_returns_transcript_result_type():
    """fetch_transcript_chain returns TranscriptResult (not 3-tuple)."""
    with (
        mock.patch("csf.transcript._fetch_via_ytdlp") as mock_ytdlp,
        mock.patch("csf.transcript._fetch_via_ytdlp_with_cookies") as mock_ejs,
        mock.patch("csf.transcript._fetch_via_selenium_firefox") as mock_selenium,
        mock.patch("csf.transcript._fetch_via_notebooklm") as mock_nlm,
        mock.patch("csf.transcript._fetch_via_whisper") as mock_whisper,
        mock.patch("time.sleep"),
    ):
        mock_ytdlp.return_value = (False, None, "ytdlp failed")
        mock_ejs.return_value = (False, None, "ejs failed")
        mock_selenium.return_value = (False, None, "selenium failed")
        mock_nlm.return_value = (True, "english transcript", None)
        mock_whisper.return_value = (True, "should not be called", None)
        result = fetch_transcript_chain(
            "dQw4w9WgXcQ",
            LanguageConfig(prefer_lang="en"),
        )
        assert isinstance(result, TranscriptResult)
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.transcript == "english transcript"
        assert result.was_translated is False
        assert result.lang == "en"
        assert result.raw_lang == "en"
