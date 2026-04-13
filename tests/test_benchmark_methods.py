"""Benchmark test for transcript extraction methods.

Tests 50 videos per method to measure speed and reliability:
1. yt-dlp (base)
2. Selenium Firefox
3. NotebookLM

Results are saved to benchmark_results.json for analysis.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# Add csf to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from csf.transcript import (
    LanguageConfig,
    TranscriptResult,
)
from csf.batch_status import get_entries_for_source


# Method labels for results
METHOD_LABELS = {
    "ytdlp": "yt-dlp (WEB client)",
    "selenium": "Selenium Firefox",
    "notebooklm": "NotebookLM (ephemeral)",
}


def _measure_method(
    video_id: str, method: Literal["ytdlp", "selenium", "notebooklm"]
) -> dict:
    """Measure a single method for one video.

    Returns dict with success, duration_seconds, source, error.
    """
    start = time.time()

    if method == "notebooklm":
        # Call NotebookLM directly (bypass chain and cache)
        from csf.transcript import _fetch_via_notebooklm
        success, transcript, error = _fetch_via_notebooklm(video_id, "en")
        source = "notebooklm" if success else "none"
    elif method == "selenium":
        # Call Selenium directly (bypass chain and cache)
        from csf.transcript import _fetch_via_selenium_firefox
        success, transcript, error = _fetch_via_selenium_firefox(video_id, "en")
        source = "selenium" if success else "none"
    else:  # ytdlp
        # Call yt-dlp directly (bypass chain and cache)
        from csf.transcript import _fetch_via_ytdlp
        success, transcript, error = _fetch_via_ytdlp(video_id, "en")
        source = "ytdlp" if success else "none"

    duration = time.time() - start

    # Cache successful results
    if success and transcript:
        from csf.cache import set_cached_transcript
        try:
            set_cached_transcript(video_id, "en", source, transcript)
        except Exception as e:
            print(f"  [WARN] Failed to cache transcript: {e}")

    return {
        "video_id": video_id,
        "method": method,
        "success": success,
        "duration_seconds": round(duration, 2),
        "source": source,
        "error": error,
        "transcript_length": len(transcript) if transcript else 0,
    }


def benchmark_method(
    video_ids: list[str],
    method: Literal["ytdlp", "selenium", "notebooklm"],
    max_videos: int = 50,
) -> dict:
    """Benchmark a specific method on a list of videos.

    Args:
        video_ids: List of video IDs to test
        method: Which method to test
        max_videos: Maximum videos to test (default 50)

    Returns:
        dict with results summary
    """
    print(f"\n{'='*60}")
    print(f"Benchmarking: {METHOD_LABELS[method]}")
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
                "method_label": METHOD_LABELS[method],
                "total_attempted": 0,
                "successful": 0,
                "failed": 0,
                "success_rate": "N/A",
                "total_duration_seconds": 0,
                "avg_duration_seconds": 0,
                "avg_success_duration_seconds": 0,
                "min_duration_seconds": 0,
                "max_duration_seconds": 0,
                "results": [],
            }
        except subprocess.TimeoutExpired:
            print("ERROR: nlm login timed out. Please authenticate manually.")
            return {
                "method": method,
                "method_label": METHOD_LABELS[method],
                "total_attempted": 0,
                "successful": 0,
                "failed": 0,
                "success_rate": "N/A",
                "total_duration_seconds": 0,
                "avg_duration_seconds": 0,
                "avg_success_duration_seconds": 0,
                "min_duration_seconds": 0,
                "max_duration_seconds": 0,
                "results": [],
            }

    results = []
    successful = 0
    failed = 0

    # Limit to max_videos
    test_videos = video_ids[:max_videos]

    results = []
    successful = 0
    failed = 0

    # Limit to max_videos
    test_videos = video_ids[:max_videos]

    for i, video_id in enumerate(test_videos, 1):
        print(f"[{i}/{len(test_videos)}] Testing {video_id}...")

        result = _measure_method(video_id, method)
        results.append(result)

        if result["success"]:
            successful += 1
            print(f"  [OK] {result['duration_seconds']}s ({result['transcript_length']} chars)")
        else:
            failed += 1
            print(f"  [FAIL] {result['error'][:50]}")

        # Rate limiting between attempts
        time.sleep(2)

    # Calculate statistics
    durations = [r["duration_seconds"] for r in results]
    success_durations = [r["duration_seconds"] for r in results if r["success"]]
    success_lengths = [r["transcript_length"] for r in results if r["success"]]

    total_chars = sum(success_lengths)
    total_time = sum(durations)
    avg_chars_per_video = total_chars / successful if successful > 0 else 0
    chars_per_second = total_chars / total_time if total_time > 0 else 0

    summary = {
        "method": method,
        "method_label": METHOD_LABELS[method],
        "total_attempted": len(results),
        "successful": successful,
        "failed": failed,
        "success_rate": f"{(successful / len(results) * 100):.1f}%",
        "total_duration_seconds": round(total_time, 2),
        "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
        "avg_success_duration_seconds": round(sum(success_durations) / len(success_durations), 2) if success_durations else 0,
        "min_duration_seconds": round(min(durations), 2) if durations else 0,
        "max_duration_seconds": round(max(durations), 2) if durations else 0,
        "total_chars": total_chars,
        "avg_chars_per_video": round(avg_chars_per_video, 0),
        "chars_per_second": round(chars_per_second, 0),
        "results": results,
    }

    print(f"\nResults for {method}:")
    print(f"  Success rate: {summary['success_rate']}")
    print(f"  Avg duration: {summary['avg_duration_seconds']}s")
    print(f"  Total time: {summary['total_duration_seconds']}s")
    print(f"  Total chars: {summary['total_chars']:,}")
    print(f"  Avg chars/video: {summary['avg_chars_per_video']:,.0f}")
    print(f"  EFFICIENCY: {summary['chars_per_second']:,.0f} chars/sec")

    return summary


def main():
    """Run benchmark tests on all three methods."""
    print("Transcript Extraction Benchmark Test")
    print("=" * 60)
    print("Testing 50 videos per method (already completed videos):")
    print("  1. yt-dlp (WEB client)")
    print("  2. Selenium Firefox")
    print("  3. NotebookLM (ephemeral)")
    print("=" * 60)

    # Get sample video IDs from completed videos (for benchmarking speed)
    print("\nGetting sample video IDs from completed videos...")

    from csf.batch_status import _get_batch_status_storage

    storage = _get_batch_status_storage()
    conn = storage._get_conn()
    cursor = conn.execute(
        "SELECT video_id FROM analysis_status WHERE status = 'complete' LIMIT 150"
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("ERROR: No completed videos found to benchmark against.")
        sys.exit(1)

    video_ids = [row[0] for row in rows]
    print(f"Found {len(video_ids)} completed videos. Using first 150 for testing.")

    # Split videos across 3 methods (50 each)
    videos_per_method = 50
    videos_ytdlp = video_ids[0:50]
    videos_selenium = video_ids[50:100]
    videos_notebooklm = video_ids[100:150]

    # Run benchmarks
    all_summaries = []

    try:
        all_summaries.append(benchmark_method(videos_ytdlp, "ytdlp", videos_per_method))
        all_summaries.append(benchmark_method(videos_selenium, "selenium", videos_per_method))
        all_summaries.append(benchmark_method(videos_notebooklm, "notebooklm", videos_per_method))
    except KeyboardInterrupt:
        print("\n\nBenchmark interrupted by user.")

    # Save results
    results_file = Path(__file__).parent / "benchmark_results.json"

    final_results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "videos_per_method": videos_per_method,
        "summaries": all_summaries,
    }

    with open(results_file, "w") as f:
        json.dump(final_results, f, indent=2)

    print(f"\n{'='*60}")
    print("Benchmark Complete")
    print(f"{'='*60}")
    print(f"\nComparison:")
    print(f"{'Method':<20} {'Success':<10} {'Avg Time':<10} {'Efficiency (chars/sec)':<20}")
    print("-" * 62)

    for summary in all_summaries:
        print(f"{summary['method_label']:<20} {summary['success_rate']:<10} "
              f"{summary['avg_duration_seconds']}s  {summary['chars_per_second']:>15,.0f} chars/sec")

    print(f"\nResults saved to: {results_file}")


if __name__ == "__main__":
    main()
