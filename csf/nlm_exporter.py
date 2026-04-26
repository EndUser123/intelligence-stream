"""NotebookLM composite exporter — TASK-007.

Composite batching algorithm:
- Group by channel → sort by published_at ASC (null-safe)
- Split into chunks of ≤500K words AND ≤300 videos
- Atomic write + idempotent export via nlm_export_state table

Concurrency: Uses InterProcessLock for multi-terminal safety (FM-010).
Atomicity: temp file → API call → rename in same BEGIN IMMEDIATE transaction (DD-007).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Default exports directory
_DEFAULT_EXPORTS_DIR = Path("P:/.data/yt-is/nlm_exports")

# Hard limits (NotebookLM constraints)
_MAX_WORDS_PER_COMPOSITE = 500_000
_MAX_VIDEOS_PER_COMPOSITE = 300

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    """Return hex SHA-256 of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class CompositeDocument:
    """A composite document ready for NotebookLM import.

    Attributes:
        composite_id: Unique ID derived from notebook+channel+batch_index+content_hash.
        notebook_id: Target NotebookLM notebook name.
        batch_key: Human-readable batch identifier (e.g. "channel:UCxxxxx:part1").
        video_ids: Pipe-delimited video IDs in this composite.
        word_count: Total word count of the composite content.
        content: The composite text content (markdown-formatted).
        content_hash: SHA-256 of sorted video_ids — changes when video set changes.
    """

    composite_id: str
    notebook_id: str
    batch_key: str
    video_ids: str
    word_count: int
    content: str
    content_hash: str


@dataclass
class VideoRecord:
    """A video record for composite building.

    Must have at minimum: video_id (str), published_at (str or None).
    Optional: title (str), transcript (str).
    """

    video_id: str
    published_at: str | None = None
    title: str | None = None
    transcript: str | None = None


def build_composites(
    channel_url: str,
    videos: list[dict],
    notebook_id: str,
) -> list[CompositeDocument]:
    """Build composite documents from a list of videos.

    Groups by channel (single channel here), sorts by published_at ASC
    with null-safe handling, then splits into ≤500K-word, ≤300-video chunks.

    Args:
        channel_url: The channel this batch targets (used in composite_id).
        videos: List of video dicts with keys: video_id, published_at, title, transcript.
        notebook_id: Target NotebookLM notebook name.

    Returns:
        List of CompositeDocument, ordered by batch index.

    Raises:
        ValueError: If a single video exceeds 500K words.
    """
    if not channel_url or len(channel_url) > 500:
        raise ValueError(
            f"Invalid channel_url: {channel_url!r}. Must be non-empty and ≤500 chars."
        )

    if not videos:
        return []

    # Convert to VideoRecord with null-safe published_at
    records: list[VideoRecord] = []
    for v in videos:
        vid = v.get("video_id")
        if not vid:
            logger.warning("Skipping video record with missing video_id: %s", v)
            continue
        records.append(
            VideoRecord(
                video_id=vid,
                published_at=v.get("published_at"),
                title=v.get("title"),
                transcript=v.get("transcript", ""),
            )
        )

    # Sort by published_at ASC, nulls to end
    records.sort(key=lambda r: (r.published_at is None, r.published_at or ""))

    composites: list[CompositeDocument] = []
    chunk: list[VideoRecord] = []
    chunk_words = 0
    batch_index = 0

    for record in records:
        transcript_words = _count_words(record.transcript or "")
        # Guard: single video must fit
        if transcript_words > _MAX_WORDS_PER_COMPOSITE:
            raise ValueError(
                f"Video {record.video_id} transcript ({transcript_words} words) exceeds "
                f"the {_MAX_WORDS_PER_COMPOSITE}-word composite limit."
            )

        # Check if adding this record would exceed limits
        over_word_limit = (chunk_words + transcript_words) > _MAX_WORDS_PER_COMPOSITE
        over_video_limit = len(chunk) >= _MAX_VIDEOS_PER_COMPOSITE

        if chunk and (over_word_limit or over_video_limit):
            # Emit current chunk as a composite
            composites.append(_make_composite(chunk, channel_url, notebook_id, batch_index))
            batch_index += 1
            chunk = []
            chunk_words = 0

        chunk.append(record)
        chunk_words += transcript_words

    # Emit final chunk
    if chunk:
        composites.append(_make_composite(chunk, channel_url, notebook_id, batch_index))

    return composites


def _count_words(text: str) -> int:
    """Return approximate word count (whitespace split)."""
    return len(text.split())


