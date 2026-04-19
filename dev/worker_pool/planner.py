"""Trace-driven worker-pool planner for yt-is.

This is a dev-only tool. It does not touch the production fetch path.

It reads JSONL trace files produced by ``csf-source fetch`` and models
how much throughput we would gain from 1..N isolated notebook workers.
The goal is to make worker-count decisions with data before changing the
live industrial pipeline.
"""

from __future__ import annotations

import argparse
import heapq
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


TRACE_ACTION = "nlm_batch_reusable_process_completed"


@dataclass(frozen=True)
class BatchSample:
    """One completed reusable NotebookLM batch from a trace."""

    timestamp: str
    batch_size: int
    succeeded: int
    failed: int
    total_elapsed_s: float
    setup_elapsed_s: float
    extract_elapsed_s: float
    cleanup_elapsed_s: float
    notebook_reused: bool
    setup_mode: str
    strategy: str
    nb_id: str | None

    @property
    def processed(self) -> int:
        return self.succeeded + self.failed


@dataclass(frozen=True)
class ScheduleResult:
    """A makespan estimate for a given worker count."""

    workers: int
    makespan_s: float
    successes: int
    processed: int
    success_per_hour: float
    processed_per_hour: float
    utilization: float


def load_samples(trace_paths: Iterable[Path]) -> list[BatchSample]:
    """Load completed batch samples from one or more JSONL traces."""
    samples: list[BatchSample] = []
    for trace_path in trace_paths:
        with trace_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("action") != TRACE_ACTION:
                    continue
                data = record.get("data") or {}
                samples.append(
                    BatchSample(
                        timestamp=str(record.get("timestamp", "")),
                        batch_size=int(data.get("batch_size", 0) or 0),
                        succeeded=int(data.get("succeeded", 0) or 0),
                        failed=int(data.get("failed", 0) or 0),
                        total_elapsed_s=float(data.get("total_elapsed_s", 0.0) or 0.0),
                        setup_elapsed_s=float(data.get("setup_elapsed_s", 0.0) or 0.0),
                        extract_elapsed_s=float(data.get("extract_elapsed_s", 0.0) or 0.0),
                        cleanup_elapsed_s=float(data.get("cleanup_elapsed_s", 0.0) or 0.0),
                        notebook_reused=bool(data.get("notebook_reused", False)),
                        setup_mode=str(data.get("setup_mode", "")),
                        strategy=str(data.get("strategy", "")),
                        nb_id=(str(data["nb_id"]) if data.get("nb_id") else None),
                    )
                )
    return samples


def schedule_batches(samples: list[BatchSample], workers: int) -> ScheduleResult:
    """Estimate throughput for a given number of workers.

    We use a longest-processing-time-first schedule so the estimate is a
    reasonable upper bound on a greedy worker pool with isolated notebooks.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")

    if not samples:
        return ScheduleResult(
            workers=workers,
            makespan_s=0.0,
            successes=0,
            processed=0,
            success_per_hour=0.0,
            processed_per_hour=0.0,
            utilization=0.0,
        )

    ordered = sorted(samples, key=lambda sample: sample.total_elapsed_s, reverse=True)
    worker_heap: list[tuple[float, int]] = [(0.0, idx) for idx in range(workers)]
    heapq.heapify(worker_heap)

    worker_loads = [0.0] * workers
    successes = 0
    processed = 0
    total_work = 0.0

    for sample in ordered:
        current_load, worker_idx = heapq.heappop(worker_heap)
        finish = current_load + sample.total_elapsed_s
        worker_loads[worker_idx] = finish
        heapq.heappush(worker_heap, (finish, worker_idx))
        successes += sample.succeeded
        processed += sample.processed
        total_work += sample.total_elapsed_s

    makespan_s = max(worker_loads)
    if makespan_s <= 0:
        return ScheduleResult(
            workers=workers,
            makespan_s=0.0,
            successes=successes,
            processed=processed,
            success_per_hour=0.0,
            processed_per_hour=0.0,
            utilization=0.0,
        )

    success_per_hour = successes / makespan_s * 3600.0
    processed_per_hour = processed / makespan_s * 3600.0
    utilization = total_work / (makespan_s * workers)

    return ScheduleResult(
        workers=workers,
        makespan_s=makespan_s,
        successes=successes,
        processed=processed,
        success_per_hour=success_per_hour,
        processed_per_hour=processed_per_hour,
        utilization=utilization,
    )


def recommend_workers(samples: list[BatchSample], max_workers: int) -> list[ScheduleResult]:
    """Return worker-count scenarios from 1..max_workers."""
    max_workers = max(1, max_workers)
    return [schedule_batches(samples, workers) for workers in range(1, max_workers + 1)]


def _format_rate(value: float) -> str:
    return f"{value:,.0f}"


def _format_seconds(value: float) -> str:
    return f"{value:,.1f}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estimate NotebookLM worker-pool throughput from trace logs.")
    parser.add_argument(
        "trace",
        nargs="+",
        type=Path,
        help="One or more JSONL trace files from .logs/",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Maximum worker count to model (default: 8)",
    )
    args = parser.parse_args(argv)

    samples = load_samples(args.trace)
    if not samples:
        print("No nlm_batch_reusable_process_completed events found.")
        return 1

    total_successes = sum(sample.succeeded for sample in samples)
    total_processed = sum(sample.processed for sample in samples)
    total_elapsed = sum(sample.total_elapsed_s for sample in samples)
    serial_success_per_hour = total_successes / total_elapsed * 3600.0 if total_elapsed > 0 else 0.0
    serial_processed_per_hour = total_processed / total_elapsed * 3600.0 if total_elapsed > 0 else 0.0

    print(f"Loaded {len(samples)} completed batches from {len(args.trace)} trace file(s).")
    print(f"Serial success throughput:   {_format_rate(serial_success_per_hour)} videos/hour")
    print(f"Serial processed throughput: {_format_rate(serial_processed_per_hour)} videos/hour")
    print()
    print("Worker-count model (LPT schedule):")
    print("workers | makespan | success/hr | processed/hr | util")
    print("--------+----------+------------+--------------+------")

    results = recommend_workers(samples, args.max_workers)
    for result in results:
        print(
            f"{result.workers:>7} | "
            f"{_format_seconds(result.makespan_s):>8} | "
            f"{_format_rate(result.success_per_hour):>10} | "
            f"{_format_rate(result.processed_per_hour):>12} | "
            f"{result.utilization:>4.2f}"
        )

    if len(results) > 1:
        best = max(results, key=lambda r: r.success_per_hour)
        gain = best.success_per_hour / results[0].success_per_hour if results[0].success_per_hour else 0.0
        print()
        print(
            f"Best modeled worker count: {best.workers} "
            f"({gain:,.2f}x vs 1 worker, success throughput)"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

