#!/usr/bin/env python3
"""DEPRECATED — Use `fetch_transcript_chain()` from transcript.py instead.

This script creates one ephemeral NotebookLM notebook per video, which is
inefficient (wastes NotebookLM slots) and slow.

The recommended approach uses batch notebooks (up to 300 YouTube sources per
notebook) via `_fetch_via_notebooklm_batch()` in transcript.py, which:
- Reuses a single notebook for up to 300 videos
- Uses nlm source content (raw text) instead of nlm notebook query (LLM)
- Has auth auto-recovery built in

This file is kept for reference only and may be removed in a future version.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add csf module to path
sys.path.insert(0, str(Path(__file__).parent))

from batch_status import (
    _get_batch_status_storage,
    _STATUS_PENDING,
    mark_complete,
    set_status,
)
from cache import set_cached_transcript

try:
    from csf import nlm_auth_guard
except ImportError:
    import nlm_auth_guard


def get_all_pending_videos():
    """Get all pending videos from batch_status database.

    Returns list of dicts with video_id, source, published_at.
    """
    storage = _get_batch_status_storage()
    conn = storage._get_conn()
    cursor = conn.execute(
        "SELECT video_id, source, published_at FROM analysis_status WHERE status = ?",
        (_STATUS_PENDING,)
    )
    rows = cursor.fetchall()
    conn.close()

    return [
        {'video_id': row[0], 'source': row[1], 'published_at': row[2]}
        for row in rows
    ]


def run_nlm_command(args: list[str]) -> subprocess.CompletedProcess:
    """Run nlm CLI command and return result."""
    return nlm_auth_guard.run_nlm(nlm_auth_guard.add_profile_args(args), timeout_s=300)


def create_ephemeral_notebook(video_id: str) -> Optional[str]:
    """Create ephemeral notebook for a single video.

    Returns notebook ID if successful, None otherwise.
    """
    result = run_nlm_command([
        "notebook", "create",
        f"Video {video_id}",
    ])

    if result.returncode != 0:
        print(f"Failed to create notebook for {video_id}: {result.stderr}")
        return None

    # Parse notebook ID from output
    # Expected output format: "Created notebook <id>" or similar
    lines = result.stdout.strip().split('\n')
    for line in lines:
        if line.strip():
            notebook_id = line.strip()
            return notebook_id

    return None


def add_video_source(notebook_id: str, video_url: str) -> bool:
    """Add video URL as source to notebook."""
    result = run_nlm_command([
        "source", "add", notebook_id,
        "--url", video_url
    ])

    return result.returncode == 0


def extract_transcript(notebook_id: str) -> bool:
    """Trigger transcript extraction via audio report."""
    result = run_nlm_command([
        "audio", "report", "create", notebook_id,
        "--confirm",
    ])

    return result.returncode == 0


def download_transcript(notebook_id: str, output_path: Path) -> bool:
    """Download transcript artifact from notebook."""
    result = run_nlm_command([
        "download", "audio", notebook_id,
        "--output", str(output_path)
    ])

    return result.returncode == 0


def delete_notebook(notebook_id: str) -> bool:
    """Delete ephemeral notebook to reclaim slots."""
    result = run_nlm_command([
        "notebook", "delete", notebook_id,
        "--confirm",
    ])

    return result.returncode == 0


def ingest_video(video_id: str, video_url: str, output_dir: Path) -> bool:
    """Ingest a single video via ephemeral notebook workflow.

    Returns True if successful, False otherwise.
    """
    print(f"Processing {video_id}...")

    # Step 1: Create ephemeral notebook
    notebook_id = create_ephemeral_notebook(video_id)
    if not notebook_id:
        return False

    # Step 2: Add video source
    if not add_video_source(notebook_id, video_url):
        delete_notebook(notebook_id)
        return False

    # Step 3: Extract transcript
    if not extract_transcript(notebook_id):
        delete_notebook(notebook_id)
        return False

    # Step 4: Download transcript
    output_path = output_dir / f"transcript_{video_id}.txt"
    if not download_transcript(notebook_id, output_path):
        delete_notebook(notebook_id)
        return False

    # Step 4.5: Write to database cache
    try:
        with open(output_path, 'r', encoding='utf-8') as f:
            transcript_content = f.read()
        set_cached_transcript(
            video_id,
            "en",
            "notebooklm",
            transcript_content,
            metadata={
                "notebook_id": notebook_id,
                "video_url": video_url,
                "output_path": str(output_path),
                "importer": "csf_nlm_ingest",
            },
        )
        print(f"✓ {video_id} cached to database")
    except Exception as e:
        print(f"Warning: Failed to cache {video_id} to database: {e}")

    # Step 5: Cleanup ephemeral notebook
    if not delete_notebook(notebook_id):
        print(f"Warning: Failed to delete ephemeral notebook {notebook_id}")

    print(f"✓ {video_id} transcript saved to {output_path}")
    return True


def combine_transcripts(transcript_files: list[Path], output_path: Path) -> None:
    """Combine multiple transcripts into single markdown source with structural anchors."""
    with open(output_path, 'w', encoding='utf-8') as out:
        for transcript_file in transcript_files:
            video_id = transcript_file.stem.replace('transcript_', '')

            # Write structural header
            out.write(f"\n\n# Video: {video_id}\n\n")
            out.write(f"**Source:** https://www.youtube.com/watch?v={video_id}\n\n")
            out.write("**Transcript:**\n\n")

            # Write transcript content
            with open(transcript_file, 'r', encoding='utf-8') as f:
                content = f.read()
                out.write(content)

            out.write("\n\n---\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="YouTube transcript ingestion via NotebookLM — ephemeral notebook workflow."
    )

    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what will be ingested without processing"
    )
    parser.add_argument(
        "--channel", type=str,
        help="Process specific channel only (by URL)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=20,
        help="Transcripts per combined source (default: 20)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed output"
    )

    args = parser.parse_args()

    # Output directory for transcripts
    output_dir = Path("P:\\.data/yt-is")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Get pending videos from batch_status
    print("Fetching pending videos...")
    pending = get_all_pending_videos()

    if not pending:
        print("No pending videos to process.")
        return

    print(f"Found {len(pending)} pending videos")

    if args.channel:
        # Filter by channel URL
        pending = [v for v in pending if v.get('source') == args.channel]
        if not pending:
            print(f"No pending videos found for channel: {args.channel}")
            return
        print(f"Filtered to {len(pending)} videos for channel")

    if args.dry_run:
        print("\nDry run mode — will not ingest videos.")
        for video in pending[:10]:  # Show first 10
            print(f"  - {video.get('video_id')}")
        if len(pending) > 10:
            print(f"  ... and {len(pending) - 10} more")
        return

    # Show what will be processed before starting
    print(f"\nWill ingest {len(pending)} videos...")
    for video in pending[:10]:  # Show first 10
        print(f"  - {video.get('video_id')}")
    if len(pending) > 10:
        print(f"  ... and {len(pending) - 10} more")
    print("\nStarting NotebookLM ingestion...\n")

    # Ingest videos
    success_count = 0
    failed_count = 0
    transcript_files = []

    for video in pending:
        video_id = video.get('video_id')
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        if ingest_video(video_id, video_url, output_dir):
            success_count += 1
            transcript_files.append(output_dir / f"transcript_{video_id}.txt")
            mark_complete(video_id, source=video.get('source'))
        else:
            failed_count += 1
            set_status(video_id, "failed")

    print(f"\nCompleted: {success_count} successful, {failed_count} failed")

    # Combine transcripts into batches
    if success_count > 0:
        batch_size = args.batch_size
        for i in range(0, len(transcript_files), batch_size):
            batch = transcript_files[i:i+batch_size]
            batch_num = i // batch_size + 1
            combined_path = output_dir / f"combined_batch_{batch_num}.md"
            combine_transcripts(batch, combined_path)
            print(f"✓ Combined batch {batch_num} ({len(batch)} transcripts) → {combined_path}")

        print(f"\nCombined transcript files ready for use in your knowledge system.")


if __name__ == "__main__":
    main()
