"""Parallel batch processing for multiple videos.

Uses ThreadPoolExecutor for concurrent processing.
Each worker calls analyze_video for one video_id.
"""

import os
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from csf.batch_status import is_complete, mark_complete, mark_failed
from csf.cache import has_cached_transcript
from csf.logging import log_action

# Lazy-loaded reference to analyze_video (set once, can be mocked)
_analyze_video_ref: Callable[..., Any] | None = None


def _get_analyze_video() -> Callable[..., Any]:
    """Get the analyze_video function, loading it lazily once."""
    global _analyze_video_ref
    if _analyze_video_ref is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "csf_analyze",
            str(Path(__file__).parent.parent / "bin" / "csf-analyze"),
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load csf-analyze module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = module.analyze_video
        _analyze_video_ref = fn
    assert _analyze_video_ref is not None
    return _analyze_video_ref


def analyze_videos_parallel(
    video_ids: list[str],
    max_workers: int = 4,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    force: bool = False,
) -> Tuple[Dict[str, dict], List[str]]:
    """Analyze multiple videos in parallel using ThreadPoolExecutor.

    Args:
        video_ids: List of YouTube video IDs to analyze.
        max_workers: Maximum number of parallel workers. Bounded at min(os.cpu_count() or 4, 8).
        progress_callback: Optional callback(pending, done, failed, cached) called after each
            video completes. pending=remaining, done=successful count, failed=failure count,
            cached=successful count (alias for done). Enables --progress flag.
        force: If False (default), skip videos already marked 'complete' in the batch status DB
            (idempotent restart). If True, process all videos regardless of status.

    Returns:
        Tuple of (successful_results: dict, failed_video_ids: list).
        successful_results is a dict mapping video_id -> analysis result dict.
        failed_video_ids is a list of video IDs that failed to analyze.
        If batch_timeout is reached, in-progress videos are cancelled and added to failed_video_ids.
    """

    effective_workers = min(os.cpu_count() or 4, 8, max_workers)

    # PROC-02: Filter out already-complete videos unless force=True
    if not force:
        video_ids = [vid for vid in video_ids if not is_complete(vid)]

    successful_results: Dict[str, dict] = {}
    failed_video_ids: List[str] = []
    total = len(video_ids)
    completed = 0

    def _analyze_one(video_id: str) -> Tuple[str, dict | None, bool, str | None]:
        """Analyze a single video, returning (video_id, result or None, success, error_detail)."""
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            analyze_video = _get_analyze_video()
            # If transcript is already cached, use transcript-only mode to skip expensive
            # Gemini API calls. This is the Path B optimization: reuse cached transcripts.
            if has_cached_transcript(video_id):
                result: dict = analyze_video(video_id, video_url, mode="transcript")  # type: ignore[assignment]
            else:
                result = analyze_video(video_id, video_url)  # type: ignore[assignment]
            return (video_id, result, True, None)
        except Exception as e:
            log_action("batch_analyze_error", {"video_id": video_id, "error": repr(e)})
            return (video_id, None, False, f"{type(e).__name__}: {e}")

    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        futures = {executor.submit(_analyze_one, vid): vid for vid in video_ids}
        # Use wait() with timeout to prevent indefinite hangs from stalled workers
        done, not_done = wait(
            futures, timeout=getattr(analyze_videos_parallel, "_default_timeout", 7200)
        )
        for future in done:
            video_id, result, success, _err = future.result()
            if success and result is not None:
                successful_results[video_id] = result
                mark_complete(video_id)
            else:
                failed_video_ids.append(video_id)
                mark_failed(video_id)
            completed += 1
            if progress_callback:
                pending = total - completed
                progress_callback(
                    pending,
                    len(successful_results),
                    len(failed_video_ids),
                    len(successful_results),
                )
        # Cancel any still-running futures and mark their video_ids as failed
        for future in not_done:
            vid = futures[future]
            future.cancel()
            if vid not in failed_video_ids:
                failed_video_ids.append(vid)
                mark_failed(vid)
            completed += 1
            if progress_callback:
                pending = total - completed
                progress_callback(
                    pending,
                    len(successful_results),
                    len(failed_video_ids),
                    len(successful_results),
                )

    return (successful_results, failed_video_ids)