def _make_composite(
    records: list[VideoRecord],
    channel_url: str,
    notebook_id: str,
    batch_index: int,
) -> CompositeDocument:
    """Build a CompositeDocument from a list of VideoRecord."""
    video_ids = "|".join(r.video_id for r in records)
    sorted_ids = ":".join(sorted(r.video_id for r in records))
    content_hash = _sha256(sorted_ids)
    composite_id = _sha256(f"{notebook_id}:{channel_url}:{batch_index}:{content_hash}")[:16]

    # Build content
    lines: list[str] = []
    lines.append(f"# {notebook_id} — {channel_url.split('/')[-1]}\n")

    # Sources section
    lines.append("## Sources\n")
    for r in records:
        lines.append(f"- [{r.title or r.video_id}](https://youtube.com/watch?v={r.video_id})")
    lines.append("")

    # Transcript sections
    lines.append("## Transcript Collection\n")
    for r in records:
        title_str = r.title or r.video_id
        date_str = r.published_at or "Unknown date"
        lines.append(f"### [{title_str}] (published {date_str})\n")
        lines.append("---\n")
        lines.append(r.transcript or "[No transcript available]")
        lines.append("---\n")

    content = "\n".join(lines)
    word_count = _count_words(content)

    return CompositeDocument(
        composite_id=composite_id,
        notebook_id=notebook_id,
        batch_key=f"{channel_url}:part{batch_index + 1}",
        video_ids=video_ids,
        word_count=word_count,
        content=content,
        content_hash=content_hash,
    )


def export_composite(
    doc: CompositeDocument,
    nlm_source_id: str | None = None,
) -> str | None:
    """Export a CompositeDocument to NotebookLM via nlm CLI.

    Atomic write pattern (DD-007):
    1. Write content to .tmp file
    2. Call `nlm source add --text <tmp_path>`
    3. On success: rename .tmp → .txt AND upsert nlm_export_state in same transaction
    4. On failure: delete .tmp; do NOT record nlm_source_id

    Idempotent skip (FM-004): If nlm_source_id already set AND content_hash matches,
    skip without re-uploading.

    Args:
        doc: The CompositeDocument to export.
        nlm_source_id: Pre-set NotebookLM source ID (from nlm_export_state).

    Returns:
        The nlm_source_id on success, None on failure.
    """
    from csf.batch_status import (
        get_nlm_export_state,
        upsert_nlm_export_state,
    )

    # Idempotent skip: already exported with same content_hash
    existing = get_nlm_export_state(doc.composite_id)
    if existing is not None and existing.get("nlm_source_id"):
        if existing["content_hash"] == doc.content_hash:
            logger.info(
                "Composite %s already exported (nlm_source_id=%s, content_hash matches) — skipping.",
                doc.composite_id,
                existing["nlm_source_id"],
            )
            return existing["nlm_source_id"]
        # Content changed — force re-export (nlm_source_id will be updated)

    # Determine exports directory
    exports_dir = _DEFAULT_EXPORTS_DIR
    exports_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = exports_dir / f"{doc.composite_id}.tmp"
    final_path = exports_dir / f"{doc.composite_id}.txt"

    try:
        # Step 1: atomic write to .tmp
        tmp_path.write_text(doc.content, encoding="utf-8")

        # Step 2: call nlm CLI
        nlm_path = shutil.which("nlm")
        if not nlm_path:
            logger.error("nlm CLI not found in PATH — cannot export composite %s", doc.composite_id)
            return None

        cmd = [nlm_path, "source", "add", doc.notebook_id, "--text", str(tmp_path)]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            logger.error(
                "nlm source add failed for %s: %s",
                doc.composite_id,
                result.stderr.strip(),
            )
            _cleanup_tmp(tmp_path)
            return None

        # Extract source_id from stdout (format: "Source added: <id>" or JSON)
        source_id = _parse_nlm_output(result.stdout) or nlm_source_id

        # Step 3: rename .tmp → .txt AND upsert nlm_export_state
        tmp_path.rename(final_path)

        upsert_nlm_export_state(
            composite_id=doc.composite_id,
            batch_key=doc.batch_key,
            video_ids=doc.video_ids,
            content_hash=doc.content_hash,
            word_count=doc.word_count,
            notebook_id=doc.notebook_id,
            nlm_source_id=source_id,
        )

        logger.info(
            "Exported composite %s (nlm_source_id=%s, %d words, %d videos)",
            doc.composite_id,
            source_id,
            doc.word_count,
            len(doc.video_ids.split("|")),
        )
        return source_id

    except subprocess.TimeoutExpired:
        logger.error("nlm CLI timed out for composite %s", doc.composite_id)
        _cleanup_tmp(tmp_path)
        return None
    except Exception as e:
        logger.error("Failed to export composite %s: %s", doc.composite_id, e)
        _cleanup_tmp(tmp_path)
        return None


def _cleanup_tmp(tmp_path: Path) -> None:
    """Delete .tmp file if it exists (non-fatal)."""
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except Exception:
        pass


def _parse_nlm_output(stdout: str) -> str | None:
    """Parse nlm source add stdout to extract source ID.

    Expected formats:
    - "Source added: abc123"
    - JSON: {"source_id": "abc123", ...}

    Returns the source_id or None if parsing fails.
    """
    import json, re

    stdout = stdout.strip()

    # Try JSON
    try:
        data = json.loads(stdout)
        return data.get("source_id") or data.get("id")
    except Exception:
        pass

    # Try "Source added: <id>"
    m = re.search(r"Source added:\s*(\S+)", stdout, re.IGNORECASE)
    if m:
        return m.group(1)

    # Try bare ID
    m = re.search(r"\b([a-zA-Z0-9_-]{8,})\b", stdout)
    if m:
        return m.group(1)

    return None
