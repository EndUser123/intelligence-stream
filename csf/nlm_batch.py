#!/usr/bin/env python3
"""NotebookLM Industrial Batch Ingestor (High-Speed Version).

This version uses 'nlm source content' which is 10x faster than queries
and doesn't use AI credits. It fixes the mapping bug by correlating
titles from the CLI list with our input IDs.
"""

import json
import logging
import os
import subprocess
import time
import re
import threading
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import fasteners
from csf.batch_status import summarize_video_ids
from csf.display import format_result_row
from csf.csf_logging import log_action


_DEFAULT_REUSABLE_NOTEBOOK_STATE_PATH = Path("P:/__csf/.data/yt-is/reusable_nlm_notebook.json")
_DEFAULT_REUSABLE_NOTEBOOK_TITLE = "yt-is::industrial::reusable"
_DEFAULT_INDUSTRIAL_WORKER_STATE_ROOT = Path("P:/__csf/.data/yt-is/industrial-worker-states")
_DEFAULT_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX = "yt-is::industrial::worker"
_DEFAULT_NOTEBOOKLM_PROFILE = "default"
_AUTH_LOCK_PATH = Path("P:/__csf/.data/yt-is/locks/nlm-auth.lock")
DEFAULT_NOTEBOOKLM_BATCH_SIZE = 200
DEFAULT_NOTEBOOKLM_SOURCE_CAP = 225
_NOTEBOOK_SOURCE_CAP = DEFAULT_NOTEBOOKLM_SOURCE_CAP  # Rotate before the 300-source notebook ceiling and stay below the single-call add cliff.


def _get_reusable_notebook_state_path() -> Path:
    override = os.getenv("YTIS_NLM_REUSABLE_STATE_PATH", "").strip()
    return Path(override) if override else _DEFAULT_REUSABLE_NOTEBOOK_STATE_PATH


def _get_reusable_notebook_title() -> str:
    override = os.getenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "").strip()
    return override or _DEFAULT_REUSABLE_NOTEBOOK_TITLE


def _get_worker_run_id() -> str:
    return os.getenv("YTIS_INDUSTRIAL_RUN_ID", "").strip()


def _get_notebooklm_profile() -> str:
    override = os.getenv("NOTEBOOKLM_PROFILE", "").strip()
    return override or _DEFAULT_NOTEBOOKLM_PROFILE


def _load_reusable_notebook_id() -> Optional[str]:
    try:
        state_path = _get_reusable_notebook_state_path()
        if not state_path.exists():
            return None
        data = json.loads(state_path.read_text(encoding="utf-8"))
        nb_id = (data.get("nb_id") or "").strip()
        return nb_id or None
    except Exception:
        return None


def _save_reusable_notebook_id(nb_id: str) -> None:
    try:
        state_path = _get_reusable_notebook_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "nb_id": nb_id,
                    "title": _get_reusable_notebook_title(),
                    "run_id": _get_worker_run_id() or None,
                    "updated_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_reusable_notebook_state() -> None:
    try:
        state_path = _get_reusable_notebook_state_path()
        if state_path.exists():
            state_path.unlink()
    except Exception:
        pass


def _delete_notebook_with_retries(
    ingestor,
    nb_id: str,
    *,
    timeout: int = 120,
    retries: int = 2,
    purpose: str = "cleanup",
) -> subprocess.CompletedProcess:
    """Delete a notebook with bounded retries for transient NotebookLM failures."""
    last_result: subprocess.CompletedProcess | None = None
    total_attempts = retries + 1
    for attempt in range(1, total_attempts + 1):
        log_action(
            "nlm_batch_notebook_delete_attempt",
            {
                "nb_id": nb_id,
                "attempt": attempt,
                "total_attempts": total_attempts,
                "timeout_s": timeout,
                "purpose": purpose,
            },
        )
        try:
            result = ingestor._run_cmd(["notebook", "delete", nb_id, "--confirm"], timeout=timeout)
        except Exception as exc:
            result = subprocess.CompletedProcess(
                ["nlm", "notebook", "delete", nb_id, "--confirm"],
                1,
                "",
                str(exc),
            )
        last_result = result
        if result.returncode == 0:
            return result
        if attempt < total_attempts:
            time.sleep(min(5 * attempt, 15))
    log_action(
        "nlm_batch_notebook_delete_failed",
        {
            "nb_id": nb_id,
            "attempts": total_attempts,
            "timeout_s": timeout,
            "purpose": purpose,
            "returncode": None if last_result is None else last_result.returncode,
            "stderr": "" if last_result is None else (last_result.stderr or "")[:200],
        },
    )
    return last_result or subprocess.CompletedProcess(
        ["nlm", "notebook", "delete", nb_id, "--confirm"],
        1,
        "",
        "delete failed",
    )


def retire_reusable_notebook_state() -> dict[str, object]:
    """Delete the currently recorded reusable notebook and clear its state file.

    This is intended for worker startup when we want a clean notebook for a fresh
    run but still want to retire the notebook from the previous run instead of
    silently leaving it behind.
    """
    nb_id = _load_reusable_notebook_id()
    state_path = _get_reusable_notebook_state_path()
    notebooklm_profile = _get_notebooklm_profile()
    result: dict[str, object] = {
        "nb_id": nb_id,
        "state_path": str(state_path),
        "notebooklm_profile": notebooklm_profile,
    }
    if not nb_id:
        _clear_reusable_notebook_state()
        result["status"] = "empty"
        return result

    ingestor = NLMBatchIngestor()
    ingestor._nb_id = nb_id
    try:
        started = time.monotonic()
        res = _delete_notebook_with_retries(
            ingestor,
            nb_id,
            timeout=120,
            retries=2,
            purpose="retire_reusable",
        )
        result["returncode"] = res.returncode
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        result["status"] = "deleted" if res.returncode == 0 else "delete_failed"
        if res.returncode != 0:
            result["stdout"] = (res.stdout or "")[:200]
            result["stderr"] = (res.stderr or "")[:200]
    except Exception as exc:
        result["status"] = "delete_failed"
        result["error"] = str(exc)
    finally:
        _clear_reusable_notebook_state()
    return result


def _get_worker_state_root() -> Path:
    override = os.getenv("YTIS_INDUSTRIAL_WORKER_STATE_ROOT", "").strip()
    return Path(override) if override else _DEFAULT_INDUSTRIAL_WORKER_STATE_ROOT


