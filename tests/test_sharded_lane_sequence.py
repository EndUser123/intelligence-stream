"""Tests for the guarded sharded lane benchmark sequence."""

from __future__ import annotations

import json
from pathlib import Path

from csf.run_evidence_check import EvidenceCheckResult
from csf.sharded_lane_series import DEFAULT_TRACE_ROOT, LaneConfig

import csf.sharded_lane_sequence as mod


def _lane_config(tmp_path: Path) -> Path:
    lane_config = tmp_path / "lanes.json"
    lane_config.write_text("[]", encoding="utf-8")
    return lane_config


def _lanes(tmp_path: Path) -> tuple[LaneConfig, ...]:
    return (
        LaneConfig(
            lane="pro",
            account_class="pro",
            workers=1,
            notebooklm_profile_prefix="ytis-pro-worker",
            notebooklm_profiles=("ytis-pro-worker-01",),
            browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-pro"),
            worker_state_root=tmp_path / "pro" / "worker_states",
            notebook_prefix="benchmark-shard-pro",
        ),
    )


def test_main_runs_doctor_smoke_evidence_soak_in_order(tmp_path, monkeypatch):
    calls: list[str] = []
    run_root = tmp_path / "run"
    smoke_output_root = tmp_path / "run" / "smoke"
    soak_output_root = tmp_path / "run" / "soak"
    stale_report = run_root / "sharded_lane_series_summary.json"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text('{"stale": true}', encoding="utf-8")

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))
    monkeypatch.setattr(
        mod,
        "_check_post_run_default_profile_hygiene",
        lambda: {
            "status": "clean",
            "detected_count": 0,
            "reaped_count": 0,
            "remaining_count": 0,
            "detected_pids": [],
            "reaped_pids": [],
            "remaining_pids": [],
        },
    )

    def fake_run_sharded_lane_series(*, output_root, trace_root, limit, batch_size, **kwargs):
        phase = "smoke" if output_root == smoke_output_root else "soak"
        calls.append(phase)
        assert trace_root == DEFAULT_TRACE_ROOT
        if phase == "smoke":
            assert limit == 5
            assert batch_size == 2
        else:
            assert limit == 400
            assert batch_size == 200
        return {
            "report_version": 1,
            "status": "ok",
            "report_path": str(output_root / "sharded_lane_series_summary.json"),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        }

    def fake_inspect_run_root(run_root, *, require_forced_refresh_marker=False):
        calls.append("evidence")
        assert run_root == smoke_output_root
        assert require_forced_refresh_marker is False
        return EvidenceCheckResult(True, smoke_output_root / "sharded_lane_series_summary.json", ())

    monkeypatch.setattr(mod, "run_sharded_lane_series", fake_run_sharded_lane_series)
    monkeypatch.setattr(mod, "inspect_run_root", fake_inspect_run_root)

    result = mod.main([
        "--lane-config",
        str(_lane_config(tmp_path)),
        "--run-root",
        str(tmp_path / "run"),
        "--smoke-limit",
        "5",
        "--smoke-batch-size",
        "2",
    ])

    assert result == 0
    assert calls == ["doctor", "smoke", "evidence", "soak"]
    persisted = json.loads(stale_report.read_text(encoding="utf-8"))
    assert persisted["report_path"] == str(stale_report)
    assert persisted["sequence_smoke_report_path"] == str(smoke_output_root / "sharded_lane_series_summary.json")
    assert persisted["sequence_soak_report_path"] == str(soak_output_root / "sharded_lane_series_summary.json")
    assert persisted["status"] == "ok"
    assert persisted["post_run_hygiene"]["status"] == "clean"


def test_main_stops_before_soak_when_evidence_fails(tmp_path, monkeypatch):
    calls: list[str] = []
    smoke_output_root = tmp_path / "run" / "smoke"

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))
    monkeypatch.setattr(
        mod,
        "_check_post_run_default_profile_hygiene",
        lambda: {
            "status": "clean",
            "detected_count": 0,
            "reaped_count": 0,
            "remaining_count": 0,
            "detected_pids": [],
            "reaped_pids": [],
            "remaining_pids": [],
        },
    )

    def fake_run_sharded_lane_series(*, output_root, **kwargs):
        calls.append("smoke")
        assert output_root == smoke_output_root
        return {
            "report_version": 1,
            "status": "ok",
            "report_path": str(output_root / "sharded_lane_series_summary.json"),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        }

    def fake_inspect_run_root(run_root, *, require_forced_refresh_marker=False):
        calls.append("evidence")
        return EvidenceCheckResult(False, smoke_output_root / "sharded_lane_series_summary.json", ("missing marker",))

    monkeypatch.setattr(mod, "run_sharded_lane_series", fake_run_sharded_lane_series)
    monkeypatch.setattr(mod, "inspect_run_root", fake_inspect_run_root)

    result = mod.main([
        "--lane-config",
        str(_lane_config(tmp_path)),
        "--run-root",
        str(tmp_path / "run"),
    ])

    assert result == 1
    assert calls == ["doctor", "smoke", "evidence"]


