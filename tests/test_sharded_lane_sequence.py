"""Tests for the guarded sharded lane benchmark sequence."""

from __future__ import annotations

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
    smoke_output_root = tmp_path / "run" / "smoke"
    soak_output_root = tmp_path / "run" / "soak"

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))

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


def test_main_stops_before_soak_when_evidence_fails(tmp_path, monkeypatch):
    calls: list[str] = []
    smoke_output_root = tmp_path / "run" / "smoke"

    monkeypatch.setattr(mod, "doctor_lane_setup", lambda *args, **kwargs: calls.append("doctor") or _lanes(tmp_path))

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
