"""Tests for post-run evidence validation."""

from __future__ import annotations

import json

import csf.run_evidence_check as run_evidence_check


def test_run_evidence_check_accepts_clean_run_root(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"report_version": 1, "combined": {"hot_path_success_count_total": 1}}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps({"action": "nlm_auth_checked", "data": {"status": "ok"}}) + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root)])

    assert result == 0


def test_run_evidence_check_rejects_default_profile_marker(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"report_version": 1, "combined": {"hot_path_success_count_total": 1}}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps({"action": "nlm_auth_failed", "data": {"status": "default_profile_running"}}) + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root)])

    assert result == 1


def test_run_evidence_check_rejects_zero_growth_terminal_marker(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"report_version": 1, "combined": {"hot_path_success_count_total": 1}}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps(
            {
                "action": "nlm_batch_subbatch_zero_growth_terminal",
                "data": {
                    "failure_reason": "source_add_failed",
                    "source_count_before": 0,
                    "source_count_after": 0,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root)])

    assert result == 1


def test_run_evidence_check_rejects_invalidated_summary_status(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps(
            {"report_version": 1, "status": "invalidated", "combined": {"hot_path_success_count_total": 0}}
        ),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps({"action": "nlm_auth_checked", "data": {"status": "ok"}}) + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root)])

    assert result == 1


def test_run_evidence_check_requires_forced_refresh_marker_when_requested(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"report_version": 1, "combined": {"hot_path_success_count_total": 1}}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps({"action": "nlm_auth_forced_refresh_scheduled", "data": {"status": "scheduled"}}) + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root), "--require-forced-refresh-marker"])

    assert result == 0


def test_run_evidence_check_rejects_missing_report_version(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"combined": {"hot_path_success_count_total": 1}}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        json.dumps({"action": "nlm_auth_checked", "data": {"status": "ok"}}) + "\n",
        encoding="utf-8",
    )

    result = run_evidence_check.main(["--run-root", str(run_root)])

    assert result == 1
