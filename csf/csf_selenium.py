#!/usr/bin/env python3
"""YouTube transcript extraction via Selenium Firefox browser automation.

Selenium is a fallback method when faster approaches fail due to bot detection:
- Launches real Firefox browser with your profile (cookies, login)
- Navigates to YouTube, clicks transcript button via JavaScript
- Bypasses TLS fingerprinting bot detection
- Slower (~15-30s/video) due to browser overhead

Usage:
    python -m csf.csf_selenium              # Dry run: show pending videos
    python -m csf.csf_selenium --run        # Extract transcripts via Selenium
    python -m csf.csf_selenium --run --channel <url>  # Specific channel only
    python -m csf.csf_selenium --run --workers 2      # Parallel workers
"""

import argparse
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from csf.transcript import TranscriptResult

# Add csf to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from csf.batch_status import _get_batch_status_storage, get_entries_for_source
from csf.cache import has_cached_transcript, set_cached_transcript
from csf.batch_scheduler import BatchScheduler
from csf.display import format_kv_block

# Module-level singleton for Selenium-only scheduler
_selenium_scheduler: BatchScheduler | None = None


def _get_selenium_scheduler() -> BatchScheduler:
    """Get or create the Selenium-specific batch scheduler."""
    global _selenium_scheduler
    if _selenium_scheduler is None:
        _selenium_scheduler = BatchScheduler()
    return _selenium_scheduler


def _fetch_via_selenium_only(
    video_id: str, source: str, lang: str = "en"
) -> tuple[str, bool, str, str]:
    """Fetch transcript using Selenium Firefox ONLY (bypasses fetch_chain).

    This directly calls _fetch_via_selenium_firefox instead of the full
    fallback chain, ensuring Selenium is used. Useful when you know yt-dlp
    will fail due to bot detection.

    Args:
        video_id: YouTube video ID
        source: Channel URL (for cooldown tracking)
        lang: Preferred language code

    Returns:
        (video_id, success, source='selenium', error)
    """
    from csf.transcript import (
        _is_source_rate_limited,
        _record_source_429,
        _record_source_success,
        _apply_jitter_with_backoff,
        _apply_jitter,
        _SOURCE_SELENIUM,
        _fetch_via_selenium_firefox,
    )

    # Check circuit breaker
    if _is_source_rate_limited(_SOURCE_SELENIUM):
        return (
            video_id,
            False,
            "",
            f"Selenium in cooldown (circuit breaker open)",
        )

    # Apply jitter before request
    _apply_jitter()

    # Language fallback: prefer_lang → en → None (any available)
    lang_fallbacks = [lang]
    if lang != "en":
        lang_fallbacks.append("en")
    lang_fallbacks.append(None)  # Any available

    for try_lang in lang_fallbacks:
        try_lang_str = try_lang if try_lang is not None else "en"
        success, transcript, error = _fetch_via_selenium_firefox(
            video_id, try_lang_str
        )

        if success and transcript:
            _record_source_success(_SOURCE_SELENIUM, video_id)
            set_cached_transcript(video_id, try_lang_str, _SOURCE_SELENIUM, transcript)
            return (video_id, True, _SOURCE_SELENIUM, "")

        # Handle rate limit
        if error and ("429" in error.lower() or "rate limited" in error.lower()):
            _record_source_429(_SOURCE_SELENIUM, video_id)
            _apply_jitter_with_backoff(_SOURCE_SELENIUM)
            return (video_id, False, "", f"Rate limited: {error}")

    # All language fallbacks failed
    return (video_id, False, "", error or "No transcript available")


