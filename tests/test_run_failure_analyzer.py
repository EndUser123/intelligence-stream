"""Tests for post-run failure analysis."""

from __future__ import annotations

import json
from pathlib import Path

from csf import run_failure_analyzer


def test_analyze_run_root_summarizes_failures_and_recovery(tmp_path):
    run_root = tmp_path / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (run_root / "sharded_lane_series_summary.json").write_text(
        json.dumps({"report_version": 1, "status": "ok"}),
        encoding="utf-8",
    )
    (logs / "term.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {
                            "nb_id": "nb-old",
                            "video_id": "vid-1",
                            "source_id": "src-1",
                            "status": "command_failed",
                            "failure_reason": "Fetch failed for src-1: command_failed",
                            "stderr": "API error (code 5): NOT_FOUND",
                        },
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_command_retried",
                        "data": {"status": "default_profile_reaped_before_command", "nb_id": "nb-old"},
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_dead_notebook_recreated",
                        "data": {"old_nb_id": "nb-old", "nb_id": "nb-fresh", "recovery_batch_size": 1},
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {
                            "nb_id": "nb-fresh",
                            "video_id": "vid-2",
                            "source_id": "src-2",
                            "status": "command_failed",
                            "failure_reason": "Fetch failed for src-2: command_failed",
                            "stderr": "command failed",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    analysis = run_failure_analyzer.analyze_run_root(run_root)

    assert analysis.run_root == run_root
    assert analysis.summary_path == run_root / "sharded_lane_series_summary.json"
    assert analysis.jsonl_file_count == 1
    assert analysis.unique_failed_video_ids == ("vid-1", "vid-2")
    assert analysis.unique_failed_source_ids == ("src-1", "src-2")
    assert analysis.unique_notebook_ids == ("nb-fresh", "nb-old")
    assert analysis.not_found_count == 1
    assert analysis.command_failed_count == 2
    assert analysis.default_profile_reap_count == 1
    assert analysis.recovery_event_count == 1
    assert analysis.pre_recovery_failure_count == 1
    assert analysis.post_recovery_failure_count == 1
