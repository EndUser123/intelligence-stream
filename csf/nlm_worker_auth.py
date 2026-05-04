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

from csf import nlm_auth_guard

run_nlm = nlm_auth_guard.run_nlm


DEFAULT_PROFILE_ROOT = Path.home() / ".notebooklm-mcp-cli" / "profiles"
DEFAULT_NLM_CHROME_PROFILE_ROOT = nlm_auth_guard.DEFAULT_NLM_CHROME_PROFILE_ROOT


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
        sibling_profiles=("ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04", "ytis-pro-worker-05"),
        expected_email="a.hominidae@gmail.com",
        cdp_browser_root=r"P:\.data\yt-is\browser\notebooklm-pro",
        cdp_browser_profile_directory="Profile",
        cdp_port=18870,
    ),
    AuthFamily(
        source_profile="ytis-free1-worker-01",
        sibling_profiles=("ytis-free1-worker-02", "ytis-free1-worker-03", "ytis-free1-worker-04", "ytis-free1-worker-05"),
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
_PROFILE_SNAPSHOT_DIRNAME = "verified-worker-profile-snapshots"
_PROFILE_SNAPSHOT_MANIFEST = "manifest.json"
_PROFILE_SNAPSHOT_KIND = "notebooklm-worker-profile-snapshot"


def _validate_auth_families(families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES) -> tuple[AuthFamily, ...]:
    """Fail closed if the static auth-family map contains collisions or holes."""
    family_tuple = tuple(families)
    if not family_tuple:
        raise ValueError("at least one auth family is required")

    seen_profiles: set[str] = set()
    seen_cdp_roots: set[str] = set()
    seen_cdp_ports: set[int] = set()
    for family in family_tuple:
        if not family.expected_email.strip():
            raise ValueError(f"{family.source_profile}: expected_email is required")
        source_profile = family.source_profile.strip()
        if not source_profile:
            raise ValueError("auth family source_profile is required")
        if source_profile in seen_profiles:
            raise ValueError(f"duplicate auth family profile: {source_profile}")
        seen_profiles.add(source_profile)
        for sibling in family.sibling_profiles:
            sibling_profile = sibling.strip()
            if not sibling_profile:
                raise ValueError(f"{source_profile}: sibling profile names must be non-empty")
            if sibling_profile in seen_profiles:
                raise ValueError(f"duplicate auth family profile: {sibling_profile}")
            seen_profiles.add(sibling_profile)
        if family.cdp_browser_root:
            cdp_root = str(Path(family.cdp_browser_root))
            if cdp_root in seen_cdp_roots:
                raise ValueError(f"duplicate auth family cdp_browser_root: {cdp_root}")
            seen_cdp_roots.add(cdp_root)
        if family.cdp_port:
            if family.cdp_port in seen_cdp_ports:
                raise ValueError(f"duplicate auth family cdp_port: {family.cdp_port}")
            seen_cdp_ports.add(family.cdp_port)
    return family_tuple


def expected_email_for_profile(profile: str, families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES) -> str:
    profile = profile.strip()
    if not profile:
        return ""
    mapped = _expected_email_by_profile(_validate_auth_families(families)).get(profile, "")
    return mapped or os.getenv("YTIS_NLM_EXPECTED_EMAIL", "").strip().lower()


def family_for_profile(
    profile: str,
    families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES,
) -> AuthFamily | None:
    """Return the configured auth family that owns a NotebookLM profile."""
    profile = profile.strip()
    if not profile:
        return None
    for family in _validate_auth_families(families):
        if profile == family.source_profile or profile in family.sibling_profiles:
            return family
    return None


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
    return nlm_auth_guard.build_nlm_command(*args)


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


def _snapshot_parent(profile_root: Path, snapshot_root: Path | None = None) -> Path:
    return Path(snapshot_root) if snapshot_root is not None else Path(profile_root) / _PROFILE_SNAPSHOT_DIRNAME


def _profile_snapshot_path(parent: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = parent / f"snapshot-{stamp}"
    suffix = 1
    while path.exists():
        path = parent / f"snapshot-{stamp}-{suffix}"
        suffix += 1
    return path


def _validate_profile_for_snapshot(
    profile_root: Path,
    profile: str,
    expected_email: str,
    *,
    session_checker: Callable[[str, str], bool] | None,
) -> dict[str, str]:
    metadata = _load_metadata(profile_root, profile)
    actual_email = str(metadata.get("email", "")).strip().lower()
    expected = expected_email.strip().lower()
    if actual_email != expected:
        raise RuntimeError(f"{profile} is {actual_email or '<missing email>'}, expected {expected_email}")
    cookies = _cookies_path(profile_root, profile)
    if not cookies.exists():
        raise FileNotFoundError(f"missing cookies for {profile}: {cookies}")
    if session_checker is not None:
        session_ok = session_checker(profile, expected_email)
    else:
        session_ok = profile_session_matches_expected(profile, expected_email)
    if not session_ok:
        raise RuntimeError(f"{profile} live session does not match expected account {expected_email}")
    return {"actual_email": actual_email, "expected_email": expected}


def _prune_profile_snapshots(parent: Path, keep: int, *, keep_path: Path) -> None:
    if keep <= 0 or not parent.exists():
        return
    snapshots = [
        path
        for path in parent.iterdir()
        if path.is_dir() and (path / _PROFILE_SNAPSHOT_MANIFEST).exists()
    ]
    snapshots.sort(key=lambda path: path.name, reverse=True)
    retained = 0
    for path in snapshots:
        if path == keep_path:
            retained += 1
            continue
        retained += 1
        if retained > keep:
            shutil.rmtree(path, ignore_errors=True)


def snapshot_worker_profiles(
    profile_root: Path = DEFAULT_PROFILE_ROOT,
    families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES,
    *,
    snapshot_root: Path | None = None,
    retention_count: int = 5,
    session_checker: Callable[[str, str], bool] | None = None,
) -> Path:
    """Create a verified snapshot of all configured NotebookLM worker auth profiles."""
    profile_root = Path(profile_root)
    profiles = iter_worker_profiles(families)
    expected_by_profile = _expected_email_by_profile(families)
    validated = {
        profile: _validate_profile_for_snapshot(
            profile_root,
            profile,
            expected_by_profile[profile],
            session_checker=session_checker,
        )
        for profile in profiles
    }

    parent = _snapshot_parent(profile_root, snapshot_root)
    parent.mkdir(parents=True, exist_ok=True)
    snapshot_path = _profile_snapshot_path(parent)
    snapshot_path.mkdir(parents=True, exist_ok=False)
    manifest_profiles: list[dict[str, object]] = []
    for profile in profiles:
        src_dir = profile_root / profile
        dst_dir = snapshot_path / profile
        dst_dir.mkdir(parents=True, exist_ok=False)
        for filename in _PROFILE_STATE_FILES:
            shutil.copy2(src_dir / filename, dst_dir / filename)
        manifest_profiles.append(
            {
                "profile": profile,
                "expected_email": validated[profile]["expected_email"],
                "actual_email": validated[profile]["actual_email"],
                "files": list(_PROFILE_STATE_FILES),
            }
        )
    manifest = {
        "kind": _PROFILE_SNAPSHOT_KIND,
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "profile_root": str(profile_root),
        "profiles": manifest_profiles,
    }
    (snapshot_path / _PROFILE_SNAPSHOT_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _prune_profile_snapshots(parent, retention_count, keep_path=snapshot_path)
    return snapshot_path


def _latest_profile_snapshot(parent: Path) -> Path:
    if not parent.exists():
        raise FileNotFoundError(f"no worker auth snapshots found under {parent}")
    snapshots = [
        path
        for path in parent.iterdir()
        if path.is_dir() and (path / _PROFILE_SNAPSHOT_MANIFEST).exists()
    ]
    if not snapshots:
        raise FileNotFoundError(f"no worker auth snapshots found under {parent}")
    snapshots.sort(key=lambda path: path.name, reverse=True)
    return snapshots[0]


def _load_snapshot_manifest(snapshot_path: Path) -> dict[str, object]:
    manifest_path = snapshot_path / _PROFILE_SNAPSHOT_MANIFEST
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing snapshot manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(f"invalid snapshot manifest: {manifest_path}") from exc
    if manifest.get("kind") != _PROFILE_SNAPSHOT_KIND:
        raise RuntimeError(f"unsupported snapshot manifest kind in {manifest_path}")
    return manifest


def _validate_snapshot_manifest(
    snapshot_path: Path,
    families: tuple[AuthFamily, ...],
) -> list[str]:
    manifest = _load_snapshot_manifest(snapshot_path)
    entries = manifest.get("profiles")
    if not isinstance(entries, list):
        raise RuntimeError(f"snapshot manifest has no profile list: {snapshot_path}")
    expected_by_profile = _expected_email_by_profile(families)
    manifest_by_profile: dict[str, dict[str, object]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("profile"), str):
            manifest_by_profile[str(entry["profile"])] = entry
    profiles = iter_worker_profiles(families)
    for profile in profiles:
        entry = manifest_by_profile.get(profile)
        if entry is None:
            raise RuntimeError(f"snapshot missing profile {profile}: {snapshot_path}")
        expected = expected_by_profile[profile].strip().lower()
        manifest_expected = str(entry.get("expected_email", "")).strip().lower()
        if manifest_expected != expected:
            raise RuntimeError(
                f"snapshot expected email mismatch for {profile}: {manifest_expected or '<missing>'} != {expected}"
            )
        metadata = json.loads((snapshot_path / profile / "metadata.json").read_text(encoding="utf-8"))
        actual = str(metadata.get("email", "")).strip().lower()
        if actual != expected:
            raise RuntimeError(f"snapshot profile {profile} is {actual or '<missing email>'}, expected {expected}")
        cookies = snapshot_path / profile / "cookies.json"
        if not cookies.exists():
            raise FileNotFoundError(f"snapshot missing cookies for {profile}: {cookies}")
    return profiles


def restore_worker_profiles(
    profile_root: Path = DEFAULT_PROFILE_ROOT,
    families: tuple[AuthFamily, ...] = DEFAULT_FAMILIES,
    *,
    snapshot_path: Path | None = None,
    snapshot_root: Path | None = None,
) -> Path:
    """Restore all configured worker auth profiles from a verified snapshot."""
    profile_root = Path(profile_root)
    resolved_snapshot = Path(snapshot_path) if snapshot_path is not None else _latest_profile_snapshot(
        _snapshot_parent(profile_root, snapshot_root)
    )
    profiles = _validate_snapshot_manifest(resolved_snapshot, families)
    for profile in profiles:
        dst_dir = profile_root / profile
        dst_dir.mkdir(parents=True, exist_ok=True)
        for filename in _PROFILE_STATE_FILES:
            shutil.copy2(resolved_snapshot / profile / filename, dst_dir / filename)
    return resolved_snapshot


def profile_session_is_valid(profile: str, *, timeout_s: float = 30.0) -> bool:
    """Return whether the NotebookLM CLI can use the profile without interactive login."""
    res = _run_nlm_command_fail_closed(["login", "--check", "--profile", profile], timeout_s=timeout_s)
    if res is None:
        return False
    return res.returncode == 0


def profile_session_account(profile: str, *, timeout_s: float = 30.0) -> str:
    """Return the account reported by `nlm login --check`, or empty string if invalid."""
    res = _run_nlm_command_fail_closed(["login", "--check", "--profile", profile], timeout_s=timeout_s)
    if res is None:
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
    res = _run_nlm_command_fail_closed(["login", "--force", "--profile", profile], timeout_s=timeout_s)
    if res is None:
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


def _is_noninteractive_auth() -> bool:
    value = os.getenv("YTIS_NLM_AUTH_NONINTERACTIVE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _browser_launch_visible() -> bool:
    value = os.getenv("YTIS_NLM_BROWSER_VISIBLE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _browser_launch_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt" or _browser_launch_visible():
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    return startupinfo


def _default_chrome_profile_pids() -> set[int]:
    return nlm_auth_guard.default_chrome_profile_pids()


def _run_nlm_command_fail_closed(
    args: list[str],
    *,
    timeout_s: float,
) -> subprocess.CompletedProcess | None:
    default_profile_pids_before = _default_chrome_profile_pids()
    if default_profile_pids_before:
        _stop_chrome_pids(default_profile_pids_before)
        return None
    res = run_nlm(args, timeout_s=timeout_s)
    default_profile_pids_after = _default_chrome_profile_pids()
    new_default_profile_pids = default_profile_pids_after - default_profile_pids_before
    if new_default_profile_pids:
        _stop_chrome_pids(new_default_profile_pids)
        return None
    return res


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _chrome_pids_for_root(browser_root: str | Path) -> set[int]:
    return nlm_auth_guard.chrome_pids_for_root(browser_root)


def _stop_chrome_pids(pids: set[int]) -> None:
    nlm_auth_guard.stop_chrome_pids(pids)


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
    return nlm_auth_guard.is_cdp_noise_tab(url)


def _close_cdp_noise_tabs(port: int) -> int:
    """Close harmless Chrome/CDP tabs that add auth-window noise."""
    return nlm_auth_guard.close_cdp_noise_tabs(port)


def _launch_cdp_browser(family: AuthFamily, profile_root: Path, snapshot: dict[str, str] | None) -> bool:
    """Launch the dedicated CDP browser for a family only when it is not already alive."""
    _stop_chrome_for_root(family.cdp_browser_root)
    _mark_browser_profile_clean(family.cdp_browser_root, family.cdp_browser_profile_directory or "Default")
    args = [
        _chrome_executable(),
    ]
    if not _browser_launch_visible():
        args.append("--start-minimized")
    args.extend([
        f"--user-data-dir={family.cdp_browser_root}",
        f"--profile-directory={family.cdp_browser_profile_directory or 'Default'}",
        f"--remote-debugging-port={family.cdp_port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "https://notebooklm.google.com/",
    ])
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    startupinfo = _browser_launch_startupinfo()
    if startupinfo is not None:
        popen_kwargs["startupinfo"] = startupinfo
    try:
        subprocess.Popen(args, **popen_kwargs)
    except OSError:
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    if not _wait_for_cdp(family.cdp_port):
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    return True


def refresh_source_profile(family: AuthFamily, *, timeout_s: float = 120.0) -> bool:
    """Refresh worker-01 through its dedicated browser root when configured."""
    profile_root = DEFAULT_PROFILE_ROOT
    snapshot = _snapshot_profile_state(profile_root, family.source_profile)
    default_profile_pids_before: set[int] = set()
    if _is_noninteractive_auth():
        default_profile_pids_before = _chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT)
        if default_profile_pids_before:
            if snapshot is not None:
                _restore_profile_state(profile_root, family.source_profile, snapshot)
            return False
    use_cdp = os.getenv("YTIS_NLM_WORKER_AUTH_USE_CDP", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not use_cdp or not family.cdp_browser_root or family.cdp_port <= 0:
        return refresh_profile_session(family.source_profile, timeout_s=timeout_s)

    if not _wait_for_cdp(family.cdp_port, timeout_s=1.0):
        if not _launch_cdp_browser(family, profile_root, snapshot):
            return False
    _close_cdp_noise_tabs(family.cdp_port)
    try:
        res = run_nlm(
            [
                "login",
                "--profile",
                family.source_profile,
                "--provider",
                "openclaw",
                "--cdp-url",
                f"http://127.0.0.1:{family.cdp_port}",
                "--force",
            ],
            timeout_s=timeout_s,
        )
    except subprocess.TimeoutExpired:
        if snapshot is not None:
            _restore_profile_state(profile_root, family.source_profile, snapshot)
        return False
    if _is_noninteractive_auth():
        default_profile_pids_after = _chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT)
        new_default_profile_pids = default_profile_pids_after - default_profile_pids_before
        if new_default_profile_pids:
            _stop_chrome_pids(new_default_profile_pids)
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
    for family in _validate_auth_families(families):
        profiles.append(family.source_profile)
        profiles.extend(family.sibling_profiles)
    return profiles


def _expected_email_by_profile(families: tuple[AuthFamily, ...]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for family in _validate_auth_families(families):
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


def doctor_lane_setup(lane_config: Path, run_root: Path, *, timeout_s: float = 30.0) -> tuple[object, ...]:
    """Validate lane auth and refuse stale or contaminated run roots before a benchmark starts."""
    from csf.sharded_lane_series import load_lane_configs, preflight_lane_auth_profiles

    _validate_auth_families()
    lanes = load_lane_configs(lane_config)
    run_root = Path(run_root)
    if run_root.exists():
        if not run_root.is_dir():
            raise RuntimeError(f"run root is not a directory: {run_root}")
        if any(run_root.iterdir()):
            raise RuntimeError(f"run root is not empty: {run_root}")
    preflight_lane_auth_profiles(lanes, timeout_s=timeout_s)
    return lanes


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
        "--snapshot",
        type=Path,
        default=None,
        help="Snapshot path to restore; defaults to the latest verified snapshot.",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=None,
        help="Directory that stores verified profile snapshots.",
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=5,
        help="Number of verified snapshots to keep after creating a new snapshot.",
    )
    parser.add_argument(
        "--lane-config",
        type=Path,
        default=None,
        help="Benchmark lane config used by doctor mode.",
    )
    parser.add_argument(
        "--run-root",
        type=Path,
        default=None,
        help="Benchmark run root used by doctor mode.",
    )
    parser.add_argument(
        "action",
        choices=("sync", "check", "snapshot", "restore", "doctor"),
        help=(
            "sync copies worker-01 credentials to sibling workers; check validates all workers; "
            "snapshot stores verified profiles; restore rolls back from a verified snapshot; "
            "doctor validates lane auth and run-root readiness."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.action == "check":
        return 1 if check_worker_profiles() else 0

    if args.action == "snapshot":
        try:
            snapshot_path = snapshot_worker_profiles(
                args.profile_root,
                snapshot_root=args.snapshot_root,
                retention_count=args.retention,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"[auth] ERROR: {exc}")
            return 1
        print(f"[auth] snapshot={snapshot_path}")
        return 0

    if args.action == "restore":
        try:
            snapshot_path = restore_worker_profiles(
                args.profile_root,
                snapshot_path=args.snapshot,
                snapshot_root=args.snapshot_root,
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"[auth] ERROR: {exc}")
            return 1
        print(f"[auth] restored={snapshot_path}")
        if args.skip_check:
            return 0
        failed = check_worker_profiles()
        return 1 if failed else 0

    if args.action == "doctor":
        if args.lane_config is None or args.run_root is None:
            print("[auth] ERROR: doctor requires --lane-config and --run-root")
            return 1
        try:
            lanes = doctor_lane_setup(args.lane_config, args.run_root)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"[auth] ERROR: {exc}")
            return 1
        lane_names = ",".join(getattr(lane, "lane", str(lane)) for lane in lanes)
        print(f"[auth] doctor=ok lanes={lane_names} run_root={args.run_root}")
        return 0

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
