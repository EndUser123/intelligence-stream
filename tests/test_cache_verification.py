#!/usr/bin/env python3
"""Test 10 videos per method with cache verification.

Tests yt-dlp, Selenium, and NotebookLM on 10 videos each.
Verifies that transcripts are properly cached to database.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from csf.transcript import (
    _fetch_via_ytdlp,
    _fetch_via_selenium_firefox,
    _fetch_via_notebooklm,
)
from csf.cache import (
    has_cached_transcript,
    get_cached_transcript,
    list_cached_transcripts,
)


def test_method(videos: list[str], method: str) -> dict:
    """Test a method on videos and verify caching.

    Returns dict with success count, cache_verified count.
    """
    print(f"\n{'='*60}")
    print(f"Testing: {method.upper()}")
    print(f"{'='*60}")

    # Auto-authenticate for NotebookLM
    if method == "notebooklm":
        import subprocess
        try:
            result = subprocess.run(
                ["nlm", "notebook", "list", "--quiet"],
                capture_output=True,
                timeout=10
            )
            if result.returncode != 0:
                print("NotebookLM not authenticated. Running 'nlm login'...")
                subprocess.run(["nlm", "login"], timeout=120)
                print("NotebookLM authenticated.")
        except FileNotFoundError:
            print("ERROR: nlm CLI not found. Install with: pip install nlm-cli")
            return {
                "method": method,
                "success_count": 0,
                "cache_verified": 0,
                "failed": len(videos),
                "total": len(videos),
            }

    cached_before = list_cached_transcripts()
    print(f"Cache before: {len(cached_before)} transcripts")

    success_count = 0
    cache_verified = 0
    failed = 0

    for i, video_id in enumerate(videos, 1):
        print(f"\n[{i}/{len(videos)}] {video_id}")

        # Fetch transcript
        start = time.time()
        try:
            if method == "ytdlp":
                success, transcript, error = _fetch_via_ytdlp(video_id, "en")
            elif method == "selenium":
                success, transcript, error = _fetch_via_selenium_firefox(video_id, "en")
            else:  # notebooklm
                success, transcript, error = _fetch_via_notebooklm(video_id, "en")

            duration = time.time() - start

            if success and transcript:
                # Cache the result
                from csf.cache import set_cached_transcript
                source = method
                set_cached_transcript(video_id, "en", source, transcript)

                # Verify it was cached
                if has_cached_transcript(video_id):
                    cached = get_cached_transcript(video_id, "en", source)
                    if cached and cached.transcript == transcript:
                        print(f"  [OK] {duration:.2f}s, {len(transcript)} chars, CACHED ✓")
                        success_count += 1
                        cache_verified += 1
                    else:
                        print(f"  [WARN] Fetched but cache mismatch")
                        success_count += 1
                else:
                    print(f"  [FAIL] Fetched but NOT cached")
                    success_count += 1
            else:
                print(f"  [FAIL] {error}")
                failed += 1

        except Exception as e:
            print(f"  [ERROR] {e}")
            failed += 1

    cached_after = list_cached_transcripts()
    print(f"\nCache after: {len(cached_after)} transcripts (+{len(cached_after) - len(cached_before)})")

    return {
        "method": method,
        "success_count": success_count,
        "cache_verified": cache_verified,
        "failed": failed,
        "total": len(videos),
    }


def main():
    print("Cache Verification Test: 10 videos × 3 methods")
    print("=" * 60)

    # Load test videos
    test_file = Path(__file__).parent / "test_10_each.json"
    if not test_file.exists():
        print(f"ERROR: {test_file} not found. Run setup first.")
        sys.exit(1)

    with open(test_file) as f:
        data = json.load(f)

    all_results = []

    # Test each method
    for method in ["ytdlp", "selenium", "notebooklm"]:
        videos = data[method]
        result = test_method(videos, method)
        all_results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    total_success = sum(r["success_count"] for r in all_results)
    total_verified = sum(r["cache_verified"] for r in all_results)
    total_attempted = sum(r["total"] for r in all_results)

    print(f"\n{'Method':<12} {'Success':<10} {'Verified':<10} {'Failed':<8}")
    print("-" * 40)
    for r in all_results:
        print(f"{r['method']:<12} {r['success_count']:<10} {r['cache_verified']:<10} {r['failed']:<8}")

    print(f"\nTotal: {total_success}/{total_attempted} successful, {total_verified} cache-verified")

    # Final cache check
    final_cache = list_cached_transcripts()
    print(f"\nFinal cache: {len(final_cache)} transcripts")

    if total_verified == total_success:
        print("\n✓ All successful transcripts were cached correctly!")
    else:
        print(f"\n✗ WARNING: {total_success - total_verified} transcripts not cached properly")


if __name__ == "__main__":
    main()
