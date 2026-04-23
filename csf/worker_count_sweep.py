"""Worker-count sweep runner for yt-is NotebookLM throughput trials."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CSF_SOURCE_SCRIPT = REPO_ROOT / "bin" / "csf-source"
DEFAULT_WORKER_COUNTS = (1, 2, 3, 4, 5, 6, 7, 8)
DEFAULT_LIMIT = 1200
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".logs" / "worker_count_trials"


@dataclass(slots=True)
class TrialArtifact:
    """Artifacts and summary data captured for one worker-count trial."""

    workers: int
    limit: int
    returncode: int
    elapsed_s: float
    stdout_path: str
    stderr_path: str
    log_dir: str
    log_file: str
    fetch_completed: dict[str, Any] = field(default_factory=dict)

    @property
    def success_count(self) -> int:
        return int(self.fetch_completed.get("success_count", 0) or 0)

    @property
    def fail_count(self) -> int:
        return int(self.fetch_completed.get("fail_count", 0) or 0)

    @property
    def skip_count(self) -> int:
        return int(self.fetch_completed.get("skip_count", 0) or 0)

    @property
    def processed_count(self) -> int:
        return int(self.fetch_completed.get("processed_count", 0) or 0)

    @property
    def videos_per_hour(self) -> float:
        elapsed = float(self.fetch_completed.get("elapsed_s", 0) or 0.0)
        if elapsed <= 0:
            return 0.0
        return round(self.success_count / elapsed * 3600.0, 2)

    @property
    def processed_per_hour(self) -> float:
        elapsed = float(self.fetch_completed.get("elapsed_s", 0) or 0.0)
        if elapsed <= 0:
            return 0.0
        return round(self.processed_count / elapsed * 3600.0, 2)

    @property
    def add_elapsed_s(self) -> float:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        return float(totals.get("add_sources_elapsed_s_total", 0) or 0.0)

    @property
    def readiness_elapsed_s(self) -> float:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        return float(totals.get("materialization_wait_elapsed_s_total", 0) or 0.0)

    @property
    def cleanup_elapsed_s(self) -> float:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        return float(totals.get("cleanup_elapsed_s_total", 0) or 0.0)

    @property
    def materialization_started(self) -> bool:
        return bool(self.fetch_completed.get("materialization_started", False))

    @property
    def timeout_hit(self) -> bool:
        return bool(self.fetch_completed.get("timeout_hit", False))

    @property
    def content_fetch_status_counts(self) -> dict[str, int]:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        counts = totals.get("content_fetch_status_counts_total", {})
        if not counts:
            counts = self.fetch_completed.get("content_fetch_status_counts", {}) or {}
        return dict(counts) if isinstance(counts, dict) else {}

    @property
    def source_ready_age_s_total(self) -> float:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        value = totals.get("source_ready_age_s_total")
        if value is None:
            value = self.fetch_completed.get("source_ready_age_s_total", 0)
        return float(value or 0.0)

    @property
    def source_ready_age_s_max(self) -> float:
        totals = self.fetch_completed.get("worker_stage_totals", {}) or {}
        value = totals.get("source_ready_age_s_max")
        if value is None:
            value = self.fetch_completed.get("source_ready_age_s_max", 0)
        return float(value or 0.0)

    @property
    def source_ready_age_s_avg(self) -> float:
        counts = self.content_fetch_status_counts
        total = self.source_ready_age_s_total
        if not counts:
            value = self.fetch_completed.get("source_ready_age_s_avg", 0)
            return float(value or 0.0)
        return round(total / max(sum(int(v) for v in counts.values()), 1), 3)

    def to_row(self) -> dict[str, Any]:
        payload = asdict(self)
        elapsed_s = float(self.fetch_completed.get("elapsed_s", self.elapsed_s) or self.elapsed_s)
        payload.update(
            {
                "elapsed_s": round(elapsed_s, 3),
                "process_elapsed_s": round(self.elapsed_s, 3),
                "success_count": self.success_count,
                "fail_count": self.fail_count,
                "skip_count": self.skip_count,
                "processed_count": self.processed_count,
                "videos_per_hour": self.videos_per_hour,
                "processed_per_hour": self.processed_per_hour,
                "add_elapsed_s": round(self.add_elapsed_s, 3),
                "readiness_elapsed_s": round(self.readiness_elapsed_s, 3),
                "cleanup_elapsed_s": round(self.cleanup_elapsed_s, 3),
                "materialization_started": self.materialization_started,
                "timeout_hit": self.timeout_hit,
                "source_ready_age_s_total": round(self.source_ready_age_s_total, 3),
                "source_ready_age_s_max": round(self.source_ready_age_s_max, 3),
                "source_ready_age_s_avg": round(self.source_ready_age_s_avg, 3),
                "content_fetch_status_counts": self.content_fetch_status_counts,
            }
        )
        return payload


def _parse_worker_counts(value: str) -> list[int]:
    counts = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not counts:
        raise ValueError("at least one worker count is required")
    for count in counts:
        if count < 1:
            raise ValueError("worker counts must be >= 1")
    return counts


def _latest_jsonl_file(log_dir: Path) -> Path | None:
    jsonl_files = [path for path in log_dir.glob("*.jsonl") if path.is_file()]
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda path: path.stat().st_mtime)


def _load_fetch_completed_event(log_dir: Path) -> dict[str, Any]:
    log_file = _latest_jsonl_file(log_dir)
    if log_file is None:
        raise RuntimeError(f"no JSONL trace found in {log_dir}")
    fetch_completed: dict[str, Any] | None = None
    with log_file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("action") == "fetch_completed":
                data = entry.get("data", {})
                if isinstance(data, dict):
                    fetch_completed = data
    if fetch_completed is None:
        raise RuntimeError(f"no fetch_completed event found in {log_file}")
    return fetch_completed


def _run_fetch_trial(
    *,
    workers: int,
    limit: int,
    output_dir: Path,
    python_executable: str | None = None,
) -> TrialArtifact:
    run_dir = output_dir / f"workers_{workers:02d}"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    env = os.environ.copy()
    env["INTELLIGENCE_STREAM_LOG_DIR"] = str(log_dir)

    started_at = time.monotonic()
    command = [
        python_executable or sys.executable,
        str(CSF_SOURCE_SCRIPT),
        "fetch",
        "--workers",
        str(workers),
        "--limit",
        str(limit),
    ]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    returncode = proc.returncode
    stdout_text = proc.stdout or ""
    stderr_text = proc.stderr or ""
    elapsed_s = round(time.monotonic() - started_at, 3)

    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")

    fetch_completed = _load_fetch_completed_event(log_dir)
    log_file = _latest_jsonl_file(log_dir)
    if log_file is None:
        raise RuntimeError(f"no JSONL trace found in {log_dir}")

    return TrialArtifact(
        workers=workers,
        limit=limit,
        returncode=returncode,
        elapsed_s=elapsed_s,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        log_dir=str(log_dir),
        log_file=str(log_file),
        fetch_completed=fetch_completed,
    )


def run_worker_count_sweep(
    *,
    worker_counts: list[int] | tuple[int, ...] = DEFAULT_WORKER_COUNTS,
    limit: int = DEFAULT_LIMIT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    python_executable: str | None = None,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    sweep_started_at = time.time()
    sweep_dir = output_root / time.strftime("%Y%m%d_%H%M%S", time.localtime(sweep_started_at))
    sweep_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for workers in worker_counts:
        trial = _run_fetch_trial(
            workers=workers,
            limit=limit,
            output_dir=sweep_dir,
            python_executable=python_executable,
        )
        row = trial.to_row()
        results.append(row)
        print(
            f"[trial] workers={workers} success={trial.success_count} fail={trial.fail_count} "
            f"elapsed={trial.elapsed_s:.1f}s vph={trial.videos_per_hour:.1f}"
        )

    summary = {
        "started_at": sweep_started_at,
        "output_root": str(output_root),
        "sweep_dir": str(sweep_dir),
        "limit": limit,
        "worker_counts": list(worker_counts),
        "results": results,
    }

    summary_path = sweep_dir / "sweep_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    csv_path = sweep_dir / "sweep_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "workers",
                "limit",
                "returncode",
                "elapsed_s",
                "process_elapsed_s",
                "success_count",
                "fail_count",
                "skip_count",
                "processed_count",
                "videos_per_hour",
                "processed_per_hour",
                "add_elapsed_s",
                "readiness_elapsed_s",
                "cleanup_elapsed_s",
                "materialization_started",
                "timeout_hit",
                "source_ready_age_s_total",
                "source_ready_age_s_max",
                "source_ready_age_s_avg",
                "content_fetch_status_counts",
                "stdout_path",
                "stderr_path",
                "log_dir",
                "log_file",
            ],
        )
        writer.writeheader()
        for row in results:
            writer.writerow(
                {
                    "workers": row["workers"],
                    "limit": row["limit"],
                    "returncode": row["returncode"],
                    "elapsed_s": row["elapsed_s"],
                    "process_elapsed_s": row["process_elapsed_s"],
                    "success_count": row["success_count"],
                    "fail_count": row["fail_count"],
                    "skip_count": row["skip_count"],
                    "processed_count": row["processed_count"],
                    "videos_per_hour": row["videos_per_hour"],
                    "processed_per_hour": row["processed_per_hour"],
                    "add_elapsed_s": row["add_elapsed_s"],
                    "readiness_elapsed_s": row["readiness_elapsed_s"],
                    "cleanup_elapsed_s": row["cleanup_elapsed_s"],
                    "materialization_started": row["materialization_started"],
                    "timeout_hit": row["timeout_hit"],
                    "source_ready_age_s_total": row["source_ready_age_s_total"],
                    "source_ready_age_s_max": row["source_ready_age_s_max"],
                    "source_ready_age_s_avg": row["source_ready_age_s_avg"],
                    "content_fetch_status_counts": json.dumps(row["content_fetch_status_counts"], sort_keys=True),
                    "stdout_path": row["stdout_path"],
                    "stderr_path": row["stderr_path"],
                    "log_dir": row["log_dir"],
                    "log_file": row["log_file"],
                }
            )

    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    results = summary.get("results", []) or []
    if not results:
        print("[trial] No results.")
        return

    print()
    print(f"{'workers':>7} {'succ':>6} {'fail':>6} {'elapsed_s':>10} {'v/hr':>10} {'add_s':>8} {'ready_s':>9} {'timeout':>8}")
    print("-" * 74)
    for row in results:
        print(
            f"{int(row['workers']):>7} {int(row['success_count']):>6} {int(row['fail_count']):>6} "
            f"{float(row['elapsed_s']):>10.1f} {float(row['videos_per_hour']):>10.1f} "
            f"{float(row['add_elapsed_s']):>8.1f} {float(row['readiness_elapsed_s']):>9.1f} "
            f"{str(bool(row['timeout_hit'])):>8}"
        )

    best = max(results, key=lambda row: float(row.get("videos_per_hour", 0) or 0.0))
    print("-" * 74)
    print(
        f"[trial] Best observed throughput: workers={best['workers']} "
        f"v/hr={float(best['videos_per_hour']):.1f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the yt-is worker-count throughput sweep")
    parser.add_argument(
        "--workers",
        default="1,2,3,4,5,6,7,8",
        help="Comma-separated worker counts to test (default: 1,2,3,4,5,6,7,8)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Pending-item limit for each trial (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"Root directory for sweep artifacts (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--python",
        default=None,
        help="Python executable to use for csf-source (default: current interpreter)",
    )
    args = parser.parse_args(argv)

    worker_counts = _parse_worker_counts(args.workers)
    summary = run_worker_count_sweep(
        worker_counts=worker_counts,
        limit=args.limit,
        output_root=Path(args.output_root),
        python_executable=args.python,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
