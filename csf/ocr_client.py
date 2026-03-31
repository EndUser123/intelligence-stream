"""EasyOCR wrapper for capturing code on screen from video frames.

Non-fatal: timeouts and exceptions return empty list so the orchestrator
continues with partial results.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import re

# Boilerplate patterns to filter out
_BOILERPLATE_PATTERNS: list[re.Pattern[str]] = [
    # Navigation UI
    re.compile(r"^Subscribe$", re.IGNORECASE),
    re.compile(r"^Like$", re.IGNORECASE),
    re.compile(r"^Share$", re.IGNORECASE),
    re.compile(r"^Comment$", re.IGNORECASE),
    re.compile(r"^Home$", re.IGNORECASE),
    re.compile(r"^Videos$", re.IGNORECASE),
    re.compile(r"^Playlists$", re.IGNORECASE),
    re.compile(r"^Subscribe$", re.IGNORECASE),
    # Short numbers or symbols
    re.compile(r"^\d+$"),
    re.compile(r"^[^\w\s]+$"),
]

# Very short text (less than 3 chars after stripping whitespace)
_SHORT_TEXT_PATTERN = re.compile(r"^\s*.{0,2}\s*$")


def _is_boilerplate(text: str) -> bool:
    """Return True if text matches boilerplate patterns."""
    stripped = text.strip()
    if len(stripped) < 3:
        return True
    if _SHORT_TEXT_PATTERN.match(stripped):
        return True
    for pattern in _BOILERPLATE_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


# Singleton reader — loaded once at module level
_reader: Optional["easyocr.Reader"] = None


def _get_reader() -> "easyocr.Reader":
    """Lazily create and return the EasyOCR singleton reader."""
    global _reader
    if _reader is None:
        import easyocr

        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


def _ocr_on_image(image_path: Path) -> list[str]:
    """Run OCR on a single image and return raw text results."""
    reader = _get_reader()
    results = reader.readtext(str(image_path))
    return [item[1] for item in results if item[1].strip()]


def extract_code_snippets(
    image_paths: list[Path], timeout_per_image: float = 30.0
) -> list[str]:
    """Run EasyOCR over a list of frame images and extract text.

    Args:
        image_paths: List of paths to frame image files.
        timeout_per_image: Seconds to wait per image before cancelling.

    Returns:
        List of non-boilerplate strings captured from frames.
        Returns [] if any image times out or raises an exception.
    """
    if not image_paths:
        return []

    all_snippets: list[str] = []

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_path = {
                executor.submit(_ocr_on_image, path): path for path in image_paths
            }

            for future in future_to_path:
                path = future_to_path[future]
                try:
                    snippets = future.result(timeout=timeout_per_image)
                    for snippet in snippets:
                        if not _is_boilerplate(snippet):
                            all_snippets.append(snippet.strip())
                except TimeoutError:
                    # Per-image timeout — cancel and continue
                    future.cancel()
                    continue
                except Exception:
                    # Non-fatal: continue with remaining images
                    continue
    except Exception:
        # Non-fatal at the executor level too
        return []

    # Deduplicate while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for s in all_snippets:
        if s not in seen:
            seen.add(s)
            deduped.append(s)

    return deduped
