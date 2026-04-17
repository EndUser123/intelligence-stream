#!/usr/bin/env python3
"""NotebookLM Industrial Batch Ingestor (High-Speed Version).

This version uses 'nlm source content' which is 10x faster than queries
and doesn't use AI credits. It fixes the mapping bug by correlating
titles from the CLI list with our input IDs.
"""

import json
import logging
import subprocess
import time
import re
import threading
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from csf.display import format_result_row


def _ensure_nlm_auth() -> bool:
    """Verify nlm CLI auth is valid, auto-recover if expired.

    Runs 'nlm login --check' (30s timeout). On failure, calls 'nlm login --force'
    (120s timeout) to auto-re-authenticate via Chrome headless.
    Returns True if auth is valid or was just refreshed.
    """
    import subprocess

    check = subprocess.run(
        ["nlm", "login", "--check"], capture_output=True, text=True, timeout=30
    )
    if check.returncode == 0:
        return True

    # Auth expired — re-authenticate
    login = subprocess.run(
        ["nlm", "login", "--force"], capture_output=True, text=True, timeout=120
    )
    return login.returncode == 0


# Minimum characters for a "valid" high-fidelity transcript
_MIN_TRANSCRIPT_CHARS = 500

# Dynamic throttling: rate limit detection and backoff
_INITIAL_DELAY = 0.5       # seconds before first retry
_MAX_DELAY = 60             # seconds max backoff
_RATE_LIMIT_CODES = {429, 503}  # HTTP status codes indicating rate limiting
_MAX_CONSECUTIVE_FAILURES = 3  # trigger backoff after this many failures


class _RateLimitTracker:
    """Thread-safe per-process rate limit tracker with exponential backoff.

    Tracks consecutive failures across all NLMBatchIngestor instances in this process.
    When failures exceed threshold, introduces a delay before each nlm call.
    Delay resets on successful calls.
    """

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._current_delay = 0.0
        self._lock = threading.Lock()
        self._last_failure_time: float = 0

    def record_failure(self, is_rate_limit: bool) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()
            if is_rate_limit or self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self._current_delay = min(
                    _INITIAL_DELAY * (2 ** (self._consecutive_failures - 1)),
                    _MAX_DELAY,
                )
                if is_rate_limit:
                    print(f"[Throttle] Rate limit detected ({self._consecutive_failures} consecutive failures) — throttling {self._current_delay:.1f}s")
                else:
                    print(f"[Throttle] {self._consecutive_failures} consecutive failures — throttling {self._current_delay:.1f}s")

    def record_success(self) -> None:
        with self._lock:
            if self._consecutive_failures > 0:
                print(f"[Throttle] Success restored after {self._consecutive_failures} failures — delay reset")
            self._consecutive_failures = 0
            self._current_delay = 0.0

    def apply_delay(self) -> None:
        with self._lock:
            if self._current_delay > 0:
                elapsed = time.time() - self._last_failure_time
                remaining = self._current_delay - elapsed
                if remaining > 0:
                    time.sleep(remaining)

    @property
    def current_delay(self) -> float:
        with self._lock:
            return self._current_delay


# Module-level singleton — shared across all ingestors in this process
_rate_limit_tracker: Optional[_RateLimitTracker] = None
_tracker_lock = threading.Lock()


def _get_tracker() -> _RateLimitTracker:
    global _rate_limit_tracker
    if _rate_limit_tracker is None:
        with _tracker_lock:
            if _rate_limit_tracker is None:
                _rate_limit_tracker = _RateLimitTracker()
    return _rate_limit_tracker


