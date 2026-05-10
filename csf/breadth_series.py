"""Combined breadth and worker-scaling benchmark orchestration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from csf.load_ladder import build_fallback_benchmark_command

REPO_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_BENCHMARK_SCRIPT = REPO_ROOT / "bin" / "csf-fallback-crossover-benchmark"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".logs" / "breadth_scaling_series"
DEFAULT_TRACE_ROOT = REPO_ROOT / ".logs" / "worker_count_trials"
DEFAULT_COHORT_JSON = DEFAULT_OUTPUT_ROOT / "cohort.json"
DEFAULT_SOURCE_URL = "https://www.youtube.com/channel/UCYTISFALLBACKBMK"
DEFAULT_POLICY = "notebooklm_route_plus_fallback_30s_1w"
DEFAULT_LIMIT = 400
DEFAULT_BATCH_SIZE = 200
DEFAULT_PHASE_A_WORKERS = 2
DEFAULT_PHASE_B_WORKERS = (2, 4, 6, 8, 10)
DEFAULT_REUSABLE_PIPELINE_MODE = "serial"
DEFAULT_COMPARISON = "breadth-scaling"
DEFAULT_PIPELINE_MODES = ("serial", "double_buffered")
DEFAULT_BROAD_COHORT_SHAPE = "trace"
DEFAULT_MID_COHORT_SHAPE = "mixed"
DEFAULT_NARROW_COHORT_SHAPE = "captioned"
DEFAULT_MID_MANIFEST_FAMILIES = None
DEFAULT_NARROW_MANIFEST_FAMILIES = None
DEFAULT_MANIFEST_JSON = REPO_ROOT / "tests" / "fixtures" / "shared_benchmark_manifest.json"


@dataclass(frozen=True, slots=True)
class BreadthTier:
    """A single breadth tier in the combined benchmark series."""

    name: str
    description: str
    cohort_shape: str
    sample_label: str
    manifest_families: str | None = None


def _parse_csv_list(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_worker_counts(value: str) -> tuple[int, ...]:
    counts = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not counts:
        raise ValueError("at least one worker count is required")
    for count in counts:
        if count < 1:
            raise ValueError("worker counts must be >= 1")
    return counts


def _parse_pipeline_modes(value: str) -> tuple[str, ...]:
    modes = tuple(item.strip().lower().replace("-", "_") for item in value.split(",") if item.strip())
    if not modes:
        raise ValueError("at least one pipeline mode is required")
    invalid = [mode for mode in modes if mode not in DEFAULT_PIPELINE_MODES]
    if invalid:
        raise ValueError(f"unknown pipeline mode(s): {', '.join(sorted(set(invalid)))}")
    return modes


def build_breadth_tiers(
    *,
    broad_cohort_shape: str = DEFAULT_BROAD_COHORT_SHAPE,
    mid_cohort_shape: str = DEFAULT_MID_COHORT_SHAPE,
    narrow_cohort_shape: str = DEFAULT_NARROW_COHORT_SHAPE,
    broad_manifest_families: str | None = None,
    mid_manifest_families: str | None = DEFAULT_MID_MANIFEST_FAMILIES,
    narrow_manifest_families: str | None = DEFAULT_NARROW_MANIFEST_FAMILIES,
) -> tuple[BreadthTier, ...]:
    """Build the breadth tiers used by the combined benchmark series."""

    return (
        BreadthTier(
            name="broad",
            description="Broadest cohort shape for the queue-breadth proof.",
            cohort_shape=broad_cohort_shape,
            sample_label="breadth_broad",
            manifest_families=broad_manifest_families,
        ),
        BreadthTier(
            name="mid",
            description="Mid-breadth cohort shape for the queue-breadth proof.",
            cohort_shape=mid_cohort_shape,
            sample_label="breadth_mid",
            manifest_families=mid_manifest_families,
        ),
        BreadthTier(
            name="narrow",
            description="Narrowest cohort shape for the queue-breadth proof.",
            cohort_shape=narrow_cohort_shape,
            sample_label="breadth_narrow",
            manifest_families=narrow_manifest_families,
        ),
    )


def build_breadth_series_plan(
    *,
    phase_a_workers: int,
    phase_b_workers: tuple[int, ...],
    batch_size: int,
    limit: int,
    tiers: tuple[BreadthTier, ...] | None = None,
) -> dict[str, Any]:
    """Build a machine-readable plan for the breadth series."""

    breadth_tiers = tiers or build_breadth_tiers()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase_a": {
            "name": "breadth",
            "workers": phase_a_workers,
            "batch_size": batch_size,
            "limit": limit,
            "tiers": [asdict(tier) for tier in breadth_tiers],
        },
        "phase_b": {
            "name": "scaling",
            "worker_counts": list(phase_b_workers),
            "batch_size": batch_size,
            "limit": limit,
        },
    }


def choose_best_breadth_tier(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the row with the best hot-path throughput."""

    if not rows:
        raise ValueError("at least one row is required")
    return max(
        rows,
        key=lambda row: (
            float(row.get("videos_per_hour") or row.get("hot_path_videos_per_hour") or 0.0),
            -float(row.get("worker_idle_wait_s") or 0.0),
        ),
    )


