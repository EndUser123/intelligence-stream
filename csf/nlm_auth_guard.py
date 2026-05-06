"""Shared NotebookLM auth and process guard helpers."""

from __future__ import annotations

import os
import subprocess
import threading
import time
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import psutil


DEFAULT_NLM_CHROME_PROFILE_ROOT = Path.home() / ".notebooklm-mcp-cli" / "chrome-profile"
_AUTH_CHECK_CACHE_LOCK = threading.Lock()
# Maps (profile_lower, email_lower) -> (checked_at, session_established_at)
# session_established_at is None until first successful login after the checked_at time
_AUTH_CHECK_CACHE: dict[tuple[str, str], tuple[float, float | None]] = {}


@dataclass(frozen=True)
class NLMAuthContext:
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


def get_notebooklm_profile(default: str = "default") -> str:
    override = os.getenv("NOTEBOOKLM_PROFILE", "").strip()
    return override or default


def get_login_profile_args(profile: str | None = None) -> list[str]:
    profile = (profile or os.getenv("NOTEBOOKLM_PROFILE", "")).strip()
    if not profile:
        return []
    return ["--profile", profile]


def add_profile_args(args: list[str], profile: str | None = None) -> list[str]:
    """Pin profile-aware nlm commands to the active profile."""
    resolved_profile = (profile or os.getenv("NOTEBOOKLM_PROFILE", "")).strip()
    if not resolved_profile or "--profile" in args or "-p" in args:
        return list(args)
    if not args:
        return list(args)
    command = args[0]
    if command in {"login", "help", "--help", "-h", "version"}:
        return list(args)
    return [*args, "--profile", resolved_profile]


