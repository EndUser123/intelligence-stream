"""Tests for concurrent NotebookLM lane sharding benchmarks."""

from __future__ import annotations

import json
from pathlib import Path

from csf.sharded_lane_series import (
    LaneConfig,
    compute_combined_hot_path_vph,
    load_lane_configs,
    run_sharded_lane_series,
)


def test_load_lane_configs_requires_distinct_profile_and_state_namespaces(tmp_path):
    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "pro",
                    "account_class": "pro",
                    "workers": 4,
                    "notebooklm_profile_prefix": "ytis-worker",
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-pro",
                    "worker_state_root": "P:/.logs/shards/pro/worker_states",
                    "notebook_prefix": "benchmark-shard-pro",
                },
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 4,
                    "notebooklm_profile_prefix": "ytis-worker",
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
                    "worker_state_root": "P:/.logs/shards/free/worker_states",
                    "notebook_prefix": "benchmark-shard-free",
                },
            ]
        ),
        encoding="utf-8",
    )

    try:
        load_lane_configs(config_path)
    except ValueError as exc:
        assert "notebooklm_profile_namespace" in str(exc)
    else:
        raise AssertionError("duplicate profile prefixes must be rejected")


def test_load_lane_configs_accepts_same_browser_root_with_distinct_directories(tmp_path):
    config_path = tmp_path / "lanes.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "lane": "pro",
                    "account_class": "pro",
                    "workers": 1,
                    "notebooklm_profiles": ["alt"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm",
                    "browser_profile_directory": "Profile 2",
                    "worker_state_root": "P:/.logs/shards/pro/worker_states",
                    "notebook_prefix": "benchmark-shard-pro",
                },
                {
                    "lane": "free",
                    "account_class": "free",
                    "workers": 1,
                    "notebooklm_profiles": ["default"],
                    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm",
                    "browser_profile_directory": "Default",
                    "worker_state_root": "P:/.logs/shards/free/worker_states",
                    "notebook_prefix": "benchmark-shard-free",
                },
            ]
        ),
        encoding="utf-8",
    )

    lanes = load_lane_configs(config_path)

    assert lanes[0].coordinator_profile == "alt"
    assert lanes[1].coordinator_profile == "default"


