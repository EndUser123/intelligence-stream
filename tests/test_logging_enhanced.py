"""RED phase tests for enhanced logging: QueueHandler + trace_id."""

import sys
import json
import queue
from pathlib import Path
from unittest import mock

_ROOT = Path(r"P:\packages\intelligence-stream")
sys.path.insert(0, str(_ROOT))

from csf.logging import log_action, log_user_message  # noqa: E402


class TestTraceIdInJsonl:
    """Verify JSONL entries include a trace_id field for correlation."""

    def test_jsonl_entry_has_trace_id(self, tmp_path):
        """_write_jsonl_entry includes trace_id in the JSONL entry."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.logging.resolve_tid", return_value="test-tid"),
        ):
            log_action("test_action", {"key": "value"})
        log_file = tmp_path / ".logs" / "test-tid.jsonl"
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert "trace_id" in entries[0]
        assert entries[0]["trace_id"] == "test-tid"

    def test_trace_id_matches_terminal_id(self, tmp_path):
        """trace_id in JSONL matches the resolved terminal ID."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.logging.resolve_tid", return_value="console_12345"),
        ):
            log_action("action_a", {"a": 1})
        log_file = tmp_path / ".logs" / "console_12345.jsonl"
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert entries[0]["trace_id"] == "console_12345"

    def test_user_message_jsonl_has_trace_id(self, tmp_path):
        """log_user_message JSONL sink includes trace_id."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.logging.resolve_tid", return_value="console_12345"),
        ):
            log_user_message("hello world")
        log_file = tmp_path / ".logs" / "console_12345.jsonl"
        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert entries[0]["trace_id"] == "console_12345"


class TestQueueHandlerNonBlocking:
    """Verify QueueHandler + QueueListener for non-blocking async log I/O."""

    def test_queue_handler_accepts_records(self):
        """_create_queue_handler returns a Handler that accepts LogRecord objects."""
        from csf.logging import _create_queue_handler
        import logging

        q: queue.Queue[logging.LogRecord] = queue.Queue()
        handler = _create_queue_handler(q)
        record = logging.makeLogRecord({"msg": "test"})
        handler.emit(record)  # must not raise

    def test_queue_handler_places_record_in_queue(self):
        """_create_queue_handler uses put_nowait to place records in the queue."""
        from csf.logging import _create_queue_handler
        import logging

        q: queue.Queue[logging.LogRecord] = queue.Queue()
        handler = _create_queue_handler(q)
        record = logging.makeLogRecord({"msg": "test"})
        handler.emit(record)
        assert not q.empty(), "Record should be in the queue via put_nowait"

    def test_queue_listener_writes_to_file(self, tmp_path):
        """_QueueListener writes queued records to the JSONL file on stop."""
        from csf.logging import _create_queue_handler, _create_queue_listener
        import logging

        tid = "queue-listener-tid"
        log_file = tmp_path / ".logs" / f"{tid}.jsonl"

        q: queue.Queue[logging.LogRecord] = queue.Queue()
        handler = _create_queue_handler(q)
        listener = _create_queue_listener(q, log_file)
        listener.start()

        class MockRecord:
            def __init__(self):
                self.msg = "queued test"
                self.levelname = "INFO"
                self.created = 1234567890.0
                self.trace_id = tid

            def getMessage(self):
                return self.msg

        handler.emit(MockRecord())
        listener.stop()  # drain happens after stop_event is set
        assert log_file.exists(), "Listener should have written the record to file"

    def test_queue_handler_does_not_block_on_emit(self):
        """_queue_emit uses put_nowait and silently drops on queue.Full."""
        from csf.logging import _queue_emit
        import logging

        q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
        q.put("dummy")  # fill the queue

        record = logging.makeLogRecord({"msg": "test"})
        # _queue_emit should return immediately (no blocking) and silently drop
        _queue_emit(None, record, q)  # type: ignore[arg-type]
        # Record was dropped, queue still has the original dummy
        assert q.get_nowait() == "dummy"
        assert q.empty()