def is_nlm_auth_noninteractive() -> bool:
    value = os.getenv("YTIS_NLM_AUTH_NONINTERACTIVE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def get_nlm_auth_context(*, profile: str | None = None, expected_email: str = "") -> NLMAuthContext:
    resolved_profile = (profile or get_notebooklm_profile()).strip()
    resolved_expected_email = expected_email.strip().lower() or os.getenv("YTIS_NLM_EXPECTED_EMAIL", "").strip().lower()
    return NLMAuthContext(
        profile=resolved_profile or "default",
        login_profile_args=get_login_profile_args(resolved_profile),
        requires_profile=is_nlm_auth_noninteractive(),
        expected_email=resolved_expected_email,
    )


def build_nlm_command(*args: str) -> list[str]:
    return [get_nlm_executable(), *args]


def get_nlm_executable() -> str:
    override = os.getenv("YTIS_NLM_CLI", "").strip()
    return override or "nlm"


def run_nlm(args: list[str], *, timeout_s: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            build_nlm_command(*args),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(build_nlm_command(*args), 1, "", "NLM command timed out")


def chrome_pids_for_root(browser_root: str | Path) -> set[int]:
    if os.name != "nt" or not browser_root:
        return set()
    root = str(browser_root)
    ps = (
        "$root = "
        + _ps_single_quote(root)
        + "; "
        + "$matches = Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        + "Where-Object { $_.CommandLine -like \"*$root*\" }; "
        + "$matches | ForEach-Object { $_.ProcessId }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return set()
    if res.returncode != 0:
        return set()
    pids: set[int] = set()
    for line in (res.stdout or "").splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def _collect_chrome_process_records() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    records: list[dict[str, Any]] = []
    try:
        processes = psutil.process_iter(["pid", "name", "cmdline"])
    except Exception:
        return records
    for proc in processes:
        try:
            if (proc.info.get("name") or "").strip().lower() != "chrome.exe":
                continue
            cmdline = " ".join(
                str(part).strip()
                for part in (proc.info.get("cmdline") or [])
                if str(part).strip()
            )
            try:
                rss_bytes = int(getattr(proc.memory_info(), "rss", 0) or 0)
            except Exception:
                rss_bytes = 0
            records.append({"pid": int(proc.pid), "cmdline": cmdline, "rss_bytes": rss_bytes})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return records


def _sample_browser_health(allowed_browser_roots: Iterable[str | Path]) -> dict[str, Any]:
    allowed_roots = tuple(
        sorted(
            {
                str(Path(root)).strip()
                for root in allowed_browser_roots
                if str(root).strip()
            }
        )
    )
    default_root = str(DEFAULT_NLM_CHROME_PROFILE_ROOT)
    allowed_profile_pid_counts_by_root = {root: 0 for root in allowed_roots}
    default_profile_pids: list[int] = []
    unexpected_processes: list[dict[str, Any]] = []
    chrome_process_count = 0
    chrome_rss_bytes_total = 0
    for record in _collect_chrome_process_records():
        chrome_process_count += 1
        pid = int(record.get("pid") or 0)
        cmdline = str(record.get("cmdline") or "")
        rss_bytes = int(record.get("rss_bytes") or 0)
        chrome_rss_bytes_total += rss_bytes
        matched_root = next((root for root in allowed_roots if root and root in cmdline), None)
        if matched_root is not None:
            allowed_profile_pid_counts_by_root[matched_root] += 1
            continue
        if default_root in cmdline:
            default_profile_pids.append(pid)
            continue
        unexpected_processes.append({"pid": pid, "cmdline": cmdline})
    return {
        "allowed_browser_roots": list(allowed_roots),
        "allowed_profile_pid_count": sum(allowed_profile_pid_counts_by_root.values()),
        "allowed_profile_pid_counts_by_root": allowed_profile_pid_counts_by_root,
        "chrome_process_count": chrome_process_count,
        "chrome_rss_bytes_total": chrome_rss_bytes_total,
        "default_profile_pids": default_profile_pids,
        "unexpected_processes": unexpected_processes,
    }


def stop_chrome_pids(pids: set[int]) -> None:
    if os.name != "nt" or not pids:
        return
    pid_list = ",".join(str(pid) for pid in sorted(pids))
    ps = (
        "$pids = @("
        + pid_list
        + "); "
        + "$pids | ForEach-Object { "
        + "$p = Get-Process -Id $_ -ErrorAction SilentlyContinue; "
        + "if ($p) { [void]$p.CloseMainWindow() } "
        + "}; "
        + "Start-Sleep -Seconds 2; "
        + "$pids | ForEach-Object { "
        + "$p = Get-Process -Id $_ -ErrorAction SilentlyContinue; "
        + "if ($p -and -not $p.HasExited) { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } "
        + "}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20, check=False)


def default_chrome_profile_pids() -> set[int]:
    if not is_nlm_auth_noninteractive():
        return set()
    return chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT)


def reap_default_chrome_profile() -> set[int]:
    pids = default_chrome_profile_pids()
    if not pids:
        return set()
    stop_chrome_pids(pids)
    return pids


def browser_health_gate(
    allowed_browser_roots: Iterable[str | Path],
    *,
    settle_window_s: float = 30.0,
    sample_interval_s: float = 5.0,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    clock = clock or time.monotonic
    sleeper = sleeper or time.sleep
    allowed_roots = tuple(
        sorted(
            {
                str(Path(root)).strip()
                for root in allowed_browser_roots
                if str(root).strip()
            }
        )
    )
    start = clock()
    deadline = start + max(0.0, float(settle_window_s))
    initial_default_profile_pids = sorted(chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT))
    initial_default_profile_reaped_pids: list[int] = []
    if initial_default_profile_pids:
        stop_chrome_pids(set(initial_default_profile_pids))
        initial_default_profile_reaped_pids = list(initial_default_profile_pids)

    detected_default_profile_pids: set[int] = set(initial_default_profile_pids)
    reaped_default_profile_pids: set[int] = set(initial_default_profile_reaped_pids)
    unexpected_processes: dict[int, str] = {}
    sample_count = 0
    chrome_process_count_max = 0
    chrome_rss_bytes_max = 0

    while True:
        sample = _sample_browser_health(allowed_roots)
        sample_count += 1
        chrome_process_count_max = max(chrome_process_count_max, int(sample["chrome_process_count"]))
        chrome_rss_bytes_max = max(chrome_rss_bytes_max, int(sample["chrome_rss_bytes_total"]))
        default_profile_pids = {int(pid) for pid in sample["default_profile_pids"]}
        if default_profile_pids:
            detected_default_profile_pids.update(default_profile_pids)
            reaped_default_profile_pids.update(default_profile_pids)
            stop_chrome_pids(default_profile_pids)
        for process in sample["unexpected_processes"]:
            pid = int(process.get("pid") or 0)
            if pid:
                unexpected_processes[pid] = str(process.get("cmdline") or "")
        if clock() >= deadline:
            break
        sleep_for = min(max(0.0, float(sample_interval_s)), max(0.0, deadline - clock()))
        if sleep_for > 0:
            sleeper(sleep_for)

    final_sample = _sample_browser_health(allowed_roots)
    sample_count += 1
    chrome_process_count_max = max(chrome_process_count_max, int(final_sample["chrome_process_count"]))
    chrome_rss_bytes_max = max(chrome_rss_bytes_max, int(final_sample["chrome_rss_bytes_total"]))
    remaining_default_profile_pids = sorted(int(pid) for pid in final_sample["default_profile_pids"])
    for process in final_sample["unexpected_processes"]:
        pid = int(process.get("pid") or 0)
        if pid:
            unexpected_processes[pid] = str(process.get("cmdline") or "")

    issues: list[str] = []
    if unexpected_processes:
        issues.append(
            "unexpected Chrome processes detected during browser health settle: "
            + ", ".join(
                f"{pid}:{cmdline}" for pid, cmdline in sorted(unexpected_processes.items())[:5]
            )
        )
    if remaining_default_profile_pids:
        issues.append(
            "default NotebookLM chrome-profile still present after browser health settle: "
            f"pids={remaining_default_profile_pids}"
        )

    if issues:
        status = "unhealthy"
    elif reaped_default_profile_pids:
        status = "recovered_clean"
    else:
        status = "clean"

    return {
        "status": status,
        "settle_window_s": float(settle_window_s),
        "sample_interval_s": float(sample_interval_s),
        "sample_count": sample_count,
        "elapsed_s": round(clock() - start, 3),
        "allowed_browser_roots": list(allowed_roots),
        "initial_default_profile_detected_count": len(initial_default_profile_pids),
        "initial_default_profile_detected_pids": initial_default_profile_pids,
        "initial_default_profile_reaped_count": len(initial_default_profile_reaped_pids),
        "initial_default_profile_reaped_pids": initial_default_profile_reaped_pids,
        "default_profile_detected_count": len(detected_default_profile_pids),
        "default_profile_detected_pids": sorted(detected_default_profile_pids),
        "default_profile_reaped_count": len(reaped_default_profile_pids),
        "default_profile_reaped_pids": sorted(reaped_default_profile_pids),
        "default_profile_remaining_count": len(remaining_default_profile_pids),
        "default_profile_remaining_pids": remaining_default_profile_pids,
        "unexpected_process_count": len(unexpected_processes),
        "unexpected_processes": [
            {"pid": pid, "cmdline": cmdline}
            for pid, cmdline in sorted(unexpected_processes.items())
        ],
        "chrome_process_count_max": chrome_process_count_max,
        "chrome_rss_bytes_max": chrome_rss_bytes_max,
        "issues": issues,
    }


def auth_check_cache_ttl_seconds(default: float = 30.0) -> float:
    raw = os.getenv("YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return default
    try:
        ttl = float(raw)
    except ValueError:
        return default
    return max(0.0, ttl)


def auth_check_cache_key(context: NLMAuthContext) -> tuple[str, str]:
    return (context.profile.strip().lower(), context.expected_email.strip().lower())


def auth_check_cache_hit(context: NLMAuthContext, *, ttl_s: float | None = None) -> tuple[bool, float | None]:
    """Return (is_hit, session_established_at_or_none)."""
    ttl = auth_check_cache_ttl_seconds() if ttl_s is None else max(0.0, float(ttl_s))
    if ttl <= 0:
        return False, None
    key = auth_check_cache_key(context)
    with _AUTH_CHECK_CACHE_LOCK:
        cached = _AUTH_CHECK_CACHE.get(key)
    if cached is None:
        return False, None
    checked_at, session_established_at = cached
    return (time.monotonic() - checked_at) <= ttl, session_established_at


def auth_check_cache_store(context: NLMAuthContext, *, session_established_at: float | None = None) -> None:
    with _AUTH_CHECK_CACHE_LOCK:
        _AUTH_CHECK_CACHE[auth_check_cache_key(context)] = (time.monotonic(), session_established_at)


def auth_check_cache_session_age(context: NLMAuthContext) -> float | None:
    """Return session age in seconds, or None if session establishment time is unknown."""
    key = auth_check_cache_key(context)
    with _AUTH_CHECK_CACHE_LOCK:
        cached = _AUTH_CHECK_CACHE.get(key)
    if cached is None:
        return None
    _checked_at, session_established_at = cached
    if session_established_at is None:
        return None
    return time.monotonic() - session_established_at


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def is_cdp_noise_tab(url: str) -> bool:
    url = (url or "").strip()
    if url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
        return True
    parsed = urlparse(url)
    return parsed.hostname == "0.0.0.2"


def close_cdp_noise_tabs(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            pages = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return 0

    closed = 0
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict) or not is_cdp_noise_tab(str(page.get("url") or "")):
            continue
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/close/{page_id}", timeout=3):
                closed += 1
        except (OSError, urllib.error.URLError):
            continue
    return closed
