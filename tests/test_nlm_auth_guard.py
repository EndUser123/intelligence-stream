"""Tests for shared NotebookLM auth command routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from csf import nlm_auth_guard


@pytest.mark.parametrize(
    "args, expected",
    [
        (["source", "list", "nb-1"], ["source", "list", "nb-1", "--profile", "worker-01"]),
        (["notebook", "query", "nb-1", "prompt"], ["notebook", "query", "nb-1", "prompt", "--profile", "worker-01"]),
        (["audio", "create", "nb-1", "--confirm"], ["audio", "create", "nb-1", "--confirm", "--profile", "worker-01"]),
    ],
)
def test_add_profile_args_pins_non_login_commands(monkeypatch, args, expected):
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "worker-01")

    assert nlm_auth_guard.add_profile_args(args) == expected


def test_add_profile_args_leaves_login_commands_unpinned(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "worker-01")

    assert nlm_auth_guard.add_profile_args(["login", "--check"]) == ["login", "--check"]


def _browser_health_sample(*, default_pids=None, unexpected=None, chrome_process_count=0, chrome_rss_bytes_total=0):
    return {
        "allowed_browser_roots": [r"P:\\.data\yt-is\browser\notebooklm-pro"],
        "allowed_profile_pid_count": 0,
        "allowed_profile_pid_counts_by_root": {r"P:\\.data\yt-is\browser\notebooklm-pro": 0},
        "chrome_process_count": chrome_process_count,
        "chrome_rss_bytes_total": chrome_rss_bytes_total,
        "default_profile_pids": list(default_pids or []),
        "unexpected_processes": list(unexpected or []),
    }


def test_browser_health_gate_passes_when_environment_is_clean(monkeypatch):
    monkeypatch.setattr(nlm_auth_guard, "chrome_pids_for_root", lambda root: set())
    monkeypatch.setattr(nlm_auth_guard, "stop_chrome_pids", lambda pids: None)
    monkeypatch.setattr(
        nlm_auth_guard,
        "_sample_browser_health",
        lambda allowed_roots: _browser_health_sample(chrome_process_count=2, chrome_rss_bytes_total=1234),
    )

    report = nlm_auth_guard.browser_health_gate(
        [Path(r"P:\\.data\yt-is\browser\notebooklm-pro")],
        settle_window_s=0.0,
        sample_interval_s=0.0,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert report["status"] == "clean"
    assert report["initial_default_profile_detected_count"] == 0
    assert report["default_profile_remaining_count"] == 0
    assert report["unexpected_process_count"] == 0
    assert report["sample_count"] == 2


def test_browser_health_gate_marks_recovered_clean_after_default_profile_cleanup(monkeypatch):
    reaped: list[set[int]] = []
    monkeypatch.setattr(nlm_auth_guard, "chrome_pids_for_root", lambda root: {12345})
    monkeypatch.setattr(nlm_auth_guard, "stop_chrome_pids", lambda pids: reaped.append(set(pids)))
    monkeypatch.setattr(
        nlm_auth_guard,
        "_sample_browser_health",
        lambda allowed_roots: _browser_health_sample(chrome_process_count=1, chrome_rss_bytes_total=256),
    )

    report = nlm_auth_guard.browser_health_gate(
        [Path(r"P:\\.data\yt-is\browser\notebooklm-pro")],
        settle_window_s=0.0,
        sample_interval_s=0.0,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert report["status"] == "recovered_clean"
    assert report["initial_default_profile_detected_count"] == 1
    assert report["default_profile_reaped_count"] == 1
    assert report["default_profile_remaining_count"] == 0
    assert reaped == [{12345}]


def test_browser_health_gate_is_unhealthy_for_unexpected_chrome(monkeypatch):
    monkeypatch.setattr(nlm_auth_guard, "chrome_pids_for_root", lambda root: set())
    monkeypatch.setattr(nlm_auth_guard, "stop_chrome_pids", lambda pids: None)
    monkeypatch.setattr(
        nlm_auth_guard,
        "_sample_browser_health",
        lambda allowed_roots: _browser_health_sample(
            unexpected=[{"pid": 222, "cmdline": r"chrome.exe --user-data-dir=C:\Users\brsth\AppData"}],
            chrome_process_count=1,
            chrome_rss_bytes_total=512,
        ),
    )

    report = nlm_auth_guard.browser_health_gate(
        [Path(r"P:\\.data\yt-is\browser\notebooklm-pro")],
        settle_window_s=0.0,
        sample_interval_s=0.0,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert report["status"] == "unhealthy"
    assert report["unexpected_process_count"] == 1
    assert report["issues"]
