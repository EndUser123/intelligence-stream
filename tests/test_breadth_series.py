"""Tests for the breadth and scaling benchmark series."""

from __future__ import annotations

from pathlib import Path

from csf.breadth_series import (
    BreadthTier,
    build_breadth_series_plan,
    build_breadth_tiers,
    choose_best_breadth_tier,
    run_breadth_series,
)


def test_choose_best_breadth_tier_prefers_highest_hot_path_vph():
    rows = [
        {"tier": "broad", "videos_per_hour": 2466.87, "worker_idle_wait_s": 120.0},
        {"tier": "mid", "videos_per_hour": 1800.0, "worker_idle_wait_s": 80.0},
        {"tier": "narrow", "videos_per_hour": 327.7, "worker_idle_wait_s": 2239.2},
    ]

    winner = choose_best_breadth_tier(rows)

    assert winner["tier"] == "broad"


def test_build_breadth_series_plan_includes_phase_names_and_workers():
    tiers = build_breadth_tiers()
    plan = build_breadth_series_plan(
        phase_a_workers=2,
        phase_b_workers=(2, 4, 6, 8, 10),
        batch_size=200,
        limit=400,
        tiers=tiers,
    )

    assert plan["phase_a"]["name"] == "breadth"
    assert plan["phase_b"]["name"] == "scaling"
    assert plan["phase_a"]["workers"] == 2
    assert plan["phase_b"]["worker_counts"] == [2, 4, 6, 8, 10]
    assert [tier["name"] for tier in plan["phase_a"]["tiers"]] == ["broad", "mid", "narrow"]
    assert [tier["cohort_shape"] for tier in plan["phase_a"]["tiers"]] == ["trace", "mixed", "captioned"]


