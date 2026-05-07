#!/usr/bin/env python3
"""NotebookLM Industrial Batch Ingestor (High-Speed Version).

This version uses 'nlm source content' which is 10x faster than queries
and doesn't use AI credits. It fixes the mapping bug by correlating
titles from the CLI list with our input IDs.
"""

import json
import hashlib
import logging
import os
import subprocess
import time
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import fasteners
from csf.batch_status import summarize_video_ids
from csf.display import format_result_row
from csf.csf_logging import log_action
from csf.nlm_config import get_nlm_config
from csf import nlm_auth_guard
from csf.nlm_worker_auth import (
    DEFAULT_FAMILIES,
    expected_email_for_profile,
    refresh_source_profile,
    sync_worker_profiles,
)
from csf.shared_retry_pool import enqueue as enqueue_shared_retry
from csf.youtube_page_inspector import inspect_youtube_watch_page, inspect_youtube_watch_page_via_ytdlp

run_nlm = nlm_auth_guard.run_nlm


_DEFAULT_OWNER_NOTEBOOK_STATE_PATH = Path("P:\\.data/yt-is/owner_nlm_notebook.json")
_DEFAULT_OWNER_NOTEBOOK_TITLE = "yt-is-worker-01"
_DEFAULT_INDUSTRIAL_WORKER_STATE_ROOT = Path("P:\\.data/yt-is/industrial-worker-states")
_DEFAULT_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX = "yt-is-worker"
_LEGACY_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX = "yt-is::industrial::worker"
_DEFAULT_NOTEBOOKLM_PROFILE = "default"
_AUTH_LOCK_PATH = Path("P:\\.data/yt-is/locks/nlm-auth.lock")
DEFAULT_NLM_CHROME_PROFILE_ROOT = nlm_auth_guard.DEFAULT_NLM_CHROME_PROFILE_ROOT

_NLM_CONFIG = get_nlm_config()
DEFAULT_NOTEBOOKLM_BATCH_SIZE = _NLM_CONFIG.notebook_batch_size
DEFAULT_NOTEBOOKLM_SOURCE_CAP = _NLM_CONFIG.notebook_source_cap
DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S = _NLM_CONFIG.notebook_source_materialization_timeout_s
_NOTEBOOK_SOURCE_CAP = DEFAULT_NOTEBOOKLM_SOURCE_CAP  # Keep the free-tier worker notebook below its source ceiling.
_READY_PROBE_EARLY = os.getenv("YTIS_NLM_READY_PROBE_EARLY", "").strip().lower() in {"1", "true", "yes", "on"}
_READY_PROBE_INTERVAL_S = float(os.getenv("YTIS_NLM_READY_PROBE_INTERVAL_S", "1.0"))
_READY_PROBE_TIMEOUT_S = float(
    os.getenv(
        "YTIS_NLM_READY_PROBE_TIMEOUT_S",
        str(DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S),
    )
)
_SOURCE_CONTENT_RETRY_ATTEMPTS = max(1, int(_NLM_CONFIG.source_content_retry_attempts))
_SOURCE_CONTENT_RETRY_INITIAL_DELAY_S = max(0.0, float(_NLM_CONFIG.source_content_retry_initial_delay_s))
_SOURCE_CONTENT_RETRY_MAX_DELAY_S = max(
    _SOURCE_CONTENT_RETRY_INITIAL_DELAY_S,
    float(_NLM_CONFIG.source_content_retry_max_delay_s),
)
_SOURCE_CONTENT_RETRY_BUDGET_S = max(0.0, float(_NLM_CONFIG.source_content_retry_budget_s))
_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S = max(0.0, float(_NLM_CONFIG.source_content_retry_queue_delay_s))
_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S = max(0.0, float(_NLM_CONFIG.source_content_retry_queue_budget_s))
_SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED = bool(_NLM_CONFIG.source_content_shared_retry_pool_enabled)
_NLM_CONTENT_READY_THRESHOLD = 100
_NLM_CONTENT_BELOW_THRESHOLD_STATUS = "nlm_content_below_threshold"
_LEGACY_NLM_CONTENT_BELOW_THRESHOLD_STATUS = "too_short"
_ZERO_GROWTH_ADD_RESET_RETRY_LIMIT = 1
_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = 0
_NLM_AUTH_RUNTIME_CONFIG_LOGGED = False
_NLM_AUTH_RUNTIME_CONFIG_LOCK = threading.Lock()


def _summarize_add_failure_batch_ids(batch_ids: List[str]) -> dict[str, object]:
    """Return stable, compact identity fields for a failed source-add batch."""
    digest_input = "\n".join(str(video_id) for video_id in batch_ids).encode("utf-8")
    return {
        "batch_video_id_count": len(batch_ids),
        "sample_video_ids": [str(video_id) for video_id in batch_ids[:5]],
        "batch_video_id_digest": hashlib.sha256(digest_input).hexdigest()[:16],
    }