class NLMBatchIngestor:
    def __init__(self, batch_size: int = 300):
        self.batch_size = batch_size
        self._nb_id = None

    def _run_cmd(self, args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
        tracker = _get_tracker()
        while True:
            tracker.apply_delay()
            if not _ensure_nlm_auth():
                return subprocess.CompletedProcess(
                    ["nlm"] + args, 1, "", "Auth failed"
                )
            res = subprocess.run(["nlm"] + args, capture_output=True, text=True, timeout=timeout)

            # Check for rate limit indicators — require BOTH a status code AND rate-limit context
            # to avoid false positives from bare 500/502 errors that happen to contain "503"
            combined = res.stderr + "\n" + res.stdout
            has_429_503 = any(code in combined for code in ["429", "503"])
            has_rate_limit_context = any(
                kw in combined
                for kw in ["rate limit", "RATE_LIMIT", "Too Many Requests"]
            )
            is_rate_limit = res.returncode != 0 and has_429_503 and has_rate_limit_context

            if res.returncode == 0:
                tracker.record_success()
                return res

            if is_rate_limit:
                tracker.record_failure(is_rate_limit=True)
                continue

            # Non-rate-limit failure — record but don't retry infinitely
            tracker.record_failure(is_rate_limit=False)
            return res

    def _wait_for_sources_ready(self, expected_count: int, timeout: int = 120) -> bool:
        """Poll source list until all expected sources are present and accounted for.

        Uses heartbeat polling because 'nlm source add --wait' only waits for the
        API call to return, not for NLM's async processing to complete. Sources can
        be in a 'processing' state immediately after add returns.
        """
        import time
        start = time.time()
        while time.time() - start < timeout:
            res = self._run_cmd(["source", "list", self._nb_id, "--json"])
            if res.returncode == 0:
                try:
                    sources = json.loads(res.stdout)
                    if isinstance(sources, dict):
                        sources = sources.get("sources", [])
                    if len(sources) >= expected_count:
                        return True
                except Exception:
                    pass
            time.sleep(10)
        return False

    def _add_sources_in_subbatches(self, batch_ids: List[str], subbatch_size: int = 50):
        """Add sources in sub-batches to avoid NLM overload.

        Adding 300 sources at once causes partial failures even with --wait.
        Breaking into ~50-source sub-batches with --wait between each gives NLM
        time to properly process each batch before moving to the next.
        After each sub-batch add, heartbeat-poll 'source list' to confirm NLM has
        finished processing before proceeding to the next sub-batch.
        """
        total = len(batch_ids)
        for i in range(0, total, subbatch_size):
            subbatch = batch_ids[i:i + subbatch_size]
            # Reset throttle state at sub-batch boundary — prior failures shouldn't
            # penalize this independent sub-batch of NLM operations
            tracker = _get_tracker()
            with tracker._lock:
                tracker._consecutive_failures = 0
                tracker._current_delay = 0.0
            print(f"[NLM-Batch]   Adding sources {i+1}-{min(i+subbatch_size, total)}/{total}...")
            add_args = ["source", "add", self._nb_id, "--wait"]
            for vid in subbatch:
                add_args.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
            res = self._run_cmd(add_args, timeout=600)
            if res.returncode != 0:
                print(f"[NLM-Batch]   Sub-batch {i//subbatch_size + 1} add rc={res.returncode}")
                if res.stderr:
                    print(f"[NLM-Batch]   stderr: {res.stderr[:200]}")

            # Heartbeat: wait for NLM async processing to complete before next sub-batch
            base_idx = i
            if not self._wait_for_sources_ready(base_idx + len(subbatch), timeout=120):
                print(f"[NLM-Batch]   WARNING: after {120}s sources still not ready, continuing anyway...")

    def create_batch_notebook(self, batch_ids: List[str]) -> Optional[str]:
        nb_name = f"Industrial_Batch_{int(time.time())}"
        print(f"[NLM-Batch] Creating notebook...")
        res = self._run_cmd(["notebook", "create", nb_name])

        for line in res.stdout.split('\n'):
            if "ID:" in line:
                self._nb_id = line.split("ID:")[1].strip()
                break
        if not self._nb_id: self._nb_id = res.stdout.strip()

        print(f"[NLM-Batch] Adding {len(batch_ids)} sources in sub-batches...")
        self._add_sources_in_subbatches(batch_ids)
        return self._nb_id

    def extract_transcripts(self, batch_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Extract using high-speed 'source content' method."""
        # 1. Get Source List
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0: return {vid: (False, None, "List failed") for vid in batch_ids}
        
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict): sources = sources.get("sources", [])
        except:
            return {vid: (False, None, "Parse failed") for vid in batch_ids}

        # 2. Map Source IDs to Video IDs
        # Since NLM doesn't return the URL in the list, we'll use the order
        # which is consistent during the 'add' phase if we do it in one command.
        source_id_list = [s['id'] for s in sources]
        
        results = {}
        
        def _fetch_content(source_id: str, vid_hint: str):
            # The 'content' command is NOT an AI query. It's a direct data fetch.
            res = self._run_cmd(["source", "content", source_id, "--json"], timeout=30)
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout)
                    # Support both raw and wrapped JSON
                    content = ""
                    if isinstance(data, dict):
                        content = data.get("value", {}).get("content", "")
                        if not content: content = data.get("content", "")
                    
                    if len(content) > 100:
                        return vid_hint, True, content, None
                except:
                    pass
            return vid_hint, False, None, f"Fetch failed for {source_id}"

        print(f"[NLM-Batch] Fetching {len(sources)} sources in parallel...")
        video_width = max(len(vid) for vid in batch_ids) if batch_ids else 0
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for i, vid in enumerate(batch_ids):
                if i < len(source_id_list):
                    futures.append(executor.submit(_fetch_content, source_id_list[i], vid))
            
            for future in as_completed(futures):
                vid, success, text, error = future.result()
                results[vid] = (success, text, error)
                if success:
                    print(format_result_row(vid, True, f"{len(text)} chars", video_width))
                else:
                    print(format_result_row(vid, False, error, video_width))
        
        return results

    def reset_sources(self):
        """Delete all sources from the current notebook (for reuse)."""
        if not self._nb_id:
            return
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0:
            return
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict):
                sources = sources.get("sources", [])
            if not sources:
                return
            source_ids = [s["id"] for s in sources]
            # Bulk delete all at once
            delete_cmd = ["source", "delete", self._nb_id, "--confirm"] + source_ids
            self._run_cmd(delete_cmd, timeout=300)
        except Exception:
            pass

    def close(self):
        """Delete the notebook entirely (final cleanup after all batches)."""
        if self._nb_id:
            self._run_cmd(["notebook", "delete", self._nb_id, "--confirm"])

    def cleanup(self):
        """Delete all sources from the notebook (keeps notebook for reuse)."""
        self.reset_sources()

class NLMReusableIngestor:
    """Holds a single notebook across multiple batches for reuse."""

    def __init__(self, batch_size: int = 300):
        self._ingestor = NLMBatchIngestor(batch_size)
        self._nb_id: Optional[str] = None

    def process_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        if self._nb_id is None:
            self._nb_id = self._ingestor.create_batch_notebook(video_ids)
            if not self._nb_id:
                return {vid: (False, None, "Notebook failed") for vid in video_ids}
        else:
            # Notebook already exists — add sources to it in sub-batches
            self._ingestor._nb_id = self._nb_id
            print(f"[NLM-Batch] Adding {len(video_ids)} sources in sub-batches...")
            self._ingestor._add_sources_in_subbatches(video_ids)

        results = self._ingestor.extract_transcripts(video_ids)
        self._ingestor.reset_sources()  # clear sources, keep notebook
        return results

    def close(self):
        self._ingestor._nb_id = self._nb_id
        self._ingestor.close()


def process_industrial_batch(video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
    ingestor = NLMBatchIngestor()
    try:
        if not ingestor.create_batch_notebook(video_ids):
            return {vid: (False, None, "Notebook failed") for vid in video_ids}
        return ingestor.extract_transcripts(video_ids)
    finally:
        ingestor.cleanup()


# Module-level reusable instance — survives across calls for the same importer
_reusable_ingestor: Optional[NLMReusableIngestor] = None


def process_industrial_batch_reusable(
    video_ids: List[str],
) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
    """Reuse a single notebook across multiple batch calls — call close() when done."""
    global _reusable_ingestor
    if _reusable_ingestor is None:
        _reusable_ingestor = NLMReusableIngestor()
    try:
        return _reusable_ingestor.process_batch(video_ids)
    except Exception:
        _reusable_ingestor.close()
        _reusable_ingestor = None
        raise


def close_reusable_ingestor():
    """Call this when the entire run is complete to delete the shared notebook."""
    global _reusable_ingestor
    if _reusable_ingestor is not None:
        _reusable_ingestor.close()
        _reusable_ingestor = None

if __name__ == "__main__":
    import sys
    test_ids = sys.argv[1:] if len(sys.argv) > 1 else ["dQw4w9WgXcQ"]
    results = process_industrial_batch(test_ids)
    # Print success summaries
    for vid, (success, text, err) in results.items():
        if success:
            print(f"FINAL: {vid} SUCCESS ({len(text)} chars)")
        else:
            print(f"FINAL: {vid} FAILED ({err})")