def _get_worker_notebook_prefix() -> str:
    override = os.getenv("YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX", "").strip()
    return override or _DEFAULT_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX


def _infer_worker_profile_from_notebook_name(name: str) -> str:
    match = re.search(r"worker-(\d+)$", name.strip())
    if not match:
        return _get_notebooklm_profile()
    worker_idx = int(match.group(1))
    return f"ytis-worker-{worker_idx:02d}"


def _load_current_worker_notebook_ids() -> set[str]:
    state_root = _get_worker_state_root()
    expected_run_id = _get_worker_run_id()
    ids: set[str] = set()
    if not state_root.exists():
        return ids
    for state_path in state_root.glob("worker-*.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if expected_run_id:
                run_id = (data.get("run_id") or "").strip()
                if run_id != expected_run_id:
                    continue
            nb_id = (data.get("nb_id") or "").strip()
            if nb_id:
                ids.add(nb_id)
        except Exception:
            continue
    return ids


def cleanup_stale_worker_notebooks() -> tuple[int, int]:
    """Delete worker notebooks that are no longer referenced by state files."""
    ingestor = NLMBatchIngestor()
    active_nb_ids = _load_current_worker_notebook_ids()
    prefix = _get_worker_notebook_prefix()
    run_id = _get_worker_run_id()
    log_action(
        "nlm_worker_notebook_cleanup_started",
        {
            "state_root": str(_get_worker_state_root()),
            "notebook_prefix": prefix,
            "run_id": run_id or None,
            "active_nb_ids": len(active_nb_ids),
        },
    )
    res = ingestor._run_cmd(["notebook", "list", "--json"], timeout=30)
    if res.returncode != 0:
        log_action(
            "nlm_worker_notebook_cleanup_complete",
            {
                "deleted": 0,
                "failed": 0,
                "status": "list_failed",
                "stderr": (res.stderr or "")[:200],
            },
        )
        return (0, 0)

    try:
        notebooks = json.loads(res.stdout)
        if isinstance(notebooks, dict):
            notebooks = notebooks.get("notebooks", [])
    except Exception as exc:
        log_action(
            "nlm_worker_notebook_cleanup_complete",
            {
                "deleted": 0,
                "failed": 0,
                "status": "parse_failed",
                "error": str(exc),
            },
        )
        return (0, 0)

    deleted = 0
    failed = 0
    cdp_needed = False  # True if any CLI delete failed — run CDP once at end
    for nb in notebooks:
        name = ""
        if isinstance(nb, dict):
            name = (nb.get("name") or nb.get("title") or nb.get("notebookTitle") or "").strip()
        nb_id = (nb.get("id") or nb.get("notebookId") or "").strip() if isinstance(nb, dict) else ""
        if not nb_id or not name.startswith(prefix):
            continue
        if nb_id in active_nb_ids:
            continue
        print(f"[NLM-Batch]   Removing stale worker notebook '{name}' ({nb_id})...")
        worker_profile = _infer_worker_profile_from_notebook_name(name)
        old_profile = _get_notebooklm_profile()
        try:
            os.environ["NOTEBOOKLM_PROFILE"] = worker_profile
            ingestor._nb_id = nb_id
            try:
                ingestor.reset_sources()
            except Exception:
                pass
            result = _delete_notebook_with_retries(
                ingestor,
                nb_id,
                timeout=180,
                retries=3,
                purpose="cleanup_stale_worker_notebooks",
            )
        finally:
            if old_profile:
                os.environ["NOTEBOOKLM_PROFILE"] = old_profile
            else:
                os.environ.pop("NOTEBOOKLM_PROFILE", None)
        if result.returncode == 0:
            deleted += 1
        else:
            failed += 1
            cdp_needed = True

    # CDP fallback: run exactly once after all CLI attempts if any failed.
    # nlm-puppeteer.js --delete-worker finds all stale worker notebooks and
    # deletes them via 3-step UI click, bypassing the API layer that times out
    # on the CLI path. Safe to run even if all CLI deletes succeeded (it's a
    # no-op when no stale notebooks remain).
    if cdp_needed:
        cdp_script = Path(__file__).parent.parent / "bin" / "nlm-puppeteer.js"
        try:
            cdp_res = subprocess.run(
                ["node", str(cdp_script), "--delete-worker"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if cdp_res.returncode == 0:
                # CDP deleted all stale worker notebooks in one pass.
                # Re-scan to get accurate deleted/failed counts.
                res2 = ingestor._run_cmd(["notebook", "list", "--json"], timeout=30)
                if res2.returncode == 0:
                    try:
                        remaining = json.loads(res2.stdout)
                        if isinstance(remaining, dict):
                            remaining = remaining.get("notebooks", [])
                    except Exception:
                        remaining = []
                    stale_after = [
                        nb for nb in remaining
                        if isinstance(nb, dict) and
                        (nb.get("name") or nb.get("title") or "").strip().startswith(prefix)
                        and (nb.get("id") or "").strip() not in active_nb_ids
                    ]
                    deleted = (len(remaining) - len(stale_after)) if remaining else 0
                    failed = len(stale_after)
                    log_action(
                        "nlm_worker_notebook_cleanup_cdp_fallback",
                        {
                            "cdp_stdout": (cdp_res.stdout or "")[:300],
                            "stale_remaining": len(stale_after),
                        },
                    )
                else:
                    log_action(
                        "nlm_worker_notebook_cleanup_cdp_fallback_rescan_failed",
                        {"cdp_stderr": (cdp_res.stderr or "")[:200]},
                    )
            else:
                log_action(
                    "nlm_worker_notebook_cleanup_cdp_fallback_failed",
                    {"cdp_stderr": (cdp_res.stderr or "")[:200]},
                )
        except Exception as exc:
            log_action(
                "nlm_worker_notebook_cleanup_cdp_fallback_error",
                {"error": str(exc)},
            )

    log_action(
        "nlm_worker_notebook_cleanup_complete",
        {
            "deleted": deleted,
            "failed": failed,
            "status": "ok",
            "active_nb_ids": len(active_nb_ids),
            "notebook_prefix": prefix,
            "run_id": run_id or None,
        },
    )
    return (deleted, failed)


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
        log_action("nlm_auth_checked", {"component": "nlm_batch", "status": "ok"})
        return True

    # Auth expired — serialize refresh so multiple workers do not launch
    # duplicate browser login flows at the same time.
    _AUTH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(_AUTH_LOCK_PATH)):
        check = subprocess.run(
            ["nlm", "login", "--check"], capture_output=True, text=True, timeout=30
        )
        if check.returncode == 0:
            log_action("nlm_auth_checked", {"component": "nlm_batch", "status": "ok"})
            return True

        login_started = time.perf_counter()
        log_action(
            "nlm_login_started",
            {"component": "nlm_batch", "mode": "force", "status": "started"},
        )
        login = subprocess.run(
            ["nlm", "login", "--force"], capture_output=True, text=True, timeout=120
        )
        login_elapsed = round(time.perf_counter() - login_started, 3)
        if login.returncode == 0:
            log_action(
                "nlm_login_completed",
                {
                    "component": "nlm_batch",
                    "mode": "force",
                    "status": "ok",
                    "elapsed_s": login_elapsed,
                },
            )
            log_action("nlm_auth_refreshed", {"component": "nlm_batch", "status": "ok"})
            return True
        log_action(
            "nlm_login_failed",
            {
                "component": "nlm_batch",
                "mode": "force",
                "status": "failed",
                "elapsed_s": login_elapsed,
                "returncode": login.returncode,
            },
        )
        log_action("nlm_auth_failed", {"component": "nlm_batch", "status": "refresh_failed"})
        return False


# Minimum characters for a "valid" high-fidelity transcript
_MIN_TRANSCRIPT_CHARS = 500
_MAX_SUBBATCH_RETRY_DEPTH = 4

# Dynamic throttling: rate limit detection and backoff
_INITIAL_DELAY = 0.5       # seconds before first retry
_MAX_DELAY = 60             # seconds max backoff
_RATE_LIMIT_CODES = {429, 503}  # HTTP status codes indicating rate limiting
_MAX_CONSECUTIVE_FAILURES = 3  # trigger backoff after this many failures


def _classify_subbatch_add_failure(
    res: subprocess.CompletedProcess,
    *,
    materialization_waited: bool,
) -> str:
    stderr = (res.stderr or "").lower()
    stdout = (res.stdout or "").lower()
    text = f"{stdout}\n{stderr}"
    if "auth failed" in text or "authentication error" in text:
        return "auth_failed"
    if "could not add url sources" in text or "could not add" in text:
        return "source_add_failed"
    if "429" in text or "503" in text or "rate limit" in text:
        return "rate_limited"
    if materialization_waited and res.returncode == 0:
        return "materialization_wait_failed"
    if res.returncode != 0:
        return "add_failed"
    return "unknown"


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
    def __init__(self, batch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE):
        self.batch_size = batch_size
        self._nb_id = None
        self._last_added_video_ids: List[str] = []
        self._last_subbatch_metrics: list[dict[str, object]] = []
        self._last_add_failure_reason: Optional[str] = None
        self._last_add_returncode: Optional[int] = None
        self._last_add_cmd_elapsed_s: float = 0.0
        self._last_materialization_wait_elapsed_s: float = 0.0
        self._current_source_count: int = 0

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

            # Auth-error patterns in stderr (expired between _ensure_nlm_auth and command execution)
            is_auth_error = any(
                kw in combined
                for kw in ["Authentication Error", "authentication error", "Auth Error", "auth error"]
            )
            if is_auth_error:
                login = subprocess.run(
                    ["nlm", "login", "--force"],
                    capture_output=True, text=True, timeout=120,
                )
                if login.returncode == 0:
                    res = subprocess.run(
                        ["nlm"] + args, capture_output=True, text=True, timeout=timeout,
                    )
                    if res.returncode == 0:
                        tracker.record_success()
                        return res
                tracker.record_failure(is_rate_limit=False)
                return res

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
        poll_count = 0
        while time.time() - start < timeout:
            res = self._run_cmd(["source", "list", self._nb_id, "--json"])
            poll_count += 1
            if res.returncode == 0:
                try:
                    sources = json.loads(res.stdout)
                    if isinstance(sources, dict):
                        sources = sources.get("sources", [])
                    if len(sources) >= expected_count:
                        return True
                    if poll_count == 1 or poll_count % 3 == 0:
                        log_action(
                            "nlm_batch_source_materialization_wait_progress",
                            {
                                "nb_id": self._nb_id,
                                "expected_total": expected_count,
                                "observed_total": len(sources),
                                "poll_count": poll_count,
                                "elapsed_s": round(time.time() - start, 3),
                            },
                        )
                except Exception:
                    log_action(
                        "nlm_batch_source_materialization_wait_poll_failed",
                        {
                            "nb_id": self._nb_id,
                            "expected_total": expected_count,
                            "poll_count": poll_count,
                            "elapsed_s": round(time.time() - start, 3),
                            "stdout": (res.stdout or "")[:200],
                            "stderr": (res.stderr or "")[:200],
                        },
                    )
            else:
                log_action(
                    "nlm_batch_source_materialization_wait_poll_failed",
                    {
                        "nb_id": self._nb_id,
                        "expected_total": expected_count,
                        "poll_count": poll_count,
                        "elapsed_s": round(time.time() - start, 3),
                        "returncode": res.returncode,
                        "stdout": (res.stdout or "")[:200],
                        "stderr": (res.stderr or "")[:200],
                    },
                )
            time.sleep(10)
        log_action(
            "nlm_batch_source_materialization_wait_timeout",
            {
                "nb_id": self._nb_id,
                "expected_total": expected_count,
                "poll_count": poll_count,
                "elapsed_s": round(time.time() - start, 3),
            },
        )
        return False

    def _add_sources_chunk(
        self,
        batch_ids: List[str],
        *,
        subbatch_index: int,
        expected_total: int,
        retry_depth: int = 0,
        source_profile: Optional[dict[str, object]] = None,
    ) -> List[str]:
        """Add one chunk, recursively splitting on add failures.

        The NotebookLM CLI occasionally returns a nonzero exit code for a large
        add batch even though some sources were accepted. Splitting the failing
        chunk helps isolate transient add failures and narrows any true bad URLs
        to single-source retries instead of losing the whole 50-source block.
        """
        if not batch_ids:
            return []

        chunk_started_at = time.monotonic()
        self._last_add_failure_reason = None
        self._last_add_returncode = None
        self._last_add_cmd_elapsed_s = 0.0
        self._last_materialization_wait_elapsed_s = 0.0
        if source_profile is None:
            source_profile = summarize_video_ids(batch_ids)
        # Log source count before add — this is the diagnostic key for capacity correlation
        source_count_before = self._get_current_source_count()
        print(
            f"[NLM-Batch]   Adding sub-batch {subbatch_index} "
            f"({len(batch_ids)} sources, retry_depth={retry_depth}, "
            f"nb_sources_before={source_count_before})..."
        )
        log_action(
            "nlm_batch_subbatch_add_started",
            {
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
            },
        )
        add_args = ["source", "add", self._nb_id, "--wait"]
        for vid in batch_ids:
            add_args.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
        res = self._run_cmd(add_args, timeout=600)
        add_cmd_elapsed_s = round(time.monotonic() - chunk_started_at, 3)
        self._last_add_cmd_elapsed_s = add_cmd_elapsed_s
        self._last_add_returncode = res.returncode
        # Probe source count after add — key diagnostic for capacity correlation
        source_count_after = self._get_current_source_count()
        added_count = len(batch_ids) if res.returncode == 0 else 0
        log_action(
            "nlm_batch_subbatch_add_completed",
            {
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "returncode": res.returncode,
                "added_count": added_count,
                "elapsed_s": add_cmd_elapsed_s,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
                "source_count_after": source_count_after,
                "failure_reason": self._last_add_failure_reason,
                "stdout": (res.stdout or "")[:200],
                "stderr": (res.stderr or "")[:200],
            },
        )
        if res.returncode == 0:
            wait_started_at = time.monotonic()
            log_action(
                "nlm_batch_source_materialization_wait_started",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "source_profile": source_profile,
                    "source_count_before_wait": source_count_after,
                },
            )
            wait_succeeded = self._wait_for_sources_ready(expected_total, timeout=120)
            wait_elapsed_s = round(time.monotonic() - wait_started_at, 3)
            self._last_materialization_wait_elapsed_s = wait_elapsed_s
            if not wait_succeeded:
                print(f"[NLM-Batch]   WARNING: after {120}s sources still not ready, continuing anyway...")
                self._last_add_failure_reason = "materialization_wait_failed"
                log_action(
                    "nlm_batch_source_materialization_wait_failed",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "expected_total": expected_total,
                        "retry_depth": retry_depth,
                        "source_profile": source_profile,
                        "failure_reason": "materialization_wait_failed",
                        "elapsed_s": wait_elapsed_s,
                        "source_count_after_wait": self._get_current_source_count(),
                        "source_count_before_wait": source_count_after,
                    },
                )
            else:
                log_action(
                    "nlm_batch_source_materialization_wait_succeeded",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "expected_total": expected_total,
                        "retry_depth": retry_depth,
                        "source_profile": source_profile,
                        "elapsed_s": wait_elapsed_s,
                        "source_count_after_wait": self._get_current_source_count(),
                        "source_count_before_wait": source_count_after,
                    },
                )
            return list(batch_ids)

        print(
            f"[NLM-Batch]   Sub-batch {subbatch_index} add rc={res.returncode}"
            f" (retry_depth={retry_depth})"
        )
        if res.stderr:
            print(f"[NLM-Batch]   stderr: {res.stderr[:200]}")

        self._last_add_failure_reason = _classify_subbatch_add_failure(res, materialization_waited=False)
        log_action(
            "nlm_batch_subbatch_add_failed",
            {
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "returncode": res.returncode,
                "elapsed_s": add_cmd_elapsed_s,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
                "source_count_after": source_count_after,
                "failure_reason": _classify_subbatch_add_failure(res, materialization_waited=False),
                "stdout": (res.stdout or "")[:200],
                "stderr": (res.stderr or "")[:200],
            },
        )
        return []

    def _add_sources_in_subbatches(self, batch_ids: List[str], subbatch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE) -> List[str]:
        """Add sources in sub-batches to avoid NLM overload.

        The reusable industrial path defaults to a 200-source window, which
        was measured as the throughput-optimal setting for this backlog shape.
        Smaller or larger windows can still be passed explicitly for sweeps or
        recovery if needed.
        """
        total = len(batch_ids)
        added_ids: List[str] = []
        self._last_subbatch_metrics = []
        current_subbatch_size = max(1, subbatch_size)
        next_index = 0
        subbatch_index = 0
        while next_index < total:
            subbatch_index += 1
            window_size = min(current_subbatch_size, total - next_index)
            source_count_before = self._get_current_source_count()
            self._current_source_count = source_count_before
            if source_count_before >= _NOTEBOOK_SOURCE_CAP:
                log_action(
                    "nlm_batch_subbatch_capacity_rotation_requested",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "current_source_count": source_count_before,
                        "cap_threshold": _NOTEBOOK_SOURCE_CAP,
                        "requested_subbatch_size": window_size,
                        "remaining": total - next_index,
                        "rotation_reason": "source_cap_near_threshold",
                    },
                )
                self._rotate_notebook()
                source_count_before = self._current_source_count
            capacity_remaining = max(0, _NOTEBOOK_SOURCE_CAP - source_count_before)
            if 0 < capacity_remaining < window_size:
                log_action(
                    "nlm_batch_subbatch_size_adjusted",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "requested_subbatch_size": window_size,
                        "adjusted_subbatch_size": capacity_remaining,
                        "current_source_count": source_count_before,
                        "cap_threshold": _NOTEBOOK_SOURCE_CAP,
                        "remaining": total - next_index,
                        "rotation_reason": "capacity_headroom",
                    },
                )
                window_size = capacity_remaining
            subbatch = batch_ids[next_index:next_index + window_size]
            # Reset throttle state at sub-batch boundary — prior failures shouldn't
            # penalize this independent sub-batch of NLM operations
            tracker = _get_tracker()
            with tracker._lock:
                tracker._consecutive_failures = 0
                tracker._current_delay = 0.0
            print(f"[NLM-Batch]   Adding sources {next_index+1}-{min(next_index+window_size, total)}/{total}...")
            source_profile = summarize_video_ids(subbatch)
            log_action(
                "nlm_batch_subbatch_size_selected",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": window_size,
                    "remaining": total - next_index,
                    "target_subbatch_size": current_subbatch_size,
                },
            )
            added_chunk_ids = self._add_sources_chunk(
                subbatch,
                subbatch_index=subbatch_index,
                expected_total=next_index + len(subbatch),
                source_profile=source_profile,
            )
            # Track running source count after each subbatch
            self._current_source_count = self._get_current_source_count()
            added_ids.extend(added_chunk_ids)
            subbatch_metrics = {
                "subbatch_index": subbatch_index,
                "subbatch_size": window_size,
                "target_subbatch_size": current_subbatch_size,
                "attempted_count": len(subbatch),
                "added_count": len(added_chunk_ids),
                "add_cmd_elapsed_s": float(getattr(self, "_last_add_cmd_elapsed_s", 0.0) or 0.0),
                "materialization_wait_elapsed_s": float(getattr(self, "_last_materialization_wait_elapsed_s", 0.0) or 0.0),
                "elapsed_s": float(
                    (getattr(self, "_last_add_cmd_elapsed_s", 0.0) or 0.0)
                    + (getattr(self, "_last_materialization_wait_elapsed_s", 0.0) or 0.0)
                ),
                "returncode": self._last_add_returncode,
                "failure_reason": self._last_add_failure_reason,
                "source_profile": source_profile,
                "current_source_count": self._current_source_count,
            }
            if len(added_chunk_ids) < len(subbatch):
                if self._current_source_count >= _NOTEBOOK_SOURCE_CAP:
                    log_action(
                        "nlm_batch_subbatch_shortfall_cap_triggered",
                        {
                            "nb_id": self._nb_id,
                            "subbatch_index": subbatch_index,
                            "current_source_count": self._current_source_count,
                            "cap_threshold": _NOTEBOOK_SOURCE_CAP,
                            "added_count": len(added_chunk_ids),
                            "attempted_count": len(subbatch),
                            "rotation_reason": "shortfall_cap",
                        },
                    )
                    self._rotate_notebook()
                    subbatch_metrics["status"] = "shortfall_cap_rotated"
                else:
                    log_action(
                        "nlm_batch_subbatch_add_shortfall",
                        {
                            "nb_id": self._nb_id,
                            "subbatch_index": subbatch_index,
                            "subbatch_size": window_size,
                            "added_count": len(added_chunk_ids),
                            "attempted_count": len(subbatch),
                            "elapsed_s": getattr(self, "_last_add_cmd_elapsed_s", 0.0)
                            + getattr(self, "_last_materialization_wait_elapsed_s", 0.0),
                            "source_profile": source_profile,
                            "sample_video_ids": subbatch[:5],
                            "current_source_count": self._current_source_count,
                        },
                    )
                    subbatch_metrics["status"] = "shortfall"
            elif self._last_add_failure_reason:
                subbatch_metrics["status"] = "warn"
            else:
                subbatch_metrics["status"] = "ok"
            self._last_subbatch_metrics.append(subbatch_metrics)
            next_index += window_size

        self._last_added_video_ids = added_ids
        return added_ids

    def create_batch_notebook(self, batch_ids: List[str]) -> Optional[str]:
        nb_name = _get_reusable_notebook_title()
        notebooklm_profile = _get_notebooklm_profile()
        self._last_added_video_ids = []
        self._last_subbatch_metrics = []
        print(f"[NLM-Batch] Creating notebook...")
        log_action(
            "nlm_batch_notebook_create_started",
            {
                "batch_size": len(batch_ids),
                "nb_name": nb_name,
                "notebooklm_profile": notebooklm_profile,
            },
        )
        res = self._run_cmd(["notebook", "create", nb_name])

        for line in res.stdout.split('\n'):
            if "ID:" in line:
                self._nb_id = line.split("ID:")[1].strip()
                break
        if not self._nb_id: self._nb_id = res.stdout.strip()
        if self._nb_id:
            log_action(
                "nlm_batch_notebook_create_succeeded",
                {
                    "batch_size": len(batch_ids),
                    "nb_id": self._nb_id,
                    "nb_name": nb_name,
                    "notebooklm_profile": notebooklm_profile,
                },
            )
        else:
            log_action(
                "nlm_batch_notebook_create_failed",
                {
                    "batch_size": len(batch_ids),
                    "nb_name": nb_name,
                    "notebooklm_profile": notebooklm_profile,
                    "stdout": (res.stdout or "")[:200],
                    "stderr": (res.stderr or "")[:200],
                },
            )

        print(f"[NLM-Batch] Adding {len(batch_ids)} sources in sub-batches...")
        self._add_sources_in_subbatches(batch_ids, subbatch_size=self.batch_size)
        return self._nb_id

    def extract_transcripts(self, batch_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Extract using high-speed 'source content' method."""
        start = time.time()
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
        log_action(
            "nlm_batch_extract_started",
            {
                "nb_id": self._nb_id,
                "batch_size": len(batch_ids),
                "sources_visible": len(sources),
            },
        )
        
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
        succeeded = sum(1 for ok, _, _ in results.values() if ok)
        log_action(
            "nlm_batch_extract_completed",
            {
                "nb_id": self._nb_id,
                "batch_size": len(batch_ids),
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
                "elapsed_s": round(time.time() - start, 3),
            },
        )

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
            # Delete in smaller chunks so NotebookLM does not time out on large notebooks.
            chunk_size = 25
            for start in range(0, len(source_ids), chunk_size):
                chunk = source_ids[start:start + chunk_size]
                delete_cmd = ["source", "delete", self._nb_id, "--confirm"] + chunk
                self._run_cmd(delete_cmd, timeout=300)
        except Exception:
            pass

    def close(self):
        """Delete the notebook entirely (final cleanup after all batches)."""
        if self._nb_id:
            _delete_notebook_with_retries(self, self._nb_id, timeout=120, retries=2, purpose="close")

    def _get_current_source_count(self) -> int:
        """Query the current source count in the active notebook."""
        if not self._nb_id:
            return 0
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0:
            return 0
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict):
                sources = sources.get("sources", [])
            return len(sources)
        except Exception:
            return 0

    def _rotate_notebook(self) -> None:
        """Close the current notebook and create a fresh one, logging the rotation event."""
        old_nb_id = self._nb_id
        old_count = self._current_source_count
        self.close()
        log_action(
            "nlm_batch_notebook_rotated",
            {
                "old_nb_id": old_nb_id,
                "old_source_count": old_count,
                "reason": "source_cap_near_threshold",
                "cap_threshold": _NOTEBOOK_SOURCE_CAP,
            },
        )
        # Recreate the notebook under the same logical title so cleanup can
        # retire the current worker notebook deterministically on restart.
        nb_name = _get_reusable_notebook_title()
        res = self._run_cmd(["notebook", "create", nb_name], timeout=60)
        for line in res.stdout.split("\n"):
            if "ID:" in line:
                self._nb_id = line.split("ID:")[1].strip()
                break
        if not self._nb_id:
            self._nb_id = res.stdout.strip()
        self._current_source_count = 0
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
            log_action(
                "nlm_batch_reusable_state_saved",
                {
                    "nb_id": self._nb_id,
                    "state_path": str(_get_reusable_notebook_state_path()),
                    "notebooklm_profile": _get_notebooklm_profile(),
                },
            )
        log_action(
            "nlm_batch_notebook_rotated_new_created",
            {
                "old_nb_id": old_nb_id,
                "new_nb_id": self._nb_id,
                "old_source_count": old_count,
                "nb_name": nb_name,
            },
        )

    def cleanup(self):
        """Delete all sources from the notebook (keeps notebook for reuse)."""
        self.reset_sources()

    def experiment_add_acceptance(
        self,
        batch_ids: List[str],
        subbatch_sizes: List[int],
        *,
        notebook_title: Optional[str] = None,
    ) -> list[dict[str, object]]:
        """Measure NotebookLM add acceptance across multiple sub-batch sizes.

        This is a disposable experiment helper. It creates a fresh notebook,
        runs the requested size sweep, records add acceptance, and then cleans up
        the notebook so the run does not affect the reusable worker path.
        """
        if not batch_ids:
            return []

        sizes = [max(1, int(size)) for size in subbatch_sizes if int(size) > 0]
        if not sizes:
            raise ValueError("subbatch_sizes must contain at least one positive integer")

        nb_name = notebook_title or f"{_get_worker_notebook_prefix()}::experiment::{int(time.time())}"
        results: list[dict[str, object]] = []
        started_at = time.monotonic()
        log_action(
            "nlm_batch_size_sweep_started",
            {
                "nb_name": nb_name,
                "batch_size": len(batch_ids),
                "sizes": sizes,
                "notebooklm_profile": _get_notebooklm_profile(),
            },
        )
        try:
            res = self._run_cmd(["notebook", "create", nb_name], timeout=60)
            nb_id = ""
            for line in res.stdout.split("\n"):
                if "ID:" in line:
                    nb_id = line.split("ID:")[1].strip()
                    break
            if not nb_id:
                nb_id = res.stdout.strip()
            if not nb_id:
                log_action(
                    "nlm_batch_size_sweep_failed",
                    {
                        "nb_name": nb_name,
                        "status": "create_failed",
                        "returncode": res.returncode,
                        "stdout": (res.stdout or "")[:200],
                        "stderr": (res.stderr or "")[:200],
                    },
                )
                return []

            self._nb_id = nb_id
            for size in sizes:
                add_started = time.monotonic()
                self._last_added_video_ids = []
                print(f"[NLM-Batch] Experimenting with sub-batch size {size}...")
                added_ids = self._add_sources_in_subbatches(batch_ids, subbatch_size=size)
                success_count = len(added_ids)
                attempted_count = len(batch_ids)
                acceptance_rate = round(success_count / attempted_count * 100, 2) if attempted_count else 0.0
                elapsed_s = round(time.monotonic() - add_started, 3)
                result = {
                    "nb_id": nb_id,
                    "batch_size": attempted_count,
                    "subbatch_size": size,
                    "added_count": success_count,
                    "attempted_count": attempted_count,
                    "acceptance_rate": acceptance_rate,
                    "elapsed_s": elapsed_s,
                    "notebooklm_profile": _get_notebooklm_profile(),
                }
                results.append(result)
                log_action(
                    "nlm_batch_size_sweep_result",
                    result,
                )
                # Clear any accepted sources before the next size so each
                # measurement is isolated to the same input set.
                self.reset_sources()
        finally:
            cleanup_started = time.monotonic()
            try:
                if self._nb_id:
                    self.close()
            finally:
                log_action(
                    "nlm_batch_size_sweep_completed",
                    {
                        "nb_id": self._nb_id,
                        "batch_size": len(batch_ids),
                        "sizes": sizes,
                        "elapsed_s": round(time.monotonic() - started_at, 3),
                        "cleanup_elapsed_s": round(time.monotonic() - cleanup_started, 3),
                        "notebooklm_profile": _get_notebooklm_profile(),
                    },
                )
                self._nb_id = None
        return results

class NLMReusableIngestor:
    """Holds a single notebook across multiple batches for reuse."""

    def __init__(self, batch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE):
        self._ingestor = NLMBatchIngestor(batch_size)
        self._nb_id: Optional[str] = _load_reusable_notebook_id()
        self._last_prepare_metrics: dict[str, object] | None = None
        self._last_process_metrics: dict[str, object] | None = None
        log_action(
            "nlm_batch_reusable_state_loaded",
            {
                "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
                "status": "loaded" if self._nb_id else "empty",
            },
        )

    def prepare(self) -> tuple[bool, str]:
        """Create or reuse the notebook, then clear it so the worker starts ready."""
        prep_started_at = time.monotonic()
        self._last_prepare_metrics = None
        log_action(
            "nlm_batch_reusable_prep_started",
            {
                "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
                "strategy": "reusable",
            },
        )
        created_new_notebook, setup_mode = self._ensure_notebook([])
        if not self._nb_id:
            log_action(
                "nlm_batch_reusable_prep_failed",
                {
                    "nb_id": None,
                    "state_path": str(_get_reusable_notebook_state_path()),
                    "notebooklm_profile": _get_notebooklm_profile(),
                    "setup_mode": setup_mode,
                    "strategy": "reusable",
                "status": "notebook_create_failed",
                "elapsed_s": round(time.monotonic() - prep_started_at, 3),
            },
        )
            self._last_prepare_metrics = {
                "created_new_notebook": created_new_notebook,
                "setup_mode": setup_mode,
                "notebook_check_elapsed_s": self._last_ensure_metrics.get("notebook_check_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "create_elapsed_s": self._last_ensure_metrics.get("create_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "retire_elapsed_s": self._last_ensure_metrics.get("retire_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "cleanup_elapsed_s": 0.0,
                "total_elapsed_s": round(time.monotonic() - prep_started_at, 3),
            }
            return False, setup_mode

        cleanup_started_at = time.monotonic()
        self._ingestor._nb_id = self._nb_id
        self._ingestor.cleanup()
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
            log_action(
                "nlm_batch_reusable_state_saved",
                {
                    "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
            },
        )
        self._last_prepare_metrics = {
            "created_new_notebook": created_new_notebook,
            "setup_mode": setup_mode,
            "notebook_check_elapsed_s": self._last_ensure_metrics.get("notebook_check_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "create_elapsed_s": self._last_ensure_metrics.get("create_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "retire_elapsed_s": self._last_ensure_metrics.get("retire_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "cleanup_elapsed_s": round(time.monotonic() - cleanup_started_at, 3),
            "total_elapsed_s": round(time.monotonic() - prep_started_at, 3),
        }
        log_action(
            "nlm_batch_reusable_prep_completed",
            {
                "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
                "setup_mode": setup_mode,
                "created_new_notebook": created_new_notebook,
                "cleanup_elapsed_s": round(time.monotonic() - cleanup_started_at, 3),
                "total_elapsed_s": round(time.monotonic() - prep_started_at, 3),
                "strategy": "reusable",
            },
        )
        return True, setup_mode

    def get_last_prepare_metrics(self) -> dict[str, object] | None:
        if self._last_prepare_metrics is None:
            return None
        return dict(self._last_prepare_metrics)

    def _is_notebook_usable(self) -> bool:
        if not self._nb_id:
            return False
        self._ingestor._nb_id = self._nb_id
        res = self._ingestor._run_cmd(["source", "list", self._nb_id, "--json"], timeout=60)
        return res.returncode == 0

    def _ensure_notebook(self, batch_ids: List[str]) -> Tuple[bool, str]:
        if self._nb_id and self._is_notebook_usable():
            self._ingestor._nb_id = self._nb_id
            self._last_ensure_metrics = {
                "notebook_check_elapsed_s": 0.0,
                "retire_elapsed_s": 0.0,
                "create_elapsed_s": 0.0,
            }
            return False, "reuse"

        if self._nb_id:
            log_action(
                "nlm_batch_reusable_state_stale",
                {
                    "nb_id": self._nb_id,
                    "state_path": str(_get_reusable_notebook_state_path()),
                    "notebooklm_profile": _get_notebooklm_profile(),
                },
            )
            try:
                self._ingestor._nb_id = self._nb_id
                stale_started = time.monotonic()
                self._ingestor.close()
                retire_elapsed_s = round(time.monotonic() - stale_started, 3)
                log_action(
                    "nlm_batch_reusable_state_retired",
                    {
                        "nb_id": self._nb_id,
                        "state_path": str(_get_reusable_notebook_state_path()),
                        "notebooklm_profile": _get_notebooklm_profile(),
                        "elapsed_s": retire_elapsed_s,
                    },
                )
            except Exception as exc:
                log_action(
                    "nlm_batch_reusable_state_retire_failed",
                    {
                        "nb_id": self._nb_id,
                        "state_path": str(_get_reusable_notebook_state_path()),
                        "notebooklm_profile": _get_notebooklm_profile(),
                        "error": str(exc),
                    },
                )
            self._nb_id = None
            _clear_reusable_notebook_state()
            self._last_ensure_metrics = {
                "notebook_check_elapsed_s": 0.0,
                "retire_elapsed_s": retire_elapsed_s if "retire_elapsed_s" in locals() else 0.0,
                "create_elapsed_s": 0.0,
            }

        create_started_at = time.monotonic()
        self._nb_id = self._ingestor.create_batch_notebook(batch_ids)
        create_elapsed_s = round(time.monotonic() - create_started_at, 3)
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
            log_action(
                "nlm_batch_reusable_state_saved",
                {
                    "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
            },
        )
        self._last_ensure_metrics = {
            "notebook_check_elapsed_s": 0.0,
            "retire_elapsed_s": self._last_ensure_metrics.get("retire_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "create_elapsed_s": create_elapsed_s,
        }
        return True, "create"

    def process_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        batch_started_at = time.monotonic()
        notebook_reused = self._nb_id is not None
        self._last_process_metrics = None
        self._last_process_stage_metrics = None
        log_action(
            "nlm_batch_reusable_process_started",
            {
                "batch_size": len(video_ids),
                "nb_id": self._nb_id,
                "notebook_reused": notebook_reused,
                "notebooklm_profile": _get_notebooklm_profile(),
                "subbatch_size": self._ingestor.batch_size,
                "strategy": "reusable",
            },
        )

        setup_started_at = time.monotonic()
        created_new_notebook, setup_mode = self._ensure_notebook(video_ids)
        log_action(
            "nlm_batch_reusable_process_ready",
            {
                "batch_size": len(video_ids),
                "nb_id": self._nb_id,
                "notebook_reused": notebook_reused,
                "created_new_notebook": created_new_notebook,
                "setup_mode": setup_mode,
                "notebooklm_profile": _get_notebooklm_profile(),
                "strategy": "reusable",
            },
        )
        if not self._nb_id:
            log_action(
                "nlm_batch_reusable_process_completed",
                {
                    "batch_size": len(video_ids),
                    "nb_id": None,
                    "notebook_reused": notebook_reused,
                    "notebooklm_profile": _get_notebooklm_profile(),
                    "setup_mode": "create",
                    "status": "notebook_create_failed",
                    "subbatch_size": self._ingestor.batch_size,
                    "strategy": "reusable",
                    "total_elapsed_s": round(time.monotonic() - batch_started_at, 3),
                },
            )
            return {vid: (False, None, "Notebook failed") for vid in video_ids}
        add_sources_elapsed_s = 0.0
        if not created_new_notebook:
            # Notebook already exists — add sources to it in sub-batches
            self._ingestor._nb_id = self._nb_id
            print(f"[NLM-Batch] Adding {len(video_ids)} sources in sub-batches...")
            add_sources_started_at = time.monotonic()
            self._ingestor._add_sources_in_subbatches(
                video_ids,
                subbatch_size=self._ingestor.batch_size,
            )
            add_sources_elapsed_s = round(time.monotonic() - add_sources_started_at, 3)
            setup_mode = "reuse_add"
        elif self._ingestor._last_added_video_ids is not None:
            add_sources_elapsed_s = 0.0
        setup_elapsed_s = round(time.monotonic() - setup_started_at, 3)

        extract_started_at = time.monotonic()
        results: Dict[str, Tuple[bool, Optional[str], Optional[str]]]
        cleanup_elapsed_s = 0.0
        try:
            added_video_ids = self._ingestor._last_added_video_ids or list(video_ids)
            results = self._ingestor.extract_transcripts(added_video_ids)
            if len(added_video_ids) != len(video_ids):
                for vid in video_ids:
                    if vid not in results:
                        results[vid] = (False, None, "Source add failed")
            extract_elapsed_s = round(time.monotonic() - extract_started_at, 3)
        finally:
            cleanup_started_at = time.monotonic()
            self._ingestor.reset_sources()  # clear sources, keep notebook
            if self._nb_id:
                _save_reusable_notebook_id(self._nb_id)
                log_action(
                    "nlm_batch_reusable_state_saved",
                    {
                        "nb_id": self._nb_id,
                        "state_path": str(_get_reusable_notebook_state_path()),
                    },
                )
            cleanup_elapsed_s = round(time.monotonic() - cleanup_started_at, 3)

        succeeded = sum(1 for success, transcript, _ in results.values() if success and transcript)
        failed = len(results) - succeeded
        total_elapsed_s = round(time.monotonic() - batch_started_at, 3)
        log_action(
            "nlm_batch_reusable_process_completed",
            {
                "batch_size": len(video_ids),
                "nb_id": self._nb_id,
                "notebook_reused": notebook_reused,
                "setup_mode": setup_mode,
                "setup_elapsed_s": setup_elapsed_s,
                "extract_elapsed_s": extract_elapsed_s,
                "cleanup_elapsed_s": cleanup_elapsed_s,
                "notebooklm_profile": _get_notebooklm_profile(),
                "succeeded": succeeded,
                "failed": failed,
                "add_sources_elapsed_s": add_sources_elapsed_s,
                "ensure_notebook_elapsed_s": round(time.monotonic() - setup_started_at, 3),
                "notebook_check_elapsed_s": self._last_ensure_metrics.get("notebook_check_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "notebook_create_elapsed_s": self._last_ensure_metrics.get("create_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "notebook_retire_elapsed_s": self._last_ensure_metrics.get("retire_elapsed_s", 0.0)
                if getattr(self, "_last_ensure_metrics", None)
                else 0.0,
                "subbatch_size": self._ingestor.batch_size,
                "strategy": "reusable",
                "total_elapsed_s": total_elapsed_s,
            },
        )
        self._last_process_metrics = {
            "batch_size": len(video_ids),
            "nb_id": self._nb_id,
            "notebook_reused": notebook_reused,
            "setup_mode": setup_mode,
            "setup_elapsed_s": setup_elapsed_s,
            "extract_elapsed_s": extract_elapsed_s,
            "cleanup_elapsed_s": cleanup_elapsed_s,
            "add_sources_elapsed_s": add_sources_elapsed_s,
            "add_cmd_elapsed_s": float(self._ingestor._last_add_cmd_elapsed_s or 0.0),
            "materialization_wait_elapsed_s": float(self._ingestor._last_materialization_wait_elapsed_s or 0.0),
            "ensure_notebook_elapsed_s": round(time.monotonic() - setup_started_at, 3),
            "notebook_check_elapsed_s": self._last_ensure_metrics.get("notebook_check_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "notebook_create_elapsed_s": self._last_ensure_metrics.get("create_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "notebook_retire_elapsed_s": self._last_ensure_metrics.get("retire_elapsed_s", 0.0)
            if getattr(self, "_last_ensure_metrics", None)
            else 0.0,
            "succeeded": succeeded,
            "failed": failed,
            "subbatch_metrics": [dict(item) for item in self._ingestor._last_subbatch_metrics],
            "subbatch_size": self._ingestor.batch_size,
            "strategy": "reusable",
            "total_elapsed_s": total_elapsed_s,
        }
        return results

    def close(self, delete: bool = False):
        self._ingestor._nb_id = self._nb_id
        if delete:
            self._ingestor.close()
            _clear_reusable_notebook_state()
            return
        self._ingestor.cleanup()
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
            log_action(
                "nlm_batch_reusable_state_saved",
                {
                    "nb_id": self._nb_id,
                    "state_path": str(_get_reusable_notebook_state_path()),
                    "notebooklm_profile": _get_notebooklm_profile(),
                },
            )

    def get_last_process_metrics(self) -> dict[str, object] | None:
        if self._last_process_metrics is None:
            return None
        return dict(self._last_process_metrics)


def process_industrial_batch(video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
    ingestor = NLMBatchIngestor()
    try:
        if not ingestor.create_batch_notebook(video_ids):
            return {vid: (False, None, "Notebook failed") for vid in video_ids}
        added_video_ids = ingestor._last_added_video_ids or list(video_ids)
        results = ingestor.extract_transcripts(added_video_ids)
        if len(added_video_ids) != len(video_ids):
            for vid in video_ids:
                if vid not in results:
                    results[vid] = (False, None, "Source add failed")
        return results
    finally:
        ingestor.cleanup()


# Module-level reusable instance — survives across calls for the same importer
_reusable_ingestor: Optional[NLMReusableIngestor] = None


def set_reusable_ingestor(ingestor: Optional[NLMReusableIngestor]) -> None:
    """Install a reusable ingestor instance for the current process."""
    global _reusable_ingestor
    _reusable_ingestor = ingestor


def process_industrial_batch_reusable(
    video_ids: List[str],
) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
    """Reuse a single notebook across multiple batch calls."""
    global _reusable_ingestor
    if _reusable_ingestor is None:
        _reusable_ingestor = NLMReusableIngestor()
    try:
        return _reusable_ingestor.process_batch(video_ids)
    except Exception:
        _reusable_ingestor.close(delete=False)
        _reusable_ingestor = None
        raise


def get_last_reusable_process_metrics() -> dict[str, object] | None:
    """Return the most recent reusable-batch timing summary, if available."""
    if _reusable_ingestor is None:
        return None
    return _reusable_ingestor.get_last_process_metrics()


def get_last_prepare_metrics() -> dict[str, object] | None:
    """Return the most recent reusable prewarm timing summary, if available."""
    if _reusable_ingestor is None:
        return None
    return _reusable_ingestor.get_last_prepare_metrics()


def close_reusable_ingestor(delete: bool = False):
    """Release the reusable notebook.

    By default this keeps the notebook around for reuse across future runs and
    only clears its sources. Pass delete=True for explicit destructive cleanup.
    """
    global _reusable_ingestor
    if _reusable_ingestor is not None:
        _reusable_ingestor.close(delete=delete)
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
