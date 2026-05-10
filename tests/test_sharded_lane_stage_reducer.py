import json

from csf import sharded_lane_stage_reducer as reducer


def test_load_sweep_summary_prefers_valid_json_before_backslash_repair(tmp_path):
    summary_path = tmp_path / "sweep_summary.json"
    summary_path.write_text(
        json.dumps({"message": "line\nbreak", "path": r"P:\.data\yt-is"}),
        encoding="utf-8",
    )

    loaded = reducer._load_sweep_summary(summary_path)

    assert loaded["message"] == "line\nbreak"
    assert loaded["path"] == r"P:\.data\yt-is"


def test_extract_lane_metrics_uses_lane_aggregate_not_combined(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    (run_root / "soak" / "a_hominidae_pro").mkdir(parents=True)
    (run_root / reducer.SUMMARY_NAME).write_text(
        json.dumps(
            {
                "combined": {
                    "hot_path_videos_per_hour": 4123.28,
                    "wall_elapsed_s": 694.1,
                    "hot_path_success_count_total": 795,
                    "fail_count_total": 5,
                    "processed_count_total": 800,
                },
                "runs": [
                    {
                        "lane": "a_hominidae_pro",
                        "wall_elapsed_s": 694.1,
                        "aggregate": {
                            "hot_path_videos_per_hour": 2061.27,
                            "add_elapsed_s_total": 590.027,
                            "cleanup_elapsed_s_total": 112.3,
                            "worker_idle_wait_s_total": 17.0,
                            "source_ready_age_s_avg": 31.1,
                            "hot_path_success_count_total": 397,
                            "fail_count_total": 3,
                            "processed_count_total": 400,
                        },
                    },
                    {
                        "lane": "troup_hominidae_free",
                        "aggregate": {
                            "hot_path_videos_per_hour": 2299.84,
                            "processed_count_total": 400,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    lane = reducer._extract_lane_metrics(run_root, "a_hominidae_pro")

    assert lane.aggregate_vph == 2061.27
    assert lane.processed_count == 400
    assert lane.success_count == 397
    assert lane.wall_elapsed_s == 694.1
    assert lane.add_elapsed_s_total == 590.027


def test_bottleneck_label_does_not_claim_tail_timing_from_worker_counts_only():
    lane = reducer.LaneMetrics(
        lane_name="a_hominidae_pro",
        aggregate_vph=0.0,
        wall_elapsed_s=0.0,
        add_elapsed_s_total=0.0,
        cleanup_elapsed_s_total=0.0,
        worker_idle_wait_s_total=0.0,
        source_ready_age_s_avg=0.0,
        success_count=0,
        fail_count=0,
        processed_count=0,
        batches=(
            reducer.BatchMetrics(
                timestamp="20260507_000000",
                workers=1,
                elapsed_s=10.0,
                succeeded=1,
                fail_count=0,
                setup_sum=10.0,
                extract_sum=30.0,
                add_sum=5.0,
                cleanup_sum=1.0,
                sr_age_avg=0.0,
                sr_age_max=0.0,
                command_failed=2,
                nlm_below_threshold=0,
                ready=10,
                content_fetch_total=12,
                batch_entries=(
                    reducer.BatchEntry(
                        worker_id="worker-01",
                        batch_count=1,
                        succeeded=1,
                        failed=0,
                    ),
                ),
            ),
        ),
    )

    bottleneck = reducer._compute_bottleneck(lane)

    assert bottleneck.startswith("stage-sum-suggested:extract")
    assert "tail-suggested" not in bottleneck
