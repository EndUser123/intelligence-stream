"""Tests for csf/summarize.py — LLM direct summarization via Gemini CLI."""

import sys
import json
import subprocess
from pathlib import Path
from unittest import mock


# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.summarize import summarize
from csf.providers import VideoAnalysisResult


class TestSummarize:
    """Tests for summarize() Gemini CLI wrapper."""

    def test_summarize_returns_video_analysis_result(self):
        """With valid Gemini CLI response, VideoAnalysisResult is returned."""
        valid_json = {
            "title": "Test Video",
            "summary": "A test summary.",
            "key_topics": ["topic1", "topic2", "topic3", "topic4", "topic5"],
            "key_points": ["point1", "point2", "point3"],
        }

        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(valid_json)
        mock_result.stderr = ""

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", return_value=mock_result),
        ):
            result = summarize(
                transcript="Hello world",
                code_snippets=["x = 1"],
                visual_tags=["code screenshot"],
            )

        assert isinstance(result, VideoAnalysisResult)
        assert result.mode == "summarize"
        assert result.title == "Test Video"

    def test_transcript_truncation(self):
        """Transcript > 32,000 chars triggers truncation."""
        long_transcript = "a" * 50_000

        captured_prompt = None

        def capture_run(cmd, **kwargs):
            nonlocal captured_prompt
            # Capture the prompt from the call
            prompt_idx = cmd.index("-p") + 1
            captured_prompt = cmd[prompt_idx]
            mock_result = mock.Mock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps(
                {
                    "title": "Test",
                    "summary": "Summary",
                    "key_topics": ["a", "b", "c", "d", "e"],
                    "key_points": ["1", "2", "3"],
                }
            )
            mock_result.stderr = ""
            return mock_result

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", side_effect=capture_run),
        ):
            summarize(transcript=long_transcript, code_snippets=[], visual_tags=[])

        # The prompt should contain truncated transcript (near end)
        assert len(captured_prompt) < len(long_transcript)
        assert " [truncated]..." in captured_prompt

    def test_gemini_timeout_returns_partial_result(self):
        """subprocess.TimeoutExpired returns partial result with mode=transcript."""
        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("gemini", 120),
            ),
        ):
            result = summarize(transcript="Hello", code_snippets=[], visual_tags=[])

        assert isinstance(result, VideoAnalysisResult)
        assert result.mode == "transcript"
        assert "timeout" in result.fallback_reason

    def test_parse_error_returns_partial_result(self):
        """Malformed JSON from Gemini CLI returns partial result with mode=transcript."""
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = "NOT VALID JSON {{{"
        mock_result.stderr = ""

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", return_value=mock_result),
        ):
            result = summarize(transcript="Hello", code_snippets=[], visual_tags=[])

        assert isinstance(result, VideoAnalysisResult)
        assert result.mode == "transcript"
        assert "parse_error" in result.fallback_reason

    def test_prompt_has_separate_sections(self):
        """Captured prompt contains ## TRANSCRIPT, ## CODE SNIPPETS, and ## VISUAL TAGS."""
        captured_cmd = None

        def capture_run(cmd, **kwargs):
            nonlocal captured_cmd
            captured_cmd = cmd
            mock_result = mock.Mock()
            mock_result.returncode = 0
            mock_result.stdout = json.dumps(
                {
                    "title": "T",
                    "summary": "S",
                    "key_topics": ["a", "b", "c", "d", "e"],
                    "key_points": ["1", "2", "3"],
                }
            )
            mock_result.stderr = ""
            return mock_result

        with (
            mock.patch("shutil.which", return_value="/usr/bin/gemini"),
            mock.patch("subprocess.run", side_effect=capture_run),
        ):
            summarize(
                transcript="transcript text",
                code_snippets=["code1", "code2"],
                visual_tags=["tag1"],
            )

        prompt = captured_cmd[captured_cmd.index("-p") + 1]
        assert "## TRANSCRIPT" in prompt
        assert "## CODE SNIPPETS" in prompt
        assert "## VISUAL TAGS" in prompt

    def test_gemini_cli_not_found(self):
        """shutil.which returns None returns partial result."""
        with mock.patch("shutil.which", return_value=None):
            result = summarize(transcript="Hello", code_snippets=[], visual_tags=[])

        assert isinstance(result, VideoAnalysisResult)
        assert result.mode == "transcript"
        assert "gemini_cli_not_found" in result.fallback_reason
