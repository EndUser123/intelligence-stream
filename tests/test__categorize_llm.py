"""Tests for csf/_categorize_llm.py — LLM subcategorization via Gemini CLI."""

import json
import subprocess
from pathlib import Path
from unittest import mock
import sys

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\yt-is").absolute()))

from csf._categorize_llm import classify_channel, SUBCATEGORIES


class TestClassifyChannel:
    """Tests for classify_channel() Gemini CLI wrapper."""

    def test_classify_channel_returns_subcategory(self):
        """With valid Gemini CLI response, subcategory is returned."""
        # Mock Gemini response
        mock_result = mock.Mock()
        mock_result.returncode = 0
        # Gemini can return bare text for single-word responses
        mock_result.stdout = "AI Coding & Tutorials"
        mock_result.stderr = ""

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", return_value=mock_result),
        ):
            result = classify_channel(
                channel_title="Tech With Tim",
                channel_description="I'm Tim, a self-taught developer who brings you educational tech content.",
            )

        assert result == "AI Coding & Tutorials"

    def test_classify_channel_returns_reclassify(self):
        """When LLM returns 'RECLASSIFY', it is passed through."""
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = "RECLASSIFY"
        mock_result.stderr = ""

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", return_value=mock_result),
        ):
            result = classify_channel(
                channel_title="Random Vlog",
                channel_description="Daily vlogs about my life.",
            )

        assert result == "RECLASSIFY"

    def test_classify_channel_uses_stdin(self):
        """Prompt is passed via stdin (input kwarg) to avoid Windows truncation."""
        captured_cmd = None
        captured_kwargs = {}

        def capture_run(cmd, **kwargs):
            nonlocal captured_cmd, captured_kwargs
            captured_cmd = cmd
            captured_kwargs = kwargs
            mock_result = mock.Mock()
            mock_result.returncode = 0
            mock_result.stdout = "Research & Papers"
            mock_result.stderr = ""
            return mock_result

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", side_effect=capture_run),
        ):
            classify_channel(
                channel_title="Yannic Kilcher",
                channel_description="ML research papers.",
            )

        assert "input" in captured_kwargs
        assert "You are an AI/ML subcategory classifier" in captured_kwargs["input"]
        # Ensure -p is NOT in the command list (using stdin instead)
        assert "-p" not in captured_cmd
        assert "--output-format" in captured_cmd
