"""Tests for the worker-count sweep runner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from csf import worker_count_sweep


def test_load_fetch_completed_event_from_jsonl(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    trace = log_dir / "fake-terminal.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"action": "log", "data": {"msg": "ignore me"}}),
                json.dumps(
                    {
                        "action": "fetch_completed",
                        "data": {
                            "success_count": 10,
                            "fail_count": 2,
                            "skip_count": 1,
                            "processed_count": 13,
                            "elapsed_s": 26.0,
                            "worker_stage_totals": {
                                "add_sources_elapsed_s_total": 3.0,
                                "content_fetch_status_counts_total": {"ready": 2, "too_short": 1},
                                "source_ready_age_s_total": 6.0,
                                "source_ready_age_s_max": 4.0,
                                "youtube_ytdlp_elapsed_s_total": 2.5,
                                "youtube_ytdlp_elapsed_s_max": 1.5,
                                "youtube_ytdlp_elapsed_s_count": 2,
                                "youtube_page_elapsed_s_total": 1.0,
                                "youtube_page_elapsed_s_max": 1.0,
                                "youtube_page_elapsed_s_count": 1,
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = worker_count_sweep._load_fetch_completed_event(log_dir)

    assert payload["success_count"] == 10
    assert payload["fail_count"] == 2
    assert payload["skip_count"] == 1
    assert payload["processed_count"] == 13
    assert payload["worker_stage_totals"]["add_sources_elapsed_s_total"] == 3.0
    assert payload["worker_stage_totals"]["content_fetch_status_counts_total"]["ready"] == 2
    assert payload["worker_stage_totals"]["youtube_ytdlp_elapsed_s_total"] == 2.5
    assert payload["worker_stage_totals"]["youtube_page_elapsed_s_count"] == 1


def test_run_fetch_trial_captures_fetch_completed_summary(tmp_path, monkeypatch):
    def fake_run(command, capture_output, text, cwd, env, check, timeout=None):
        assert command[0] == "python.exe" or command[0].endswith("python.exe") or command[0].endswith("python")
        assert command[2] == "fetch"
        assert command[4] == "2"
        assert command[6] == "37"
        log_dir = Path(env["INTELLIGENCE_STREAM_LOG_DIR"])
        log_dir.mkdir(parents=True, exist_ok=True)
        trace = log_dir / "fake-terminal.jsonl"
        trace.write_text(
            json.dumps(
                {
                    "action": "fetch_completed",
                    "data": {
                        "success_count": 12,
                        "fail_count": 3,
                        "skip_count": 5,
                        "processed_count": 20,
                        "elapsed_s": 10.0,
                        "worker_stage_totals": {
                            "add_sources_elapsed_s_total": 4.5,
                            "materialization_wait_elapsed_s_total": 2.0,
                            "cleanup_elapsed_s_total": 0.5,
                            "content_fetch_status_counts_total": {"ready": 2, "too_short": 1},
                            "source_ready_age_s_total": 6.0,
                            "source_ready_age_s_max": 4.0,
                            "youtube_ytdlp_elapsed_s_total": 2.5,
                            "youtube_ytdlp_elapsed_s_max": 1.5,
                            "youtube_ytdlp_elapsed_s_count": 2,
                            "youtube_page_elapsed_s_total": 1.0,
                            "youtube_page_elapsed_s_max": 1.0,
                            "youtube_page_elapsed_s_count": 1,
                        },
                        "materialization_started": True,
                        "timeout_hit": False,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return mock.Mock(returncode=0, stdout="done\n", stderr="")

    monkeypatch.setattr(worker_count_sweep.subprocess, "run", fake_run)
    summary = worker_count_sweep._run_fetch_trial(
        workers=2,
        limit=37,
        sample_label="mixed_lane",
        output_dir=tmp_path,
        python_executable="python.exe",
    )

    assert summary.workers == 2
    assert summary.limit == 37
    assert summary.returncode == 0
    assert summary.success_count == 12
    assert summary.fail_count == 3
    assert summary.skip_count == 5
    assert summary.processed_count == 20
    assert summary.videos_per_hour == 4320.0
    assert summary.processed_per_hour == 7200.0
    assert summary.add_elapsed_s == 4.5
    assert summary.readiness_elapsed_s == 2.0
    assert summary.cleanup_elapsed_s == 0.5
    assert summary.worker_idle_wait_s == 3.0
    assert summary.sample_label == "mixed_lane"
    assert summary.materialization_started is True
    assert summary.timeout_hit is False
    assert summary.content_fetch_status_counts == {"ready": 2, "too_short": 1}
    assert summary.source_ready_age_s_total == 6.0
    assert summary.source_ready_age_s_max == 4.0
    assert summary.source_ready_age_s_avg == 2.0
    assert summary.youtube_ytdlp_elapsed_s_total == 2.5
    assert summary.youtube_ytdlp_elapsed_s_count == 2
    assert summary.youtube_ytdlp_elapsed_s_avg == 1.25
    assert summary.youtube_page_elapsed_s_total == 1.0
    assert summary.youtube_page_elapsed_s_count == 1
    assert summary.youtube_page_elapsed_s_avg == 1.0
    assert Path(summary.stdout_path).read_text(encoding="utf-8") == "done\n"
    assert Path(summary.stderr_path).read_text(encoding="utf-8") == ""
    assert Path(summary.log_file).exists()
