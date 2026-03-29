"""Terminal context utilities for multi-terminal isolation."""

import os
import re
import socket
import hashlib

# Validation: allow alphanumeric, hyphen, underscore. Max 64 chars.
_TID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def resolve_tid() -> str:
    """Resolve terminal ID from environment variables or generate from hostname + pid.

    Returns a unique terminal identifier for per-terminal state isolation.
    Terminates with non-zero exit code if TERMINAL_ID is set but invalid.
    """
    # Check for explicit terminal ID (must pass validation)
    tid = os.getenv("TERMINAL_ID")
    if tid:
        if _TID_PATTERN.match(tid):
            return tid
        else:
            raise ValueError(
                f"Invalid TERMINAL_ID {tid!r}: must match ^[a-zA-Z0-9_-]{{1,64}}$"
            )

    # Check for Claude Code terminal
    tid = os.getenv("CLAUDE_TERMINAL_ID")
    if tid:
        if _TID_PATTERN.match(tid):
            return tid
        else:
            raise ValueError(
                f"Invalid CLAUDE_TERMINAL_ID {tid!r}: must match ^[a-zA-Z0-9_-]{{1,64}}$"
            )

    # Generate from hostname + process ID
    hostname = socket.gethostname()
    pid = os.getpid()
    raw = f"{hostname}-{pid}"

    # Create a short hash for brevity
    h = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"term_{h}"
