"""CKS store integration - uses P:-- CKS API for multi-terminal isolation."""

import sys
from pathlib import Path

# Add P:\\\\\\packages/search-research/core to sys.path — APPEND not insert(0,) to avoid shadowing
# cks_store.py at P:\\\\\\packages/yt-is/csf/cks_store.py
# .parent = csf/, .parent.parent = yt-is/, .parent.parent.parent = packages/
# Then /search-research/core to reach the CKS
_src = Path(__file__).parent.parent.parent / "search-research" / "core"
# Validate path exists before inserting
_cks_module_path = _src / "cks" / "unified.py"
if not _cks_module_path.exists():
    raise RuntimeError(
        f"CKS module not found at {_cks_module_path}. "
        f"Verify yt-is/ directory structure matches path assumption."
    )
if str(_src) not in sys.path:
    sys.path.append(str(_src))  # append instead of insert(0,) — safe fallback if wrong

from cks.unified import get_cks  # noqa: E402  # Intentionally after sys.path setup above


def append_to_cks(artifact: dict[str, object]) -> None:
    """Ingest video analysis artifact into P:-- CKS.

    artifact shape:
        {"type": "memory|pattern|learning",
         "title": str, "content": str, "source": str, ...}
    """
    entry_type = artifact.get("type", "memory")
    title = artifact.get("title", "untitled")
    content = artifact.get("content", "")
    source = artifact.get("source", "")

    VALID_TYPES = {"memory", "pattern", "learning"}
    if entry_type not in VALID_TYPES:
        # Log unknown type but fall back to memory
        from .csf_logging import log_action

        log_action("cks_store_unknown_type", {"type": entry_type, "title": title})
        entry_type = "memory"

    try:
        with get_cks() as cks:
            # Validate CKS methods exist before calling
            if entry_type == "memory":
                if not hasattr(cks, "ingest_memory"):
                    raise AttributeError("cks.ingest_memory method not found")
                cks.ingest_memory(question=title, answer=content, source_chunk=source)
            elif entry_type == "pattern":
                if not hasattr(cks, "ingest_pattern"):
                    raise AttributeError("cks.ingest_pattern method not found")
                cks.ingest_pattern(title=title, content=content, source_chunk=source)
            elif entry_type == "learning":
                if not hasattr(cks, "ingest_learning"):
                    raise AttributeError("cks.ingest_learning method not found")
                cks.ingest_learning(title=title, content=content)
    except Exception as e:
        from .csf_logging import log_action

        log_action("cks_store_error", {"title": title, "error": str(e)})
        raise  # Re-raise so caller knows the operation failed
