"""RED phase test for TASK-001: LanguageConfig + TranscriptResult dataclasses."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.transcript import LanguageConfig, TranscriptResult


def test_language_config_defaults():
    """LanguageConfig has correct default values per SEC-001."""
    cfg = LanguageConfig()
    assert cfg.prefer_lang == "en"
    assert cfg.allow_translation is False
    assert cfg.translation_provider == "gemini"


def test_language_config_custom():
    """LanguageConfig accepts custom values."""
    cfg = LanguageConfig(prefer_lang="es", allow_translation=True, translation_provider="gemini")
    assert cfg.prefer_lang == "es"
    assert cfg.allow_translation is True
    assert cfg.translation_provider == "gemini"


def test_transcript_result_fields():
    """TranscriptResult has all required fields including detected_lang."""
    result = TranscriptResult(
        video_id="dQw4w9WgXcQ",
        lang="es",
        raw_lang="es",
        was_translated=False,
        transcript="texto de prueba",
        source="youtube_transcript_api",
        detected_lang="es",
    )
    assert result.video_id == "dQw4w9WgXcQ"
    assert result.lang == "es"
    assert result.raw_lang == "es"
    assert result.was_translated is False
    assert result.transcript == "texto de prueba"
    assert result.source == "youtube_transcript_api"
    assert result.detected_lang == "es"


def test_transcript_result_detected_lang_none():
    """TranscriptResult.detected_lang can be None when language detection fails."""
    result = TranscriptResult(
        video_id="dQw4w9WgXcQ",
        lang="en",
        raw_lang=None,
        was_translated=False,
        transcript="",
        source="none",
        detected_lang=None,
    )
    assert result.detected_lang is None
