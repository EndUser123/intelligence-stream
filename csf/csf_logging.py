"""Logging utilities for intelligence stream."""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from collections.abc import Callable

from csf.terminal_context import resolve_tid

_logger = logging.getLogger(__name__)

# Allowed base directories for log output (prevents path traversal)
_ALLOWED_LOG_BASES = (Path.cwd(), Path.home())

# Rich print, lazy imported to avoid hard dependency
_rich_print: Callable[..., None] | None = None


def _get_rich_print():
    """Lazily import rich.print to avoid hard dependency."""
    global _rich_print
    if _rich_print is None:
        from rich import print as _rich_impl

        _rich_print = _rich_impl
    return _rich_print


def log_user_message(
    msg: str, level: Literal["info", "warning", "error"] = "info"
) -> None:
    """Log a user-facing message to both JSONL file and console (Rich).

    Silently degrades on console errors — file logging takes priority.

    Args:
        msg: The user-facing message text.
        level: Log level — info, warning, or error.
    """
    # Always write JSONL entry (primary, reliable sink)
    _write_jsonl_entry(action="user_message", data={"msg": msg, "level": level})

    # Console sink (secondary, best-effort via Rich)
    _print_console(msg, level)


def _print_console(msg: str, level: str) -> None:
    """Print to console via Rich with level-appropriate styling.

    Silently degrades on any error to preserve the JSONL sink.
    """
    try:
        rich_fn = _get_rich_print()
        if rich_fn is None:
            return
        if level == "warning":
            rich_fn(f"[yellow]{msg}[/yellow]")
        elif level == "error":
            rich_fn(f"[red]{msg}[/red]")
        else:
            rich_fn(msg)
    except Exception:
        # Console degradation is silent — never let it affect the JSONL sink
        pass


def _write_jsonl_entry(action: str, data: dict) -> None:
    """Write a JSONL entry to the terminal-specific log file.

    Silently degrades on errors to prevent logging failures from crashing callers.
    """
    import os

    try:
        log_dir = Path(os.getenv("INTELLIGENCE_STREAM_LOG_DIR", ".logs"))
        # Resolve and validate log_dir is within allowed bases
        try:
            log_dir = log_dir.resolve()
        except (OSError, ValueError):
            _logger.warning("Invalid log directory, using default: %s", log_dir)
            log_dir = Path(".logs").resolve()

        if not any(log_dir.is_relative_to(base) for base in _ALLOWED_LOG_BASES):
            _logger.warning(
                "Log directory outside allowed bases, using default: %s", log_dir
            )
            log_dir = Path(".logs").resolve()

        log_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as e:
        _logger.warning("Could not create log directory: %s", e)
        return

    tid = resolve_tid()
    log_file = log_dir / f"{tid}.jsonl"

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": tid,
        "action": action,
        "data": data,
    }

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, PermissionError) as e:
        _logger.warning("Could not write to log file %s: %s", log_file, e)


def log_action(action: str, data: dict) -> None:
    """Log an action to the terminal-specific log file.

    Silently degrades on errors to prevent logging failures from crashing callers.
    """
    _write_jsonl_entry(action=action, data=data)


# ---------------------------------------------------------------------------
# Async log handler: QueueHandler + QueueListener for non-blocking I/O
# ---------------------------------------------------------------------------


def _create_queue_handler(log_queue: queue.Queue[logging.LogRecord]) -> logging.Handler:
    """Create a QueueHandler that places log records in a queue without blocking.

    This decouples log emission from log I/O, preventing synchronous file
    writes from blocking the calling thread (e.g. batch worker threads).

    Args:
        log_queue: A queue.Queue to receive LogRecord objects.

    Returns:
        A logging.Handler instance configured for non-blocking emit.
    """
    handler = logging.Handler()
    handler.setLevel(logging.DEBUG)
    handler.emit = lambda record: _queue_emit(handler, record, log_queue)  # type: ignore[assignment]
    return handler


def _queue_emit(
    _handler: logging.Handler,
    record: logging.LogRecord,
    log_queue: queue.Queue[logging.LogRecord],
) -> None:
    """Non-blocking emit: put record in queue using put_nowait."""
    try:
        log_queue.put_nowait(record)
    except queue.Full:
        # Never block — silently drop if queue is full
        pass


class _QueueListener:
    """Background thread that drains a queue and writes log records to a file.

    Call start() to begin background writing, stop() to drain remaining items
    and wait for the thread to finish.
    """

    def __init__(self, log_queue: queue.Queue[logging.LogRecord], log_file: Path):
        self._queue = log_queue
        self._log_file = log_file
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background listener thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal stop, drain remaining queue items, then wait for thread to finish."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _drain_queue(self) -> None:
        """Drain all remaining items from the queue and write them."""
        while True:
            try:
                record = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._write_record(record)
            except Exception:
                pass

    def _write_record(self, record: logging.LogRecord) -> None:
        """Write a single log record as JSONL to the log file."""
        msg = record.getMessage()
        # Ensure parent directory exists before writing
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError):
            pass
        with open(self._log_file, "a") as f:
            f.write(
                json.dumps(
                    {
                        "timestamp": datetime.fromtimestamp(
                            record.created, tz=timezone.utc
                        ).isoformat(),
                        "trace_id": getattr(record, "trace_id", "unknown"),
                        "action": "log",
                        "data": {"level": record.levelname, "msg": msg},
                    }
                )
                + "\n"
            )

    def _run(self) -> None:
        """Run loop: process queue items until stop event is set."""
        while not self._stop_event.is_set():
            try:
                record = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._write_record(record)
        # Drain any remaining items after stop is signaled
        self._drain_queue()


def _create_queue_listener(
    log_queue: queue.Queue[logging.LogRecord],
    log_file: Path,
) -> _QueueListener:
    """Create a QueueListener that writes queued log records to a JSONL file.

    Args:
        log_queue: Queue to drain records from.
        log_file: Path to write JSONL entries to.

    Returns:
        A _QueueListener instance. Call start() to begin, stop() to end.
    """
    return _QueueListener(log_queue, log_file)