def test_main_rewrites_run_root_summary_on_invalidated_soak(tmp_path, monkeypatch):
    calls: list[str] = []
    run_root = tmp_path / "run"
    smoke_output_root = tmp_path / "run" / "smoke"
    soak_output_root = tmp_path / "run" / "soak"
    stale_report = run_root / "sharded_lane_series_summary.json"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text('{"stale": true}', encoding="utf-8")

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))
    monkeypatch.setattr(
        mod,
        "_check_post_run_default_profile_hygiene",
        lambda: {
            "status": "clean",
            "detected_count": 0,
            "reaped_count": 0,
            "remaining_count": 0,
            "detected_pids": [],
            "reaped_pids": [],
            "remaining_pids": [],
        },
    )

    def fake_run_sharded_lane_series(*, output_root, **kwargs):
        phase = "smoke" if output_root == smoke_output_root else "soak"
        calls.append(phase)
        report_path = output_root / "sharded_lane_series_summary.json"
        return {
            "report_version": 1,
            "status": "invalidated",
            "failure_count": 1,
            "failures": [{"lane": "pro", "error": "boom"}],
            "report_path": str(report_path),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        }

    def fake_inspect_run_root(run_root_arg, *, require_forced_refresh_marker=False):
        calls.append("evidence")
        assert run_root_arg == smoke_output_root
        return EvidenceCheckResult(True, smoke_output_root / "sharded_lane_series_summary.json", ())

    monkeypatch.setattr(mod, "run_sharded_lane_series", fake_run_sharded_lane_series)
    monkeypatch.setattr(mod, "inspect_run_root", fake_inspect_run_root)

    result = mod.main([
        "--lane-config",
        str(_lane_config(tmp_path)),
        "--run-root",
        str(run_root),
    ])

    assert result == 1
    assert calls == ["doctor", "smoke", "evidence", "soak"]
    persisted = json.loads(stale_report.read_text(encoding="utf-8"))
    assert persisted["report_path"] == str(stale_report)
    assert persisted["status"] == "invalidated"
    assert persisted["failure_count"] == 1
    assert persisted["sequence_smoke_report_path"] == str(smoke_output_root / "sharded_lane_series_summary.json")
    assert persisted["sequence_soak_report_path"] == str(soak_output_root / "sharded_lane_series_summary.json")


def test_main_records_post_run_hygiene_and_fails_when_default_profile_persists(tmp_path, monkeypatch):
    calls: list[str] = []
    run_root = tmp_path / "run"
    smoke_output_root = tmp_path / "run" / "smoke"
    soak_output_root = tmp_path / "run" / "soak"
    stale_report = run_root / "sharded_lane_series_summary.json"
    stale_report.parent.mkdir(parents=True, exist_ok=True)
    stale_report.write_text('{"stale": true}', encoding="utf-8")

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))

    def fake_run_sharded_lane_series(*, output_root, **kwargs):
        phase = "smoke" if output_root == smoke_output_root else "soak"
        calls.append(phase)
        return {
            "report_version": 1,
            "status": "ok",
            "report_path": str(output_root / "sharded_lane_series_summary.json"),
            "combined": {
                "hot_path_videos_per_hour": 0.0,
                "hot_path_success_count_total": 0,
                "fail_count_total": 0,
                "wall_elapsed_s": 0.0,
            },
        }

    monkeypatch.setattr(mod, "run_sharded_lane_series", fake_run_sharded_lane_series)
    monkeypatch.setattr(
        mod,
        "inspect_run_root",
        lambda run_root_arg, *, require_forced_refresh_marker=False: calls.append("evidence")
        or EvidenceCheckResult(True, smoke_output_root / "sharded_lane_series_summary.json", ()),
    )
    monkeypatch.setattr(
        mod,
        "_check_post_run_default_profile_hygiene",
        lambda: {
            "status": "still_running",
            "detected_count": 1,
            "reaped_count": 0,
            "remaining_count": 1,
            "detected_pids": [1234],
            "reaped_pids": [],
            "remaining_pids": [1234],
        },
    )

    result = mod.main([
        "--lane-config",
        str(_lane_config(tmp_path)),
        "--run-root",
        str(run_root),
    ])

    assert result == 1
    assert calls == ["doctor", "smoke", "evidence", "soak"]
    persisted = json.loads(stale_report.read_text(encoding="utf-8"))
    assert persisted["post_run_hygiene"]["status"] == "still_running"
    assert persisted["post_run_hygiene"]["remaining_pids"] == [1234]