def test_pro_free_lane_config_uses_dedicated_browser_roots():
    config_path = Path("P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json")
    lanes = load_lane_configs(config_path)

    assert len(lanes) == 2
    assert lanes[0].lane == "a_hominidae_pro"
    assert lanes[1].lane == "troup_hominidae_free"
    assert str(lanes[0].browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-pro")
    assert str(lanes[1].browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-free")
    assert lanes[0].browser_profile_root != lanes[1].browser_profile_root


def test_free_only_lane_config_uses_free_account_route():
    config_path = Path("P:/packages/yt-is/.logs/sharded_lane_series/free_only_lanes.json")
    (lane,) = load_lane_configs(config_path)

    assert lane.lane == "troup_hominidae_free"
    assert lane.account_class == "free"
    assert lane.notebooklm_profiles == (
        "ytis-free-worker-01",
        "ytis-free-worker-02",
        "ytis-free-worker-03",
        "ytis-free-worker-04",
    )
    assert str(lane.browser_profile_root).replace("\\", "/").endswith("browser/notebooklm-free")
    assert lane.browser_profile_directory == "Default"


def test_compute_combined_hot_path_vph_uses_wall_clock_not_sum_of_lane_elapsed():
    lanes = [
        {"lane": "pro", "hot_path_success_count_total": 398, "started_at": 100.0, "finished_at": 470.0},
        {"lane": "free", "hot_path_success_count_total": 196, "started_at": 110.0, "finished_at": 520.0},
    ]

    combined = compute_combined_hot_path_vph(lanes)

    assert combined["hot_path_success_count_total"] == 594
    assert combined["wall_elapsed_s"] == 420.0
    assert combined["hot_path_videos_per_hour"] == 5091.43


def test_run_sharded_lane_series_passes_isolated_lane_env(tmp_path, monkeypatch):
    import csf.sharded_lane_series as mod

    calls: list[dict[str, object]] = []

    def fake_run_lane(*, lane, trace_root, output_root, cohort_json, source_url, policy, limit, batch_size, manifest_json, python_executable, reusable_pipeline_mode, env):
        calls.append(
            {
                "lane": lane.lane,
                "output_root": output_root,
                "cohort_json": cohort_json,
                "env": {
                    key: env.get(key)
                    for key in (
                        "YTIS_INDUSTRIAL_WORKER_STATE_ROOT",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX",
                        "YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES",
                        "YTIS_NLM_BROWSER_PROFILE_ROOT",
                        "YTIS_NLM_BROWSER_PROFILE_DIRECTORY",
                        "YTIS_REUSABLE_PIPELINE_MODE",
                    )
                },
            }
        )
        success = 398 if lane.lane == "pro" else 196
        started = 100.0 if lane.lane == "pro" else 110.0
        finished = 470.0 if lane.lane == "pro" else 520.0
        return {
            "lane": lane.lane,
            "account_class": lane.account_class,
            "started_at": started,
            "finished_at": finished,
            "wall_elapsed_s": round(finished - started, 3),
            "hot_path_success_count_total": success,
            "fail_count_total": 2,
            "transcript_fallback_success_count_total": 0,
            "hot_path_videos_per_hour": round(success / (finished - started) * 3600.0, 2),
        }

    monkeypatch.setattr(mod, "_run_lane", fake_run_lane)

    report = run_sharded_lane_series(
        lanes=(
            LaneConfig(
                lane="pro",
                account_class="pro",
                workers=4,
                notebooklm_profile_prefix="ytis-pro-worker",
                notebooklm_profiles=("alt", "ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04"),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-pro"),
                browser_profile_directory="Profile 2",
                worker_state_root=tmp_path / "pro" / "worker_states",
                notebook_prefix="benchmark-shard-pro",
            ),
            LaneConfig(
                lane="free",
                account_class="free",
                workers=4,
                notebooklm_profile_prefix="ytis-free-worker",
                notebooklm_profiles=("default", "ytis-free-worker-02", "ytis-free-worker-03", "ytis-free-worker-04"),
                browser_profile_root=Path("P:/.data/yt-is/browser/notebooklm-free"),
                browser_profile_directory="Profile 1",
                worker_state_root=tmp_path / "free" / "worker_states",
                notebook_prefix="benchmark-shard-free",
            ),
        ),
        trace_root=tmp_path / "trace",
        output_root=tmp_path / "out",
        cohort_json=tmp_path / "out" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        limit=400,
        batch_size=200,
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        reusable_pipeline_mode="serial",
    )

    assert [call["lane"] for call in calls] == ["pro", "free"]
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_STATE_ROOT"].endswith("pro\\worker_states")
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX"] == "benchmark-shard-pro"
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX"] == "ytis-pro-worker"
    assert calls[0]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES"] == "alt,ytis-pro-worker-02,ytis-pro-worker-03,ytis-pro-worker-04"
    assert calls[0]["env"]["YTIS_NLM_BROWSER_PROFILE_ROOT"] == "P:\\.data\\yt-is\\browser\\notebooklm-pro"
    assert calls[0]["env"]["YTIS_NLM_BROWSER_PROFILE_DIRECTORY"] == "Profile 2"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_STATE_ROOT"].endswith("free\\worker_states")
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX"] == "benchmark-shard-free"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX"] == "ytis-free-worker"
    assert calls[1]["env"]["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES"] == "default,ytis-free-worker-02,ytis-free-worker-03,ytis-free-worker-04"
    assert calls[1]["env"]["YTIS_NLM_BROWSER_PROFILE_ROOT"] == "P:\\.data\\yt-is\\browser\\notebooklm-free"
    assert calls[1]["env"]["YTIS_NLM_BROWSER_PROFILE_DIRECTORY"] == "Profile 1"
    assert report["combined"]["hot_path_success_count_total"] == 594
    assert report["combined"]["hot_path_videos_per_hour"] == 5091.43
    assert Path(report["report_path"]).exists()