def _get_nlm_auth_force_refresh_every_checks() -> int:
    raw = os.getenv("YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _log_nlm_auth_runtime_config_once(auth_context) -> None:
    """Emit the resolved auth config once per worker process."""
    global _NLM_AUTH_RUNTIME_CONFIG_LOGGED

    if _NLM_AUTH_RUNTIME_CONFIG_LOGGED:
        return

    payload = {
        "component": "nlm_batch",
        "notebooklm_profile": auth_context.profile,
        "account": auth_context.expected_email or None,
        "env_auth_check_cache_ttl_raw": os.getenv("YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS") or None,
        "resolved_auth_check_cache_ttl_s": nlm_auth_guard.auth_check_cache_ttl_seconds(),
        "resolved_auth_check_interval_s": _NLM_CONFIG.auth_check_interval,
        "resolved_auth_cooldown_s": _NLM_CONFIG.auth_cooldown,
        "resolved_auth_force_refresh_every_checks": _get_nlm_auth_force_refresh_every_checks(),
    }

    with _NLM_AUTH_RUNTIME_CONFIG_LOCK:
        if _NLM_AUTH_RUNTIME_CONFIG_LOGGED:
            return
        _NLM_AUTH_RUNTIME_CONFIG_LOGGED = True

    log_action("nlm_auth_runtime_config_snapshot", payload)


def _next_nlm_auth_check_count() -> int:
    global _NLM_AUTH_CHECK_COUNT
    with _NLM_AUTH_CHECK_COUNT_LOCK:
        _NLM_AUTH_CHECK_COUNT += 1
        return _NLM_AUTH_CHECK_COUNT


def _extract_account(stdout: str, stderr: str = "") -> str:
    for line in f"{stdout}\n{stderr}".splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("account:"):
            return stripped.split(":", 1)[1].strip().lower()
    return ""


def _session_matches_expected_account(check: subprocess.CompletedProcess, expected_email: str) -> bool:
    expected = expected_email.strip().lower()
    if not expected:
        return True
    return _extract_account(check.stdout or "", check.stderr or "") == expected


def _auth_family_for_profile(profile: str):
    profile = profile.strip()
    if not profile:
        return None
    for family in DEFAULT_FAMILIES:
        if profile == family.source_profile or profile in family.sibling_profiles:
            return family
    return None


def _refresh_nlm_auth_session(
    auth_context: _NLMAuthContext,
    *,
    timeout_s: float = 120.0,
    force_source_refresh: bool = False,
) -> bool:
    expected_email = auth_context.expected_email.strip().lower()
    family = _auth_family_for_profile(auth_context.profile) if expected_email else None
    if family is not None:
        return _refresh_family_nlm_auth_session(
            auth_context,
            family,
            timeout_s=timeout_s,
        )

    try:
        login = run_nlm(
            ["login", "--force", *auth_context.login_profile_args],
            timeout_s=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False
    if login.returncode != 0:
        return False
    if not expected_email:
        return True
    return _extract_account(login.stdout or "", login.stderr or "") == expected_email


def _refresh_family_nlm_auth_session(
    auth_context: _NLMAuthContext,
    family,
    *,
    timeout_s: float = 120.0,
    check_count: int | None = None,
) -> bool:
    """Refresh a mapped worker family through the canonical source profile path."""
    started = time.perf_counter()
    log_action(
        "nlm_family_refresh_started",
        {
            "component": "nlm_batch",
            "notebooklm_profile": auth_context.profile,
            "source_profile": family.source_profile,
            "expected_email": auth_context.expected_email or None,
            "check_count": check_count,
        },
    )
    outcome = "failed"
    try:
        _reap_default_chrome_profile_for_auth(
            auth_context,
            args=["login", "--force", "--profile", family.source_profile],
            phase="pre_auth_family",
        )
        if not refresh_source_profile(family, timeout_s=timeout_s):
            return False
        sync_worker_profiles(
            families=(family,),
            backup=False,
            source_session_checker=lambda _profile: True,
        )
        outcome = "ok"
        return True
    except (FileNotFoundError, RuntimeError, ValueError):
        return False
    finally:
        log_action(
            "nlm_family_refresh_completed",
            {
                "component": "nlm_batch",
                "status": outcome,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "notebooklm_profile": auth_context.profile,
                "source_profile": family.source_profile,
                "expected_email": auth_context.expected_email or None,
                "check_count": check_count,
            },
        )


_NLM_AUTH_CHECK_COUNT = 0
_NLM_AUTH_CHECK_COUNT_LOCK = threading.Lock()


@dataclass(frozen=True)
class _NLMAuthContext:
    profile: str
    login_profile_args: list[str]
    requires_profile: bool
    expected_email: str = ""

    @property
    def has_profile(self) -> bool:
        return bool(self.login_profile_args)

    @property
    def should_fail_closed(self) -> bool:
        return self.requires_profile and not self.has_profile


class NotebookSourceMaterializationTimeout(RuntimeError):
    """Raised when NotebookLM sources never become ready within the wait window."""


def _get_owner_notebook_state_path() -> Path:
    override = os.getenv("YTIS_NLM_OWNER_STATE_PATH", "").strip()
    legacy_override = os.getenv("YTIS_NLM_REUSABLE_STATE_PATH", "").strip()
    if override:
        return Path(override)
    if legacy_override:
        return Path(legacy_override)
    return _DEFAULT_OWNER_NOTEBOOK_STATE_PATH


def _get_reusable_notebook_state_path() -> Path:
    return _get_owner_notebook_state_path()


def _get_owner_notebook_title() -> str:
    override = os.getenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", "").strip()
    legacy_override = os.getenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "").strip()
    return override or legacy_override or _DEFAULT_OWNER_NOTEBOOK_TITLE


def _get_reusable_notebook_title() -> str:
    return _get_owner_notebook_title()


def _get_worker_run_id() -> str:
    return os.getenv("YTIS_INDUSTRIAL_RUN_ID", "").strip()


def _get_notebooklm_profile() -> str:
    override = os.getenv("NOTEBOOKLM_PROFILE", "").strip()
    return override or _DEFAULT_NOTEBOOKLM_PROFILE


def _get_nlm_login_profile_args() -> list[str]:
    """Return CLI args that target the active NotebookLM auth profile."""
    profile = os.getenv("NOTEBOOKLM_PROFILE", "").strip()
    if not profile:
        return []
    return nlm_auth_guard.get_login_profile_args(profile)


def _is_nlm_auth_noninteractive() -> bool:
    return nlm_auth_guard.is_nlm_auth_noninteractive()


def _get_nlm_auth_context() -> _NLMAuthContext:
    """Centralize the profile pinning decision for NotebookLM auth refresh."""
    profile_override = os.getenv("NOTEBOOKLM_PROFILE", "").strip()
    profile = profile_override or _DEFAULT_NOTEBOOKLM_PROFILE
    login_profile_args = nlm_auth_guard.get_login_profile_args(profile_override or None)
    expected_email = os.getenv("YTIS_NLM_EXPECTED_EMAIL", "").strip().lower() or expected_email_for_profile(profile)
    return _NLMAuthContext(
        profile=profile,
        login_profile_args=login_profile_args,
        requires_profile=nlm_auth_guard.is_nlm_auth_noninteractive(),
        expected_email=expected_email,
    )


def _default_chrome_profile_pids() -> set[int]:
    return nlm_auth_guard.default_chrome_profile_pids()


def _stop_chrome_pids(pids: set[int]) -> None:
    nlm_auth_guard.stop_chrome_pids(pids)


def _reap_default_chrome_profile_for_auth(
    auth_context: _NLMAuthContext,
    *,
    args: List[str],
    phase: str,
) -> bool:
    """Reap a transient default chrome-profile before auth can poison the batch."""
    return _fail_closed_on_default_chrome_profile(
        auth_context,
        args=args,
        phase=phase,
        allow_pre_auth_recovery=True,
    )


def _reap_default_chrome_profile_after_auth_command(
    auth_context: _NLMAuthContext,
    *,
    args: List[str],
    phase: str,
) -> set[int]:
    """Close a transient default chrome-profile after an auth probe and continue once."""
    default_profile_pids = _default_chrome_profile_pids()
    if not default_profile_pids:
        return set()
    _stop_chrome_pids(default_profile_pids)
    log_action(
        "nlm_auth_recovered",
        {
            "component": "nlm_batch",
            "status": "default_profile_reaped_after_auth_command",
            "phase": phase,
            "notebooklm_profile": auth_context.profile,
            "expected_email": auth_context.expected_email or None,
            "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
            "default_chrome_profile_pids": sorted(default_profile_pids),
            "command": ["nlm"] + args,
        },
    )
    return set(default_profile_pids)


def _reap_default_chrome_profile_before_command(
    auth_context: _NLMAuthContext,
    *,
    args: List[str],
    phase: str,
) -> set[int]:
    """Close a transient default chrome-profile before a non-auth command and continue once."""
    default_profile_pids = _default_chrome_profile_pids()
    if not default_profile_pids:
        return set()
    _stop_chrome_pids(default_profile_pids)
    log_action(
        "nlm_auth_recovered",
        {
            "component": "nlm_batch",
            "status": "default_profile_reaped_before_command",
            "phase": phase,
            "notebooklm_profile": auth_context.profile,
            "expected_email": auth_context.expected_email or None,
            "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
            "default_chrome_profile_pids": sorted(default_profile_pids),
            "command": ["nlm"] + args,
        },
    )
    return set(default_profile_pids)


def _is_cleanup_command(args: List[str]) -> bool:
    return len(args) >= 2 and tuple(args[:2]) in {("source", "delete"), ("notebook", "delete")}


def _fail_closed_on_default_chrome_profile(
    auth_context: _NLMAuthContext,
    *,
    args: List[str],
    phase: str,
    stdout: str = "",
    stderr: str = "",
    allow_pre_auth_recovery: bool = False,
    allow_post_command_recovery: bool = False,
    command_succeeded: bool = False,
) -> subprocess.CompletedProcess | None:
    default_profile_pids = _default_chrome_profile_pids()
    if not default_profile_pids:
        return None
    _stop_chrome_pids(default_profile_pids)
    if _is_cleanup_command(args):
        log_action(
            "nlm_auth_recovered",
            {
                "component": "nlm_batch",
                "status": "default_profile_reaped_during_cleanup",
                "phase": phase,
                "notebooklm_profile": auth_context.profile,
                "expected_email": auth_context.expected_email or None,
                "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
                "default_chrome_profile_pids": sorted(default_profile_pids),
                "command": ["nlm"] + args,
            },
        )
        return None
    if allow_pre_auth_recovery and phase.startswith("pre_auth"):
        log_action(
            "nlm_auth_recovered",
            {
                "component": "nlm_batch",
                "status": "default_profile_reaped_before_auth",
                "phase": phase,
                "notebooklm_profile": auth_context.profile,
                "expected_email": auth_context.expected_email or None,
                "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
                "default_chrome_profile_pids": sorted(default_profile_pids),
                "command": ["nlm"] + args,
            },
        )
        return None
    if allow_post_command_recovery:
        log_action(
            "nlm_auth_recovered",
            {
                "component": "nlm_batch",
                "status": "default_profile_reaped_after_command",
                "phase": phase,
                "notebooklm_profile": auth_context.profile,
                "expected_email": auth_context.expected_email or None,
                "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
                "default_chrome_profile_pids": sorted(default_profile_pids),
                "command_succeeded": command_succeeded,
                "command": ["nlm"] + args,
            },
        )
        return None
    log_action(
        "nlm_auth_failed",
        {
            "component": "nlm_batch",
            "status": "default_profile_running",
            "phase": phase,
            "notebooklm_profile": auth_context.profile,
            "expected_email": auth_context.expected_email or None,
            "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
            "default_chrome_profile_pids": sorted(default_profile_pids),
            "command": ["nlm"] + args,
        },
    )
    message = (
        f"default NotebookLM chrome-profile is already running: "
        f"{DEFAULT_NLM_CHROME_PROFILE_ROOT}"
    )
    return subprocess.CompletedProcess(["nlm"] + args, 1, stdout, stderr or message)


def _run_guarded_nlm_auth_command(
    auth_context: _NLMAuthContext,
    args: list[str],
    *,
    timeout: int,
    phase: str,
) -> subprocess.CompletedProcess | None:
    """Run an auth command and fail closed if upstream opens the default Chrome profile."""
    try:
        res = run_nlm(args, timeout_s=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["nlm"] + args, 1, "", "NLM auth command timed out")
    default_profile_pids = _reap_default_chrome_profile_after_auth_command(
        auth_context,
        args=args,
        phase=f"{phase}_after",
    )
    if default_profile_pids:
        try:
            res = run_nlm(args, timeout_s=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(["nlm"] + args, 1, "", "NLM auth command timed out")
        retry_default_profile_pids = _reap_default_chrome_profile_after_auth_command(
            auth_context,
            args=args,
            phase=f"{phase}_after_retry",
        )
        if retry_default_profile_pids:
            log_action(
                "nlm_auth_failed",
                {
                    "component": "nlm_batch",
                    "status": "default_profile_running",
                    "phase": f"{phase}_after_retry",
                    "notebooklm_profile": auth_context.profile,
                    "expected_email": auth_context.expected_email or None,
                    "default_chrome_profile": str(DEFAULT_NLM_CHROME_PROFILE_ROOT),
                    "default_chrome_profile_pids": sorted(retry_default_profile_pids),
                    "command": ["nlm"] + args,
                },
            )
            message = (
                f"default NotebookLM chrome-profile is already running: "
                f"{DEFAULT_NLM_CHROME_PROFILE_ROOT}"
            )
            return subprocess.CompletedProcess(["nlm"] + args, 1, res.stdout or "", res.stderr or message)
    return res


def _extract_video_id_from_source_entry(source: object) -> str | None:
    """Best-effort extraction of a video ID from a NotebookLM source entry."""
    if not isinstance(source, dict):
        return None
    for key in ("video_id", "videoId"):
        value = str(source.get(key) or "").strip()
        if value:
            return value
    for key in ("title", "name", "url", "source_url", "video_url", "display_url"):
        value = str(source.get(key) or "").strip()
        if not value:
            continue
        match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", value)
        if match:
            return match.group(1)
        match = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", value)
        if match:
            return match.group(1)
        if re.fullmatch(r"[a-zA-Z0-9_-]{11}", value):
            return value
    return None


def _extract_source_ids_from_add_stdout(stdout: str) -> list[str]:
    """Extract NotebookLM source IDs from a successful add command's stdout."""
    source_ids: list[str] = []
    for line in (stdout or "").splitlines():
        match = re.search(r"Source ID:\s*([^\s]+)", line)
        if match:
            source_ids.append(match.group(1))
    return source_ids


def _should_retry_source_content_fetch(status: str, res: subprocess.CompletedProcess) -> bool:
    """Retry content fetches that look transient rather than terminal."""
    if status in {_NLM_CONTENT_BELOW_THRESHOLD_STATUS, _LEGACY_NLM_CONTENT_BELOW_THRESHOLD_STATUS}:
        return True
    if status != "command_failed":
        return False
    combined = f"{res.stdout or ''}\n{res.stderr or ''}".upper()
    transient_markers = ("NOT_FOUND", "RATE LIMIT", "TOO MANY REQUESTS", "TEMPORARILY UNAVAILABLE")
    return any(marker in combined for marker in transient_markers)


def _outcome_mentions_not_found(outcome: dict[str, object]) -> bool:
    """Return True when a fetch outcome looks like a NotebookLM missing-source storm."""
    combined = "\n".join(
        str(outcome.get(key) or "")
        for key in ("error", "failure_reason", "stdout", "stderr")
    ).upper()
    return "NOT_FOUND" in combined


def _source_count_probe_indicates_dead_notebook(probe_error: dict[str, object] | None) -> bool:
    """Return True when a source-count probe says the notebook no longer exists."""
    if not probe_error:
        return False
    combined = f"{probe_error.get('stdout') or ''}\n{probe_error.get('stderr') or ''}".upper()
    return "API ERROR (CODE 5): NOT_FOUND" in combined or "NOT_FOUND" in combined


def _should_defer_source_content_fetch(ytdlp_probe: dict[str, object], status: str) -> bool:
    """Return True when a failure should be queued for a second NotebookLM pass."""
    if status not in {"command_failed", _NLM_CONTENT_BELOW_THRESHOLD_STATUS, _LEGACY_NLM_CONTENT_BELOW_THRESHOLD_STATUS}:
        return False
    classification = str(ytdlp_probe.get("classification") or "").strip().lower()
    return classification == "ok"


def _load_reusable_notebook_id() -> Optional[str]:
    try:
        state_path = _get_owner_notebook_state_path()
        if not state_path.exists():
            return None
        data = json.loads(state_path.read_text(encoding="utf-8"))
        nb_id = (data.get("nb_id") or "").strip()
        return nb_id or None
    except Exception:
        return None


def _save_reusable_notebook_id(nb_id: str) -> None:
    try:
        state_path = _get_owner_notebook_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "nb_id": nb_id,
                    "title": _get_owner_notebook_title(),
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
        for state_path in {
            _get_owner_notebook_state_path(),
            _DEFAULT_OWNER_NOTEBOOK_STATE_PATH,
            Path("P:\\.data/yt-is/reusable_nlm_notebook.json"),
        }:
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


def _get_worker_notebook_prefixes() -> tuple[str, ...]:
    prefixes: list[str] = []
    current = _get_worker_notebook_prefix().strip()
    if current:
        prefixes.append(current)
    if _LEGACY_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX not in prefixes:
        prefixes.append(_LEGACY_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX)
    return tuple(prefixes)


def _infer_worker_profile_from_notebook_name(name: str) -> str:
    match = re.search(r"worker-(\d+)$", name.strip())
    if not match:
        return _get_notebooklm_profile()
    worker_idx = int(match.group(1))
    return f"ytis-worker-{worker_idx:02d}"


def _notebook_entry_title(nb: object) -> str:
    if not isinstance(nb, dict):
        return ""
    return (nb.get("title") or nb.get("name") or nb.get("notebookTitle") or "").strip()


def _notebook_entry_id(nb: object) -> str:
    if not isinstance(nb, dict):
        return ""
    return (nb.get("id") or nb.get("notebookId") or "").strip()


def _find_notebooks_with_title(notebooks: list[object], title: str) -> list[dict[str, object]]:
    exact_title = title.strip()
    if not exact_title:
        return []
    matches: list[dict[str, object]] = []
    for nb in notebooks:
        if not isinstance(nb, dict):
            continue
        if _notebook_entry_title(nb) == exact_title:
            matches.append(nb)
    return matches


def _choose_notebook_keeper(matches: list[dict[str, object]], preferred_id: str = "") -> dict[str, object] | None:
    if not matches:
        return None
    preferred_id = preferred_id.strip()
    if preferred_id:
        for nb in matches:
            if _notebook_entry_id(nb) == preferred_id:
                return nb
    return max(matches, key=lambda nb: (_notebook_entry_title(nb), _notebook_entry_id(nb)))


def _delete_worker_notebooks_by_title_with_cdp(title: str) -> subprocess.CompletedProcess[str]:
    cdp_script = Path(__file__).parent.parent / "bin" / "nlm-puppeteer.js"
    cmd = ["node", str(cdp_script), "--delete-title", title]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _load_current_worker_notebook_ids() -> set[str]:
    state_root = _get_worker_state_root()
    ids: set[str] = set()
    if not state_root.exists():
        return ids
    for state_path in state_root.glob("worker-*.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            nb_id = (data.get("nb_id") or "").strip()
            if nb_id:
                ids.add(nb_id)
        except Exception:
            continue
    return ids


def cleanup_stale_worker_notebooks(*, delete: bool = False) -> tuple[int, int]:
    """Audit worker notebooks and optionally delete stale ones."""
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

    worker_notebooks = [
        nb
        for nb in notebooks
        if isinstance(nb, dict)
        and any(
            (nb.get("name") or nb.get("title") or "").strip().startswith(worker_prefix)
            for worker_prefix in _get_worker_notebook_prefixes()
        )
    ]
    if not delete:
        log_action(
            "nlm_worker_notebook_cleanup_complete",
            {
                "deleted": 0,
                "failed": 0,
                "status": "audit_only",
                "active_nb_ids": len(active_nb_ids),
                "notebook_prefix": prefix,
                "run_id": run_id or None,
                "worker_notebook_count": len(worker_notebooks),
            },
        )
        return (0, 0)

    deleted = 0
    failed = 0
    stale_worker_notebooks = [
        nb
        for nb in worker_notebooks
        if _notebook_entry_id(nb) and _notebook_entry_id(nb) not in active_nb_ids
    ]
    for nb in sorted(stale_worker_notebooks, key=lambda item: (_notebook_entry_title(item), _notebook_entry_id(item))):
        nb_id = _notebook_entry_id(nb)
        if not nb_id:
            continue
        ingestor._nb_id = nb_id
        try:
            res = _delete_notebook_with_retries(
                ingestor,
                nb_id,
                timeout=120,
                retries=2,
                purpose="cleanup_stale_worker_notebooks",
            )
        except Exception:
            res = subprocess.CompletedProcess(
                ["nlm", "notebook", "delete", nb_id, "--confirm"],
                1,
                "",
                "delete failed",
            )
        if res.returncode == 0:
            deleted += 1
        else:
            failed += 1
    log_action(
        "nlm_worker_notebook_cleanup_complete",
        {
            "deleted": deleted,
            "failed": failed,
            "status": "deleted" if failed == 0 else "delete_failed",
            "active_nb_ids": len(active_nb_ids),
            "notebook_prefix": prefix,
            "run_id": run_id or None,
            "worker_notebook_count": len(worker_notebooks),
            "stale_worker_notebook_count": len(stale_worker_notebooks),
        },
    )
    return (deleted, failed)


def _ensure_nlm_auth() -> bool:
    """Verify nlm CLI auth is valid, auto-recover if expired.

    Known worker-family profiles refresh through the canonical source-profile
    path so the probe never opens the shared default NotebookLM chrome-profile.
    Unknown profiles still use the profile-pinned `nlm login --check` and
    `nlm login --force` fallback. Returns True if auth is valid or was just
    refreshed.
    """
    import subprocess

    auth_context = _get_nlm_auth_context()
    if auth_context.should_fail_closed:
        _log_nlm_auth_runtime_config_once(auth_context)
        log_action(
            "nlm_auth_failed",
            {
                "component": "nlm_batch",
                "status": "missing_profile",
                "mode": "noninteractive",
                "notebooklm_profile": auth_context.profile,
            },
        )
        return False

    _log_nlm_auth_runtime_config_once(auth_context)
    check_count = _next_nlm_auth_check_count()
    force_every = _get_nlm_auth_force_refresh_every_checks()
    force_scheduled = force_every > 0 and check_count % force_every == 0
    if not force_scheduled and nlm_auth_guard.auth_check_cache_hit(auth_context)[0]:
        session_age_s = nlm_auth_guard.auth_check_cache_session_age(auth_context)
        log_action(
            "nlm_auth_checked",
            {
                "component": "nlm_batch",
                "status": "cached",
                "notebooklm_profile": auth_context.profile,
                "account": auth_context.expected_email or None,
                "expected_email": auth_context.expected_email or None,
                "check_count": check_count,
                "session_age_s": round(session_age_s, 3) if session_age_s is not None else None,
            },
        )
        return True
    expected_email = auth_context.expected_email.strip().lower()
    family = _auth_family_for_profile(auth_context.profile) if expected_email else None
    if family is not None:
        if force_scheduled:
            log_action(
                "nlm_auth_forced_refresh_scheduled",
                {
                    "component": "nlm_batch",
                    "notebooklm_profile": auth_context.profile,
                    "expected_email": expected_email or None,
                    "check_count": check_count,
                },
            )

        # Family-backed auth probes refresh the canonical source profile instead of
        # opening the mapped worker profile's default NotebookLM chrome-profile.
        _AUTH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with fasteners.InterProcessLock(str(_AUTH_LOCK_PATH)):
            login_started = time.perf_counter()
            log_action(
                "nlm_login_started",
                {
                    "component": "nlm_batch",
                    "mode": "family_refresh",
                    "status": "started",
                    "notebooklm_profile": auth_context.profile,
                    "source_profile": family.source_profile,
                    "check_count": check_count,
                },
            )
            login = _refresh_family_nlm_auth_session(
                auth_context,
                family,
                timeout_s=120,
                check_count=check_count,
            )
            login_elapsed = round(time.perf_counter() - login_started, 3)
            session_established_at = round(time.monotonic(), 3)
            if login:
                nlm_auth_guard.auth_check_cache_store(auth_context, session_established_at=session_established_at)
                log_action(
                    "nlm_login_completed",
                    {
                        "component": "nlm_batch",
                        "mode": "family_refresh",
                        "status": "ok",
                        "elapsed_s": login_elapsed,
                        "notebooklm_profile": auth_context.profile,
                        "source_profile": family.source_profile,
                        "check_count": check_count,
                        "session_established_at": session_established_at,
                    },
                )
                log_action(
                    "nlm_auth_refreshed",
                    {
                        "component": "nlm_batch",
                        "status": "ok",
                        "notebooklm_profile": auth_context.profile,
                        "source_profile": family.source_profile,
                        "check_count": check_count,
                        "session_established_at": session_established_at,
                    },
                )
                return True
            log_action(
                "nlm_login_failed",
                {
                    "component": "nlm_batch",
                    "mode": "family_refresh",
                    "status": "failed",
                    "elapsed_s": login_elapsed,
                    "returncode": 1,
                    "notebooklm_profile": auth_context.profile,
                    "source_profile": family.source_profile,
                    "check_count": check_count,
                },
            )
            log_action(
                "nlm_auth_failed",
                {
                    "component": "nlm_batch",
                    "status": "refresh_failed",
                    "notebooklm_profile": auth_context.profile,
                    "source_profile": family.source_profile,
                    "check_count": check_count,
                },
            )
            return False

    _reap_default_chrome_profile_for_auth(
        auth_context,
        args=["login", "--check", *auth_context.login_profile_args],
        phase="pre_auth",
    )
    check = _run_guarded_nlm_auth_command(
        auth_context,
        ["login", "--check", *auth_context.login_profile_args],
        timeout=30,
        phase="auth_check",
    )
    if check is None or (
        check.returncode != 0 and "default NotebookLM chrome-profile" in (check.stderr or "")
    ):
        return False
    check_account = _extract_account(check.stdout or "", check.stderr or "")
    check_matches_expected = check.returncode == 0 and (not expected_email or check_account == expected_email)
    if check_matches_expected and not force_scheduled:
        nlm_auth_guard.auth_check_cache_store(auth_context)
        log_action(
            "nlm_auth_checked",
            {
                "component": "nlm_batch",
                "status": "ok",
                "notebooklm_profile": auth_context.profile,
                "account": check_account or None,
                "expected_email": expected_email or None,
                "check_count": check_count,
                "session_age_s": None,
            },
        )
        return True

    if check.returncode == 0 and expected_email and check_account and check_account != expected_email:
        log_action(
            "nlm_auth_failed",
            {
                "component": "nlm_batch",
                "status": "wrong_account",
                "notebooklm_profile": auth_context.profile,
                "account": check_account,
                "expected_email": expected_email,
                "check_count": check_count,
            },
        )
    elif force_scheduled and check_matches_expected:
        log_action(
            "nlm_auth_forced_refresh_scheduled",
            {
                "component": "nlm_batch",
                "notebooklm_profile": auth_context.profile,
                "expected_email": expected_email or None,
                "check_count": check_count,
            },
        )
    elif check.returncode != 0:
        log_action(
            "nlm_auth_failed",
            {
                "component": "nlm_batch",
                "status": "check_failed",
                "notebooklm_profile": auth_context.profile,
                "check_count": check_count,
            },
        )

    # Auth expired — serialize refresh so multiple workers do not launch
    # duplicate browser login flows at the same time.
    _AUTH_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(_AUTH_LOCK_PATH)):
        _reap_default_chrome_profile_for_auth(
            auth_context,
            args=["login", "--check", *auth_context.login_profile_args],
            phase="pre_auth_locked",
        )
        check = _run_guarded_nlm_auth_command(
            auth_context,
            ["login", "--check", *auth_context.login_profile_args],
            timeout=30,
            phase="auth_check_locked",
        )
        if check is None or (
            check.returncode != 0 and "default NotebookLM chrome-profile" in (check.stderr or "")
        ):
            return False
        check_account = _extract_account(check.stdout or "", check.stderr or "")
        check_matches_expected = check.returncode == 0 and (not expected_email or check_account == expected_email)
        if check_matches_expected and not force_scheduled:
            nlm_auth_guard.auth_check_cache_store(auth_context)
            log_action(
                "nlm_auth_checked",
                {
                    "component": "nlm_batch",
                    "status": "ok",
                    "notebooklm_profile": auth_context.profile,
                    "account": check_account or None,
                    "expected_email": expected_email or None,
                    "check_count": check_count,
                    "session_age_s": None,
                },
            )
            return True

        if check.returncode == 0 and expected_email and check_account and check_account != expected_email:
            log_action(
                "nlm_auth_failed",
                {
                    "component": "nlm_batch",
                    "status": "wrong_account",
                    "notebooklm_profile": auth_context.profile,
                    "account": check_account,
                    "expected_email": expected_email,
                    "check_count": check_count,
                },
            )
        elif force_scheduled and check_matches_expected:
            log_action(
                "nlm_auth_forced_refresh_scheduled",
                {
                    "component": "nlm_batch",
                    "notebooklm_profile": auth_context.profile,
                    "expected_email": expected_email or None,
                    "check_count": check_count,
                },
            )

        login_started = time.perf_counter()
        log_action(
            "nlm_login_started",
            {
                "component": "nlm_batch",
                "mode": "force",
                "status": "started",
                "notebooklm_profile": auth_context.profile,
                "check_count": check_count,
            },
        )
        login = _refresh_nlm_auth_session(auth_context, timeout_s=120, force_source_refresh=force_scheduled)
        login_elapsed = round(time.perf_counter() - login_started, 3)
        session_established_at = round(time.monotonic(), 3)
        if login:
            nlm_auth_guard.auth_check_cache_store(auth_context, session_established_at=session_established_at)
            log_action(
                "nlm_login_completed",
                {
                    "component": "nlm_batch",
                    "mode": "force",
                    "status": "ok",
                    "elapsed_s": login_elapsed,
                    "notebooklm_profile": auth_context.profile,
                    "check_count": check_count,
                    "session_established_at": session_established_at,
                },
            )
            log_action(
                "nlm_auth_refreshed",
                {
                    "component": "nlm_batch",
                    "status": "ok",
                    "notebooklm_profile": auth_context.profile,
                    "check_count": check_count,
                    "session_established_at": session_established_at,
                },
            )
            return True
        log_action(
            "nlm_login_failed",
            {
                "component": "nlm_batch",
                "mode": "force",
                "status": "failed",
                "elapsed_s": login_elapsed,
                "returncode": 1,
                "notebooklm_profile": auth_context.profile,
                "check_count": check_count,
            },
        )
        log_action(
            "nlm_auth_failed",
            {
                "component": "nlm_batch",
                "status": "refresh_failed",
                "notebooklm_profile": auth_context.profile,
                "check_count": check_count,
            },
        )
        return False


# Minimum characters for a "valid" high-fidelity transcript
_MIN_TRANSCRIPT_CHARS = 500
_MAX_SUBBATCH_RETRY_DEPTH = 4
_ZERO_GROWTH_ADD_RETRY_LIMIT = 1
_ZERO_GROWTH_ADD_RETRY_DELAY_S = 5.0

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
        self._last_materialization_ready_at_epoch: float = 0.0
        self._last_added_source_ids: List[str] = []
        self._last_extract_metrics: dict[str, object] | None = None
        self._current_source_count: int = 0
        self._video_ready_epoch_by_id: dict[str, float] = {}
        self._last_source_count_probe_ok: bool = True
        self._last_source_count_probe_error: dict[str, object] | None = None

    def _run_cmd(self, args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
        tracker = _get_tracker()
        pre_command_retry_attempted = False
        while True:
            tracker.apply_delay()
            auth_context = _get_nlm_auth_context()
            cmd_args = nlm_auth_guard.add_profile_args(args, auth_context.profile if auth_context.has_profile else None)
            default_profile_guard = _fail_closed_on_default_chrome_profile(
                auth_context,
                args=cmd_args,
                phase="pre_auth",
                allow_pre_auth_recovery=True,
            )
            if default_profile_guard is not None:
                tracker.record_failure(is_rate_limit=False)
                return default_profile_guard
            if not _ensure_nlm_auth():
                return subprocess.CompletedProcess(["nlm"] + cmd_args, 1, "", "Auth failed")
            default_profile_pids = _reap_default_chrome_profile_before_command(
                auth_context,
                args=cmd_args,
                phase="pre_command",
            )
            if default_profile_pids:
                tracker.record_failure(is_rate_limit=False)
                if pre_command_retry_attempted:
                    return subprocess.CompletedProcess(
                        ["nlm"] + cmd_args,
                        1,
                        "",
                        f"default NotebookLM chrome-profile is already running: {DEFAULT_NLM_CHROME_PROFILE_ROOT}",
                    )
                pre_command_retry_attempted = True
                continue
            res = run_nlm(cmd_args, timeout_s=timeout)
            default_profile_guard = _fail_closed_on_default_chrome_profile(
                auth_context,
                args=cmd_args,
                phase="post_command",
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                allow_post_command_recovery=True,
                command_succeeded=res.returncode == 0,
            )
            if default_profile_guard is not None:
                tracker.record_failure(is_rate_limit=False)
                return default_profile_guard

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
                auth_context = _get_nlm_auth_context()
                if auth_context.should_fail_closed:
                    tracker.record_failure(is_rate_limit=False)
                    return res
                if _refresh_nlm_auth_session(auth_context, timeout_s=120):
                    res = run_nlm(cmd_args, timeout_s=timeout)
                    if res.returncode == 0:
                        tracker.record_success()
                        return res
                tracker.record_failure(is_rate_limit=False)
                return res

            # Non-rate-limit failure — record but don't retry infinitely
            tracker.record_failure(is_rate_limit=False)
            return res

    def _wait_for_sources_ready(
        self,
        expected_count: int,
        timeout: int = DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S,
        *,
        source_count_before_wait: int = 0,
        poll_interval_s: int = 10,
    ) -> bool:
        """Poll source list until all expected sources are present and accounted for.

        Uses heartbeat polling because 'nlm source add --wait' only waits for the
        API call to return, not for NLM's async processing to complete. Sources can
        be in a 'processing' state immediately after add returns.
        """
        import time
        start = time.time()
        poll_count = 0
        last_observed_total = source_count_before_wait
        while time.time() - start < timeout:
            res = self._run_cmd(["source", "list", self._nb_id, "--json"])
            poll_count += 1
            if res.returncode == 0:
                try:
                    sources = json.loads(res.stdout)
                    if isinstance(sources, dict):
                        sources = sources.get("sources", [])
                    observed_total = len(sources)
                    last_observed_total = observed_total
                    materialization_started = observed_total > source_count_before_wait
                    if observed_total >= expected_count:
                        return True
                    if poll_count == 1 or poll_count % 3 == 0:
                        log_action(
                            "nlm_batch_source_materialization_wait_progress",
                            {
                                "nb_id": self._nb_id,
                                "expected_total": expected_count,
                                "observed_total": observed_total,
                                "source_count_before_wait": source_count_before_wait,
                                "materialization_started": materialization_started,
                                "poll_count": poll_count,
                                "elapsed_s": round(time.time() - start, 3),
                                "timeout_s": timeout,
                                "poll_interval_s": poll_interval_s,
                            },
                        )
                except Exception:
                    log_action(
                        "nlm_batch_source_materialization_wait_poll_failed",
                        {
                            "nb_id": self._nb_id,
                            "expected_total": expected_count,
                            "source_count_before_wait": source_count_before_wait,
                            "poll_count": poll_count,
                            "elapsed_s": round(time.time() - start, 3),
                            "stdout": (res.stdout or "")[:200],
                            "stderr": (res.stderr or "")[:200],
                            "timeout_s": timeout,
                            "poll_interval_s": poll_interval_s,
                        },
                    )
            else:
                log_action(
                    "nlm_batch_source_materialization_wait_poll_failed",
                    {
                        "nb_id": self._nb_id,
                        "expected_total": expected_count,
                        "source_count_before_wait": source_count_before_wait,
                        "poll_count": poll_count,
                        "elapsed_s": round(time.time() - start, 3),
                        "returncode": res.returncode,
                        "stdout": (res.stdout or "")[:200],
                        "stderr": (res.stderr or "")[:200],
                        "timeout_s": timeout,
                        "poll_interval_s": poll_interval_s,
                    },
                )
            time.sleep(poll_interval_s)
        log_action(
            "nlm_batch_source_materialization_wait_timeout",
            {
                "nb_id": self._nb_id,
                "expected_total": expected_count,
                "source_count_before_wait": source_count_before_wait,
                "poll_count": poll_count,
                "elapsed_s": round(time.time() - start, 3),
                "timeout_s": timeout,
                "last_observed_total": last_observed_total,
                "materialization_started": last_observed_total > source_count_before_wait,
                "poll_interval_s": poll_interval_s,
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
        reset_depth: int = 0,
        dead_notebook_recreate_depth: int = 0,
        source_profile: Optional[dict[str, object]] = None,
    ) -> List[str]:
        """Add one chunk with bounded retry/reset recovery on add failures.

        Zero-growth add failures are lane-invalidating after one retry and one
        notebook reset. Splitting is intentionally avoided because an all-zero
        add normally points at account/profile/service pressure, not a single
        bad URL.
        """
        if not batch_ids:
            return []

        chunk_started_at = time.monotonic()
        chunk_started_at_epoch = time.time()
        self._last_add_failure_reason = None
        self._last_add_returncode = None
        self._last_add_cmd_elapsed_s = 0.0
        self._last_materialization_wait_elapsed_s = 0.0
        if source_profile is None:
            source_profile = summarize_video_ids(batch_ids)
        # Log source count before add — this is the diagnostic key for capacity correlation
        source_count_before = self._get_current_source_count()
        source_count_before_known = bool(self._last_source_count_probe_ok)
        source_count_before_error = self._last_source_count_probe_error
        print(
            f"[NLM-Batch]   Adding sub-batch {subbatch_index} "
            f"({len(batch_ids)} sources, retry_depth={retry_depth}, "
            f"reset_depth={reset_depth}, nb_sources_before={source_count_before})..."
        )
        log_action(
            "nlm_batch_subbatch_add_started",
            {
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "reset_depth": reset_depth,
                "dead_notebook_recreate_depth": dead_notebook_recreate_depth,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
                "source_count_probe_ok_before": source_count_before_known,
                "started_at_epoch": chunk_started_at_epoch,
            },
        )
        self._last_materialization_ready_at_epoch = 0.0
        add_args = ["source", "add", self._nb_id, "--wait"]
        for vid in batch_ids:
            add_args.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
        self._last_added_source_ids = []
        res = self._run_cmd(add_args, timeout=600)
        add_cmd_elapsed_s = round(time.monotonic() - chunk_started_at, 3)
        self._last_add_cmd_elapsed_s = add_cmd_elapsed_s
        self._last_add_returncode = res.returncode
        # Probe source count after add — key diagnostic for capacity correlation
        source_count_after = self._get_current_source_count()
        source_count_after_known = bool(self._last_source_count_probe_ok)
        source_count_after_error = self._last_source_count_probe_error
        add_recovered = (
            res.returncode != 0
            and source_count_after_known
            and source_count_after >= source_count_before + len(batch_ids)
        )
        added_count = len(batch_ids) if (res.returncode == 0 or add_recovered) else 0
        count_probe_failed = res.returncode != 0 and (not source_count_before_known or not source_count_after_known)
        log_action(
            "nlm_batch_subbatch_add_completed",
            {
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "reset_depth": reset_depth,
                "dead_notebook_recreate_depth": dead_notebook_recreate_depth,
                "returncode": res.returncode,
                "added_count": added_count,
                "recovered": add_recovered,
                "elapsed_s": add_cmd_elapsed_s,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
                "source_count_probe_ok_before": source_count_before_known,
                "source_count_after": source_count_after,
                "source_count_probe_ok_after": source_count_after_known,
                "failure_reason": self._last_add_failure_reason,
                "stdout": (res.stdout or "")[:200],
                "stderr": (res.stderr or "")[:200],
                "started_at_epoch": chunk_started_at_epoch,
                "completed_at_epoch": time.time(),
            },
        )
        if res.returncode == 0 or add_recovered:
            wait_started_at = time.monotonic()
            wait_started_at_epoch = time.time()
            log_action(
                "nlm_batch_source_materialization_wait_started",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "reset_depth": reset_depth,
                    "source_profile": source_profile,
                    "source_count_before_wait": source_count_after,
                    "timeout_s": DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S,
                    "started_at_epoch": wait_started_at_epoch,
                },
            )
            wait_succeeded = self._wait_for_sources_ready(
                expected_total,
                timeout=DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S,
                source_count_before_wait=source_count_after,
            )
            wait_elapsed_s = round(time.monotonic() - wait_started_at, 3)
            self._last_materialization_wait_elapsed_s = wait_elapsed_s
            wait_completed_at_epoch = time.time()
            self._last_materialization_ready_at_epoch = wait_completed_at_epoch
            if not wait_succeeded:
                timeout_s = DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S
                print(f"[NLM-Batch]   ERROR: after {timeout_s}s sources still not ready; halting test.")
                self._last_add_failure_reason = "materialization_wait_failed"
                log_action(
                    "nlm_batch_source_materialization_wait_failed",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "expected_total": expected_total,
                        "retry_depth": retry_depth,
                        "reset_depth": reset_depth,
                        "source_profile": source_profile,
                        "failure_reason": "materialization_wait_failed",
                        "elapsed_s": wait_elapsed_s,
                        "source_count_after_wait": self._get_current_source_count(),
                        "source_count_before_wait": source_count_after,
                        "timeout_s": timeout_s,
                        "halted": True,
                        "started_at_epoch": wait_started_at_epoch,
                        "completed_at_epoch": wait_completed_at_epoch,
                        "source_materialization_ready_at_epoch": 0.0,
                    },
                )
                raise NotebookSourceMaterializationTimeout(
                    f"NotebookLM sources were not ready after {timeout_s}s "
                    f"(nb_id={self._nb_id}, subbatch_index={subbatch_index}, "
                    f"expected_total={expected_total}, source_count_before_wait={source_count_after})"
                )
            else:
                log_action(
                    "nlm_batch_source_materialization_wait_succeeded",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "expected_total": expected_total,
                        "retry_depth": retry_depth,
                        "reset_depth": reset_depth,
                        "source_profile": source_profile,
                        "elapsed_s": wait_elapsed_s,
                        "source_count_after_wait": self._get_current_source_count(),
                        "source_count_before_wait": source_count_after,
                        "timeout_s": DEFAULT_NOTEBOOKLM_SOURCE_MATERIALIZATION_TIMEOUT_S,
                        "started_at_epoch": wait_started_at_epoch,
                        "completed_at_epoch": wait_completed_at_epoch,
                        "source_materialization_ready_at_epoch": wait_completed_at_epoch,
                    },
                )
            self._last_materialization_ready_at_epoch = wait_completed_at_epoch
            parsed_source_ids = _extract_source_ids_from_add_stdout(res.stdout)
            if len(parsed_source_ids) == len(batch_ids):
                self._last_added_source_ids = parsed_source_ids
            else:
                self._last_added_source_ids = []
                log_action(
                    "nlm_batch_subbatch_add_source_id_parse_mismatch",
                    {
                        "nb_id": self._nb_id,
                        "subbatch_index": subbatch_index,
                        "subbatch_size": len(batch_ids),
                        "parsed_source_id_count": len(parsed_source_ids),
                        "expected_source_id_count": len(batch_ids),
                        "retry_depth": retry_depth,
                        "reset_depth": reset_depth,
                        "source_profile": source_profile,
                    },
                )
            for vid in batch_ids:
                self._video_ready_epoch_by_id[vid] = wait_completed_at_epoch
            return list(batch_ids)

        print(
            f"[NLM-Batch]   Sub-batch {subbatch_index} add rc={res.returncode}"
            f" (retry_depth={retry_depth})"
        )
        if res.stderr:
            print(f"[NLM-Batch]   stderr: {res.stderr[:200]}")

        self._last_add_failure_reason = _classify_subbatch_add_failure(res, materialization_waited=False)
        if count_probe_failed:
            self._last_add_failure_reason = "source_count_probe_failed"
        zero_growth_add_failure = (
            res.returncode != 0
            and source_count_before_known
            and source_count_after_known
            and source_count_after == source_count_before
        )
        failure_is_probe_or_zero_growth = zero_growth_add_failure or count_probe_failed
        dead_notebook_probe = count_probe_failed and _source_count_probe_indicates_dead_notebook(source_count_after_error)
        if dead_notebook_probe and dead_notebook_recreate_depth == 0:
            log_action(
                "nlm_batch_dead_notebook_recovery_scheduled",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": len(batch_ids),
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "reset_depth": reset_depth,
                    "source_profile": source_profile,
                    "source_count_before": source_count_before,
                    "source_count_probe_ok_before": source_count_before_known,
                    "source_count_after": source_count_after,
                    "source_count_probe_ok_after": source_count_after_known,
                    "source_count_probe_error": source_count_after_error,
                    "failure_reason": self._last_add_failure_reason,
                    "stdout": (res.stdout or "")[:200],
                    "stderr": (res.stderr or "")[:200],
                },
            )
            print(
                f"[NLM-Batch]   Sub-batch {subbatch_index} notebook missing; "
                f"creating a fresh notebook and retrying"
            )
            if self._recover_dead_notebook():
                return self._add_sources_chunk(
                    batch_ids,
                    subbatch_index=subbatch_index,
                    expected_total=expected_total,
                    retry_depth=0,
                    reset_depth=0,
                    dead_notebook_recreate_depth=1,
                    source_profile=source_profile,
                )
            self._last_add_failure_reason = "dead_notebook_recreate_failed"
            return []
        if failure_is_probe_or_zero_growth and reset_depth == 0 and retry_depth < _ZERO_GROWTH_ADD_RETRY_LIMIT:
            retry_delay_s = _ZERO_GROWTH_ADD_RETRY_DELAY_S
            log_action(
                "nlm_batch_subbatch_add_retry_scheduled",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": len(batch_ids),
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "next_retry_depth": retry_depth + 1,
                    "reset_depth": reset_depth,
                    "dead_notebook_recreate_depth": dead_notebook_recreate_depth,
                    "retry_delay_s": retry_delay_s,
                    "returncode": res.returncode,
                    "source_profile": source_profile,
                    "source_count_before": source_count_before,
                    "source_count_probe_ok_before": source_count_before_known,
                    "source_count_after": source_count_after,
                    "source_count_probe_ok_after": source_count_after_known,
                    "failure_reason": self._last_add_failure_reason,
                    "stdout": (res.stdout or "")[:200],
                    "stderr": (res.stderr or "")[:200],
                },
            )
            print(
                f"[NLM-Batch]   Sub-batch {subbatch_index} zero-growth/probe add failure; "
                f"retrying in {retry_delay_s:.1f}s (retry_depth={retry_depth})"
            )
            time.sleep(retry_delay_s)
            return self._add_sources_chunk(
                batch_ids,
                subbatch_index=subbatch_index,
                expected_total=expected_total,
                retry_depth=retry_depth + 1,
                reset_depth=reset_depth,
                dead_notebook_recreate_depth=dead_notebook_recreate_depth,
                source_profile=source_profile,
            )
        if failure_is_probe_or_zero_growth and reset_depth < _ZERO_GROWTH_ADD_RESET_RETRY_LIMIT:
            reset_delay_s = _ZERO_GROWTH_ADD_RETRY_DELAY_S
            log_action(
                "nlm_batch_subbatch_add_notebook_reset_scheduled",
                {
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": len(batch_ids),
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "reset_depth": reset_depth,
                    "next_reset_depth": reset_depth + 1,
                    "dead_notebook_recreate_depth": dead_notebook_recreate_depth,
                    "retry_delay_s": reset_delay_s,
                    "returncode": res.returncode,
                    "source_profile": source_profile,
                    "source_count_before": source_count_before,
                    "source_count_probe_ok_before": source_count_before_known,
                    "source_count_after": source_count_after,
                    "source_count_probe_ok_after": source_count_after_known,
                    "failure_reason": self._last_add_failure_reason,
                    "stdout": (res.stdout or "")[:200],
                    "stderr": (res.stderr or "")[:200],
                },
            )
            print(
                f"[NLM-Batch]   Sub-batch {subbatch_index} zero-growth/probe add failure; "
                f"resetting notebook and retrying in {reset_delay_s:.1f}s "
                f"(retry_depth={retry_depth}, reset_depth={reset_depth})"
            )
            self._rotate_notebook()
            time.sleep(reset_delay_s)
            return self._add_sources_chunk(
                batch_ids,
                subbatch_index=subbatch_index,
                expected_total=expected_total,
                retry_depth=0,
                reset_depth=reset_depth + 1,
                dead_notebook_recreate_depth=dead_notebook_recreate_depth,
                source_profile=source_profile,
            )
        terminal_batch_identity = _summarize_add_failure_batch_ids(batch_ids)
        source_count_probe_error = (
            source_count_after_error
            if not source_count_after_known
            else source_count_before_error
        )
        if zero_growth_add_failure:
            log_action(
                "nlm_batch_subbatch_zero_growth_terminal",
                {
                    **terminal_batch_identity,
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": len(batch_ids),
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "reset_depth": reset_depth,
                    "returncode": res.returncode,
                    "elapsed_s": add_cmd_elapsed_s,
                    "source_profile": source_profile,
                    "source_count_before": source_count_before,
                    "source_count_probe_ok_before": source_count_before_known,
                    "source_count_after": source_count_after,
                    "source_count_probe_ok_after": source_count_after_known,
                    "source_count_probe_error_before": source_count_before_error,
                    "source_count_probe_error_after": source_count_after_error,
                    "source_count_probe_error": source_count_probe_error,
                    "failure_reason": self._last_add_failure_reason,
                    "stdout": (res.stdout or "")[:500],
                    "stderr": (res.stderr or "")[:500],
                    "dead_notebook_recreate_depth": dead_notebook_recreate_depth,
                },
            )
        elif count_probe_failed:
            log_action(
                "nlm_batch_subbatch_source_count_probe_terminal",
                {
                    **terminal_batch_identity,
                    "nb_id": self._nb_id,
                    "subbatch_index": subbatch_index,
                    "subbatch_size": len(batch_ids),
                    "expected_total": expected_total,
                    "retry_depth": retry_depth,
                    "reset_depth": reset_depth,
                    "returncode": res.returncode,
                    "elapsed_s": add_cmd_elapsed_s,
                    "source_profile": source_profile,
                    "source_count_before": source_count_before,
                    "source_count_probe_ok_before": source_count_before_known,
                    "source_count_after": source_count_after,
                    "source_count_probe_ok_after": source_count_after_known,
                    "source_count_probe_error_before": source_count_before_error,
                    "source_count_probe_error_after": source_count_after_error,
                    "source_count_probe_error": source_count_probe_error,
                    "failure_reason": self._last_add_failure_reason,
                    "stdout": (res.stdout or "")[:500],
                    "stderr": (res.stderr or "")[:500],
                },
            )
        log_action(
            "nlm_batch_subbatch_add_failed",
            {
                **terminal_batch_identity,
                "nb_id": self._nb_id,
                "subbatch_index": subbatch_index,
                "subbatch_size": len(batch_ids),
                "expected_total": expected_total,
                "retry_depth": retry_depth,
                "returncode": res.returncode,
                "elapsed_s": add_cmd_elapsed_s,
                "source_profile": source_profile,
                "source_count_before": source_count_before,
                "source_count_probe_ok_before": source_count_before_known,
                "source_count_after": source_count_after,
                "source_count_probe_ok_after": source_count_after_known,
                "source_count_probe_error_before": source_count_before_error,
                "source_count_probe_error_after": source_count_after_error,
                "source_count_probe_error": source_count_probe_error,
                "reset_depth": reset_depth,
                "failure_reason": self._last_add_failure_reason,
                "stdout": (res.stdout or "")[:200],
                "stderr": (res.stderr or "")[:200],
            },
        )
        return []

    def _add_sources_in_subbatches(self, batch_ids: List[str], subbatch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE) -> List[str]:
        """Add sources in sub-batches to avoid NLM overload.

        The reusable industrial path defaults to a 50-source window, which
        matches the free-tier NotebookLM notebook limit for this workspace.
        Smaller or larger windows can still be passed explicitly for sweeps or
        recovery if needed.
        """
        total = len(batch_ids)
        added_ids: List[str] = []
        self._last_subbatch_metrics = []
        self._video_ready_epoch_by_id = {}
        current_subbatch_size = max(1, subbatch_size)
        next_index = 0
        subbatch_index = 0
        added_source_ids: List[str] = []
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
            try:
                added_chunk_ids = self._add_sources_chunk(
                    subbatch,
                    subbatch_index=subbatch_index,
                    expected_total=next_index + len(subbatch),
                    source_profile=source_profile,
                )
            except NotebookSourceMaterializationTimeout:
                self._last_subbatch_metrics.append(
                    {
                        "subbatch_index": subbatch_index,
                        "subbatch_size": window_size,
                        "target_subbatch_size": current_subbatch_size,
                        "attempted_count": len(subbatch),
                        "added_count": len(subbatch),
                        "add_cmd_elapsed_s": float(getattr(self, "_last_add_cmd_elapsed_s", 0.0) or 0.0),
                        "materialization_wait_elapsed_s": float(getattr(self, "_last_materialization_wait_elapsed_s", 0.0) or 0.0),
                        "elapsed_s": float(
                            (getattr(self, "_last_add_cmd_elapsed_s", 0.0) or 0.0)
                            + (getattr(self, "_last_materialization_wait_elapsed_s", 0.0) or 0.0)
                        ),
                        "returncode": self._last_add_returncode,
                        "failure_reason": self._last_add_failure_reason,
                        "source_profile": source_profile,
                        "current_source_count": self._get_current_source_count(),
                        "status": "materialization_wait_timeout",
                        "source_materialization_ready_at_epoch": 0.0,
                    }
                )
                raise
            # Track running source count after each subbatch
            self._current_source_count = self._get_current_source_count()
            added_ids.extend(added_chunk_ids)
            added_source_ids.extend(self._last_added_source_ids)
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
                "source_materialization_ready_at_epoch": float(
                    getattr(self, "_last_materialization_ready_at_epoch", 0.0) or 0.0
                ),
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
        self._last_added_source_ids = added_source_ids
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

    def extract_transcripts(
        self,
        batch_ids: List[str],
        *,
        _allow_dead_notebook_recovery: bool = True,
    ) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Extract using high-speed 'source content' method."""
        start = time.time()
        ready_reference_epoch = float(getattr(self, "_last_materialization_ready_at_epoch", 0.0) or 0.0)
        # 1. Get Source List
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0: return {vid: (False, None, "List failed") for vid in batch_ids}
        
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict): sources = sources.get("sources", [])
        except:
            return {vid: (False, None, "Parse failed") for vid in batch_ids}

        # 2. Map Source IDs to Video IDs
        # Prefer exact NotebookLM source title/url matches first because list
        # order is not guaranteed to be stable enough for correlation.
        source_id_list = [str(s.get("id") or "").strip() for s in sources if isinstance(s, dict) and str(s.get("id") or "").strip()]
        source_id_by_video_id: dict[str, str] = {}
        for source in sources:
            source_id = str(source.get("id") or "").strip() if isinstance(source, dict) else ""
            video_id = _extract_video_id_from_source_entry(source)
            if source_id and video_id and video_id not in source_id_by_video_id:
                source_id_by_video_id[video_id] = source_id
        title_match_count = sum(1 for vid in batch_ids if vid in source_id_by_video_id)
        order_fallback_count = max(0, len(batch_ids) - title_match_count)
        canonical_source_ids = [
            str(source_id).strip()
            for source_id in getattr(self, "_last_added_source_ids", [])
            if str(source_id or "").strip()
        ]
        missing_video_ids = [vid for vid in batch_ids if vid not in source_id_by_video_id]
        mapping_failure_reason = ""
        if canonical_source_ids:
            if len(canonical_source_ids) != len(batch_ids):
                mapping_failure_reason = "Source mapping failed"
            else:
                source_id_by_video_id = dict(zip(batch_ids, canonical_source_ids))
                source_id_list = canonical_source_ids
                title_match_count = len(batch_ids)
                order_fallback_count = 0
                missing_video_ids = []
        elif missing_video_ids:
            if len(source_id_list) == len(batch_ids):
                fallback_video_ids = [vid for vid in batch_ids if vid not in source_id_by_video_id]
                used_source_ids = {str(source_id).strip() for source_id in source_id_by_video_id.values() if str(source_id or "").strip()}
                fallback_source_ids = [
                    source_id
                    for source_id in source_id_list
                    if source_id not in used_source_ids
                ]
                if len(fallback_source_ids) == len(fallback_video_ids):
                    for vid, source_id in zip(fallback_video_ids, fallback_source_ids):
                        source_id_by_video_id[vid] = source_id
                    missing_video_ids = []
                    order_fallback_count = len(fallback_video_ids)
                else:
                    mapping_failure_reason = "Source mapping failed"
            else:
                mapping_failure_reason = "Source mapping failed"
        duplicate_source_ids = []
        if not mapping_failure_reason:
            seen_source_ids: dict[str, int] = {}
            for source_id in source_id_by_video_id.values():
                seen_source_ids[source_id] = seen_source_ids.get(source_id, 0) + 1
            duplicate_source_ids = [source_id for source_id, count in seen_source_ids.items() if count > 1]
            if duplicate_source_ids:
                mapping_failure_reason = "Source mapping failed"
        if mapping_failure_reason:
            log_action(
                "nlm_batch_source_mapping_failed",
                {
                    "nb_id": self._nb_id,
                    "batch_size": len(batch_ids),
                    "source_id_title_match_count": title_match_count,
                    "source_id_order_fallback_count": order_fallback_count,
                    "duplicate_source_ids": duplicate_source_ids,
                    "canonical_source_id_count": len(canonical_source_ids),
                    "expected_source_id_count": len(batch_ids),
                    "missing_video_ids": missing_video_ids[:10],
                    "source_ids": canonical_source_ids[:10],
                    "video_ids": batch_ids[:10],
                    "materialization_ready_at_epoch": ready_reference_epoch,
                },
            )
        
        results = {}
        content_fetch_stats = {
            "status_counts": {"ready": 0, _NLM_CONTENT_BELOW_THRESHOLD_STATUS: 0, "command_failed": 0, "parse_failed": 0},
            "ready_age_s_total": 0.0,
            "ready_age_s_max": 0.0,
            "attempts_total": 0,
            "attempts_max": 0,
            "youtube_ytdlp_elapsed_s_total": 0.0,
            "youtube_ytdlp_elapsed_s_max": 0.0,
            "youtube_ytdlp_elapsed_s_count": 0,
            "youtube_page_elapsed_s_total": 0.0,
            "youtube_page_elapsed_s_max": 0.0,
            "youtube_page_elapsed_s_count": 0,
        }
        status_lock = threading.Lock()

        def _record_youtube_probe_elapsed_metrics(
            ytdlp_probe: dict[str, object],
            youtube_page_probe: dict[str, object],
        ) -> None:
            ytdlp_elapsed_s = float(ytdlp_probe.get("elapsed_s", 0) or 0.0) if ytdlp_probe else 0.0
            youtube_page_elapsed_s = float(youtube_page_probe.get("elapsed_s", 0) or 0.0) if youtube_page_probe else 0.0
            with status_lock:
                if ytdlp_probe:
                    content_fetch_stats["youtube_ytdlp_elapsed_s_total"] += ytdlp_elapsed_s
                    content_fetch_stats["youtube_ytdlp_elapsed_s_max"] = max(
                        content_fetch_stats["youtube_ytdlp_elapsed_s_max"],
                        ytdlp_elapsed_s,
                    )
                    content_fetch_stats["youtube_ytdlp_elapsed_s_count"] += 1
                if youtube_page_probe:
                    content_fetch_stats["youtube_page_elapsed_s_total"] += youtube_page_elapsed_s
                    content_fetch_stats["youtube_page_elapsed_s_max"] = max(
                        content_fetch_stats["youtube_page_elapsed_s_max"],
                        youtube_page_elapsed_s,
                    )
                    content_fetch_stats["youtube_page_elapsed_s_count"] += 1
        log_action(
            "nlm_batch_extract_started",
            {
                "nb_id": self._nb_id,
                "batch_size": len(batch_ids),
                "sources_visible": len(sources),
                "materialization_ready_at_epoch": ready_reference_epoch,
                "source_id_title_match_count": title_match_count,
                "source_id_order_fallback_count": order_fallback_count,
            },
        )

        def _probe_source_content_readiness(source_id: str, vid_hint: str) -> dict[str, object]:
            """Poll a single source until content becomes readable or timeout."""
            probe_started_at = time.monotonic()
            probe_started_at_epoch = time.time()
            probe_deadline = probe_started_at + _READY_PROBE_TIMEOUT_S
            probe_attempt = 0
            while True:
                probe_attempt += 1
                started_at_epoch = time.time()
                ready_age_s = round(started_at_epoch - ready_reference_epoch, 3) if ready_reference_epoch else 0.0
                log_action(
                    "nlm_batch_source_content_readiness_probe_started",
                    {
                        "nb_id": self._nb_id,
                        "source_id": source_id,
                        "video_id": vid_hint,
                        "probe_attempt": probe_attempt,
                        "timeout_s": 30,
                        "probe_started_at_epoch": started_at_epoch,
                        "source_ready_age_s": ready_age_s,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
                res = self._run_cmd(["source", "content", source_id, "--json"], timeout=30)
                completed_at_epoch = time.time()
                content = ""
                content_length = 0
                status = "command_failed" if res.returncode != 0 else "parse_failed"
                if res.returncode == 0:
                    try:
                        data = json.loads(res.stdout)
                        if isinstance(data, dict):
                            content = data.get("value", {}).get("content", "")
                            if not content:
                                content = data.get("content", "")
                        content_length = len(content)
                        if content_length > _NLM_CONTENT_READY_THRESHOLD:
                            status = "ready"
                            log_action(
                                "nlm_batch_source_content_readiness_probe_completed",
                                {
                                    "nb_id": self._nb_id,
                                    "source_id": source_id,
                                    "video_id": vid_hint,
                                    "probe_attempt": probe_attempt,
                                    "timeout_s": 30,
                                    "probe_started_at_epoch": started_at_epoch,
                                    "probe_completed_at_epoch": completed_at_epoch,
                                    "elapsed_s": round(completed_at_epoch - started_at_epoch, 3),
                                    "returncode": res.returncode,
                                    "content_length": content_length,
                                    "status": status,
                                    "ready_threshold": _NLM_CONTENT_READY_THRESHOLD,
                                    "extraction_outcome": "nlm_ready",
                                    "nlm_content_chars": content_length,
                                    "usable_text_chars": content_length,
                                    "source_ready_age_s": ready_age_s,
                                    "materialization_ready_at_epoch": ready_reference_epoch,
                                },
                            )
                            return {
                                "status": status,
                                "attempts": probe_attempt,
                                "content_length": content_length,
                                "ready_at_epoch": completed_at_epoch,
                            }
                        status = _NLM_CONTENT_BELOW_THRESHOLD_STATUS
                    except Exception:
                        status = "parse_failed"
                log_action(
                    "nlm_batch_source_content_readiness_probe_completed",
                    {
                        "nb_id": self._nb_id,
                        "source_id": source_id,
                        "video_id": vid_hint,
                        "probe_attempt": probe_attempt,
                        "timeout_s": 30,
                        "probe_started_at_epoch": started_at_epoch,
                        "probe_completed_at_epoch": completed_at_epoch,
                        "elapsed_s": round(completed_at_epoch - started_at_epoch, 3),
                        "returncode": res.returncode,
                        "content_length": content_length,
                        "status": status,
                        "ready_threshold": _NLM_CONTENT_READY_THRESHOLD,
                        "extraction_outcome": status,
                        "nlm_content_chars": content_length,
                        "usable_text_chars": 0,
                        "source_ready_age_s": ready_age_s,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                        "stdout": (res.stdout or "")[:200],
                        "stderr": (res.stderr or "")[:200],
                    },
                )
                if time.monotonic() >= probe_deadline:
                    return {
                        "status": status,
                        "attempts": probe_attempt,
                        "content_length": content_length,
                        "ready_at_epoch": 0.0,
                    }
                time.sleep(_READY_PROBE_INTERVAL_S)

        if _READY_PROBE_EARLY and batch_ids and not mapping_failure_reason:
            probe_video_id = batch_ids[0]
            probe_source_id = source_id_by_video_id.get(probe_video_id) or (source_id_list[0] if source_id_list else "")
            if probe_source_id:
                log_action(
                    "nlm_batch_source_content_readiness_probe_window_started",
                    {
                        "nb_id": self._nb_id,
                        "video_id": probe_video_id,
                        "source_id": probe_source_id,
                        "timeout_s": _READY_PROBE_TIMEOUT_S,
                        "poll_interval_s": _READY_PROBE_INTERVAL_S,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
                probe_result = _probe_source_content_readiness(probe_source_id, probe_video_id)
                log_action(
                    "nlm_batch_source_content_readiness_probe_window_completed",
                    {
                        "nb_id": self._nb_id,
                        "video_id": probe_video_id,
                        "source_id": probe_source_id,
                        "timeout_s": _READY_PROBE_TIMEOUT_S,
                        "poll_interval_s": _READY_PROBE_INTERVAL_S,
                        "probe_result": probe_result,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
        
        def _fetch_content_round(
            source_id: str,
            vid_hint: str,
            *,
            pass_name: str,
            allow_retry_queue: bool,
        ) -> dict[str, object]:
            """Fetch source content with NotebookLM retries and optional second-pass queuing."""
            started_at_epoch = time.time()
            attempt = 0
            delay_s = _SOURCE_CONTENT_RETRY_INITIAL_DELAY_S
            retry_deadline = (
                started_at_epoch + _SOURCE_CONTENT_RETRY_BUDGET_S
                if _SOURCE_CONTENT_RETRY_BUDGET_S > 0
                else None
            )
            last_result: dict[str, object] = {
                "status": "command_failed",
                "content_length": 0,
                "failure_reason": f"Fetch failed for {source_id}: command_failed",
                "returncode": 1,
                "stdout": "",
                "stderr": "",
                "completed_at_epoch": started_at_epoch,
                "attempts": 0,
                "content": None,
            }
            log_action(
                "nlm_batch_source_content_fetch_started",
                {
                    "nb_id": self._nb_id,
                    "source_id": source_id,
                    "video_id": vid_hint,
                    "timeout_s": 30,
                    "retry_budget_s": _SOURCE_CONTENT_RETRY_BUDGET_S,
                    "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                    "started_at_epoch": started_at_epoch,
                    "source_ready_age_s": round(started_at_epoch - ready_reference_epoch, 3) if ready_reference_epoch else 0.0,
                    "materialization_ready_at_epoch": ready_reference_epoch,
                    "pass_name": pass_name,
                },
            )

            while True:
                attempt += 1
                attempt_started_at_epoch = time.time()
                attempt_ready_age_s = round(attempt_started_at_epoch - ready_reference_epoch, 3) if ready_reference_epoch else 0.0
                res = self._run_cmd(["source", "content", source_id, "--json"], timeout=30)
                attempt_completed_at_epoch = time.time()
                content = ""
                content_length = 0
                status = "command_failed" if res.returncode != 0 else "parse_failed"
                failure_reason = f"Fetch failed for {source_id}: {status}"
                retryable = False
                if res.returncode == 0:
                    try:
                        data = json.loads(res.stdout)
                        if isinstance(data, dict):
                            content = data.get("value", {}).get("content", "")
                            if not content:
                                content = data.get("content", "")
                        content_length = len(content)
                        if content_length > _NLM_CONTENT_READY_THRESHOLD:
                            status = "ready"
                            with status_lock:
                                content_fetch_stats["status_counts"][status] = content_fetch_stats["status_counts"].get(status, 0) + 1
                                content_fetch_stats["ready_age_s_total"] += attempt_ready_age_s
                                content_fetch_stats["ready_age_s_max"] = max(content_fetch_stats["ready_age_s_max"], attempt_ready_age_s)
                                content_fetch_stats["attempts_total"] += attempt
                                content_fetch_stats["attempts_max"] = max(content_fetch_stats["attempts_max"], attempt)
                            log_action(
                                "nlm_batch_source_content_fetch_completed",
                                {
                                    "nb_id": self._nb_id,
                                    "source_id": source_id,
                                    "video_id": vid_hint,
                                    "timeout_s": 30,
                                    "started_at_epoch": started_at_epoch,
                                    "completed_at_epoch": attempt_completed_at_epoch,
                                    "elapsed_s": round(attempt_completed_at_epoch - started_at_epoch, 3),
                                    "returncode": res.returncode,
                                    "content_length": content_length,
                                    "status": status,
                                    "ready_threshold": _NLM_CONTENT_READY_THRESHOLD,
                                    "extraction_outcome": "nlm_ready",
                                    "nlm_content_chars": content_length,
                                    "usable_text_chars": content_length,
                                    "source_ready_age_s": attempt_ready_age_s,
                                    "materialization_ready_at_epoch": ready_reference_epoch,
                                    "attempts": attempt,
                                    "pass_name": pass_name,
                                },
                            )
                            return {
                                "video_id": vid_hint,
                                "source_id": source_id,
                                "success": True,
                                "content": content,
                                "error": None,
                                "status": status,
                                "queued_for_retry": False,
                                "attempts": attempt,
                                "returncode": res.returncode,
                                "content_length": content_length,
                                "nlm_content_chars": content_length,
                                "usable_text_chars": content_length,
                                "youtube_ytdlp_classification": None,
                            }
                        status = _NLM_CONTENT_BELOW_THRESHOLD_STATUS
                        failure_reason = f"Fetch failed for {source_id}: {status}"
                        retryable = _should_retry_source_content_fetch(status, res)
                    except Exception:
                        status = "parse_failed"
                        failure_reason = f"Fetch failed for {source_id}: {status}"
                else:
                    retryable = _should_retry_source_content_fetch(status, res)
                last_result = {
                    "status": status,
                    "content_length": content_length,
                    "failure_reason": failure_reason,
                    "returncode": res.returncode,
                    "stdout": res.stdout or "",
                    "stderr": res.stderr or "",
                    "completed_at_epoch": attempt_completed_at_epoch,
                    "attempts": attempt,
                    "content": None,
                }
                if retry_deadline is not None and time.time() >= retry_deadline:
                    break
                if not retryable or attempt >= _SOURCE_CONTENT_RETRY_ATTEMPTS:
                    break
                if retry_deadline is not None:
                    remaining_budget_s = retry_deadline - time.time()
                    if remaining_budget_s <= 0:
                        break
                    delay_s = min(delay_s, remaining_budget_s)
                if delay_s <= 0:
                    break
                time.sleep(delay_s)
                delay_s = min(delay_s * 2 if delay_s > 0 else _SOURCE_CONTENT_RETRY_INITIAL_DELAY_S, _SOURCE_CONTENT_RETRY_MAX_DELAY_S)

            final_completed_at_epoch = time.time()
            final_status = str(last_result["status"])
            final_ready_age_s = round(final_completed_at_epoch - ready_reference_epoch, 3) if ready_reference_epoch else 0.0
            youtube_ytdlp_probe: dict[str, object] = {}
            youtube_page_probe: dict[str, object] = {}
            if final_status != "ready" and vid_hint:
                youtube_ytdlp_probe = inspect_youtube_watch_page_via_ytdlp(vid_hint)
                if str(youtube_ytdlp_probe.get("classification") or "").strip() in {"error", "unknown"}:
                    youtube_page_probe = inspect_youtube_watch_page(vid_hint)
                _record_youtube_probe_elapsed_metrics(youtube_ytdlp_probe, youtube_page_probe)
            retry_queue_eligible = (
                allow_retry_queue
                and _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S > 0
                and _should_defer_source_content_fetch(youtube_ytdlp_probe, final_status)
            )
            if retry_queue_eligible:
                log_action(
                    "nlm_batch_source_content_retry_queued",
                    {
                        "nb_id": self._nb_id,
                        "source_id": source_id,
                        "video_id": vid_hint,
                        "status": final_status,
                        "attempts": int(last_result["attempts"]),
                        "retry_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                        "youtube_ytdlp_classification": youtube_ytdlp_probe.get("classification"),
                        "youtube_ytdlp_available": youtube_ytdlp_probe.get("available"),
                        "youtube_ytdlp_availability": youtube_ytdlp_probe.get("availability"),
                        "pass_name": pass_name,
                        "source_ready_age_s": final_ready_age_s,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
                return {
                    "video_id": vid_hint,
                    "source_id": source_id,
                    "success": False,
                    "content": None,
                    "error": None,
                    "failure_reason": str(last_result["failure_reason"]),
                    "status": final_status,
                    "queued_for_retry": True,
                    "attempts": int(last_result["attempts"]),
                    "returncode": int(last_result["returncode"]),
                    "content_length": int(last_result["content_length"]),
                    "nlm_content_chars": int(last_result["content_length"]),
                    "usable_text_chars": 0,
                    "extraction_outcome": final_status,
                    "stdout": str(last_result["stdout"])[:200],
                    "stderr": str(last_result["stderr"])[:200],
                    "youtube_ytdlp_classification": youtube_ytdlp_probe.get("classification"),
                    "youtube_ytdlp_available": youtube_ytdlp_probe.get("available"),
                    "youtube_ytdlp_availability": youtube_ytdlp_probe.get("availability"),
                    "youtube_ytdlp_live_status": youtube_ytdlp_probe.get("live_status"),
                    "youtube_ytdlp_was_live": youtube_ytdlp_probe.get("was_live"),
                    "youtube_ytdlp_is_live": youtube_ytdlp_probe.get("is_live"),
                    "youtube_ytdlp_title": youtube_ytdlp_probe.get("title"),
                    "youtube_ytdlp_returncode": youtube_ytdlp_probe.get("returncode"),
                    "youtube_ytdlp_error": youtube_ytdlp_probe.get("error"),
                    "youtube_page_classification": youtube_page_probe.get("classification"),
                    "youtube_page_available": youtube_page_probe.get("available"),
                    "youtube_page_status": youtube_page_probe.get("status"),
                    "youtube_page_reason": youtube_page_probe.get("reason"),
                    "youtube_page_subreason": youtube_page_probe.get("subreason"),
                    "youtube_page_is_live_content": youtube_page_probe.get("is_live_content"),
                    "youtube_page_title": youtube_page_probe.get("title"),
                    "youtube_page_http_status": youtube_page_probe.get("http_status"),
                    "youtube_page_error": youtube_page_probe.get("error"),
                }
            with status_lock:
                content_fetch_stats["status_counts"][final_status] = content_fetch_stats["status_counts"].get(final_status, 0) + 1
                content_fetch_stats["ready_age_s_total"] += final_ready_age_s
                content_fetch_stats["ready_age_s_max"] = max(content_fetch_stats["ready_age_s_max"], final_ready_age_s)
                content_fetch_stats["attempts_total"] += int(last_result["attempts"])
                content_fetch_stats["attempts_max"] = max(content_fetch_stats["attempts_max"], int(last_result["attempts"]))

            log_action(
                "nlm_batch_source_content_fetch_completed",
                {
                    "nb_id": self._nb_id,
                    "source_id": source_id,
                    "video_id": vid_hint,
                    "timeout_s": 30,
                    "started_at_epoch": started_at_epoch,
                    "completed_at_epoch": final_completed_at_epoch,
                    "elapsed_s": round(final_completed_at_epoch - started_at_epoch, 3),
                    "returncode": int(last_result["returncode"]),
                    "content_length": int(last_result["content_length"]),
                    "status": final_status,
                    "ready_threshold": _NLM_CONTENT_READY_THRESHOLD,
                    "extraction_outcome": final_status,
                    "nlm_content_chars": int(last_result["content_length"]),
                    "usable_text_chars": 0,
                    "source_ready_age_s": final_ready_age_s,
                    "materialization_ready_at_epoch": ready_reference_epoch,
                    "failure_reason": str(last_result["failure_reason"]),
                    "attempts": int(last_result["attempts"]),
                    "stdout": str(last_result["stdout"])[:200],
                    "stderr": str(last_result["stderr"])[:200],
                    "retry_initial_delay_s": _SOURCE_CONTENT_RETRY_INITIAL_DELAY_S,
                    "retry_max_delay_s": _SOURCE_CONTENT_RETRY_MAX_DELAY_S,
                    "retry_budget_s": _SOURCE_CONTENT_RETRY_BUDGET_S,
                    "retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                    "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                    "retry_attempts_limit": _SOURCE_CONTENT_RETRY_ATTEMPTS,
                    "pass_name": pass_name,
                    "youtube_ytdlp_classification": youtube_ytdlp_probe.get("classification"),
                    "youtube_ytdlp_available": youtube_ytdlp_probe.get("available"),
                    "youtube_ytdlp_availability": youtube_ytdlp_probe.get("availability"),
                    "youtube_ytdlp_live_status": youtube_ytdlp_probe.get("live_status"),
                    "youtube_ytdlp_was_live": youtube_ytdlp_probe.get("was_live"),
                    "youtube_ytdlp_is_live": youtube_ytdlp_probe.get("is_live"),
                    "youtube_ytdlp_title": youtube_ytdlp_probe.get("title"),
                    "youtube_ytdlp_returncode": youtube_ytdlp_probe.get("returncode"),
                    "youtube_ytdlp_error": youtube_ytdlp_probe.get("error"),
                    "youtube_ytdlp_elapsed_s": youtube_ytdlp_probe.get("elapsed_s"),
                    "youtube_page_classification": youtube_page_probe.get("classification"),
                    "youtube_page_available": youtube_page_probe.get("available"),
                    "youtube_page_status": youtube_page_probe.get("status"),
                    "youtube_page_reason": youtube_page_probe.get("reason"),
                    "youtube_page_subreason": youtube_page_probe.get("subreason"),
                    "youtube_page_is_live_content": youtube_page_probe.get("is_live_content"),
                    "youtube_page_title": youtube_page_probe.get("title"),
                    "youtube_page_http_status": youtube_page_probe.get("http_status"),
                    "youtube_page_error": youtube_page_probe.get("error"),
                    "youtube_page_elapsed_s": youtube_page_probe.get("elapsed_s"),
                },
            )
            return {
                "video_id": vid_hint,
                "source_id": source_id,
                "success": False,
                "content": None,
                "error": str(last_result["failure_reason"]),
                "status": final_status,
                "queued_for_retry": False,
                "attempts": int(last_result["attempts"]),
                "returncode": int(last_result["returncode"]),
                "content_length": int(last_result["content_length"]),
                "nlm_content_chars": int(last_result["content_length"]),
                "usable_text_chars": 0,
                "extraction_outcome": final_status,
                "stdout": str(last_result["stdout"])[:200],
                "stderr": str(last_result["stderr"])[:200],
                "youtube_ytdlp_classification": youtube_ytdlp_probe.get("classification"),
                "youtube_ytdlp_available": youtube_ytdlp_probe.get("available"),
                "youtube_ytdlp_availability": youtube_ytdlp_probe.get("availability"),
                "youtube_ytdlp_live_status": youtube_ytdlp_probe.get("live_status"),
                "youtube_ytdlp_was_live": youtube_ytdlp_probe.get("was_live"),
                "youtube_ytdlp_is_live": youtube_ytdlp_probe.get("is_live"),
                "youtube_ytdlp_title": youtube_ytdlp_probe.get("title"),
                "youtube_ytdlp_returncode": youtube_ytdlp_probe.get("returncode"),
                "youtube_ytdlp_error": youtube_ytdlp_probe.get("error"),
                "youtube_ytdlp_elapsed_s": youtube_ytdlp_probe.get("elapsed_s"),
                "youtube_page_classification": youtube_page_probe.get("classification"),
                "youtube_page_available": youtube_page_probe.get("available"),
                "youtube_page_status": youtube_page_probe.get("status"),
                "youtube_page_reason": youtube_page_probe.get("reason"),
                "youtube_page_subreason": youtube_page_probe.get("subreason"),
                "youtube_page_is_live_content": youtube_page_probe.get("is_live_content"),
                "youtube_page_title": youtube_page_probe.get("title"),
                "youtube_page_http_status": youtube_page_probe.get("http_status"),
                "youtube_page_error": youtube_page_probe.get("error"),
                "youtube_page_elapsed_s": youtube_page_probe.get("elapsed_s"),
            }

        def _run_fetch_round(
            round_items: list[tuple[str, str]],
            *,
            pass_name: str,
            allow_retry_queue: bool,
        ) -> tuple[
            dict[str, tuple[bool, Optional[str], Optional[str]]],
            list[tuple[str, str, str]],
            dict[str, dict[str, object]],
        ]:
            round_results: dict[str, tuple[bool, Optional[str], Optional[str]]] = {}
            round_retry_queue: list[tuple[str, str, str]] = []
            round_outcomes: dict[str, dict[str, object]] = {}
            if not round_items:
                return round_results, round_retry_queue, round_outcomes
            print(f"[NLM-Batch] Fetching {len(round_items)} sources in parallel ({pass_name})...")
            video_width = max(len(vid) for vid, _ in round_items) if round_items else 0
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(_fetch_content_round, source_id, vid, pass_name=pass_name, allow_retry_queue=allow_retry_queue) for vid, source_id in round_items]
                for future in as_completed(futures):
                    outcome = future.result()
                    vid = str(outcome["video_id"])
                    round_outcomes[vid] = outcome
                    if outcome.get("queued_for_retry"):
                        round_retry_queue.append(
                            (
                                vid,
                                str(outcome["source_id"]),
                                str(outcome.get("failure_reason") or outcome.get("error") or "retry queued"),
                            )
                        )
                        continue
                    success = bool(outcome["success"])
                    text = outcome.get("content")
                    error = outcome.get("error")
                    round_results[vid] = (success, text if isinstance(text, str) else None, error if isinstance(error, str) else None)
                    if success and isinstance(text, str):
                        print(format_result_row(vid, True, f"{len(text)} chars", video_width))
                    else:
                        print(format_result_row(vid, False, str(error) if error is not None else "unknown error", video_width))
            return round_results, round_retry_queue, round_outcomes

        batch_items: list[tuple[str, str]] = []
        for i, vid in enumerate(batch_ids):
            source_id = source_id_by_video_id.get(vid)
            if source_id:
                batch_items.append((vid, source_id))

        retry_queue_deferred_count = 0
        retry_queue_recovered_count = 0
        retry_queue_final_failed_count = 0
        shared_retry_deferred_count = 0
        shared_retry_recovered_count = 0
        shared_retry_final_failed_count = 0
        round_outcomes: dict[str, dict[str, object]] = {}

        if mapping_failure_reason:
            for vid in batch_ids:
                results[vid] = (False, None, mapping_failure_reason)
        else:
            primary_results, retry_queue, primary_outcomes = _run_fetch_round(
                batch_items,
                pass_name="primary",
                allow_retry_queue=True,
            )
            results.update(primary_results)
            retry_queue_deferred_count += len(retry_queue)
            round_outcomes = dict(primary_outcomes)

            if retry_queue and _SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED:
                log_action(
                    "nlm_batch_source_content_shared_retry_queue_window_started",
                    {
                        "nb_id": self._nb_id,
                        "batch_size": len(batch_ids),
                        "shared_retry_queue_count": len(retry_queue),
                        "shared_retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        "shared_retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
                for vid, _source_id, queued_error in retry_queue:
                    enqueue_shared_retry(
                        vid,
                        retry_count=0,
                        delay_s=_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        last_error=str(queued_error or "retry queued"),
                    )
                shared_retry_deferred_count = len(retry_queue)
                log_action(
                    "nlm_batch_source_content_shared_retry_queue_window_completed",
                    {
                        "nb_id": self._nb_id,
                        "batch_size": len(batch_ids),
                        "shared_retry_queue_count": shared_retry_deferred_count,
                        "shared_retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        "shared_retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
            elif retry_queue and _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S > 0:
                log_action(
                    "nlm_batch_source_content_retry_queue_window_started",
                    {
                        "nb_id": self._nb_id,
                        "batch_size": len(batch_ids),
                        "retry_queue_count": len(retry_queue),
                        "retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )
                if _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S > 0:
                    time.sleep(_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S)
                retry_results, retry_queue, retry_outcomes = _run_fetch_round(
                    [(vid, source_id) for vid, source_id, _queued_error in retry_queue],
                    pass_name="retry",
                    allow_retry_queue=False,
                )
                results.update(retry_results)
                round_outcomes.update(retry_outcomes)
                retry_queue_recovered_count = sum(1 for ok, _, _ in retry_results.values() if ok)
                retry_queue_final_failed_count = len(retry_results) - retry_queue_recovered_count
                log_action(
                    "nlm_batch_source_content_retry_queue_window_completed",
                    {
                        "nb_id": self._nb_id,
                        "batch_size": len(batch_ids),
                        "retry_queue_count": retry_queue_deferred_count,
                        "recovered_count": retry_queue_recovered_count,
                        "final_failed_count": retry_queue_final_failed_count,
                        "retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                        "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )

        failed_not_found_video_ids = [
            vid
            for vid in batch_ids
            if vid in results
            and not results[vid][0]
            and _outcome_mentions_not_found(round_outcomes.get(vid, {}))
        ]
        if len(failed_not_found_video_ids) >= 2 and _allow_dead_notebook_recovery:
            log_action(
                "nlm_batch_source_content_dead_notebook_recovery_scheduled",
                {
                    "nb_id": self._nb_id,
                    **_summarize_add_failure_batch_ids(failed_not_found_video_ids),
                    "failed_video_id_count": len(failed_not_found_video_ids),
                    "recovery_reason": "not_found_storm",
                    "materialization_ready_at_epoch": ready_reference_epoch,
                },
            )
            if self._recover_dead_notebook(failed_not_found_video_ids):
                recovery_results = self.extract_transcripts(
                    failed_not_found_video_ids,
                    _allow_dead_notebook_recovery=False,
                )
                recovery_metrics = self.get_last_extract_metrics() or {}
                for key, value in (recovery_metrics.get("content_fetch_status_counts", {}) or {}).items():
                    content_fetch_stats["status_counts"][str(key)] = content_fetch_stats["status_counts"].get(str(key), 0) + int(value or 0)
                content_fetch_stats["ready_age_s_total"] += float(recovery_metrics.get("source_ready_age_s_total", 0) or 0.0)
                content_fetch_stats["ready_age_s_max"] = max(
                    content_fetch_stats["ready_age_s_max"],
                    float(recovery_metrics.get("source_ready_age_s_max", 0) or 0.0),
                )
                content_fetch_stats["attempts_total"] += int(recovery_metrics.get("content_fetch_attempts_total", 0) or 0)
                content_fetch_stats["attempts_max"] = max(
                    content_fetch_stats["attempts_max"],
                    int(recovery_metrics.get("content_fetch_attempts_max", 0) or 0),
                )
                content_fetch_stats["youtube_ytdlp_elapsed_s_total"] += float(recovery_metrics.get("youtube_ytdlp_elapsed_s_total", 0) or 0.0)
                content_fetch_stats["youtube_ytdlp_elapsed_s_max"] = max(
                    content_fetch_stats["youtube_ytdlp_elapsed_s_max"],
                    float(recovery_metrics.get("youtube_ytdlp_elapsed_s_max", 0) or 0.0),
                )
                content_fetch_stats["youtube_ytdlp_elapsed_s_count"] += int(recovery_metrics.get("youtube_ytdlp_elapsed_s_count", 0) or 0)
                content_fetch_stats["youtube_page_elapsed_s_total"] += float(recovery_metrics.get("youtube_page_elapsed_s_total", 0) or 0.0)
                content_fetch_stats["youtube_page_elapsed_s_max"] = max(
                    content_fetch_stats["youtube_page_elapsed_s_max"],
                    float(recovery_metrics.get("youtube_page_elapsed_s_max", 0) or 0.0),
                )
                content_fetch_stats["youtube_page_elapsed_s_count"] += int(recovery_metrics.get("youtube_page_elapsed_s_count", 0) or 0)
                results.update(recovery_results)
                log_action(
                    "nlm_batch_source_content_dead_notebook_recovery_completed",
                    {
                        "nb_id": self._nb_id,
                        **_summarize_add_failure_batch_ids(failed_not_found_video_ids),
                        "failed_video_id_count": len(failed_not_found_video_ids),
                        "recovered_video_id_count": sum(1 for vid in failed_not_found_video_ids if recovery_results.get(vid, (False, None, None))[0]),
                        "recovery_reason": "not_found_storm",
                        "materialization_ready_at_epoch": ready_reference_epoch,
                    },
                )

        for vid in batch_ids:
            if vid not in results:
                results[vid] = (False, None, "Source not found")
        succeeded = sum(1 for ok, _, _ in results.values() if ok)
        log_action(
            "nlm_batch_extract_completed",
            {
                "nb_id": self._nb_id,
                "batch_size": len(batch_ids),
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
                "elapsed_s": round(time.time() - start, 3),
                "retry_queue_deferred_count": retry_queue_deferred_count,
                "retry_queue_recovered_count": retry_queue_recovered_count,
                "retry_queue_final_failed_count": retry_queue_final_failed_count,
                "shared_retry_deferred_count": shared_retry_deferred_count,
                "shared_retry_recovered_count": shared_retry_recovered_count,
                "shared_retry_final_failed_count": shared_retry_final_failed_count,
                "retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
                "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
                "source_ready_age_s_total": round(content_fetch_stats["ready_age_s_total"], 3),
                "source_ready_age_s_max": round(content_fetch_stats["ready_age_s_max"], 3),
                "source_ready_age_s_avg": round(
                    content_fetch_stats["ready_age_s_total"] / max(sum(content_fetch_stats["status_counts"].values()), 1),
                    3,
                ),
                "content_fetch_attempts_total": int(content_fetch_stats["attempts_total"]),
                "content_fetch_attempts_max": int(content_fetch_stats["attempts_max"]),
                "content_fetch_attempts_avg": round(
                    content_fetch_stats["attempts_total"] / max(sum(content_fetch_stats["status_counts"].values()), 1),
                    3,
                ),
                "youtube_ytdlp_elapsed_s_total": round(content_fetch_stats["youtube_ytdlp_elapsed_s_total"], 3),
                "youtube_ytdlp_elapsed_s_max": round(content_fetch_stats["youtube_ytdlp_elapsed_s_max"], 3),
                "youtube_ytdlp_elapsed_s_count": int(content_fetch_stats["youtube_ytdlp_elapsed_s_count"]),
                "youtube_ytdlp_elapsed_s_avg": round(
                    content_fetch_stats["youtube_ytdlp_elapsed_s_total"]
                    / max(int(content_fetch_stats["youtube_ytdlp_elapsed_s_count"]), 1),
                    3,
                ),
                "youtube_page_elapsed_s_total": round(content_fetch_stats["youtube_page_elapsed_s_total"], 3),
                "youtube_page_elapsed_s_max": round(content_fetch_stats["youtube_page_elapsed_s_max"], 3),
                "youtube_page_elapsed_s_count": int(content_fetch_stats["youtube_page_elapsed_s_count"]),
                "youtube_page_elapsed_s_avg": round(
                    content_fetch_stats["youtube_page_elapsed_s_total"]
                    / max(int(content_fetch_stats["youtube_page_elapsed_s_count"]), 1),
                    3,
                ),
                "content_fetch_status_counts": content_fetch_stats["status_counts"],
                "materialization_ready_at_epoch": ready_reference_epoch,
            },
        )
        self._last_extract_metrics = {
            "content_fetch_status_counts": dict(content_fetch_stats["status_counts"]),
            "source_ready_age_s_total": round(content_fetch_stats["ready_age_s_total"], 3),
            "source_ready_age_s_max": round(content_fetch_stats["ready_age_s_max"], 3),
            "source_ready_age_s_avg": round(
                content_fetch_stats["ready_age_s_total"] / max(sum(content_fetch_stats["status_counts"].values()), 1),
                3,
            ),
            "content_fetch_attempts_total": int(content_fetch_stats["attempts_total"]),
            "content_fetch_attempts_max": int(content_fetch_stats["attempts_max"]),
            "content_fetch_attempts_avg": round(
                content_fetch_stats["attempts_total"] / max(sum(content_fetch_stats["status_counts"].values()), 1),
                3,
            ),
            "youtube_ytdlp_elapsed_s_total": round(content_fetch_stats["youtube_ytdlp_elapsed_s_total"], 3),
            "youtube_ytdlp_elapsed_s_max": round(content_fetch_stats["youtube_ytdlp_elapsed_s_max"], 3),
            "youtube_ytdlp_elapsed_s_count": int(content_fetch_stats["youtube_ytdlp_elapsed_s_count"]),
            "youtube_ytdlp_elapsed_s_avg": round(
                content_fetch_stats["youtube_ytdlp_elapsed_s_total"]
                / max(int(content_fetch_stats["youtube_ytdlp_elapsed_s_count"]), 1),
                3,
            ),
            "youtube_page_elapsed_s_total": round(content_fetch_stats["youtube_page_elapsed_s_total"], 3),
            "youtube_page_elapsed_s_max": round(content_fetch_stats["youtube_page_elapsed_s_max"], 3),
            "youtube_page_elapsed_s_count": int(content_fetch_stats["youtube_page_elapsed_s_count"]),
            "youtube_page_elapsed_s_avg": round(
                content_fetch_stats["youtube_page_elapsed_s_total"]
                / max(int(content_fetch_stats["youtube_page_elapsed_s_count"]), 1),
                3,
            ),
            "retry_queue_deferred_count": retry_queue_deferred_count,
            "retry_queue_recovered_count": retry_queue_recovered_count,
            "retry_queue_final_failed_count": retry_queue_final_failed_count,
            "shared_retry_deferred_count": shared_retry_deferred_count,
            "shared_retry_recovered_count": shared_retry_recovered_count,
            "shared_retry_final_failed_count": shared_retry_final_failed_count,
            "retry_queue_delay_s": _SOURCE_CONTENT_RETRY_QUEUE_DELAY_S,
            "retry_queue_budget_s": _SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S,
            "materialization_ready_at_epoch": ready_reference_epoch,
        }

        return results

    def get_last_extract_metrics(self) -> dict[str, object] | None:
        if self._last_extract_metrics is None:
            return None
        return dict(self._last_extract_metrics)

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
        self._last_source_count_probe_ok = True
        self._last_source_count_probe_error = None
        if not self._nb_id:
            return 0
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0:
            self._last_source_count_probe_ok = False
            auth_context = _get_nlm_auth_context()
            self._last_source_count_probe_error = {
                "nb_id": self._nb_id,
                "returncode": res.returncode,
                "notebooklm_profile": auth_context.profile,
                "expected_email": auth_context.expected_email or None,
                "stdout": (res.stdout or "")[:500],
                "stderr": (res.stderr or "")[:500],
            }
            log_action("nlm_batch_source_count_probe_failed", self._last_source_count_probe_error)
            return 0
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict):
                sources = sources.get("sources", [])
            return len(sources)
        except Exception as exc:
            self._last_source_count_probe_ok = False
            auth_context = _get_nlm_auth_context()
            self._last_source_count_probe_error = {
                "nb_id": self._nb_id,
                "returncode": res.returncode,
                "notebooklm_profile": auth_context.profile,
                "expected_email": auth_context.expected_email or None,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "stdout": (res.stdout or "")[:500],
                "stderr": (res.stderr or "")[:500],
            }
            log_action("nlm_batch_source_count_probe_failed", self._last_source_count_probe_error)
            return 0

    def _recover_dead_notebook(self, batch_ids: List[str] | None = None) -> bool:
        """Drop stale reusable state and create a fresh notebook."""
        old_nb_id = self._nb_id
        _clear_reusable_notebook_state()
        self._nb_id = None
        self._current_source_count = 0
        self._last_source_count_probe_ok = True
        self._last_source_count_probe_error = None
        self.create_batch_notebook(list(batch_ids or []))
        log_action(
            "nlm_batch_dead_notebook_recreated",
            {
                "old_nb_id": old_nb_id,
                "nb_id": self._nb_id,
                "recovery_batch_size": len(batch_ids or []),
                "created_new_notebook": bool(self._nb_id),
                "setup_mode": "create" if self._nb_id else "create_failed",
                "notebooklm_profile": _get_notebooklm_profile(),
                "state_path": str(_get_reusable_notebook_state_path()),
            },
        )
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
        return bool(self._nb_id)

    def _rotate_notebook(self) -> None:
        """Recycle the current notebook by clearing sources and keeping the same notebook."""
        old_nb_id = self._nb_id
        old_count = self._current_source_count
        self.reset_sources()
        self._current_source_count = self._get_current_source_count()
        log_action(
            "nlm_batch_notebook_recycled",
            {
                "nb_id": old_nb_id,
                "old_source_count": old_count,
                "new_source_count": self._current_source_count,
                "reason": "source_cap_near_threshold",
                "cap_threshold": _NOTEBOOK_SOURCE_CAP,
            },
        )
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

    def __init__(
        self,
        batch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE,
        cleanup_every_n_batches: int | None = None,
    ):
        self._ingestor = NLMBatchIngestor(batch_size)
        self._nb_id: Optional[str] = _load_reusable_notebook_id()
        self._last_prepare_metrics: dict[str, object] | None = None
        self._last_process_metrics: dict[str, object] | None = None
        cfg = get_nlm_config()
        self._cleanup_every_n_batches = max(
            1,
            int(cleanup_every_n_batches if cleanup_every_n_batches is not None else cfg.reusable_cleanup_every_n_batches),
        )
        self._batches_since_cleanup = 0
        log_action(
            "nlm_batch_reusable_state_loaded",
            {
                "nb_id": self._nb_id,
                "state_path": str(_get_reusable_notebook_state_path()),
                "notebooklm_profile": _get_notebooklm_profile(),
                "status": "loaded" if self._nb_id else "empty",
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
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
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
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
                    "cleanup_every_n_batches": self._cleanup_every_n_batches,
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
        self._batches_since_cleanup = 0
        if self._nb_id:
            _save_reusable_notebook_id(self._nb_id)
            log_action(
                "nlm_batch_reusable_state_saved",
                {
                    "nb_id": self._nb_id,
                    "state_path": str(_get_reusable_notebook_state_path()),
                    "notebooklm_profile": _get_notebooklm_profile(),
                    "cleanup_every_n_batches": self._cleanup_every_n_batches,
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
            "cleanup_every_n_batches": self._cleanup_every_n_batches,
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
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
                "total_elapsed_s": round(time.monotonic() - prep_started_at, 3),
                "strategy": "reusable",
            },
        )
        return True, setup_mode

    def get_last_prepare_metrics(self) -> dict[str, object] | None:
        if self._last_prepare_metrics is None:
            return None
        return dict(self._last_prepare_metrics)

    def get_last_extract_metrics(self) -> dict[str, object] | None:
        if self._last_extract_metrics is None:
            return None
        return dict(self._last_extract_metrics)

    def _is_notebook_usable(self) -> bool:
        if not self._nb_id:
            return False
        self._ingestor._nb_id = self._nb_id
        res = self._ingestor._run_cmd(["source", "list", self._nb_id, "--json"], timeout=60)
        return res.returncode == 0

    def _ensure_notebook(self, batch_ids: List[str]) -> Tuple[bool, str]:
        target_title = _get_reusable_notebook_title()
        list_started_at = time.monotonic()
        notebooks: list[dict[str, object]] = []
        res = self._ingestor._run_cmd(["notebook", "list", "--json"], timeout=30)
        if res.returncode == 0:
            try:
                parsed = json.loads(res.stdout)
                if isinstance(parsed, dict):
                    parsed = parsed.get("notebooks", [])
                if isinstance(parsed, list):
                    notebooks = [nb for nb in parsed if isinstance(nb, dict)]
            except Exception:
                notebooks = []
        title_matches = _find_notebooks_with_title(notebooks, target_title)
        if title_matches:
            duplicate_count = max(0, len(title_matches) - 1)
            keeper = _choose_notebook_keeper(title_matches, preferred_id=self._nb_id or "")
            keeper_id = _notebook_entry_id(keeper) if keeper else ""
            if duplicate_count > 0:
                log_action(
                    "nlm_batch_reusable_title_duplicates_detected",
                    {
                        "nb_title": target_title,
                        "duplicate_count": duplicate_count,
                        "keeper_id": keeper_id,
                        "notebooklm_profile": _get_notebooklm_profile(),
                    },
                )
            if keeper_id:
                previous_nb_id = self._nb_id
                self._nb_id = keeper_id
                if self._is_notebook_usable():
                    self._ingestor._nb_id = self._nb_id
                    _save_reusable_notebook_id(self._nb_id)
                    self._last_ensure_metrics = {
                        "notebook_check_elapsed_s": round(time.monotonic() - list_started_at, 3),
                        "retire_elapsed_s": 0.0,
                        "create_elapsed_s": 0.0,
                    }
                    return False, "reuse"
                self._nb_id = previous_nb_id

        if self._nb_id and self._is_notebook_usable():
            self._ingestor._nb_id = self._nb_id
            _save_reusable_notebook_id(self._nb_id)
            self._last_ensure_metrics = {
                "notebook_check_elapsed_s": round(time.monotonic() - list_started_at, 3),
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
        batch_started_at_epoch = time.time()
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
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
                "batches_since_cleanup": self._batches_since_cleanup,
                "strategy": "reusable",
                "started_at_epoch": batch_started_at_epoch,
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
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
                "batches_since_cleanup": self._batches_since_cleanup,
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
                    "started_at_epoch": batch_started_at_epoch,
                    "completed_at_epoch": time.time(),
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
            if self._ingestor._nb_id and self._ingestor._nb_id != self._nb_id:
                old_nb_id = self._nb_id
                self._nb_id = self._ingestor._nb_id
                _save_reusable_notebook_id(self._nb_id)
                log_action(
                    "nlm_batch_reusable_state_recovered",
                    {
                        "old_nb_id": old_nb_id,
                        "nb_id": self._nb_id,
                        "state_path": str(_get_reusable_notebook_state_path()),
                        "notebooklm_profile": _get_notebooklm_profile(),
                    },
                )
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
            self._batches_since_cleanup += 1
            should_cleanup = self._batches_since_cleanup >= self._cleanup_every_n_batches
            if should_cleanup:
                self._ingestor.reset_sources()  # clear sources, keep notebook
                self._batches_since_cleanup = 0
            if self._nb_id:
                _save_reusable_notebook_id(self._nb_id)
                log_action(
                    "nlm_batch_reusable_state_saved",
                    {
                        "nb_id": self._nb_id,
                        "state_path": str(_get_reusable_notebook_state_path()),
                        "cleanup_every_n_batches": self._cleanup_every_n_batches,
                        "cleanup_performed": should_cleanup,
                    },
                )
            cleanup_elapsed_s = round(time.monotonic() - cleanup_started_at, 3)

        succeeded = sum(1 for success, transcript, _ in results.values() if success and transcript)
        failed = len(results) - succeeded
        total_elapsed_s = round(time.monotonic() - batch_started_at, 3)
        extract_metrics = self._ingestor.get_last_extract_metrics() or {}
        youtube_ytdlp_elapsed_s_total = float(extract_metrics.get("youtube_ytdlp_elapsed_s_total", 0) or 0.0)
        youtube_ytdlp_elapsed_s_max = float(extract_metrics.get("youtube_ytdlp_elapsed_s_max", 0) or 0.0)
        youtube_ytdlp_elapsed_s_count = int(extract_metrics.get("youtube_ytdlp_elapsed_s_count", 0) or 0)
        youtube_ytdlp_elapsed_s_avg = float(extract_metrics.get("youtube_ytdlp_elapsed_s_avg", 0) or 0.0)
        youtube_page_elapsed_s_total = float(extract_metrics.get("youtube_page_elapsed_s_total", 0) or 0.0)
        youtube_page_elapsed_s_max = float(extract_metrics.get("youtube_page_elapsed_s_max", 0) or 0.0)
        youtube_page_elapsed_s_count = int(extract_metrics.get("youtube_page_elapsed_s_count", 0) or 0)
        youtube_page_elapsed_s_avg = float(extract_metrics.get("youtube_page_elapsed_s_avg", 0) or 0.0)
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
                "cleanup_every_n_batches": self._cleanup_every_n_batches,
                "batches_since_cleanup": self._batches_since_cleanup,
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
                "content_fetch_status_counts": dict(extract_metrics.get("content_fetch_status_counts", {}) or {}),
                "source_ready_age_s_total": float(extract_metrics.get("source_ready_age_s_total", 0) or 0.0),
                "source_ready_age_s_max": float(extract_metrics.get("source_ready_age_s_max", 0) or 0.0),
                "source_ready_age_s_avg": float(extract_metrics.get("source_ready_age_s_avg", 0) or 0.0),
                "content_fetch_attempts_total": int(extract_metrics.get("content_fetch_attempts_total", 0) or 0),
                "content_fetch_attempts_max": int(extract_metrics.get("content_fetch_attempts_max", 0) or 0),
                "content_fetch_attempts_avg": float(extract_metrics.get("content_fetch_attempts_avg", 0) or 0.0),
                "youtube_ytdlp_elapsed_s_total": youtube_ytdlp_elapsed_s_total,
                "youtube_ytdlp_elapsed_s_max": youtube_ytdlp_elapsed_s_max,
                "youtube_ytdlp_elapsed_s_count": youtube_ytdlp_elapsed_s_count,
                "youtube_ytdlp_elapsed_s_avg": youtube_ytdlp_elapsed_s_avg,
                "youtube_page_elapsed_s_total": youtube_page_elapsed_s_total,
                "youtube_page_elapsed_s_max": youtube_page_elapsed_s_max,
                "youtube_page_elapsed_s_count": youtube_page_elapsed_s_count,
                "youtube_page_elapsed_s_avg": youtube_page_elapsed_s_avg,
                "retry_queue_deferred_count": int(extract_metrics.get("retry_queue_deferred_count", 0) or 0),
                "retry_queue_recovered_count": int(extract_metrics.get("retry_queue_recovered_count", 0) or 0),
                "retry_queue_final_failed_count": int(extract_metrics.get("retry_queue_final_failed_count", 0) or 0),
                "shared_retry_deferred_count": int(extract_metrics.get("shared_retry_deferred_count", 0) or 0),
                "shared_retry_recovered_count": int(extract_metrics.get("shared_retry_recovered_count", 0) or 0),
                "shared_retry_final_failed_count": int(extract_metrics.get("shared_retry_final_failed_count", 0) or 0),
                "materialization_ready_at_epoch": float(extract_metrics.get("materialization_ready_at_epoch", 0) or 0.0),
                "started_at_epoch": batch_started_at_epoch,
                "completed_at_epoch": time.time(),
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
            "cleanup_every_n_batches": self._cleanup_every_n_batches,
            "batches_since_cleanup": self._batches_since_cleanup,
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
            "content_fetch_status_counts": dict(extract_metrics.get("content_fetch_status_counts", {}) or {}),
            "source_ready_age_s_total": float(extract_metrics.get("source_ready_age_s_total", 0) or 0.0),
            "source_ready_age_s_max": float(extract_metrics.get("source_ready_age_s_max", 0) or 0.0),
            "source_ready_age_s_avg": float(extract_metrics.get("source_ready_age_s_avg", 0) or 0.0),
            "content_fetch_attempts_total": int(extract_metrics.get("content_fetch_attempts_total", 0) or 0),
            "content_fetch_attempts_max": int(extract_metrics.get("content_fetch_attempts_max", 0) or 0),
            "content_fetch_attempts_avg": float(extract_metrics.get("content_fetch_attempts_avg", 0) or 0.0),
            "youtube_ytdlp_elapsed_s_total": youtube_ytdlp_elapsed_s_total,
            "youtube_ytdlp_elapsed_s_max": youtube_ytdlp_elapsed_s_max,
            "youtube_ytdlp_elapsed_s_count": youtube_ytdlp_elapsed_s_count,
            "youtube_ytdlp_elapsed_s_avg": youtube_ytdlp_elapsed_s_avg,
            "youtube_page_elapsed_s_total": youtube_page_elapsed_s_total,
            "youtube_page_elapsed_s_max": youtube_page_elapsed_s_max,
            "youtube_page_elapsed_s_count": youtube_page_elapsed_s_count,
            "youtube_page_elapsed_s_avg": youtube_page_elapsed_s_avg,
            "retry_queue_deferred_count": int(extract_metrics.get("retry_queue_deferred_count", 0) or 0),
            "retry_queue_recovered_count": int(extract_metrics.get("retry_queue_recovered_count", 0) or 0),
            "retry_queue_final_failed_count": int(extract_metrics.get("retry_queue_final_failed_count", 0) or 0),
            "shared_retry_deferred_count": int(extract_metrics.get("shared_retry_deferred_count", 0) or 0),
            "shared_retry_recovered_count": int(extract_metrics.get("shared_retry_recovered_count", 0) or 0),
            "shared_retry_final_failed_count": int(extract_metrics.get("shared_retry_final_failed_count", 0) or 0),
            "materialization_ready_at_epoch": float(extract_metrics.get("materialization_ready_at_epoch", 0) or 0.0),
        }
        return results

    def close(self, delete: bool = False):
        self._ingestor._nb_id = self._nb_id
        if delete:
            try:
                _delete_worker_notebooks_by_title_with_cdp(_get_reusable_notebook_title())
            finally:
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


class DoubleBufferedReusableIngestor:
    """Reusable batch wrapper that can later overlap staging with extraction."""

    def __init__(
        self,
        batch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE,
        cleanup_every_n_batches: int | None = None,
    ):
        self._serial_ingestor = NLMReusableIngestor(
            batch_size=batch_size,
            cleanup_every_n_batches=cleanup_every_n_batches,
        )
        self._staging_ingestor = NLMReusableIngestor(
            batch_size=batch_size,
            cleanup_every_n_batches=cleanup_every_n_batches,
        )
        self._last_process_metrics: dict[str, object] | None = None
        self._last_prepare_metrics: dict[str, object] | None = None
        self._last_batch_metrics: list[dict[str, object]] | None = None

    def prepare(self) -> tuple[bool, str]:
        serial_prepared, serial_mode = self._serial_ingestor.prepare()
        staging_prepared, staging_mode = self._staging_ingestor.prepare()
        serial_metrics = self._serial_ingestor.get_last_prepare_metrics() or {}
        staging_metrics = self._staging_ingestor.get_last_prepare_metrics() or {}
        self._last_prepare_metrics = {
            "created_new_notebook": bool(serial_metrics.get("created_new_notebook") or staging_metrics.get("created_new_notebook")),
            "setup_mode": "double_buffered",
            "notebook_check_elapsed_s": float(serial_metrics.get("notebook_check_elapsed_s") or 0.0)
            + float(staging_metrics.get("notebook_check_elapsed_s") or 0.0),
            "create_elapsed_s": float(serial_metrics.get("create_elapsed_s") or 0.0)
            + float(staging_metrics.get("create_elapsed_s") or 0.0),
            "retire_elapsed_s": float(serial_metrics.get("retire_elapsed_s") or 0.0)
            + float(staging_metrics.get("retire_elapsed_s") or 0.0),
            "cleanup_elapsed_s": float(serial_metrics.get("cleanup_elapsed_s") or 0.0)
            + float(staging_metrics.get("cleanup_elapsed_s") or 0.0),
            "total_elapsed_s": float(serial_metrics.get("total_elapsed_s") or 0.0)
            + float(staging_metrics.get("total_elapsed_s") or 0.0),
        }
        return serial_prepared and staging_prepared, "double_buffered"

    def _prepare_staging_notebook(self, video_ids: List[str]) -> bool:
        """Prepare a future staging notebook.

        The wrapper needs a lightweight gate before it launches a background
        staging batch. We keep this conservative: empty batches never stage.
        """
        return bool(video_ids)

    def _process_serial_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        return self._serial_ingestor.process_batch(video_ids)

    def _run_serial_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        return self._serial_ingestor.process_batch(video_ids)

    def _run_staging_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        return self._staging_ingestor.process_batch(video_ids)

    def process_batch(self, video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        staging_started_at = time.monotonic()
        staging_ready = self._prepare_staging_notebook(video_ids)
        staging_wait_elapsed_s = round(time.monotonic() - staging_started_at, 3)
        if not staging_ready:
            results = self._process_serial_batch(video_ids)
            serial_metrics = self._serial_ingestor.get_last_process_metrics() or {}
            self._last_batch_metrics = [dict(serial_metrics)]
            self._last_process_metrics = {
                **dict(serial_metrics),
                "staging_overlap_elapsed_s": 0.0,
                "staging_wait_elapsed_s": staging_wait_elapsed_s,
                "stage_swap_count": 0,
                "strategy": "double_buffered_reusable",
                "serial_fallback": True,
            }
            return results

        results = self._process_serial_batch(video_ids)
        serial_metrics = self._serial_ingestor.get_last_process_metrics() or {}
        self._last_batch_metrics = [dict(serial_metrics)]
        self._last_process_metrics = {
            **dict(serial_metrics),
            "staging_overlap_elapsed_s": 0.0,
            "staging_wait_elapsed_s": staging_wait_elapsed_s,
            "stage_swap_count": 0,
            "strategy": "double_buffered_reusable",
            "serial_fallback": False,
        }
        return results

    def process_batches(self, batch_groups: list[list[str]]) -> list[Dict[str, Tuple[bool, Optional[str], Optional[str]]]]:
        batches = [list(batch) for batch in batch_groups if batch]
        if not batches:
            self._last_process_metrics = {
                "staging_overlap_elapsed_s": 0.0,
                "staging_wait_elapsed_s": 0.0,
                "stage_swap_count": 0,
                "strategy": "double_buffered_reusable",
                "serial_fallback": False,
                "total_elapsed_s": 0.0,
            }
            return []

        started_at = time.monotonic()
        results: list[Dict[str, Tuple[bool, Optional[str], Optional[str]]]] = []
        stage_swap_count = 0
        staging_overlap_elapsed_s = 0.0
        staging_wait_elapsed_s = 0.0
        batch_metrics: list[dict[str, object]] = []

        with ThreadPoolExecutor(max_workers=1) as executor:
            index = 0
            while index < len(batches):
                current_batch = batches[index]
                next_index = index + 1
                staged_future = None
                staged_started_at = 0.0
                if next_index < len(batches):
                    next_batch = batches[next_index]
                    preflight_started_at = time.monotonic()
                    staging_ready = self._prepare_staging_notebook(next_batch)
                    staging_wait_elapsed_s += round(time.monotonic() - preflight_started_at, 3)
                    if staging_ready:
                        staged_started_at = time.monotonic()
                        staged_future = executor.submit(self._run_staging_batch, next_batch)
                current_result = self._run_serial_batch(current_batch)
                current_metrics = dict(self._serial_ingestor.get_last_process_metrics() or {})
                results.append(current_result)
                batch_metrics.append(current_metrics)
                if staged_future is not None:
                    staged_result = staged_future.result()
                    staged_metrics = dict(self._staging_ingestor.get_last_process_metrics() or {})
                    staging_overlap_elapsed_s += round(time.monotonic() - staged_started_at, 3)
                    results.append(staged_result)
                    batch_metrics.append(staged_metrics)
                    stage_swap_count += 1
                    index += 2
                else:
                    index += 1

        self._last_batch_metrics = batch_metrics
        self._last_process_metrics = {
            "staging_overlap_elapsed_s": round(staging_overlap_elapsed_s, 3),
            "staging_wait_elapsed_s": round(staging_wait_elapsed_s, 3),
            "stage_swap_count": stage_swap_count,
            "strategy": "double_buffered_reusable",
            "serial_fallback": False,
            "total_elapsed_s": round(time.monotonic() - started_at, 3),
        }
        return results

    def get_last_process_metrics(self) -> dict[str, object] | None:
        if self._last_process_metrics is None:
            return None
        return dict(self._last_process_metrics)

    def get_last_prepare_metrics(self) -> dict[str, object] | None:
        if self._last_prepare_metrics is None:
            return None
        return dict(self._last_prepare_metrics)

    def get_last_batch_metrics(self) -> list[dict[str, object]] | None:
        if self._last_batch_metrics is None:
            return None
        return [dict(item) for item in self._last_batch_metrics]

    def close(self, delete: bool = False) -> None:
        self._staging_ingestor.close(delete=delete)
        self._serial_ingestor.close(delete=delete)


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
