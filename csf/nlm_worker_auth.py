"""Utilities for maintaining NotebookLM worker auth profiles."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


DEFAULT_PROFILE_ROOT = Path.home() / ".notebooklm-mcp-cli" / "profiles"


@dataclass(frozen=True)
class AuthFamily:
    source_profile: str
    sibling_profiles: tuple[str, ...]
    expected_email: str
    cdp_browser_root: str = ""
    cdp_browser_profile_directory: str = ""
    cdp_port: int = 0


DEFAULT_FAMILIES = (
    AuthFamily(
        source_profile="ytis-pro-worker-01",
        sibling_profiles=("ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04"),
        expected_email="a.hominidae@gmail.com",
        cdp_browser_root=r"P:\.data\yt-is\browser\notebooklm-pro",
        cdp_browser_profile_directory="Profile",
        cdp_port=18870,
    ),
    AuthFamily(
        source_profile="ytis-free1-worker-01",
        sibling_profiles=("ytis-free1-worker-02", "ytis-free1-worker-03", "ytis-free1-worker-04"),
        expected_email="troup.hominidae@gmail.com",
        cdp_browser_root=r"P:\.data\yt-is\browser\notebooklm-free",
        cdp_browser_profile_directory="Default",
        cdp_port=18871,
    ),
    AuthFamily(
        source_profile="ytis-free2-worker-01",
        sibling_profiles=("ytis-free2-worker-02", "ytis-free2-worker-03", "ytis-free2-worker-04"),
        expected_email="brsthomson@hotmail.com",
        cdp_browser_root=r"P:\.data\yt-is\browser\notebooklm-free-2",
        cdp_browser_profile_directory="Default",
        cdp_port=18872,
    ),
)
_PROFILE_STATE_FILES = ("cookies.json", "metadata.json")


def expected_email_for_profile(profile: str, families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES) -> str:
    profile = profile.strip()
    if not profile:
        return ""
    return _expected_email_by_profile(families).get(profile, "")


def _metadata_path(profile_root: Path, profile: str) -> Path:
    return profile_root / profile / "metadata.json"


def _cookies_path(profile_root: Path, profile: str) -> Path:
    return profile_root / profile / "cookies.json"


def _load_metadata(profile_root: Path, profile: str) -> dict[str, object]:
    path = _metadata_path(profile_root, profile)
    if not path.exists():
        raise FileNotFoundError(f"missing metadata for {profile}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _nlm_command(*args: str) -> list[str]:
    return [os.getenv("YTIS_NLM_CLI", "nlm"), *args]


def _validate_source_profile(profile_root: Path, family: AuthFamily) -> None:
    metadata = _load_metadata(profile_root, family.source_profile)
    actual = str(metadata.get("email", "")).strip().lower()
    expected = family.expected_email.lower()
    if actual != expected:
        raise ValueError(
            f"{family.source_profile} is {actual or '<missing email>'}, expected {family.expected_email}"
        )
    cookies = _cookies_path(profile_root, family.source_profile)
    if not cookies.exists():
        raise FileNotFoundError(f"missing cookies for {family.source_profile}: {cookies}")


def _extract_account(stdout: str, stderr: str = "") -> str:
    for line in f"{stdout}\n{stderr}".splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("account:"):
            return stripped.split(":", 1)[1].strip().lower()
    return ""


def _snapshot_profile_state(profile_root: Path, profile: str) -> dict[str, str] | None:
    profile_dir = profile_root / profile
    snapshot: dict[str, str] = {}
    for filename in _PROFILE_STATE_FILES:
        path = profile_dir / filename
        if not path.exists():
            return None
        snapshot[filename] = path.read_text(encoding="utf-8")
    return snapshot


def _restore_profile_state(profile_root: Path, profile: str, snapshot: dict[str, str]) -> None:
    profile_dir = profile_root / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in snapshot.items():
        (profile_dir / filename).write_text(content, encoding="utf-8")


def profile_session_is_valid(profile: str, *, timeout_s: float = 30.0) -> bool:
    """Return whether the NotebookLM CLI can use the profile without interactive login."""
    try:
        res = subprocess.run(
            _nlm_command("login", "--check", "--profile", profile),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return res.returncode == 0


def profile_session_account(profile: str, *, timeout_s: float = 30.0) -> str:
    """Return the account reported by `nlm login --check`, or empty string if invalid."""
    try:
        res = subprocess.run(
            _nlm_command("login", "--check", "--profile", profile),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ""
    if res.returncode != 0:
        return ""
    return _extract_account(res.stdout or "", res.stderr or "")


def profile_session_matches_expected(profile: str, expected_email: str, *, timeout_s: float = 30.0) -> bool:
    """Validate both session liveness and the account bound to the profile."""
    return profile_session_account(profile, timeout_s=timeout_s) == expected_email.strip().lower()


def refresh_profile_session(profile: str, *, timeout_s: float = 120.0) -> bool:
    """Ask nlm to renew a profile using its bounded automatic force-login path."""
    profile_root = DEFAULT_PROFILE_ROOT
    snapshot = _snapshot_profile_state(profile_root, profile)
    try:
        res = subprocess.run(
            _nlm_command("login", "--force", "--profile", profile),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if snapshot is not None:
            _restore_profile_state(profile_root, profile, snapshot)
        return False
    expected_email = expected_email_for_profile(profile)
    success = res.returncode == 0 and (
        not expected_email or _extract_account(res.stdout or "", res.stderr or "") == expected_email.lower()
    )
    if not success and snapshot is not None:
        _restore_profile_state(profile_root, profile, snapshot)
    return success


def _chrome_executable() -> str:
    return os.getenv("YTIS_NLM_BROWSER_EXECUTABLE", r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def _stop_chrome_for_root(browser_root: str) -> None:
    if os.name != "nt" or not browser_root:
        return
    ps = (
        "$matches = Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{browser_root}*' }}; "
        "$matches | ForEach-Object { "
        "$p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "if ($p) { [void]$p.CloseMainWindow() } "
        "}; "
        "Start-Sleep -Seconds 3; "
        "$matches | ForEach-Object { "
        "$p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "if ($p -and -not $p.HasExited) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } "
        "}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30, check=False)


def _mark_browser_profile_clean(browser_root: str, profile_directory: str) -> None:
    if not browser_root:
        return
    prefs_path = Path(browser_root) / (profile_directory or "Default") / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    profile = prefs.setdefault("profile", {})
    if isinstance(profile, dict):
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
    try:
        prefs_path.write_text(json.dumps(prefs, separators=(",", ":")), encoding="utf-8")
    except OSError:
        return


def _wait_for_cdp(port: int, *, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    return False


def _is_cdp_noise_tab(url: str) -> bool:
    """Return True for auth-window tabs that are safe to close."""
    url = (url or "").strip()
    if url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"}:
        return True
    parsed = urlparse(url)
    return parsed.hostname == "0.0.0.2"


def _close_cdp_noise_tabs(port: int) -> int:
    """Close harmless Chrome/CDP tabs that add auth-window noise."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            pages = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return 0

    closed = 0
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict) or not _is_cdp_noise_tab(str(page.get("url") or "")):
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