def test_run_breadth_series_runs_breadth_then_scaling(tmp_path, monkeypatch):
    import csf.breadth_series as mod

    tiers = (
        BreadthTier("broad", "Broad cohort", "mixed", "breadth_broad"),
        BreadthTier("mid", "Mid cohort", "manifest", "breadth_mid", "routing,whisper_admission"),
        BreadthTier("narrow", "Narrow cohort", "manifest", "breadth_narrow", "hot_path_control"),
    )
    calls: list[dict[str, object]] = []
    vph_by_tier = {"broad": 100.0, "mid": 240.0, "narrow": 140.0}

    def fake_run_benchmark_tier(
        *,
        tier,
        trace_root,
        cohort_json,
        output_root,
        source_url,
        workers,
        limit,
        batch_size,
        policy,
        manifest_json,
        python_executable,
        reusable_pipeline_mode,
    ):
        calls.append(
            {
                "tier": tier.name,
                "workers": workers,
                "cohort_json": Path(cohort_json).name,
                "output_root": Path(output_root).name,
                "policy": policy,
                "manifest_json": Path(manifest_json).name,
                "reusable_pipeline_mode": reusable_pipeline_mode,
            }
        )
        hot_vph = vph_by_tier[tier.name]
        summary = {
            "batches": [
                {
                    "policies": [
                        {
                            "policy": policy,
                            "results": [
                                {
                                    "workers": workers,
                                    "success_count": 10,
                                    "fail_count": 0,
                                    "skip_count": 0,
                                    "processed_count": 10,
                                    "hot_path_success_count": 10,
                                    "transcript_fallback_success_count": 0,
                                    "elapsed_s": 3600.0 * 10 / hot_vph,
                                    "process_elapsed_s": 3600.0 * 10 / hot_vph,
                                    "add_elapsed_s": 10.0,
                                    "readiness_elapsed_s": 5.0,
                                    "cleanup_elapsed_s": 2.0,
                                    "worker_idle_wait_s": 1.0,
                                    "source_ready_age_s_total": 20.0,
                                    "source_ready_age_s_max": 4.0,
                                    "shared_retry_deferred_count": 0.0,
                                    "shared_retry_recovered_count": 0.0,
                                    "shared_retry_final_failed_count": 0.0,
                                    "shared_retry_processed_count": 0.0,
                                    "youtube_ytdlp_elapsed_s_total": 0.0,
                                    "youtube_ytdlp_elapsed_s_max": 0.0,
                                    "youtube_ytdlp_elapsed_s_count": 0,
                                    "youtube_page_elapsed_s_total": 0.0,
                                    "youtube_page_elapsed_s_max": 0.0,
                                    "youtube_page_elapsed_s_count": 0,
                                    "content_fetch_status_counts": {"ready": 10},
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        return {
            "tier": {
                "name": tier.name,
                "description": tier.description,
                "cohort_shape": tier.cohort_shape,
                "sample_label": tier.sample_label,
                "manifest_families": tier.manifest_families,
            },
            "workers": workers,
            "limit": limit,
            "batch_size": batch_size,
            "policy": policy,
            "reusable_pipeline_mode": reusable_pipeline_mode,
            "trace_root": str(trace_root),
            "cohort_json": str(cohort_json),
            "output_root": str(output_root),
            "command": ["python", tier.name],
            "returncode": 0,
            "benchmark_summary_path": str(Path(output_root) / "benchmark_summary.json"),
            "aggregate": {
                "videos_per_hour": hot_vph,
                "hot_path_videos_per_hour": hot_vph,
                "worker_idle_wait_s_total": 1.0,
                "elapsed_s_total": 3600.0 * 10 / hot_vph,
                "hot_path_success_count_total": 10,
                "transcript_fallback_success_count_total": 0,
            },
            "videos_per_hour": hot_vph,
            "hot_path_videos_per_hour": hot_vph,
            "worker_idle_wait_s_total": 1.0,
            "elapsed_s_total": 3600.0 * 10 / hot_vph,
            "hot_path_success_count_total": 10,
            "transcript_fallback_success_count_total": 0,
            "summary": summary,
        }

    monkeypatch.setattr(mod, "build_breadth_tiers", lambda: tiers)
    monkeypatch.setattr(mod, "_run_benchmark_tier", fake_run_benchmark_tier)

    report = run_breadth_series(
        trace_root=tmp_path / "trace-root",
        output_root=tmp_path / "output",
        cohort_json=tmp_path / "output" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        phase_a_workers=2,
        phase_b_workers=(2, 4, 6, 8, 10),
        limit=400,
        batch_size=200,
        manifest_json=Path("P:\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        tiers=tiers,
    )

    assert [call["tier"] for call in calls] == [
        "broad",
        "mid",
        "narrow",
        "mid",
        "mid",
        "mid",
        "mid",
        "mid",
    ]
    assert report["phase_a"]["winner"]["tier"]["name"] == "mid"
    assert report["phase_b"]["tier"]["name"] == "mid"
    assert report["phase_b"]["runs"][0]["workers"] == 2
    assert report["phase_b"]["runs"][-1]["workers"] == 10
    assert report["reusable_pipeline_mode"] == "serial"
    assert Path(report["report_path"]).name == "breadth_series_summary.json"


def test_run_breadth_series_propagates_reusable_pipeline_mode(tmp_path, monkeypatch):
    import csf.breadth_series as mod

    tiers = (
        BreadthTier("narrow", "Narrow cohort", "captioned", "breadth_narrow"),
    )
    calls: list[dict[str, object]] = []

    def fake_run_benchmark_tier(**kwargs):
        calls.append(kwargs)
        return {
            "tier": {
                "name": kwargs["tier"].name,
                "description": kwargs["tier"].description,
                "cohort_shape": kwargs["tier"].cohort_shape,
                "sample_label": kwargs["tier"].sample_label,
                "manifest_families": kwargs["tier"].manifest_families,
            },
            "workers": kwargs["workers"],
            "limit": kwargs["limit"],
            "batch_size": kwargs["batch_size"],
            "policy": kwargs["policy"],
            "trace_root": str(kwargs["trace_root"]),
            "cohort_json": str(kwargs["cohort_json"]),
            "output_root": str(kwargs["output_root"]),
            "command": ["python", kwargs["tier"].name],
            "returncode": 0,
            "benchmark_summary_path": str(Path(kwargs["output_root"]) / "benchmark_summary.json"),
            "aggregate": {
                "videos_per_hour": 123.0,
                "hot_path_videos_per_hour": 123.0,
                "worker_idle_wait_s_total": 0.0,
                "elapsed_s_total": 1.0,
                "hot_path_success_count_total": 1,
                "transcript_fallback_success_count_total": 0,
            },
            "videos_per_hour": 123.0,
            "hot_path_videos_per_hour": 123.0,
            "worker_idle_wait_s_total": 0.0,
            "elapsed_s_total": 1.0,
            "hot_path_success_count_total": 1,
            "transcript_fallback_success_count_total": 0,
        }

    monkeypatch.setattr(mod, "build_breadth_tiers", lambda: tiers)
    monkeypatch.setattr(mod, "_run_benchmark_tier", fake_run_benchmark_tier)

    report = run_breadth_series(
        trace_root=tmp_path / "trace-root",
        output_root=tmp_path / "output",
        cohort_json=tmp_path / "output" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        phase_a_workers=2,
        phase_b_workers=(2,),
        limit=400,
        batch_size=200,
        manifest_json=Path("P:\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        tiers=tiers,
        reusable_pipeline_mode="double_buffered",
    )

    assert calls[0]["reusable_pipeline_mode"] == "double_buffered"
    assert report["reusable_pipeline_mode"] == "double_buffered"


def test_run_pipeline_mode_comparison_runs_serial_then_double_buffered(tmp_path, monkeypatch):
    import csf.breadth_series as mod

    calls: list[str] = []

    def fake_run_breadth_series(**kwargs):
        mode = str(kwargs["reusable_pipeline_mode"])
        calls.append(mode)
        hot_vph = 3900.0 if mode == "serial" else 4100.0
        return {
            "generated_at": "2026-04-28T00:00:00Z",
            "trace_root": str(kwargs["trace_root"]),
            "output_root": str(kwargs["output_root"]),
            "reusable_pipeline_mode": mode,
            "phase_a": {
                "winner": {
                    "tier": {"name": "narrow"},
                    "videos_per_hour": hot_vph,
                    "hot_path_videos_per_hour": hot_vph,
                    "hot_path_success_count_total": 200,
                    "transcript_fallback_success_count_total": 0,
                }
            },
            "phase_b": {"runs": []},
        }

    monkeypatch.setattr(mod, "run_breadth_series", fake_run_breadth_series)

    report = mod.run_pipeline_mode_comparison(
        trace_root=tmp_path / "trace-root",
        output_root=tmp_path / "comparison",
        workers=4,
        batch_size=200,
        limit=400,
        phase_b_workers=(4,),
        tiers=(
            BreadthTier("narrow", "Narrow cohort", "captioned", "breadth_narrow"),
        ),
    )

    assert calls == ["serial", "double_buffered"]
    assert report["winner"]["reusable_pipeline_mode"] == "double_buffered"
    assert Path(report["report_path"]).exists()


def test_main_dispatches_pipeline_mode_comparison(tmp_path, monkeypatch):
    import csf.breadth_series as mod

    captured: dict[str, object] = {}

    def fake_run_pipeline_mode_comparison(**kwargs):
        captured.update(kwargs)
        return {
            "report_path": str(tmp_path / "comparison" / "pipeline_mode_comparison_summary.json"),
            "winner": {
                "reusable_pipeline_mode": "double_buffered",
                "phase_a": {"winner": {"videos_per_hour": 4100.0}},
            },
        }

    monkeypatch.setattr(mod, "run_pipeline_mode_comparison", fake_run_pipeline_mode_comparison)

    exit_code = mod.main(
        [
            "--comparison",
            "pipeline-mode",
            "--trace-root",
            str(tmp_path / "trace-root"),
            "--output-root",
            str(tmp_path / "comparison"),
            "--phase-a-workers",
            "4",
            "--batch-size",
            "200",
            "--limit",
            "400",
        ]
    )

    assert exit_code == 0
    assert captured["workers"] == 4
    assert captured["batch_size"] == 200
    assert captured["limit"] == 400
    assert captured["modes"] == ("serial", "double_buffered")
