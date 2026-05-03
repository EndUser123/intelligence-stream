"""Tests for NotebookLM worker auth profile maintenance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest import mock

from csf import nlm_worker_auth


def _write_profile(root, name: str, email: str, cookie_marker: str) -> None:
    profile = root / name
    profile.mkdir(parents=True)
    (profile / "cookies.json").write_text(json.dumps([{"name": cookie_marker}]), encoding="utf-8")
    (profile / "metadata.json").write_text(
        json.dumps({"email": email, "last_validated": "2026-04-29T10:00:00"}),
        encoding="utf-8",
    )


def _write_fake_nlm_executable(bin_dir, log_path, valid_marker) -> None:
    fake_py = bin_dir / "fake_nlm.py"
    fake_py.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                f"log_path = Path({str(log_path)!r})",
                f"valid_path = Path({str(valid_marker)!r})",
                "args = sys.argv[1:]",
                "log_path.parent.mkdir(parents=True, exist_ok=True)",
                "with log_path.open('a', encoding='utf-8') as handle:",
                "    handle.write(' '.join(args) + '\\n')",
                "profile = args[args.index('--profile') + 1] if '--profile' in args else 'default'",
                "valid = set(json.loads(valid_path.read_text(encoding='utf-8')) if valid_path.exists() else [])",
                "profile_root = Path(os.environ.get('YTIS_FAKE_NLM_PROFILE_ROOT', ''))",
                "def copied_from_valid_source(name):",
                "    if not profile_root:",
                "        return False",
                "    if name.startswith('ytis-pro-'):",
                "        source = 'ytis-pro-worker-01'",
                "    elif name.startswith('ytis-free2-'):",
                "        source = 'ytis-free2-worker-01'",
                "    else:",
                "        source = 'ytis-free1-worker-01'",
                "    if source not in valid:",
                "        return False",
                "    try:",
                "        return (profile_root / name / 'cookies.json').read_text(encoding='utf-8') == (profile_root / source / 'cookies.json').read_text(encoding='utf-8')",
                "    except OSError:",
                "        return False",
                "if args[:2] == ['login', '--check']:",
                "    if profile in valid or copied_from_valid_source(profile):",
                "        try:",
                "            email = json.loads((profile_root / profile / 'metadata.json').read_text(encoding='utf-8')).get('email', '')",
                "        except Exception:",
                "            email = ''",
                "        print(f'Account: {email}')",
                "        raise SystemExit(0)",
                "    raise SystemExit(1)",
                "if args[:2] == ['login', '--force']:",
                "    valid.add(profile)",
                "    valid_path.write_text(json.dumps(sorted(valid)), encoding='utf-8')",
                "    try:",
                "        email = json.loads((profile_root / profile / 'metadata.json').read_text(encoding='utf-8')).get('email', '')",
                "    except Exception:",
                "        email = ''",
                "    print(f'Account: {email}')",
                "    raise SystemExit(0)",
                "raise SystemExit(9)",
            ]
        ),
        encoding="utf-8",
    )
    if os.name == "nt":
        script = bin_dir / "nlm.cmd"
        script.write_text(
            f'@echo off\n"{sys.executable}" "{fake_py}" %*\n',
            encoding="utf-8",
        )
        return
    script = bin_dir / "nlm"
    script.write_text(
        f'#!/bin/sh\n"{sys.executable}" "{fake_py}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_sync_worker_profiles_copies_by_account_family_and_backs_up(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    _write_profile(root, "ytis-pro-worker-02", "a.hominidae@gmail.com", "stale-pro")
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "fresh-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    families = (
        nlm_worker_auth.AuthFamily(
            "ytis-pro-worker-01",
            ("ytis-pro-worker-02",),
            "a.hominidae@gmail.com",
        ),
        nlm_worker_auth.AuthFamily(
            "ytis-free1-worker-01",
            ("ytis-free1-worker-02",),
            "troup.hominidae@gmail.com",
        ),
    )

    backup = nlm_worker_auth.sync_worker_profiles(root, families, source_session_checker=lambda profile: True)

    assert backup is not None
    assert (backup / "ytis-pro-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "stale-pro"}]
    )
    assert (root / "ytis-pro-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "fresh-pro"}]
    )
    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "fresh-free"}]
    )


def test_snapshot_worker_profiles_copies_source_and_sibling_profiles_with_manifest(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    _write_profile(root, "ytis-pro-worker-02", "a.hominidae@gmail.com", "fresh-pro-sibling")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-pro-worker-01",
            ("ytis-pro-worker-02",),
            "a.hominidae@gmail.com",
        ),
    )

    snapshot = nlm_worker_auth.snapshot_worker_profiles(
        root,
        family,
        session_checker=lambda profile, expected: True,
    )

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "notebooklm-worker-profile-snapshot"
    assert [profile["profile"] for profile in manifest["profiles"]] == [
        "ytis-pro-worker-01",
        "ytis-pro-worker-02",
    ]
    assert (snapshot / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "fresh-pro"}]
    )
    assert (snapshot / "ytis-pro-worker-02" / "metadata.json").exists()


def test_restore_worker_profiles_uses_latest_valid_snapshot_to_repair_source_profile(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "good-pro")
    _write_profile(root, "ytis-pro-worker-02", "a.hominidae@gmail.com", "good-pro-sibling")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-pro-worker-01",
            ("ytis-pro-worker-02",),
            "a.hominidae@gmail.com",
        ),
    )
    snapshot = nlm_worker_auth.snapshot_worker_profiles(
        root,
        family,
        session_checker=lambda profile, expected: True,
    )
    _write_profile(root / "replacement", "ytis-pro-worker-01", "troup.hominidae@gmail.com", "corrupt")
    shutil_source = root / "replacement" / "ytis-pro-worker-01"
    target = root / "ytis-pro-worker-01"
    (target / "cookies.json").write_text((shutil_source / "cookies.json").read_text(encoding="utf-8"), encoding="utf-8")
    (target / "metadata.json").write_text((shutil_source / "metadata.json").read_text(encoding="utf-8"), encoding="utf-8")

    restored = nlm_worker_auth.restore_worker_profiles(root, family)

    assert restored == snapshot
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "good-pro"}]
    )
    assert json.loads((root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8"))["email"] == "a.hominidae@gmail.com"


def test_restore_worker_profiles_rejects_snapshot_with_wrong_expected_email(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "good-pro")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-pro-worker-01",
            (),
            "a.hominidae@gmail.com",
        ),
    )
    snapshot = nlm_worker_auth.snapshot_worker_profiles(
        root,
        family,
        session_checker=lambda profile, expected: True,
    )
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profiles"][0]["expected_email"] = "troup.hominidae@gmail.com"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        nlm_worker_auth.restore_worker_profiles(root, family, snapshot_path=snapshot)
    except RuntimeError as exc:
        assert "expected email mismatch" in str(exc)
    else:
        raise AssertionError("restore should reject a mismatched snapshot manifest")


def test_expected_email_for_profile_includes_second_free_account():
    assert nlm_worker_auth.expected_email_for_profile("ytis-free2-worker-01") == "brsthomson@hotmail.com"
    assert nlm_worker_auth.expected_email_for_profile("ytis-free2-worker-04") == "brsthomson@hotmail.com"


def test_expected_email_for_profile_falls_back_to_env_for_unmapped_profile(monkeypatch):
    monkeypatch.setenv("YTIS_NLM_EXPECTED_EMAIL", "future.account@example.com")

    assert nlm_worker_auth.expected_email_for_profile("ytis-future-worker-01") == "future.account@example.com"


def test_default_pro_family_uses_signed_in_pro_chrome_profile():
    pro_family = nlm_worker_auth.DEFAULT_FAMILIES[0]

    assert pro_family.source_profile == "ytis-pro-worker-01"
    assert pro_family.expected_email == "a.hominidae@gmail.com"
    assert pro_family.cdp_browser_root == r"P:\.data\yt-is\browser\notebooklm-pro"
    assert pro_family.cdp_browser_profile_directory == "Profile"


def test_profile_session_is_valid_fails_closed_when_default_chrome_profile_is_running(monkeypatch):
    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    stop_calls = []

    with mock.patch("csf.nlm_worker_auth._default_chrome_profile_pids", return_value={12345}):
        with mock.patch("csf.nlm_worker_auth._stop_chrome_pids", side_effect=lambda pids: stop_calls.append(set(pids))):
            with mock.patch("csf.nlm_worker_auth.run_nlm") as mock_run:
                mock_run.side_effect = AssertionError("default chrome-profile should fail closed before nlm runs")
                assert nlm_worker_auth.profile_session_is_valid("ytis-free1-worker-01") is False

    assert stop_calls == [{12345}]


def test_refresh_profile_session_stops_default_chrome_profile_after_force_login(monkeypatch):
    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    stop_calls = []

    with mock.patch("csf.nlm_worker_auth._default_chrome_profile_pids", side_effect=[set(), {67890}]):
        with mock.patch("csf.nlm_worker_auth._stop_chrome_pids", side_effect=lambda pids: stop_calls.append(set(pids))):
            with mock.patch(
                "csf.nlm_worker_auth.run_nlm",
                return_value=subprocess.CompletedProcess(["login", "--force", "--profile", "ytis-free1-worker-01"], 0, "Account: troup.hominidae@gmail.com\n", ""),
            ):
                assert nlm_worker_auth.refresh_profile_session("ytis-free1-worker-01") is False

    assert stop_calls == [{67890}]


def test_sync_worker_profiles_rejects_wrong_source_account(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "troup.hominidae@gmail.com", "wrong")

    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-pro-worker-01",
            ("ytis-pro-worker-02",),
            "a.hominidae@gmail.com",
        ),
    )

    try:
        nlm_worker_auth.sync_worker_profiles(
            root,
            family,
            source_session_refresher=lambda profile: False,
        )
    except RuntimeError as exc:
        assert "expected account a.hominidae@gmail.com" in str(exc)
    else:
        raise AssertionError("wrong account should be rejected before syncing")


def test_sync_worker_profiles_rejects_wrong_live_account_before_copy(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "fresh-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-free1-worker-01",
            ("ytis-free1-worker-02",),
            "troup.hominidae@gmail.com",
        ),
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, "Account: a.hominidae@gmail.com\n", "")

    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    try:
        nlm_worker_auth.sync_worker_profiles(
            root,
            family,
            source_session_refresher=lambda profile: False,
        )
    except RuntimeError as exc:
        assert "expected troup.hominidae@gmail.com" in str(exc)
    else:
        raise AssertionError("wrong live account should be rejected before syncing")

    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "stale-free"}]
    )


def test_sync_worker_profiles_can_repair_wrong_source_metadata_before_copy(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "a.hominidae@gmail.com", "wrong-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-free1-worker-01",
            ("ytis-free1-worker-02",),
            "troup.hominidae@gmail.com",
        ),
    )
    calls: list[str] = []

    def repair_source(profile: str) -> bool:
        calls.append(profile)
        _write_profile(root, profile + "-repaired", "troup.hominidae@gmail.com", "unused")
        repaired = root / (profile + "-repaired")
        target = root / profile
        (target / "cookies.json").write_text((repaired / "cookies.json").read_text(encoding="utf-8"), encoding="utf-8")
        (target / "metadata.json").write_text(
            json.dumps({"email": "troup.hominidae@gmail.com", "last_validated": "2026-04-30T10:00:00"}),
            encoding="utf-8",
        )
        return True

    backup = nlm_worker_auth.sync_worker_profiles(
        root,
        family,
        source_session_checker=lambda profile: True,
        source_session_refresher=repair_source,
    )

    assert backup is not None
    assert calls == ["ytis-free1-worker-01"]
    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "unused"}]
    )


def test_sync_worker_profiles_rejects_expired_source_session_before_copy(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "expired-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "still-current")
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-free1-worker-01",
            ("ytis-free1-worker-02",),
            "troup.hominidae@gmail.com",
        ),
    )

    try:
        nlm_worker_auth.sync_worker_profiles(
            root,
            family,
            source_session_checker=lambda profile: False,
            source_session_refresher=lambda profile: False,
        )
    except RuntimeError as exc:
        assert "ytis-free1-worker-01" in str(exc)
    else:
        raise AssertionError("expired source session should be rejected before syncing")

    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "still-current"}]
    )
    assert not any(root.glob("backup-before-worker-auth-sync-*"))


def test_sync_worker_profiles_auto_refreshes_source_profile_before_copy(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "renewed-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    calls: list[str] = []
    family = (
        nlm_worker_auth.AuthFamily(
            "ytis-free1-worker-01",
            ("ytis-free1-worker-02",),
            "troup.hominidae@gmail.com",
        ),
    )

    def fake_checker(profile: str) -> bool:
        calls.append(f"check:{profile}")
        return len(calls) > 1

    backup = nlm_worker_auth.sync_worker_profiles(
        root,
        family,
        source_session_checker=fake_checker,
        source_session_refresher=lambda profile: calls.append(f"refresh:{profile}") or True,
    )

    assert backup is not None
    assert calls == [
        "check:ytis-free1-worker-01",
        "refresh:ytis-free1-worker-01",
        "check:ytis-free1-worker-01",
    ]
    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "renewed-free"}]
    )


def test_sync_worker_profiles_uses_real_nlm_process_for_force_recovery(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "nlm-args.log"
    valid_marker = tmp_path / "session-valid"
    _write_fake_nlm_executable(bin_dir, log_path, valid_marker)
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "renewed-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    monkeypatch.setenv("YTIS_NLM_CLI", str(bin_dir / ("nlm.cmd" if os.name == "nt" else "nlm")))
    monkeypatch.setenv("YTIS_FAKE_NLM_PROFILE_ROOT", str(root))

    backup = nlm_worker_auth.sync_worker_profiles(
        root,
        (
            nlm_worker_auth.AuthFamily(
                "ytis-free1-worker-01",
                ("ytis-free1-worker-02",),
                "troup.hominidae@gmail.com",
            ),
        ),
    )

    assert backup is not None
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "login --check --profile ytis-free1-worker-01",
        "login --force --profile ytis-free1-worker-01",
        "login --check --profile ytis-free1-worker-01",
    ]
    assert (root / "ytis-free1-worker-02" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "renewed-free"}]
    )


def test_worker_auth_cli_sync_uses_real_nlm_process_for_force_recovery(tmp_path):
    root = tmp_path / "profiles"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "nlm-args.log"
    valid_marker = tmp_path / "session-valid"
    _write_fake_nlm_executable(bin_dir, log_path, valid_marker)
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "renewed-pro")
    _write_profile(root, "ytis-pro-worker-02", "a.hominidae@gmail.com", "stale-pro")
    _write_profile(root, "ytis-pro-worker-03", "a.hominidae@gmail.com", "stale-pro")
    _write_profile(root, "ytis-pro-worker-04", "a.hominidae@gmail.com", "stale-pro")
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "renewed-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "stale-free")
    _write_profile(root, "ytis-free1-worker-03", "troup.hominidae@gmail.com", "stale-free")
    _write_profile(root, "ytis-free1-worker-04", "troup.hominidae@gmail.com", "stale-free")
    _write_profile(root, "ytis-free2-worker-01", "brsthomson@hotmail.com", "renewed-free2")
    _write_profile(root, "ytis-free2-worker-02", "brsthomson@hotmail.com", "stale-free2")
    _write_profile(root, "ytis-free2-worker-03", "brsthomson@hotmail.com", "stale-free2")
    _write_profile(root, "ytis-free2-worker-04", "brsthomson@hotmail.com", "stale-free2")

    env = os.environ.copy()
    env["PYTHONPATH"] = "P:\\packages\\yt-is"
    env["YTIS_NLM_CLI"] = str(bin_dir / ("nlm.cmd" if os.name == "nt" else "nlm"))
    env["YTIS_FAKE_NLM_PROFILE_ROOT"] = str(root)
    env["YTIS_NLM_WORKER_AUTH_USE_CDP"] = "0"
    result = subprocess.run(
        [
            sys.executable,
            "P:/packages/yt-is/bin/csf-nlm-worker-auth",
            "--profile-root",
            str(root),
            "sync",
        ],
        capture_output=True,
        text=True,
        cwd="P:/packages/yt-is",
        env=env,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "synced worker auth profiles" in result.stdout
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "login --check --profile ytis-pro-worker-01",
        "login --force --profile ytis-pro-worker-01",
        "login --check --profile ytis-pro-worker-01",
        "login --check --profile ytis-free1-worker-01",
        "login --force --profile ytis-free1-worker-01",
        "login --check --profile ytis-free1-worker-01",
        "login --check --profile ytis-free2-worker-01",
        "login --force --profile ytis-free2-worker-01",
        "login --check --profile ytis-free2-worker-01",
        "login --check --profile ytis-pro-worker-01",
        "login --check --profile ytis-pro-worker-02",
        "login --check --profile ytis-pro-worker-03",
        "login --check --profile ytis-pro-worker-04",
        "login --check --profile ytis-free1-worker-01",
        "login --check --profile ytis-free1-worker-02",
        "login --check --profile ytis-free1-worker-03",
        "login --check --profile ytis-free1-worker-04",
        "login --check --profile ytis-free2-worker-01",
        "login --check --profile ytis-free2-worker-02",
        "login --check --profile ytis-free2-worker-03",
        "login --check --profile ytis-free2-worker-04",
    ]


def test_worker_auth_cli_snapshot_and_restore_round_trip(tmp_path):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "good-pro")
    _write_profile(root, "ytis-pro-worker-02", "a.hominidae@gmail.com", "good-pro")
    _write_profile(root, "ytis-pro-worker-03", "a.hominidae@gmail.com", "good-pro")
    _write_profile(root, "ytis-pro-worker-04", "a.hominidae@gmail.com", "good-pro")
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "good-free")
    _write_profile(root, "ytis-free1-worker-02", "troup.hominidae@gmail.com", "good-free")
    _write_profile(root, "ytis-free1-worker-03", "troup.hominidae@gmail.com", "good-free")
    _write_profile(root, "ytis-free1-worker-04", "troup.hominidae@gmail.com", "good-free")
    _write_profile(root, "ytis-free2-worker-01", "brsthomson@hotmail.com", "good-free2")
    _write_profile(root, "ytis-free2-worker-02", "brsthomson@hotmail.com", "good-free2")
    _write_profile(root, "ytis-free2-worker-03", "brsthomson@hotmail.com", "good-free2")
    _write_profile(root, "ytis-free2-worker-04", "brsthomson@hotmail.com", "good-free2")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "nlm-args.log"
    valid_marker = tmp_path / "session-valid"
    valid_marker.write_text(json.dumps(nlm_worker_auth.iter_worker_profiles()), encoding="utf-8")
    _write_fake_nlm_executable(bin_dir, log_path, valid_marker)
    env = os.environ.copy()
    env["PYTHONPATH"] = "P:\\packages\\yt-is"
    env["YTIS_NLM_CLI"] = str(bin_dir / ("nlm.cmd" if os.name == "nt" else "nlm"))
    env["YTIS_FAKE_NLM_PROFILE_ROOT"] = str(root)

    snapshot_result = subprocess.run(
        [
            sys.executable,
            "P:/packages/yt-is/bin/csf-nlm-worker-auth",
            "--profile-root",
            str(root),
            "snapshot",
        ],
        capture_output=True,
        text=True,
        cwd="P:/packages/yt-is",
        env=env,
        timeout=30,
        check=False,
    )

    assert snapshot_result.returncode == 0, snapshot_result.stderr
    assert "[auth] snapshot=" in snapshot_result.stdout
    (root / "ytis-pro-worker-01" / "cookies.json").write_text(json.dumps([{"name": "corrupt"}]), encoding="utf-8")
    restore_result = subprocess.run(
        [
            sys.executable,
            "P:/packages/yt-is/bin/csf-nlm-worker-auth",
            "--profile-root",
            str(root),
            "--skip-check",
            "restore",
        ],
        capture_output=True,
        text=True,
        cwd="P:/packages/yt-is",
        env=env,
        timeout=30,
        check=False,
    )

    assert restore_result.returncode == 0, restore_result.stderr
    assert "[auth] restored=" in restore_result.stdout
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "good-pro"}]
    )


def test_refresh_source_profile_restores_source_snapshot_on_failed_cdp_refresh(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    before_metadata = (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8")
    before_cookies = (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8")

    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: True)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: object())

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["login"] and "--force" in cmd and "--provider" in cmd:
            profile = root / "ytis-pro-worker-01"
            (profile / "cookies.json").write_text(json.dumps([{"name": "poisoned-pro"}]), encoding="utf-8")
            (profile / "metadata.json").write_text(
                json.dumps({"email": "troup.hominidae@gmail.com", "last_validated": "2026-04-30T10:00:00"}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 1, "Account: troup.hominidae@gmail.com\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1)

    assert ok is False
    assert (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8") == before_metadata
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == before_cookies


def test_refresh_source_profile_restores_source_snapshot_when_cdp_unreachable(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    before_metadata = (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8")
    before_cookies = (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8")

    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: False)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: object())

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1)

    assert ok is False
    assert (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8") == before_metadata
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == before_cookies


def test_refresh_source_profile_fails_closed_when_default_chrome_profile_appears(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    before_metadata = (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8")
    before_cookies = (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8")
    pid_snapshots = iter([set(), {12345}])
    stopped_pids: list[int] = []
    called: list[list[str]] = []

    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: True)
    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", lambda port: 0)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(nlm_worker_auth, "_chrome_pids_for_root", lambda browser_root: next(pid_snapshots))
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_pids", lambda pids: stopped_pids.extend(sorted(pids)))

    def fake_run(cmd, **kwargs):
        called.append(cmd)
        if cmd[:2] == ["login"] and "--provider" in cmd and "--cdp-url" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1)

    assert ok is False
    assert stopped_pids == [12345]
    assert called == [
        [
            "nlm",
            "login",
            "--profile",
            "ytis-pro-worker-01",
            "--provider",
            "openclaw",
            "--cdp-url",
            "http://127.0.0.1:18870",
            "--force",
        ]
    ]
    assert (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8") == before_metadata
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == before_cookies


def test_refresh_source_profile_refuses_existing_default_chrome_profile_in_noninteractive_mode(
    tmp_path, monkeypatch
):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "fresh-free")
    popen_calls: list[object] = []
    run_calls: list[list[str]] = []

    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_chrome_pids_for_root", lambda browser_root: {999})
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append(args))

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[1], timeout_s=1)

    assert ok is False
    assert popen_calls == []
    assert run_calls == []
    assert (root / "ytis-free1-worker-01" / "metadata.json").read_text(encoding="utf-8") == json.dumps(
        {"email": "troup.hominidae@gmail.com", "last_validated": "2026-04-29T10:00:00"}
    )
    assert (root / "ytis-free1-worker-01" / "cookies.json").read_text(encoding="utf-8") == json.dumps(
        [{"name": "fresh-free"}]
    )


def test_close_cdp_noise_tabs_only_closes_known_false_tabs(monkeypatch):
    calls = []
    pages = [
        {"id": "tab-noise", "url": "http://0.0.0.2/"},
        {"id": "tab-blank", "url": "about:blank"},
        {"id": "tab-nlm", "url": "https://notebooklm.google.com/"},
        {"id": "tab-login", "url": "https://accounts.google.com/signin"},
    ]

    class FakeResponse:
        def __init__(self, body=b""):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.body

    def fake_urlopen(url, timeout):
        calls.append(url)
        if url == "http://127.0.0.1:18870/json":
            return FakeResponse(json.dumps(pages).encode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(nlm_worker_auth.urllib.request, "urlopen", fake_urlopen)

    assert nlm_worker_auth._close_cdp_noise_tabs(18870) == 2
    assert calls == [
        "http://127.0.0.1:18870/json",
        "http://127.0.0.1:18870/json/close/tab-noise",
        "http://127.0.0.1:18870/json/close/tab-blank",
    ]


def test_mark_browser_profile_clean_updates_crashed_preferences(tmp_path):
    root = tmp_path / "browser"
    profile = root / "Profile 2"
    profile.mkdir(parents=True)
    prefs_path = profile / "Preferences"
    prefs_path.write_text(json.dumps({"profile": {"exit_type": "Crashed"}}), encoding="utf-8")

    nlm_worker_auth._mark_browser_profile_clean(str(root), "Profile 2")

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["profile"]["exit_type"] == "Normal"
    assert prefs["profile"]["exited_cleanly"] is True


def test_refresh_source_profile_closes_noise_tabs_before_capture(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    events = []

    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: True)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: object())

    def fake_close_noise_tabs(port):
        events.append(("close_noise", port))
        return 1

    def fake_run(cmd, **kwargs):
        events.append(("run", cmd))
        return subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", "")

    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", fake_close_noise_tabs)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    assert nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1) is True
    assert events[0] == ("close_noise", 18870)
    assert events[1][0] == "run"


def test_refresh_source_profile_launches_browser_minimized_by_default(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    popen_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    cdp_states = iter([False, True])

    monkeypatch.delenv("YTIS_NLM_BROWSER_VISIBLE", raising=False)
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: next(cdp_states))
    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", lambda port: 0)
    monkeypatch.setattr(nlm_worker_auth, "run_nlm", lambda cmd, timeout_s=1, env=None: subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", ""))

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", fake_popen)

    assert nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1) is True
    assert popen_calls
    args, kwargs = popen_calls[0]
    chrome_args = list(args[0])
    assert "--start-minimized" in chrome_args
    assert kwargs.get("startupinfo") is not None


def test_refresh_source_profile_can_launch_visible_browser_when_requested(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    popen_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    cdp_states = iter([False, True])

    monkeypatch.setenv("YTIS_NLM_BROWSER_VISIBLE", "1")
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: next(cdp_states))
    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", lambda port: 0)
    monkeypatch.setattr(nlm_worker_auth, "run_nlm", lambda cmd, timeout_s=1, env=None: subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", ""))

    def fake_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", fake_popen)

    assert nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1) is True
    assert popen_calls
    args, kwargs = popen_calls[0]
    chrome_args = list(args[0])
    assert "--start-minimized" not in chrome_args
    assert kwargs.get("startupinfo") is None


def test_refresh_source_profile_reuses_existing_cdp_browser_across_refreshes(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    popen_calls: list[list[str]] = []
    stop_calls: list[str] = []
    cdp_states = iter([False, True, True])

    monkeypatch.delenv("YTIS_NLM_BROWSER_VISIBLE", raising=False)
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: next(cdp_states))
    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", lambda port: 0)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: stop_calls.append(browser_root))
    monkeypatch.setattr(nlm_worker_auth, "run_nlm", lambda cmd, timeout_s=1, env=None: subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", ""))

    def fake_popen(args, **kwargs):
        popen_calls.append(list(args))
        return object()

    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", fake_popen)

    assert nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1) is True
    assert nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1) is True
    assert len(popen_calls) == 1
    assert len(stop_calls) == 1


