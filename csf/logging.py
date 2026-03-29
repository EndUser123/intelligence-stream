"""Logging utilities for intelligence stream."""

from __future__ import annotations

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from .terminal_context import resolve_tid

_logger = logging.getLogger(__name__)

# Allowed base directories for log output (prevents path traversal)
_ALLOWED_LOG_BASES = (Path.cwd(), Path.home())


def log_action(action: str, data: dict) -> None:
    """Log an action to the terminal-specific log file.

    Silently degrades on errors to prevent logging failures from crashing callers.
    """
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
        "action": action,
        "data": data,
    }

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, PermissionError) as e:
        _logger.warning("Could not write to log file %s: %s", log_file, e)
