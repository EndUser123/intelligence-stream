"""Compact summary printer for sharded NotebookLM benchmark runs."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUMMARY_NAME = "sharded_lane_series_summary.json"


@dataclass(frozen=True, slots=True)
class ShardedLaneRunSummary:
    """Selected metrics from a sharded lane series run."""

    run_root: Path
    summary_path: Path
    candidate: str
    status: str
    hygiene_status: str
    hot_path_videos_per_hour: float
    wall_elapsed_s: float
    add_elapsed_s_total: float
    cleanup_elapsed_s_total: float
    worker_idle_wait_s_total: float
    source_ready_age_s_avg: float
    success_count_total: int
    fail_count_total: int
    processed_count_total: int
    lane_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "run_root": str(self.run_root),
            "summary_path": str(self.summary_path),
            "status": self.status,
            "hygiene_status": self.hygiene_status,
            "hot_path_videos_per_hour": self.hot_path_videos_per_hour,
            "wall_elapsed_s": self.wall_elapsed_s,
            "add_elapsed_s_total": self.add_elapsed_s_total,
            "cleanup_elapsed_s_total": self.cleanup_elapsed_s_total,
            "worker_idle_wait_s_total": self.worker_idle_wait_s_total,
            "source_ready_age_s_avg": self.source_ready_age_s_avg,
            "success_count_total": self.success_count_total,
            "fail_count_total": self.fail_count_total,
            "processed_count_total": self.processed_count_total,
            "lane_count": self.lane_count,
        }


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


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"summary file does not contain a JSON object: {path}")
    return payload


def _summed_run_totals(payload: dict[str, Any]) -> dict[str, float]:
    totals = {
        "add_elapsed_s_total": 0.0,
        "cleanup_elapsed_s_total": 0.0,
        "worker_idle_wait_s_total": 0.0,
        "source_ready_age_s_total": 0.0,
        "processed_count_total": 0.0,
    }
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return totals

    for run in runs:
        if not isinstance(run, dict):
            continue
        aggregate = run.get("aggregate")
        if not isinstance(aggregate, dict):
            aggregate = run
        totals["add_elapsed_s_total"] += _float_value(aggregate.get("add_elapsed_s_total"))
        totals["cleanup_elapsed_s_total"] += _float_value(aggregate.get("cleanup_elapsed_s_total"))
        totals["worker_idle_wait_s_total"] += _float_value(aggregate.get("worker_idle_wait_s_total"))
        totals["source_ready_age_s_total"] += _float_value(aggregate.get("source_ready_age_s_total"))
        totals["processed_count_total"] += _float_value(aggregate.get("processed_count_total"))
    return totals


def load_sharded_lane_summary(path: Path) -> ShardedLaneRunSummary:
    path = Path(path)
    if path.is_dir():
        run_root = path
        summary_path = run_root / SUMMARY_NAME
    else:
        summary_path = path
        run_root = summary_path.parent
    if not summary_path.exists():
        raise FileNotFoundError(summary_path)

    payload = _load_payload(summary_path)
    combined = payload.get("combined") if isinstance(payload.get("combined"), dict) else {}
    post_run_hygiene = payload.get("post_run_hygiene") if isinstance(payload.get("post_run_hygiene"), dict) else {}
    run_totals = _summed_run_totals(payload)
    processed_total = _int_value(combined.get("processed_count_total"))
    if processed_total <= 0:
        processed_total = _int_value(run_totals["processed_count_total"])
    source_ready_age_s_avg = _float_value(combined.get("source_ready_age_s_avg"))
    if source_ready_age_s_avg <= 0.0 and processed_total > 0:
        source_ready_age_s_avg = round(run_totals["source_ready_age_s_total"] / processed_total, 3)

    return ShardedLaneRunSummary(
        run_root=run_root,
        summary_path=summary_path,
        candidate=run_root.name,
        status=str(payload.get("status") or ""),
        hygiene_status=str(post_run_hygiene.get("status") or ""),
        hot_path_videos_per_hour=_float_value(combined.get("hot_path_videos_per_hour")),
        wall_elapsed_s=_float_value(combined.get("wall_elapsed_s")),
        add_elapsed_s_total=_float_value(combined.get("add_elapsed_s_total")) or run_totals["add_elapsed_s_total"],
        cleanup_elapsed_s_total=_float_value(combined.get("cleanup_elapsed_s_total")) or run_totals["cleanup_elapsed_s_total"],
        worker_idle_wait_s_total=_float_value(combined.get("worker_idle_wait_s_total")) or run_totals["worker_idle_wait_s_total"],
        source_ready_age_s_avg=source_ready_age_s_avg,
        success_count_total=_int_value(combined.get("hot_path_success_count_total")),
        fail_count_total=_int_value(combined.get("fail_count_total")),
        processed_count_total=processed_total,
        lane_count=_int_value(combined.get("lane_count")),
    )


def format_sharded_lane_summary(summary: ShardedLaneRunSummary) -> str:
    return (
        f"candidate={summary.candidate} "
        f"status={summary.status} "
        f"hygiene={summary.hygiene_status or 'n/a'} "
        f"vph={summary.hot_path_videos_per_hour:.2f} "
        f"wall_s={summary.wall_elapsed_s:.3f} "
        f"add_s={summary.add_elapsed_s_total:.3f} "
        f"cleanup_s={summary.cleanup_elapsed_s_total:.3f} "
        f"idle_wait_s={summary.worker_idle_wait_s_total:.3f} "
        f"source_ready_age_s_avg={summary.source_ready_age_s_avg:.3f} "
        f"success={summary.success_count_total} "
        f"fail={summary.fail_count_total} "
        f"processed={summary.processed_count_total} "
        f"lanes={summary.lane_count}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print a compact summary for a sharded lane series run.")
    parser.add_argument(
        "--run-root",
        required=True,
        type=Path,
        help=f"Run root containing {SUMMARY_NAME}.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the compact text summary.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    summary = load_sharded_lane_summary(args.run_root)
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_sharded_lane_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
