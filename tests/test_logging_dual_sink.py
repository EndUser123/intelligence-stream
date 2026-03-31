"""Tests for dual-sink logging (file JSONL + Rich console output).

RED Phase: Tests written BEFORE implementation.
"""

import sys
import json
from pathlib import Path
from unittest import mock

import pytest

_ROOT = Path(r"P:\packages\intelligence-stream")
sys.path.insert(0, str(_ROOT))

from csf.logging import log_user_message


@pytest.fixture(autouse=True)
def _reset_rich_cache():
    """Reset _rich_print cache so mocks work in every test."""
    import csf.logging
    csf.logging._rich_print = None
    yield
    csf.logging._rich_print = None


class TestLogUserMessageDualSink:
    """Verify log_user_message emits to both file (JSONL) and console (Rich)."""

    def test_emits_jsonl_entry(self, tmp_path):
        """JSONL entry written to .logs/{tid}.jsonl with type=user_message."""
        with (
            mock.patch("csf.logging.resolve_tid", return_value="test-tid-123"),
            mock.patch.dict("os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}),
            mock.patch("csf.logging._ALLOWED_LOG_BASES", (tmp_path,)),
            mock.patch("csf.logging._get_rich_print"),
        ):
            log_user_message("Hello world")

        log_file = tmp_path / ".logs" / "test-tid-123.jsonl"
        assert log_file.exists(), "JSONL file should be created"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "user_message"
        assert entry["data"]["msg"] == "Hello world"
        assert entry["data"]["level"] == "info"

    def test_emits_rich_console_output(self):
        """Rich.print called with styled output for user-facing message."""
        import csf.logging
        csf.logging._rich_print = None  # ensure fresh import
        with (
            mock.patch("csf.logging.resolve_tid", return_value="test-tid-console"),
            mock.patch("csf.logging._get_rich_print") as mock_get_rich,
        ):
            mock_get_rich.return_value = mock.Mock()
            log_user_message("Processing video dQw4w9WgXcQ")

        mock_get_rich.return_value.assert_called_once()

    def test_jsonl_and_console_independent(self, tmp_path):
        """If console fails, JSONL still writes (silent degradation)."""
        import csf.logging
        csf.logging._rich_print = None
        with (
            mock.patch("csf.logging.resolve_tid", return_value="test-tid-indep"),
            mock.patch.dict("os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}),
            mock.patch("csf.logging._ALLOWED_LOG_BASES", (tmp_path,)),
            mock.patch("csf.logging._get_rich_print", side_effect=RuntimeError("Rich unavailable")),
        ):
            log_user_message("Should still log to file")

        log_file = tmp_path / ".logs" / "test-tid-indep.jsonl"
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["data"]["msg"] == "Should still log to file"

    def test_default_level_is_info(self, tmp_path):
        """Default log level is info when not specified."""
        import csf.logging
        csf.logging._rich_print = None
        with (
            mock.patch("csf.logging.resolve_tid", return_value="test-tid-level"),
            mock.patch.dict("os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}),
            mock.patch("csf.logging._ALLOWED_LOG_BASES", (tmp_path,)),
            mock.patch("csf.logging._get_rich_print"),
        ):
            log_user_message("Test message")

        log_file = tmp_path / ".logs" / "test-tid-level.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["data"]["level"] == "info"

    def test_warning_level(self, tmp_path):
        """Warning level maps to appropriate Rich styling."""
        import csf.logging
        csf.logging._rich_print = None
        with (
            mock.patch("csf.logging.resolve_tid", return_value="test-tid-warn"),
            mock.patch.dict("os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}),
            mock.patch("csf.logging._ALLOWED_LOG_BASES", (tmp_path,)),
            mock.patch("csf.logging._get_rich_print") as mock_get_rich,
        ):
            mock_get_rich.return_value = mock.Mock()
            log_user_message("Warning: quota low", level="warning")

        log_file = tmp_path / ".logs" / "test-tid-warn.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["data"]["level"] == "warning"
        assert mock_get_rich.return_value.called