def refresh_source_profile(family: AuthFamily, *, timeout_s: float = 120.0) -> bool:
    """Refresh worker-01 through its dedicated browser root when configured."""
    profile_root = DEFAULT_PROFILE_ROOT
    snapshot = _snapshot_profile_state(profile_root, family.source_profile)
    use_cdp = os.getenv("YTIS_NLM_WORKER_AUTH_USE_CDP", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not use_cdp or not family.cdp_browser_root or family.cdp_port <= 0:
        return refresh_profile_session(family.source_profile, timeout_s=timeout_s)

    _stop_chrome_for_root(family.cdp_browser_root)
    _mark_browser_profile_clean(family.cdp_browser_root, family.cdp_browser_profile_directory or "Default")
    args = [
        _chrome_executable(),
        f"--user-data-dir={family.cdp_browser_root}",
        f"--profile-directory={family.cdp_browser_profile_directory or 'Default'}",
        f"--remote-debugging-port={family.cdp_port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://notebooklm.google.com/",
    ]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    if not _wait_for_cdp(family.cdp_port):
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    _close_cdp_noise_tabs(family.cdp_port)
    try:
        res = subprocess.run(
            _nlm_command(
                "login",
                "--profile",
                family.source_profile,
                "--provider",
                "openclaw",
                "--cdp-url",
                f"http://127.0.0.1:{family.cdp_port}",
                "--force",
            ),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    success = res.returncode == 0 and _extract_account(res.stdout or "", res.stderr or "") == family.expected_email.lower()
    if not success and snapshot is not None:
        _restore_profile_state(profile_root, family.source_profile, snapshot)
    return success


def _refresh_with_callable(
    family: AuthFamily,
    refresher: Callable[[str], bool] | None,
) -> bool:
    if refresher is not None:
        return refresher(family.source_profile)
    return refresh_source_profile(family)


def _source_session_ok(
    family: AuthFamily,
    checker: Callable[[str], bool] | None,
) -> bool:
    if checker is not None:
        return checker(family.source_profile)
    return profile_session_matches_expected(family.source_profile, family.expected_email)


def _ensure_source_profile_ready(
    profile_root: Path,
    family: AuthFamily,
    *,
    checker: Callable[[str], bool] | None,
    refresher: Callable[[str], bool] | None,
) -> None:
    profile_error: Exception | None = None
    try:
        _validate_source_profile(profile_root, family)
    except (FileNotFoundError, ValueError) as exc:
        profile_error = exc

    if profile_error is not None:
        if not _refresh_with_callable(family, refresher):
            raise RuntimeError(
                f"{family.source_profile} is not mapped to expected account {family.expected_email}; "
                "automatic dedicated-profile refresh did not recover it"
            ) from profile_error
        _validate_source_profile(profile_root, family)

    if _source_session_ok(family, checker):
        return

    if not _refresh_with_callable(family, refresher):
        raise RuntimeError(
            f"{family.source_profile} auth is expired, invalid, or mapped to the wrong account; "
            f"expected {family.expected_email}"
        )
    _validate_source_profile(profile_root, family)
    if not _source_session_ok(family, checker):
        raise RuntimeError(
            f"{family.source_profile} auth refresh completed but the live account still does not match "
            f"expected {family.expected_email}"
        )


def sync_worker_profiles(
    profile_root: Path = DEFAULT_PROFILE_ROOT,
    families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES,
    *,
    backup: bool = True,
    source_session_checker: Callable[[str], bool] | None = None,
    source_session_refresher: Callable[[str], bool] | None = None,
) -> Path | None:
    """Copy each valid worker-01 credential to sibling workers in the same account family."""
    profile_root = Path(profile_root)
    for family in families:
        _ensure_source_profile_ready(
            profile_root,
            family,
            checker=source_session_checker,
            refresher=source_session_refresher,
        )

    backup_root: Path | None = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_root = profile_root / f"backup-before-worker-auth-sync-{stamp}"
        backup_root.mkdir(parents=True, exist_ok=False)

    for family in families:
        src_dir = profile_root / family.source_profile
        for sibling in family.sibling_profiles:
            dst_dir = profile_root / sibling
            dst_dir.mkdir(parents=True, exist_ok=True)
            if backup:
                assert backup_root is not None
                backup_dst = backup_root / sibling
                if dst_dir.exists():
                    shutil.copytree(dst_dir, backup_dst)
            shutil.copy2(src_dir / "cookies.json", dst_dir / "cookies.json")
            shutil.copy2(src_dir / "metadata.json", dst_dir / "metadata.json")

    return backup_root


def iter_worker_profiles(families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES) -> list[str]:
    profiles: list[str] = []
    for family in families:
        profiles.append(family.source_profile)
        profiles.extend(family.sibling_profiles)
    return profiles


def _expected_email_by_profile(families: tuple[AuthFamily, ...]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for family in families:
        expected[family.source_profile] = family.expected_email
        for profile in family.sibling_profiles:
            expected[profile] = family.expected_email
    return expected


def check_worker_profiles(
    profiles: list[str] | None = None,
    families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES,
) -> int:
    """Run `nlm login --check` for every worker profile."""
    failed = 0
    expected_by_profile = _expected_email_by_profile(families)
    for profile in profiles or iter_worker_profiles():
        expected = expected_by_profile.get(profile)
        if expected:
            ok = profile_session_matches_expected(profile, expected)
        else:
            ok = profile_session_is_valid(profile)
        if not ok:
            failed += 1
    return failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain NotebookLM worker auth profiles.")
    parser.add_argument(
        "--profile-root",
        type=Path,
        default=DEFAULT_PROFILE_ROOT,
        help="NotebookLM CLI profile root.",
    )
    parser.add_argument("--no-backup", action="store_true", help="Skip backup before syncing.")
    parser.add_argument("--skip-check", action="store_true", help="Do not validate profiles after sync.")
    parser.add_argument(
        "action",
        choices=("sync", "check"),
        help="sync copies worker-01 credentials to sibling workers; check validates all workers.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.action == "check":
        return 1 if check_worker_profiles() else 0

    try:
        backup_root = sync_worker_profiles(args.profile_root, backup=not args.no_backup)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[auth] ERROR: {exc}")
        return 1
    if backup_root:
        print(f"[auth] backup={backup_root}")
    print("[auth] synced worker auth profiles from worker-01 account families")
    if args.skip_check:
        return 0
    failed = check_worker_profiles()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
