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
from urllib.parse import urlparse


DEFAULT_NLM_CHROME_PROFILE_ROOT = Path.home() / ".notebooklm-mcp-cli" / "chrome-profile"
_AUTH_CHECK_CACHE_LOCK = threading.Lock()
_AUTH_CHECK_CACHE: dict[tuple[str, str], float] = {}


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


def auth_check_cache_hit(context: NLMAuthContext, *, ttl_s: float | None = None) -> bool:
    ttl = auth_check_cache_ttl_seconds() if ttl_s is None else max(0.0, float(ttl_s))
    if ttl <= 0:
        return False
    key = auth_check_cache_key(context)
    with _AUTH_CHECK_CACHE_LOCK:
        checked_at = _AUTH_CHECK_CACHE.get(key)
    if checked_at is None:
        return False
    return (time.monotonic() - checked_at) <= ttl


def auth_check_cache_store(context: NLMAuthContext) -> None:
    with _AUTH_CHECK_CACHE_LOCK:
        _AUTH_CHECK_CACHE[auth_check_cache_key(context)] = time.monotonic()


def auth_check_cache_clear(context: NLMAuthContext) -> None:
    with _AUTH_CHECK_CACHE_LOCK:
        _AUTH_CHECK_CACHE.pop(auth_check_cache_key(context), None)


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