def _load_summary_rows(summary: dict[str, Any], policy_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch in summary.get("batches", []) or []:
        if not isinstance(batch, dict):
            continue
        policy_result = None
        for result in batch.get("policies", []) or []:
            if isinstance(result, dict) and result.get("policy") == policy_name:
                policy_result = result
                break
        if policy_result is None:
            continue
        results = policy_result.get("results", []) or []
        row = results[0] if results and isinstance(results[0], dict) else {}
        rows.append(row)
    if not rows:
        raise ValueError(f"no benchmark rows found for policy {policy_name}")
    return rows


def _float_value(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _merge_counts(target: Counter[str], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key, count in value.items():
        target[str(key)] += _int_value(count)


def _aggregate_summary(summary: dict[str, Any], policy_name: str) -> dict[str, Any]:
    rows = _load_summary_rows(summary, policy_name)
    content_fetch_status_counts: Counter[str] = Counter()
    totals = {
        "row_count": len(rows),
        "success_count_total": 0,
        "fail_count_total": 0,
        "skip_count_total": 0,
        "processed_count_total": 0,
        "hot_path_success_count_total": 0,
        "transcript_fallback_success_count_total": 0,
        "elapsed_s_total": 0.0,
        "process_elapsed_s_total": 0.0,
        "startup_prepare_total_elapsed_s_total": 0.0,
        "setup_elapsed_s_total": 0.0,
        "add_elapsed_s_total": 0.0,
        "readiness_elapsed_s_total": 0.0,
        "cleanup_elapsed_s_total": 0.0,
        "worker_idle_wait_s_total": 0.0,
        "source_ready_age_s_total": 0.0,
        "source_ready_age_s_max": 0.0,
        "shared_retry_deferred_count_total": 0.0,
        "shared_retry_recovered_count_total": 0.0,
        "shared_retry_final_failed_count_total": 0.0,
        "shared_retry_processed_count_total": 0.0,
        "youtube_ytdlp_elapsed_s_total": 0.0,
        "youtube_ytdlp_elapsed_s_max": 0.0,
        "youtube_ytdlp_elapsed_s_count_total": 0,
        "youtube_page_elapsed_s_total": 0.0,
        "youtube_page_elapsed_s_max": 0.0,
        "youtube_page_elapsed_s_count_total": 0,
    }

    for row in rows:
        fetch_completed = row.get("fetch_completed", {}) if isinstance(row.get("fetch_completed"), dict) else {}
        worker_stage_totals = fetch_completed.get("worker_stage_totals", {}) if isinstance(fetch_completed.get("worker_stage_totals"), dict) else {}
        totals["success_count_total"] += _int_value(row.get("success_count"))
        totals["fail_count_total"] += _int_value(row.get("fail_count"))
        totals["skip_count_total"] += _int_value(row.get("skip_count"))
        totals["processed_count_total"] += _int_value(row.get("processed_count"))
        totals["hot_path_success_count_total"] += _int_value(row.get("hot_path_success_count"))
        totals["transcript_fallback_success_count_total"] += _int_value(row.get("transcript_fallback_success_count"))
        totals["elapsed_s_total"] += _float_value(row.get("elapsed_s"))
        totals["process_elapsed_s_total"] += _float_value(row.get("process_elapsed_s"))
        totals["startup_prepare_total_elapsed_s_total"] += _float_value(
            worker_stage_totals.get("startup_prepare_total_elapsed_s_total") or row.get("startup_prepare_total_elapsed_s")
        )
        totals["setup_elapsed_s_total"] += _float_value(
            worker_stage_totals.get("setup_elapsed_s_total") or row.get("setup_elapsed_s")
        )
        totals["add_elapsed_s_total"] += _float_value(row.get("add_elapsed_s"))
        totals["readiness_elapsed_s_total"] += _float_value(row.get("readiness_elapsed_s"))
        totals["cleanup_elapsed_s_total"] += _float_value(row.get("cleanup_elapsed_s"))
        totals["worker_idle_wait_s_total"] += _float_value(row.get("worker_idle_wait_s"))
        totals["source_ready_age_s_total"] += _float_value(row.get("source_ready_age_s_total"))
        totals["source_ready_age_s_max"] = max(totals["source_ready_age_s_max"], _float_value(row.get("source_ready_age_s_max")))
        totals["shared_retry_deferred_count_total"] += _float_value(row.get("shared_retry_deferred_count"))
        totals["shared_retry_recovered_count_total"] += _float_value(row.get("shared_retry_recovered_count"))
        totals["shared_retry_final_failed_count_total"] += _float_value(row.get("shared_retry_final_failed_count"))
        totals["shared_retry_processed_count_total"] += _float_value(row.get("shared_retry_processed_count"))
        totals["youtube_ytdlp_elapsed_s_total"] += _float_value(row.get("youtube_ytdlp_elapsed_s_total"))
        totals["youtube_ytdlp_elapsed_s_max"] = max(totals["youtube_ytdlp_elapsed_s_max"], _float_value(row.get("youtube_ytdlp_elapsed_s_max")))
        totals["youtube_ytdlp_elapsed_s_count_total"] += _int_value(row.get("youtube_ytdlp_elapsed_s_count"))
        totals["youtube_page_elapsed_s_total"] += _float_value(row.get("youtube_page_elapsed_s_total"))
        totals["youtube_page_elapsed_s_max"] = max(totals["youtube_page_elapsed_s_max"], _float_value(row.get("youtube_page_elapsed_s_max")))
        totals["youtube_page_elapsed_s_count_total"] += _int_value(row.get("youtube_page_elapsed_s_count"))
        _merge_counts(content_fetch_status_counts, row.get("content_fetch_status_counts"))

    elapsed_s_total = totals["elapsed_s_total"]
    hot_path_videos_per_hour = round(totals["hot_path_success_count_total"] / elapsed_s_total * 3600.0, 2) if elapsed_s_total > 0 else 0.0
    transcript_fallback_videos_per_hour = round(totals["transcript_fallback_success_count_total"] / elapsed_s_total * 3600.0, 2) if elapsed_s_total > 0 else 0.0
    processed_per_hour = round(totals["processed_count_total"] / elapsed_s_total * 3600.0, 2) if elapsed_s_total > 0 else 0.0
    total_status_count = sum(content_fetch_status_counts.values())
    source_ready_age_s_avg = round(totals["source_ready_age_s_total"] / max(total_status_count, 1), 3)
    youtube_ytdlp_elapsed_s_avg = round(totals["youtube_ytdlp_elapsed_s_total"] / max(totals["youtube_ytdlp_elapsed_s_count_total"], 1), 3)
    youtube_page_elapsed_s_avg = round(totals["youtube_page_elapsed_s_total"] / max(totals["youtube_page_elapsed_s_count_total"], 1), 3)

    return {
        **totals,
        "content_fetch_status_counts_total": dict(content_fetch_status_counts),
        "videos_per_hour": hot_path_videos_per_hour,
        "hot_path_videos_per_hour": hot_path_videos_per_hour,
        "transcript_fallback_videos_per_hour": transcript_fallback_videos_per_hour,
        "processed_per_hour": processed_per_hour,
        "source_ready_age_s_avg": source_ready_age_s_avg,
        "youtube_ytdlp_elapsed_s_avg": youtube_ytdlp_elapsed_s_avg,
        "youtube_page_elapsed_s_avg": youtube_page_elapsed_s_avg,
    }


def _run_benchmark_tier(
    *,
    tier: BreadthTier,
    trace_root: Path,
    cohort_json: Path,
    output_root: Path,
    source_url: str,
    workers: int,
    limit: int,
    batch_size: int,
    policy: str,
    manifest_json: Path,
    python_executable: str | None,
    reusable_pipeline_mode: str = DEFAULT_REUSABLE_PIPELINE_MODE,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    worker_state_root = output_root / "worker_states"
    cohort_json.parent.mkdir(parents=True, exist_ok=True)

    command = build_fallback_benchmark_command(
        python_executable=python_executable or sys.executable,
        fallback_benchmark_script=FALLBACK_BENCHMARK_SCRIPT,
        trace_root=trace_root,
        cohort_json=cohort_json,
        output_root=output_root,
        source_url=source_url,
        workers=workers,
        limit=limit,
        batch_size=batch_size,
        policy=policy,
        cohort_shape=tier.cohort_shape,
        sample_label=tier.sample_label,
        manifest_json=manifest_json if tier.cohort_shape == "manifest" else None,
        manifest_families=tier.manifest_families,
        worker_state_root=worker_state_root,
        preserve_worker_state_root=False,
    )

    env = os.environ.copy()
    if reusable_pipeline_mode != DEFAULT_REUSABLE_PIPELINE_MODE:
        env["YTIS_REUSABLE_PIPELINE_MODE"] = reusable_pipeline_mode
    else:
        env.pop("YTIS_REUSABLE_PIPELINE_MODE", None)

    proc = subprocess.run(command, cwd=str(REPO_ROOT), env=env, check=False)
    summary_path = output_root / "benchmark_summary.json"
    if proc.returncode != 0:
        stderr_path = output_root / "benchmark.stderr.txt"
        raise RuntimeError(
            f"benchmark run failed for tier={tier.name} workers={workers} returncode={proc.returncode}"
        )
    if not summary_path.exists():
        raise RuntimeError(f"missing benchmark summary: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    aggregate = _aggregate_summary(summary, policy)
    return {
        "tier": asdict(tier),
        "workers": workers,
        "limit": limit,
        "batch_size": batch_size,
        "policy": policy,
        "reusable_pipeline_mode": reusable_pipeline_mode,
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "output_root": str(output_root),
        "command": command,
        "returncode": proc.returncode,
        "benchmark_summary_path": str(summary_path),
        "aggregate": aggregate,
        **aggregate,
    }


def run_breadth_series(
    *,
    trace_root: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    cohort_json: Path = DEFAULT_COHORT_JSON,
    source_url: str = DEFAULT_SOURCE_URL,
    policy: str = DEFAULT_POLICY,
    phase_a_workers: int = DEFAULT_PHASE_A_WORKERS,
    phase_b_workers: Iterable[int] = DEFAULT_PHASE_B_WORKERS,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    manifest_json: Path = DEFAULT_MANIFEST_JSON,
    tiers: tuple[BreadthTier, ...] | None = None,
    python_executable: str | None = None,
    reusable_pipeline_mode: str = DEFAULT_REUSABLE_PIPELINE_MODE,
) -> dict[str, Any]:
    """Run the breadth proof followed by worker scaling on the winning tier."""

    breadth_tiers = tiers or build_breadth_tiers()
    phase_b_worker_counts = tuple(int(count) for count in phase_b_workers)
    phase_a_runs: list[dict[str, Any]] = []

    output_root.mkdir(parents=True, exist_ok=True)
    for tier in breadth_tiers:
        tier_output_root = output_root / "phase_a" / tier.name
        tier_cohort_json = cohort_json.parent / f"{cohort_json.stem}.{tier.name}{cohort_json.suffix}"
        run = _run_benchmark_tier(
            tier=tier,
            trace_root=trace_root,
            cohort_json=tier_cohort_json,
            output_root=tier_output_root,
            source_url=source_url,
            workers=phase_a_workers,
            limit=limit,
            batch_size=batch_size,
            policy=policy,
            manifest_json=manifest_json,
            python_executable=python_executable,
            reusable_pipeline_mode=reusable_pipeline_mode,
        )
        phase_a_runs.append(run)

    winner = choose_best_breadth_tier(phase_a_runs)
    winner_tier_name = str(winner.get("tier", {}).get("name") or winner.get("tier", {}).get("sample_label") or "")
    winner_tier = next(tier for tier in breadth_tiers if tier.name == winner_tier_name)

    phase_b_runs: list[dict[str, Any]] = []
    for workers in phase_b_worker_counts:
        tier_output_root = output_root / "phase_b" / winner_tier.name / f"workers_{workers:02d}"
        tier_cohort_json = cohort_json.parent / f"{cohort_json.stem}.{winner_tier.name}.workers_{workers:02d}{cohort_json.suffix}"
        run = _run_benchmark_tier(
            tier=winner_tier,
            trace_root=trace_root,
            cohort_json=tier_cohort_json,
            output_root=tier_output_root,
            source_url=source_url,
            workers=workers,
            limit=limit,
            batch_size=batch_size,
            policy=policy,
            manifest_json=manifest_json,
            python_executable=python_executable,
            reusable_pipeline_mode=reusable_pipeline_mode,
        )
        phase_b_runs.append(run)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "source_url": source_url,
        "policy": policy,
        "limit": limit,
        "batch_size": batch_size,
        "reusable_pipeline_mode": reusable_pipeline_mode,
        "phase_a_workers": phase_a_workers,
        "phase_b_workers": list(phase_b_worker_counts),
        "manifest_json": str(manifest_json),
        "phase_a": {
            "name": "breadth",
            "tiers": [asdict(tier) for tier in breadth_tiers],
            "runs": phase_a_runs,
            "winner": winner,
        },
        "phase_b": {
            "name": "scaling",
            "tier": asdict(winner_tier),
            "runs": phase_b_runs,
        },
    }
    report_path = output_root / "breadth_series_summary.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_pipeline_mode_comparison(
    *,
    trace_root: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    cohort_json: Path = DEFAULT_COHORT_JSON,
    source_url: str = DEFAULT_SOURCE_URL,
    policy: str = DEFAULT_POLICY,
    workers: int = DEFAULT_PHASE_A_WORKERS,
    phase_b_workers: Iterable[int] = DEFAULT_PHASE_B_WORKERS,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    manifest_json: Path = DEFAULT_MANIFEST_JSON,
    tiers: tuple[BreadthTier, ...] | None = None,
    python_executable: str | None = None,
    modes: Iterable[str] = DEFAULT_PIPELINE_MODES,
) -> dict[str, Any]:
    """Compare reusable pipeline modes on the same breadth/scaling benchmark."""

    breadth_tiers = tiers or build_breadth_tiers()
    pipeline_modes = tuple(mode.strip().lower().replace("-", "_") for mode in modes if str(mode).strip())
    if not pipeline_modes:
        raise ValueError("at least one pipeline mode is required")
    invalid = [mode for mode in pipeline_modes if mode not in DEFAULT_PIPELINE_MODES]
    if invalid:
        raise ValueError(f"unknown pipeline mode(s): {', '.join(sorted(set(invalid)))}")

    output_root.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for mode in pipeline_modes:
        mode_output_root = output_root / mode
        mode_cohort_json = cohort_json.parent / f"{cohort_json.stem}.{mode}{cohort_json.suffix}"
        run = run_breadth_series(
            trace_root=trace_root,
            output_root=mode_output_root,
            cohort_json=mode_cohort_json,
            source_url=source_url,
            policy=policy,
            phase_a_workers=workers,
            phase_b_workers=phase_b_workers,
            limit=limit,
            batch_size=batch_size,
            manifest_json=manifest_json,
            tiers=breadth_tiers,
            python_executable=python_executable,
            reusable_pipeline_mode=mode,
        )
        runs.append(run)

    winner = max(
        runs,
        key=lambda run: float(run.get("phase_a", {}).get("winner", {}).get("videos_per_hour")
                             or run.get("phase_a", {}).get("winner", {}).get("hot_path_videos_per_hour")
                             or 0.0),
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metric_contract": "hot_path_videos_per_hour_excludes_whisper",
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "source_url": source_url,
        "policy": policy,
        "workers": workers,
        "phase_b_workers": list(int(worker) for worker in phase_b_workers),
        "limit": limit,
        "batch_size": batch_size,
        "manifest_json": str(manifest_json),
        "modes": list(pipeline_modes),
        "runs": runs,
        "winner": winner,
    }
    report_path = output_root / "pipeline_mode_comparison_summary.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the breadth and scaling benchmark series")
    parser.add_argument(
        "--comparison",
        choices=(DEFAULT_COMPARISON, "pipeline-mode"),
        default=DEFAULT_COMPARISON,
        help="Comparison to run (default: breadth-scaling)",
    )
    parser.add_argument(
        "--trace-root",
        type=Path,
        default=DEFAULT_TRACE_ROOT,
        help=f"Trace root to freeze benchmark cohorts from (default: {DEFAULT_TRACE_ROOT})",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Root directory for benchmark artifacts (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--cohort-json",
        type=Path,
        default=DEFAULT_COHORT_JSON,
        help=f"Base path for frozen cohort manifests (default: {DEFAULT_COHORT_JSON})",
    )
    parser.add_argument(
        "--source-url",
        default=DEFAULT_SOURCE_URL,
        help=f"Synthetic source URL used to seed the frozen cohort (default: {DEFAULT_SOURCE_URL})",
    )
    parser.add_argument(
        "--policy",
        default=DEFAULT_POLICY,
        help=f"Fallback benchmark policy to run for each tier (default: {DEFAULT_POLICY})",
    )
    parser.add_argument(
        "--phase-a-workers",
        type=int,
        default=DEFAULT_PHASE_A_WORKERS,
        help=f"Worker count for the breadth proof phase (default: {DEFAULT_PHASE_A_WORKERS})",
    )
    parser.add_argument(
        "--phase-b-workers",
        default="2,4,6,8,10",
        help="Comma-separated worker counts for the scaling phase",
    )
    parser.add_argument(
        "--pipeline-modes",
        default="serial,double_buffered",
        help="Comma-separated reusable pipeline modes for the pipeline-mode comparison",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max items to include from the frozen cohort (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Batch size for each benchmark slice (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--manifest-json",
        type=Path,
        default=DEFAULT_MANIFEST_JSON,
        help=f"Path to the shared benchmark manifest (default: {DEFAULT_MANIFEST_JSON})",
    )
    parser.add_argument(
        "--cohort-shape-broad",
        choices=("trace", "captioned", "mixed", "manifest"),
        default=DEFAULT_BROAD_COHORT_SHAPE,
        help="Cohort shape used for the broad breadth tier",
    )
    parser.add_argument(
        "--cohort-shape-mid",
        choices=("trace", "captioned", "mixed", "manifest"),
        default=DEFAULT_MID_COHORT_SHAPE,
        help="Cohort shape used for the mid breadth tier",
    )
    parser.add_argument(
        "--cohort-shape-narrow",
        choices=("trace", "captioned", "mixed", "manifest"),
        default=DEFAULT_NARROW_COHORT_SHAPE,
        help="Cohort shape used for the narrow breadth tier",
    )
    parser.add_argument(
        "--broad-manifest-families",
        default=None,
        help="Comma-separated manifest families for the broad tier when it uses manifest shape",
    )
    parser.add_argument(
        "--mid-manifest-families",
        default=DEFAULT_MID_MANIFEST_FAMILIES,
        help="Comma-separated manifest families for the mid tier when it uses manifest shape",
    )
    parser.add_argument(
        "--narrow-manifest-families",
        default=DEFAULT_NARROW_MANIFEST_FAMILIES,
        help="Comma-separated manifest families for the narrow tier when it uses manifest shape",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python executable to use for csf-source (default: current interpreter)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    phase_b_workers = _parse_worker_counts(args.phase_b_workers)
    tiers = build_breadth_tiers(
        broad_cohort_shape=args.cohort_shape_broad,
        mid_cohort_shape=args.cohort_shape_mid,
        narrow_cohort_shape=args.cohort_shape_narrow,
        broad_manifest_families=args.broad_manifest_families,
        mid_manifest_families=args.mid_manifest_families,
        narrow_manifest_families=args.narrow_manifest_families,
    )
    if args.comparison == "pipeline-mode":
        report = run_pipeline_mode_comparison(
            trace_root=args.trace_root,
            output_root=args.output_root,
            cohort_json=args.cohort_json,
            source_url=args.source_url,
            policy=args.policy,
            workers=args.phase_a_workers,
            phase_b_workers=phase_b_workers,
            limit=args.limit,
            batch_size=args.batch_size,
            manifest_json=args.manifest_json,
            tiers=tiers,
            python_executable=args.python,
            modes=_parse_pipeline_modes(args.pipeline_modes),
        )
        print(f"[pipeline-mode] Wrote summary: {report['report_path']}")
        print(
            f"[pipeline-mode] Winner: {report['winner']['reusable_pipeline_mode']} "
            f"hot_v/hr={float(report['winner']['phase_a']['winner']['videos_per_hour']):.1f}"
        )
    else:
        report = run_breadth_series(
            trace_root=args.trace_root,
            output_root=args.output_root,
            cohort_json=args.cohort_json,
            source_url=args.source_url,
            policy=args.policy,
            phase_a_workers=args.phase_a_workers,
            phase_b_workers=phase_b_workers,
            limit=args.limit,
            batch_size=args.batch_size,
            manifest_json=args.manifest_json,
            tiers=tiers,
            python_executable=args.python,
        )
        print(f"[breadth] Wrote summary: {report['report_path']}")
        print(
            f"[breadth] Winner: {report['phase_a']['winner']['tier']['name']} "
            f"hot_v/hr={float(report['phase_a']['winner']['videos_per_hour']):.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
