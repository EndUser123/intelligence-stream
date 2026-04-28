"""Tests for batch-size sensitivity sweep."""

from __future__ import annotations

from pathlib import Path

from csf.batch_size_series import run_batch_size_series
from csf.breadth_series import BreadthTier


def test_run_batch_size_series_chooses_best_batch_size(tmp_path, monkeypatch):
    import csf.batch_size_series as mod

    tier = BreadthTier("narrow", "Narrow cohort", "captioned", "breadth_narrow")
    calls: list[dict[str, object]] = []
    vph_by_batch_size = {100: 300.0, 200: 500.0, 400: 450.0}

    def fake_run_benchmark(
        *,
        batch_size,
        workers,
        limit,
        tier,
        trace_root,
        cohort_json,
        output_root,
        source_url,
        policy,
        manifest_json,
        python_executable,
    ):
        calls.append(
            {
                "batch_size": batch_size,
                "workers": workers,
                "output_root": Path(output_root).name,
                "cohort_json": Path(cohort_json).name,
            }
        )
        hot_vph = vph_by_batch_size[batch_size]
        return {
            "batch_size": batch_size,
            "workers": workers,
            "limit": limit,
            "policy": policy,
            "tier": {
                "name": tier.name,
                "description": tier.description,
                "cohort_shape": tier.cohort_shape,
                "sample_label": tier.sample_label,
                "manifest_families": tier.manifest_families,
            },
            "trace_root": str(trace_root),
            "cohort_json": str(cohort_json),
            "output_root": str(output_root),
            "command": ["python", str(batch_size)],
            "returncode": 0,
            "benchmark_summary_path": str(Path(output_root) / "benchmark_summary.json"),
            "aggregate": {"videos_per_hour": hot_vph, "hot_path_videos_per_hour": hot_vph},
            "videos_per_hour": hot_vph,
            "hot_path_videos_per_hour": hot_vph,
            "report": {},
        }

    monkeypatch.setattr(mod, "_run_benchmark", fake_run_benchmark)

    report = run_batch_size_series(
        trace_root=tmp_path / "trace-root",
        output_root=tmp_path / "output",
        cohort_json=tmp_path / "output" / "cohort.json",
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        policy="notebooklm_route_plus_fallback_30s_1w",
        workers=4,
        limit=400,
        batch_sizes=(100, 200, 400),
        tier=tier,
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
    )

    assert [call["batch_size"] for call in calls] == [100, 200, 400]
    assert report["winner"]["batch_size"] == 200
    assert report["winner"]["videos_per_hour"] == 500.0
    assert Path(report["report_path"]).name == "batch_size_series_summary.json"