def _process_video(
    video_id: str, source: str, lang: str, run: bool
) -> tuple[str, bool, str, str]:
    """Process a single video.

    Args:
        video_id: YouTube video ID
        source: Channel URL
        lang: Preferred language
        run: If False, dry-run mode (check cache only)

    Returns:
        (video_id, success, source_used, error)
    """
    # Check if already cached
    if has_cached_transcript(video_id):
        return (video_id, True, "cache", "")

    if not run:
        return (video_id, False, "", "Not cached (dry run)")

    return _fetch_via_selenium_only(video_id, source, lang)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract YouTube transcripts via Selenium Firefox browser automation."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Actually extract transcripts (default: dry run).",
    )
    parser.add_argument("--channel", help="Process only this channel URL.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1).",
    )
    parser.add_argument(
        "--lang", default="en", help="Preferred language code (default: en)."
    )
    parser.add_argument(
        "--profile",
        help="Firefox profile path (default: auto-discover).",
    )
    args = parser.parse_args()

    # Load .env for potential environment variables
    from dotenv import load_dotenv

    load_dotenv("P:/.env")

    storage = _get_batch_status_storage()
    conn = storage._get_conn()
    cursor = conn.execute("SELECT channel_url, playlist_id FROM channel_metadata")
    channels = cursor.fetchall()
    conn.close()

    if args.channel:
        channels = [(u, p) for u, p in channels if args.channel in u]
        if not channels:
            print(f"Channel '{args.channel}' not found in tracked sources.")
            sys.exit(1)

    status_rows: list[tuple[str, str | int]] = [
        ("Channels", len(channels)),
        ("Mode", "LIVE (extracting)" if args.run else "DRY RUN"),
        ("Workers", args.workers),
        ("Language", args.lang),
    ]
    if args.profile:
        status_rows.append(("Firefox Profile", args.profile))
    print(format_kv_block("=== Selenium Transcript Extractor ===", status_rows))
    print()

    # Collect pending videos from all channels
    pending_videos: list[tuple[str, str]] = []  # (video_id, source_url)
    cached_count = 0

    for channel_url, playlist_id in channels:
        entries = get_entries_for_source(channel_url)
        for entry in entries:
            # entry is tuple: (video_id, status, has_captions)
            video_id = entry[0]
            if not video_id:
                continue

            if has_cached_transcript(video_id):
                cached_count += 1
            else:
                pending_videos.append((video_id, channel_url))

    total_videos = cached_count + len(pending_videos)
    print(f"Total videos: {total_videos}")
    print(f"Already cached: {cached_count}")
    print(f"Pending extraction: {len(pending_videos)}")
    print()

    if not pending_videos:
        print("All transcripts already cached. Nothing to do.")
        return

    if not args.run:
        print("Dry run complete. Use --run to extract transcripts.")
        return

    # Process videos with progress tracking
    success_count = 0
    fail_count = 0
    skip_count = 0
    results: list[tuple[str, bool, str, str]] = []

    print(f"Extracting {len(pending_videos)} transcripts via Selenium...")
    print()

    def _update_progress(
        future: "Future[tuple[str, bool, str, str]]", idx: int
    ) -> None:
        nonlocal success_count, fail_count, skip_count
        try:
            video_id, success, source_used, error = future.result()
            if success:
                if source_used == "cache":
                    skip_count += 1
                else:
                    success_count += 1
                    print(f"[{idx}] ✓ {video_id} ({source_used})")
            else:
                fail_count += 1
                print(f"[{idx}] ✗ {video_id}: {error[:80]}")
        except Exception as e:
            fail_count += 1
            print(f"[{idx}] ✗ Error: {e}")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _process_video, video_id, source, args.lang, args.run
            ): (idx, video_id)
            for idx, (video_id, source) in enumerate(pending_videos, 1)
        }

        for future in as_completed(futures):
            idx, _ = futures[future]
            _update_progress(future, idx)

    # Summary
    print()
    print(
        format_kv_block(
            "=== Summary ===",
            [
                ("Successfully extracted", success_count),
                ("Failed", fail_count),
                ("Skipped (cached)", skip_count),
                ("Total processed", len(pending_videos)),
            ],
        )
    )

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
