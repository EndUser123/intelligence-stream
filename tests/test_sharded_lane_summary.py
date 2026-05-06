"""Tests for the sharded lane run summary helper."""

from __future__ import annotations

import json
from pathlib import Path

from csf import sharded_lane_summary


def test_load_sharded_lane_summary_reads_core_metrics(tmp_path):
    run_root = tmp_path / "optimal_search_2lane_4w_v1"
    run_root.mkdir()
    summary_path = run_root / "sharded_lane_series_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "combined": {
                    "hot_path_videos_per_hour": 4213.19,
                    "wall_elapsed_s": 677.586,
                    "hot_path_success_count_total": 793,
                    "fail_count_total": 7,
                    "processed_count_total": 800,
                    "lane_count": 2,
                },
                "runs": [
                    {
                        "aggregate": {
                            "add_elapsed_s_total": 600.0,
                            "cleanup_elapsed_s_total": 90.0,
                            "worker_idle_wait_s_total": 30.0,
                            "source_ready_age_s_total": 6400.0,
                            "processed_count_total": 400,
                        }
                    },
                    {
                        "aggregate": {
                            "add_elapsed_s_total": 576.2,
                            "cleanup_elapsed_s_total": 121.0,
                            "worker_idle_wait_s_total": 127.3,
                            "source_ready_age_s_total": 6400.0,
                            "processed_count_total": 400,
                        }
                    },
                ],
                "post_run_hygiene": {"status": "clean"},
            }
        ),
        encoding="utf-8",
    )

    summary = sharded_lane_summary.load_sharded_lane_summary(run_root)

    assert summary.run_root == run_root
    assert summary.summary_path == summary_path
    assert summary.candidate == "optimal_search_2lane_4w_v1"
    assert summary.status == "ok"
    assert summary.hygiene_status == "clean"
    assert summary.hot_path_videos_per_hour == 4213.19
    assert summary.wall_elapsed_s == 677.586
    assert summary.add_elapsed_s_total == 1176.2
    assert summary.cleanup_elapsed_s_total == 211.0
    assert summary.worker_idle_wait_s_total == 157.3
    assert summary.source_ready_age_s_avg == 16.0
    assert summary.success_count_total == 793
    assert summary.fail_count_total == 7
    assert summary.processed_count_total == 800
    assert summary.lane_count == 2


def test_format_sharded_lane_summary_includes_requested_fields(tmp_path):
    run_root = tmp_path / "sweep_phase3_2lane_3w_run01"
    run_root.mkdir()
    summary_path = run_root / "sharded_lane_series_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "status": "ok",
                "combined": {
                    "hot_path_videos_per_hour": 4123.28,
                    "wall_elapsed_s": 694.107,
                    "hot_path_success_count_total": 795,
                    "fail_count_total": 5,
                    "processed_count_total": 800,
                    "lane_count": 2,
                },
                "runs": [
                    {
                        "aggregate": {
                            "add_elapsed_s_total": 560.0,
                            "cleanup_elapsed_s_total": 110.0,
                            "worker_idle_wait_s_total": 5.0,
                            "source_ready_age_s_total": 4200.0,
                            "processed_count_total": 400,
                        }
                    },
                    {
                        "aggregate": {
                            "add_elapsed_s_total": 576.3,
                            "cleanup_elapsed_s_total": 126.5,
                            "worker_idle_wait_s_total": 6.9,
                            "source_ready_age_s_total": 3320.0,
                            "processed_count_total": 400,
                        }
                    },
                ],
                "post_run_hygiene": {"status": "clean"},
            }
        ),
        encoding="utf-8",
    )

    summary = sharded_lane_summary.load_sharded_lane_summary(run_root)
    line = sharded_lane_summary.format_sharded_lane_summary(summary)

    assert "candidate=sweep_phase3_2lane_3w_run01" in line
    assert "status=ok" in line
    assert "hygiene=clean" in line
    assert "vph=4123.28" in line
    assert "wall_s=694.107" in line
    assert "add_s=1136.300" in line
    assert "cleanup_s=236.500" in line
    assert "idle_wait_s=11.900" in line
    assert "source_ready_age_s_avg=9.400" in line
    assert "success=795" in line
    assert "fail=5" in line
    assert "processed=800" in line
