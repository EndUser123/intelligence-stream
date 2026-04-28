"""Benchmark harness for sweeping NotebookLM benchmark batch sizes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from csf.breadth_series import BreadthTier, build_breadth_tiers, choose_best_breadth_tier
from csf.load_ladder import build_fallback_benchmark_command

REPO_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_BENCHMARK_SCRIPT = REPO_ROOT / "bin" / "csf-fallback-crossover-benchmark"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".logs" / "batch_size_series"
DEFAULT_TRACE_ROOT = REPO_ROOT / ".logs" / "worker_count_trials"
DEFAULT_COHORT_JSON = DEFAULT_OUTPUT_ROOT / "cohort.json"
DEFAULT_SOURCE_URL = "https://www.youtube.com/channel/UCYTISFALLBACKBMK"
DEFAULT_POLICY = "notebooklm_route_plus_fallback_30s_1w"
DEFAULT_LIMIT = 400
DEFAULT_BATCH_SIZES = (100, 200, 400)
DEFAULT_WORKERS = 4
DEFAULT_TIER_NAME = "narrow"
DEFAULT_MANIFEST_JSON = REPO_ROOT / "tests" / "fixtures" / "shared_benchmark_manifest.json"


@dataclass(frozen=True, slots=True)
class BatchSizeRun:
    """A single benchmark run at one batch size."""

    batch_size: int
    workers: int
    tier: dict[str, Any]
    benchmark_summary_path: str
    aggregate: dict[str, Any]


def _parse_ints(value: str) -> tuple[int, ...]:
    values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if not values:
        raise ValueError("at least one integer is required")
    if any(item < 1 for item in values):
        raise ValueError("all integers must be >= 1")
    return values


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


def _aggregate_summary(summary: dict[str, Any], policy_name: str) -> dict[str, Any]:
    rows = _load_summary_rows(summary, policy_name)
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
        totals["success_count_total"] += _int_value(row.get("success_count"))
        totals["fail_count_total"] += _int_value(row.get("fail_count"))
        totals["skip_count_total"] += _int_value(row.get("skip_count"))
        totals["processed_count_total"] += _int_value(row.get("processed_count"))
        totals["hot_path_success_count_total"] += _int_value(row.get("hot_path_success_count"))
        totals["transcript_fallback_success_count_total"] += _int_value(row.get("transcript_fallback_success_count"))
        totals["elapsed_s_total"] += _float_value(row.get("elapsed_s"))
        totals["process_elapsed_s_total"] += _float_value(row.get("process_elapsed_s"))
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

    elapsed = max(totals["elapsed_s_total"], 0.0)
    hot_vph = round(totals["hot_path_success_count_total"] / elapsed * 3600.0, 2) if elapsed > 0 else 0.0
    transcript_vph = round(totals["transcript_fallback_success_count_total"] / elapsed * 3600.0, 2) if elapsed > 0 else 0.0
    processed_per_hour = round(totals["processed_count_total"] / elapsed * 3600.0, 2) if elapsed > 0 else 0.0

    return {
        **totals,
        "videos_per_hour": hot_vph,
        "hot_path_videos_per_hour": hot_vph,
        "transcript_fallback_videos_per_hour": transcript_vph,
        "processed_per_hour": processed_per_hour,
        "source_ready_age_s_avg": round(
            totals["source_ready_age_s_total"] / max(sum(_int_value(row.get("processed_count")) for row in rows), 1),
            3,
        ),
        "youtube_ytdlp_elapsed_s_avg": round(
            totals["youtube_ytdlp_elapsed_s_total"] / max(totals["youtube_ytdlp_elapsed_s_count_total"], 1),
            3,
        ),
        "youtube_page_elapsed_s_avg": round(
            totals["youtube_page_elapsed_s_total"] / max(totals["youtube_page_elapsed_s_count_total"], 1),
            3,
        ),
    }


def _run_benchmark(
    *,
    batch_size: int,
    workers: int,
    limit: int,
    tier: BreadthTier,
    trace_root: Path,
    cohort_json: Path,
    output_root: Path,
    source_url: str,
    policy: str,
    manifest_json: Path,
    python_executable: str | None,
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
    proc = subprocess.run(command, cwd=str(REPO_ROOT), env=os.environ.copy(), check=False)
    summary_path = output_root / "benchmark_summary.json"
    if proc.returncode != 0:
        raise RuntimeError(f"benchmark run failed for batch_size={batch_size} workers={workers} returncode={proc.returncode}")
    if not summary_path.exists():
        raise RuntimeError(f"missing benchmark summary: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    aggregate = _aggregate_summary(summary, policy)
    return {
        "batch_size": batch_size,
        "workers": workers,
        "limit": limit,
        "policy": policy,
        "tier": asdict(tier),
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "output_root": str(output_root),
        "command": command,
        "returncode": proc.returncode,
        "benchmark_summary_path": str(summary_path),
        "aggregate": aggregate,
        **aggregate,
    }


def run_batch_size_series(
    *,
    trace_root: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    cohort_json: Path = DEFAULT_COHORT_JSON,
    source_url: str = DEFAULT_SOURCE_URL,
    policy: str = DEFAULT_POLICY,
    workers: int = DEFAULT_WORKERS,
    limit: int = DEFAULT_LIMIT,
    batch_sizes: Iterable[int] = DEFAULT_BATCH_SIZES,
    tier: BreadthTier | None = None,
    manifest_json: Path = DEFAULT_MANIFEST_JSON,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """Run a batch-size sensitivity sweep on the winning breadth tier."""

    chosen_tier = tier or next(t for t in build_breadth_tiers() if t.name == DEFAULT_TIER_NAME)
    batch_size_values = tuple(int(size) for size in batch_sizes)
    runs: list[dict[str, Any]] = []

    output_root.mkdir(parents=True, exist_ok=True)
    for batch_size in batch_size_values:
        run = _run_benchmark(
            batch_size=batch_size,
            workers=workers,
            limit=limit,
            tier=chosen_tier,
            trace_root=trace_root,
            cohort_json=cohort_json.parent / f"{cohort_json.stem}.batch_{batch_size}{cohort_json.suffix}",
            output_root=output_root / f"batch_{batch_size}",
            source_url=source_url,
            policy=policy,
            manifest_json=manifest_json,
            python_executable=python_executable,
        )
        runs.append(run)

    winner = choose_best_breadth_tier(runs)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "source_url": source_url,
        "policy": policy,
        "workers": workers,
        "limit": limit,
        "batch_sizes": list(batch_size_values),
        "manifest_json": str(manifest_json),
        "tier": asdict(chosen_tier),
        "runs": runs,
        "winner": winner,
    }
    report_path = output_root / "batch_size_series_summary.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a NotebookLM benchmark batch-size sensitivity sweep")
    parser.add_argument("--trace-root", type=Path, default=DEFAULT_TRACE_ROOT)
    parser.add_argument("--cohort-json", type=Path, default=DEFAULT_COHORT_JSON)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--batch-sizes", default=",".join(str(size) for size in DEFAULT_BATCH_SIZES))
    parser.add_argument("--tier-name", default=DEFAULT_TIER_NAME)
    parser.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    parser.add_argument("--python-executable", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    batch_sizes = _parse_ints(args.batch_sizes)
    tiers = build_breadth_tiers()
    tier = next((item for item in tiers if item.name == args.tier_name), None)
    if tier is None:
        raise KeyError(f"unknown tier: {args.tier_name}")
    report = run_batch_size_series(
        trace_root=args.trace_root,
        output_root=args.output_root,
        cohort_json=args.cohort_json,
        source_url=args.source_url,
        policy=args.policy,
        workers=args.workers,
        limit=args.limit,
        batch_sizes=batch_sizes,
        tier=tier,
        manifest_json=args.manifest_json,
        python_executable=args.python_executable,
    )
    print(
        f"[batch-size] Winner: {report['winner']['batch_size']} "
        f"hot_v/hr={float(report['winner']['videos_per_hour']):.1f}"
    )
    print(f"[batch-size] Report: {report['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
