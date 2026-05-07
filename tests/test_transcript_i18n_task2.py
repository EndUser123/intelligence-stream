"""RED phase test for TASK-002: lang parameter threading + BCP-47 validation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\\packages\\yt-is").absolute()))

import pytest
from csf.transcript import (
    _fetch_via_gemini_cli,
    _fetch_via_sdk,
    _fetch_via_youtube_transcript_api,
    _fetch_via_youtubei,
    _validate_bcp47,
    fetch_transcript_chain,
    LanguageConfig,
)


class TestBCP47Validation:
    """BLOCKER-13: Invalid BCP-47 codes must raise ValueError before any API call."""

    def test_valid_two_letter_code(self):
        """Two-letter codes like 'en', 'es' are valid."""
        _validate_bcp47("en")
        _validate_bcp47("es")
        _validate_bcp47("zh")

    def test_valid_with_region(self):
        """Region subtags like 'pt-BR', 'zh-CN' are valid."""
        _validate_bcp47("pt-BR")
        _validate_bcp47("zh-CN")

    def test_invalid_code_raises(self):
        """Invalid codes raise ValueError (BLOCKER-13)."""
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("eng")  # 3 letters

    def test_invalid_region_code_raises(self):
        """Region must be uppercase; mixed-case region codes are invalid."""
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("en-us")  # lowercase region

    def test_numeric_code_raises(self):
        """Numeric codes are invalid."""
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("123")

    def test_empty_code_raises(self):
        """Empty string is invalid."""
        with pytest.raises(ValueError, match="Invalid BCP-47"):
            _validate_bcp47("")


class TestGeminiCLIThreadsLang:
    """TASK-002: gemini CLI subprocess must include --lang flag."""

    def test_cli_includes_lang_flag(self):
        """The gemini CLI command includes --lang when calling subprocess."""
        import subprocess
        from unittest import mock

        with mock.patch.object(subprocess, "Popen") as mock_popen:
            mock_popen.return_value.communicate.return_value = ("transcript text", "")
            mock_popen.return_value.returncode = 0
            _fetch_via_gemini_cli("dQw4w9WgXcQ", "es")
            # Verify --lang es was passed
            call_args = mock_popen.call_args[0][0]
            assert "--lang" in call_args, f"Expected --lang in command: {call_args}"
            lang_idx = call_args.index("--lang")
            assert call_args[lang_idx + 1] == "es"


class TestSDKThreadsLang:
    """TASK-002: SDK prompt must include language."""

    def test_sdk_prompt_includes_language(self):
        """SDK prompt includes the requested language."""
        from unittest import mock

        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.text = "transcript"
        mock_client.models.generate_content.return_value = mock_response

        # Patch os.environ BEFORE importing google.genai to avoid websockets init failure
        mock_environ = {"GEMINI_API_KEY": "fake-key"}
        with mock.patch("os.environ", mock_environ):
            with mock.patch("google.genai.Client", return_value=mock_client):
                _fetch_via_sdk("dQw4w9WgXcQ", "es")
                # Check the prompt contents include language
                call_kwargs = mock_client.models.generate_content.call_args.kwargs
                contents = call_kwargs["contents"]
                # The prompt is wrapped in some content type; extract text
                prompt_parts = []
                for c in contents:
                    if hasattr(c, "parts"):
                        for p in c.parts:
                            if hasattr(p, "text"):
                                prompt_parts.append(p.text)
                            elif isinstance(p, str):
                                prompt_parts.append(p)
                    elif hasattr(c, "text"):
                        prompt_parts.append(c.text)
                    elif isinstance(c, str):
                        prompt_parts.append(c)
                prompt_text = " ".join(str(p) for p in prompt_parts if p)
                assert "es" in prompt_text, (
                    f"Expected 'es' in prompt, got: {prompt_text}"
                )

