"""RED phase test for TASK-003: _translate_text() via Gemini SDK."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from unittest import mock


def test_translate_text_returns_translated_string():
    """_translate_text returns translated string on success."""
    import os

    from csf.transcript import _translate_text

    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.text = "texto traducido"
    mock_client.models.generate_content.return_value = mock_response

    with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
        with mock.patch("google.genai.Client", return_value=mock_client):
            result = _translate_text("texto original", "es", "en", "gemini")
            assert result == "texto traducido"
            # Verify language was in prompt
            call_args = mock_client.models.generate_content.call_args
            prompt = str(call_args)
            assert "es" in prompt and "en" in prompt


def test_translate_text_non_fatal_on_failure():
    """Translation failure returns original text (non-fatal per FM-003)."""
    import os

    from csf.transcript import _translate_text

    mock_client = mock.MagicMock()
    mock_client.models.generate_content.side_effect = Exception("Gemini API error")

    with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
        with mock.patch("google.genai.Client", return_value=mock_client):
            result = _translate_text("texto original", "es", "en", "gemini")
            assert result == "texto original"
