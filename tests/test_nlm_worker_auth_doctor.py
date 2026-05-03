"""Tests for the NotebookLM worker-auth doctor gate."""

from __future__ import annotations

import json
from types import SimpleNamespace

from csf import nlm_worker_auth


def test_doctor_accepts_clean_lane_config_and_empty_run_root(tmp_path, monkeypatch):
    lane_config = tmp_path / "lanes.json"
    lane_config.write_text(
        json.dumps(
            [
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 1,
                    "notebooklm_profile_prefix": "ytis-free1-worker",
                    "notebooklm_profiles": ["ytis-free1-worker-01"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
                    "browser_profile_directory": "Default",
                    "worker_state_root": str(tmp_path / "worker_states"),
                    "notebook_prefix": "benchmark-shard-free",
                }
            ]
        ),
        encoding="utf-8",
    )
    run_root = tmp_path / "run-root"
    monkeypatch.setattr(
        nlm_worker_auth,
        "doctor_lane_setup",
        lambda lane_config, run_root, timeout_s=30.0: (
            SimpleNamespace(lane="free"),
        ),
    )

    result = nlm_worker_auth.main([
        "doctor",
        "--lane-config",
        str(lane_config),
        "--run-root",
        str(run_root),
    ])

    assert result == 0


def test_doctor_rejects_run_root_with_existing_contents(tmp_path, monkeypatch):
    lane_config = tmp_path / "lanes.json"
    lane_config.write_text(
        json.dumps(
            [
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 1,
                    "notebooklm_profile_prefix": "ytis-free1-worker",
                    "notebooklm_profiles": ["ytis-free1-worker-01"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
                    "browser_profile_directory": "Default",
                    "worker_state_root": str(tmp_path / "worker_states"),
                    "notebook_prefix": "benchmark-shard-free",
                }
            ]
        ),
        encoding="utf-8",
    )
    run_root = tmp_path / "run-root"
    run_root.mkdir()
    (run_root / "stale.txt").write_text("stale", encoding="utf-8")

    monkeypatch.setattr(
        nlm_worker_auth,
        "doctor_lane_setup",
        lambda lane_config, run_root, timeout_s=30.0: (_ for _ in ()).throw(RuntimeError("run root is not empty")),
    )

    result = nlm_worker_auth.main([
        "doctor",
        "--lane-config",
        str(lane_config),
        "--run-root",
        str(run_root),
    ])

    assert result == 1
