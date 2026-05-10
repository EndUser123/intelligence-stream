"""Stage-level comparison reducer for sharded NotebookLM benchmark runs.

Reads per-batch sweep_summary.json files to extract worker_stage_totals (setup,
extract, add, cleanup) and content_fetch_status_counts, then outputs a markdown
comparison table with a bottleneck annotation per run/lane.

Critical constraint: worker_stage_totals are summed across all workers, not
critical-path. The bottleneck column is therefore an aggregate stage-sum signal,
not proof of the critical-path tail.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUMMARY_NAME = "sharded_lane_series_summary.json"
LANE_DIRS = re.compile(r"(batch_\d+)/notebooklm_route_plus_fallback_\S+/(\d{8}_\d{6})")


@dataclass(frozen=True, slots=True)
class BatchEntry:
    worker_id: str
    batch_count: int
    succeeded: int
    failed: int


@dataclass(frozen=True, slots=True)
class BatchMetrics:
    timestamp: str
    workers: int
    elapsed_s: float
    succeeded: int
    fail_count: int
    setup_sum: float
    extract_sum: float
    add_sum: float
    cleanup_sum: float
    sr_age_avg: float
    sr_age_max: float
    command_failed: int
    nlm_below_threshold: int
    ready: int
    content_fetch_total: int
    batch_entries: tuple[BatchEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LaneMetrics:
    lane_name: str
    aggregate_vph: float
    wall_elapsed_s: float
    startup_prepare_total_elapsed_s_total: float = 0.0
    setup_elapsed_s_total: float = 0.0
    add_elapsed_s_total: float = 0.0
    cleanup_elapsed_s_total: float = 0.0
    worker_idle_wait_s_total: float = 0.0
    source_ready_age_s_avg: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    processed_count: int = 0
    batches: tuple[BatchMetrics, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RunMetrics:
    run_name: str
    run_root: Path
    status: str
    hygiene_status: str
    combined_vph: float
    combined_wall_s: float
    lanes: tuple[LaneMetrics, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _load_sweep_summary(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some historical artifacts have raw Windows backslashes in JSON string
        # values. Repair only after normal parsing fails, so valid JSON escapes
        # such as \n remain semantically intact.
        escaped = raw.replace(chr(92), chr(92) + chr(92))
        return json.loads(escaped)


def _lane_key(lane_name: str) -> str:
    return (
        lane_name.replace("a_hominidae_pro", "pro")
        .replace("troup_hominidae_free", "free")
    )


def _apply_aggregate_metrics(agg: dict[str, Any]) -> tuple[float, float, float, float, float, float, float, int, int, int]:
    return (
        agg.get("hot_path_videos_per_hour", 0.0) or 0.0,
        agg.get("wall_elapsed_s", 0.0) or 0.0,
        agg.get("startup_prepare_total_elapsed_s_total", 0.0) or 0.0,
        agg.get("setup_elapsed_s_total", 0.0) or 0.0,
        agg.get("add_elapsed_s_total", 0.0) or 0.0,
        agg.get("cleanup_elapsed_s_total", 0.0) or 0.0,
        agg.get("worker_idle_wait_s_total", 0.0) or 0.0,
        agg.get("source_ready_age_s_avg", 0.0) or 0.0,
        agg.get("hot_path_success_count_total", 0) or 0,
        agg.get("fail_count_total", 0) or 0,
        agg.get("processed_count_total", 0) or 0,
    )


def _extract_batch_metrics(sweep_dir: Path) -> BatchMetrics | None:
    """Find the timestamped sweep_summary.json under a sweep directory.

    The sweep_dir (e.g. batch_XX/notebooklm_route_plus_fallback_30s_1w/)
    contains a timestamp subdirectory (e.g. 20260504_220804/) which contains
    sweep_summary.json.  Unlike the caller which already navigated from batch_dir
    into the sweep_dir, this function looks *inside* sweep_dir for the timestamp
    subdir rather than looking in sweep_dir's parent (the batch dir).
    """
    if not sweep_dir.is_dir():
        return None
    timestamp_dirs = sorted(
        d for d in sweep_dir.iterdir()
        if d.is_dir() and re.match(r"\d{8}_\d{6}$", d.name)
    )
    if not timestamp_dirs:
        return None
    ts_dir = timestamp_dirs[-1]
    sweep_path = ts_dir / "sweep_summary.json"
    if not sweep_path.exists():
        return None

    summary = _load_sweep_summary(sweep_path)
    result = summary.get("results", [{}])[0]
    fc = result.get("fetch_completed", {})
    wst = fc.get("worker_stage_totals", {})
    cc_total = fc.get("content_fetch_status_counts", {})
    cc_top = result.get("content_fetch_status_counts", {})

    cf_total = cc_top.get("command_failed", 0) if isinstance(cc_top, dict) else 0
    nlm_bt = cc_top.get("nlm_content_below_threshold", 0) if isinstance(cc_top, dict) else 0
    ready = cc_top.get("ready", 0) if isinstance(cc_top, dict) else 0
    cf_total_fallback = cc_total.get("command_failed", 0) if isinstance(cc_total, dict) else 0
    nlm_bt_fallback = cc_total.get("nlm_content_below_threshold", 0) if isinstance(cc_total, dict) else 0
    ready_fallback = cc_total.get("ready", 0) if isinstance(cc_total, dict) else 0

    return BatchMetrics(
        timestamp=ts_dir.name,
        workers=result.get("workers", 0),
        elapsed_s=result.get("elapsed_s", 0.0),
        succeeded=result.get("success_count", 0),
        fail_count=result.get("fail_count", 0),
        setup_sum=wst.get("setup_elapsed_s_total", 0.0),
        extract_sum=wst.get("extract_elapsed_s_total", 0.0),
        add_sum=wst.get("add_sources_elapsed_s_total", 0.0),
        cleanup_sum=wst.get("cleanup_elapsed_s_total", 0.0),
        sr_age_avg=wst.get("source_ready_age_s_avg", 0.0),
        sr_age_max=wst.get("source_ready_age_s_max", 0.0),
        command_failed=cf_total or cf_total_fallback,
        nlm_below_threshold=nlm_bt or nlm_bt_fallback,
        ready=ready or ready_fallback,
        content_fetch_total=(cf_total or cf_total_fallback) + (nlm_bt or nlm_bt_fallback) + (ready or ready_fallback),
    )


def _parse_worker_batch_entries(stdout_path: Path) -> tuple[BatchEntry, ...]:
    """Extract per-worker succeeded/failed counts from stdout batch summary lines."""
    entries: list[BatchEntry] = []
    if not stdout_path.exists():
        return tuple(entries)
    for line in stdout_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "batch_count" not in line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        entries.append(BatchEntry(
            worker_id=obj.get("worker_id", ""),
            batch_count=obj.get("batch_count", 0),
            succeeded=obj.get("succeeded", 0),
            failed=obj.get("failed", 0),
        ))
    return tuple(entries)


def _extract_lane_metrics(run_root: Path, lane_name: str) -> LaneMetrics:
    """Extract all per-batch metrics for a single lane."""
    lane_dir = run_root / "soak" / lane_name
    summary_path = run_root / SUMMARY_NAME
    aggregate_vph = 0.0
    wall_elapsed_s = 0.0
    startup_prepare_total_elapsed_s_total = 0.0
    setup_elapsed_s_total = 0.0
    add_elapsed_s_total = 0.0
    cleanup_elapsed_s_total = 0.0
    worker_idle_wait_s_total = 0.0
    source_ready_age_s_avg = 0.0
    success_count = 0
    fail_count = 0
    processed_count = 0

    if summary_path.exists():
        try:
            summary = _load_sweep_summary(summary_path)
        except (json.JSONDecodeError, OSError):
            summary = {}
        lane_found = False
        for run in summary.get("runs", []):
            if not isinstance(run, dict) or _lane_key(str(run.get("lane", ""))) != _lane_key(lane_name):
                continue
            agg = run.get("aggregate", run)
            if not isinstance(agg, dict):
                agg = run
            merged = {**run, **agg}
            (
                aggregate_vph,
                wall_elapsed_s,
                startup_prepare_total_elapsed_s_total,
                setup_elapsed_s_total,
                add_elapsed_s_total,
                cleanup_elapsed_s_total,
                worker_idle_wait_s_total,
                source_ready_age_s_avg,
                success_count,
                fail_count,
                processed_count,
            ) = _apply_aggregate_metrics(merged)
            lane_found = True
            break

        if not lane_found:
            combined = summary.get("combined", {})
            if isinstance(combined, dict):
                (
                    aggregate_vph,
                    wall_elapsed_s,
                    startup_prepare_total_elapsed_s_total,
                    setup_elapsed_s_total,
                    add_elapsed_s_total,
                    cleanup_elapsed_s_total,
                    worker_idle_wait_s_total,
                    source_ready_age_s_avg,
                    success_count,
                    fail_count,
                    processed_count,
                ) = _apply_aggregate_metrics(combined)

    batches: list[BatchMetrics] = []
    if not lane_dir.exists():
        return LaneMetrics(
            lane_name=lane_name,
            aggregate_vph=aggregate_vph,
            wall_elapsed_s=wall_elapsed_s,
            startup_prepare_total_elapsed_s_total=startup_prepare_total_elapsed_s_total,
            setup_elapsed_s_total=setup_elapsed_s_total,
            add_elapsed_s_total=add_elapsed_s_total,
            cleanup_elapsed_s_total=cleanup_elapsed_s_total,
            worker_idle_wait_s_total=worker_idle_wait_s_total,
            source_ready_age_s_avg=source_ready_age_s_avg,
            success_count=success_count,
            fail_count=fail_count,
            processed_count=processed_count,
        )

    batch_dirs = sorted(d for d in lane_dir.iterdir() if d.is_dir() and d.name.startswith("batch_"))
    for batch_dir in batch_dirs:
        sweep_dir = None
        for candidate in batch_dir.iterdir():
            if candidate.is_dir() and re.match(r"notebooklm_route_plus_fallback", candidate.name):
                sweep_dir = candidate
                break
        if sweep_dir is None:
            continue
        batch_metrics = _extract_batch_metrics(sweep_dir)
        if batch_metrics is None:
            continue

        # Attach per-worker batch entries from stdout
        ts_dirs = sorted(
            d for d in sweep_dir.iterdir() if d.is_dir() and re.match(r"\d{8}_\d{6}$", d.name)
        )
        if ts_dirs:
            ts_dir = ts_dirs[-1]
            worker_parent = ts_dir
            # Navigate to workers_NN directory
            worker_dirs = sorted(d for d in worker_parent.iterdir() if d.is_dir() and d.name.startswith("workers_"))
            all_entries: list[BatchEntry] = []
            for wd in worker_dirs:
                stdout_path = wd / "stdout.txt"
                all_entries.extend(_parse_worker_batch_entries(stdout_path))
            batch_metrics = BatchMetrics(
                timestamp=batch_metrics.timestamp,
                workers=batch_metrics.workers,
                elapsed_s=batch_metrics.elapsed_s,
                succeeded=batch_metrics.succeeded,
                fail_count=batch_metrics.fail_count,
                setup_sum=batch_metrics.setup_sum,
                extract_sum=batch_metrics.extract_sum,
                add_sum=batch_metrics.add_sum,
                cleanup_sum=batch_metrics.cleanup_sum,
                sr_age_avg=batch_metrics.sr_age_avg,
                sr_age_max=batch_metrics.sr_age_max,
                command_failed=batch_metrics.command_failed,
                nlm_below_threshold=batch_metrics.nlm_below_threshold,
                ready=batch_metrics.ready,
                content_fetch_total=batch_metrics.content_fetch_total,
                batch_entries=tuple(all_entries),
            )
        batches.append(batch_metrics)

    return LaneMetrics(
        lane_name=lane_name,
        aggregate_vph=aggregate_vph,
        wall_elapsed_s=wall_elapsed_s,
        startup_prepare_total_elapsed_s_total=startup_prepare_total_elapsed_s_total,
        setup_elapsed_s_total=setup_elapsed_s_total,
        add_elapsed_s_total=add_elapsed_s_total,
        cleanup_elapsed_s_total=cleanup_elapsed_s_total,
        worker_idle_wait_s_total=worker_idle_wait_s_total,
        source_ready_age_s_avg=source_ready_age_s_avg,
        success_count=success_count,
        fail_count=fail_count,
        processed_count=processed_count,
        batches=tuple(batches),
    )


def load_run_metrics(run_root: Path) -> RunMetrics:
    summary_path = run_root / SUMMARY_NAME
    status = ""
    hygiene_status = ""
    combined_vph = 0.0
    combined_wall_s = 0.0

    if summary_path.exists():
        try:
            summary = _load_sweep_summary(summary_path)
        except (json.JSONDecodeError, OSError):
            summary = {}
        status = str(summary.get("status", "")) or ""
        hygiene = summary.get("post_run_hygiene", {})
        hygiene_status = str(hygiene.get("status", "")) if isinstance(hygiene, dict) else ""
        combined = summary.get("combined", {})
        if isinstance(combined, dict):
            combined_vph = combined.get("hot_path_videos_per_hour", 0.0) or 0.0
            combined_wall_s = combined.get("wall_elapsed_s", 0.0) or 0.0

    # Discover lane directories
    soak_dir = run_root / "soak"
    lanes: list[LaneMetrics] = []
    if soak_dir.exists():
        for lane_dir in sorted(soak_dir.iterdir()):
            if lane_dir.is_dir() and not lane_dir.name.startswith("."):
                lanes.append(_extract_lane_metrics(run_root, lane_dir.name))

    return RunMetrics(
        run_name=run_root.name,
        run_root=run_root,
        status=status,
        hygiene_status=hygiene_status,
        combined_vph=combined_vph,
        combined_wall_s=combined_wall_s,
        lanes=tuple(lanes),
    )


# ---------------------------------------------------------------------------
# Bottleneck analysis
# ---------------------------------------------------------------------------


def _compute_bottleneck(lane: LaneMetrics) -> str:
    """Determine the strongest bottleneck candidate from lane metrics."""
    if not lane.batches:
        return "no-batch-data"

    # Aggregate batch-level totals
    setup_sum = sum(b.setup_sum for b in lane.batches)
    extract_sum = sum(b.extract_sum for b in lane.batches)
    add_sum = sum(b.add_sum for b in lane.batches)
    cleanup_sum = sum(b.cleanup_sum for b in lane.batches)
    total_stage_sum = setup_sum + extract_sum + add_sum + cleanup_sum

    if total_stage_sum <= 0:
        return "sum-only-unknown"

    all_entries = [e for b in lane.batches for e in b.batch_entries]
    worker_failed_total = sum(e.failed for e in all_entries)

    cf_total = sum(b.command_failed for b in lane.batches)
    final_fail_total = sum(b.fail_count for b in lane.batches)
    content_fetch_denominator = sum(b.content_fetch_total for b in lane.batches)

    if not all_entries:
        recovered_note = "recovery-unproven"
    elif worker_failed_total == 0:
        recovered_note = "no-failures"
    elif final_fail_total == 0:
        recovered_note = "all-recovered"
    elif final_fail_total >= worker_failed_total:
        recovered_note = "recovered-unknown"
    else:
        recovered_note = f"{worker_failed_total - final_fail_total}/{worker_failed_total}-recovered"

    ratios = {
        "setup": setup_sum / total_stage_sum,
        "extract": extract_sum / total_stage_sum,
        "add": add_sum / total_stage_sum,
        "cleanup": cleanup_sum / total_stage_sum,
    }
    dominant = max(ratios, key=lambda k: ratios[k])
    dominant_ratio = ratios[dominant]

    notes = [
        f"{dominant}={dominant_ratio:.0%} of aggregate stage sum",
        recovered_note,
    ]
    if cf_total > 0:
        if content_fetch_denominator > 0:
            notes.insert(1, f"command_failed={cf_total} ({cf_total / content_fetch_denominator:.0%})")
        else:
            notes.insert(1, f"command_failed={cf_total}")
    return "stage-sum-suggested:" + dominant + " [" + ", ".join(notes) + "]"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_run(run: RunMetrics) -> str:
    """Format a single run as a markdown section."""
    lines = [f"## {run.run_name}", ""]
    lines.append(f"- status: {run.status or 'n/a'}, hygiene: {run.hygiene_status or 'n/a'}")
    lines.append(f"- combined VPH: {run.combined_vph:.2f}, wall: {run.combined_wall_s:.1f}s")
    lines.append("")

    for lane in run.lanes:
        lines.append(f"### Lane: {lane.lane_name}")
        bottleneck = _compute_bottleneck(lane)

        # Aggregate lane summary
        lines.append(f"- aggregate VPH: {lane.aggregate_vph:.2f}")
        lines.append(f"- aggregate wall: {lane.wall_elapsed_s:.1f}s")
        lines.append(f"- aggregate startup prepare: {lane.startup_prepare_total_elapsed_s_total:.1f}s")
        lines.append(f"- aggregate setup: {lane.setup_elapsed_s_total:.1f}s")
        lines.append(f"- aggregate add: {lane.add_elapsed_s_total:.1f}s")
        lines.append(f"- aggregate cleanup: {lane.cleanup_elapsed_s_total:.1f}s")
        lines.append(f"- aggregate idle wait: {lane.worker_idle_wait_s_total:.1f}s")
        lines.append(f"- aggregate sr_age_avg: {lane.source_ready_age_s_avg:.1f}s")
        lines.append(f"- success/fail/processed: {lane.success_count}/{lane.fail_count}/{lane.processed_count}")
        lines.append("")

        # Per-batch table
        if lane.batches:
            lines.append("| Batch | Workers | elapsed(s) | setup(s) | extract(s) | add(s) | cleanup(s) | sr_age(s) | command_failed | ready | **Lane Bottleneck** |")
            lines.append("|-------|---------|------------|----------|------------|--------|------------|----------|----------------|-------|----------------|")
            for b in lane.batches:
                bottleneck = _compute_bottleneck(lane)
                lines.append(
                    f"| {b.timestamp} | {b.workers} | {b.elapsed_s:.1f} | "
                    f"{b.setup_sum:.1f} | {b.extract_sum:.1f} | {b.add_sum:.1f} | "
                    f"{b.cleanup_sum:.1f} | {b.sr_age_avg:.1f} | "
                    f"{b.command_failed} | {b.ready} | {bottleneck} |"
                )
            lines.append("")
            # Per-worker summary if available
            all_entries = [e for b in lane.batches for e in b.batch_entries]
            if all_entries:
                lines.append("| Worker | Worker Batch Count | Succeeded | Failed |")
                lines.append("|--------|-------|-----------|--------|")
                for e in all_entries:
                    lines.append(f"| {e.worker_id} | {e.batch_count} | {e.succeeded} | {e.failed} |")
                lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage-level comparison reducer for sharded NotebookLM benchmark runs."
    )
    parser.add_argument(
        "--runs-root",
        required=True,
        type=Path,
        help="Root directory containing run subdirectories.",
    )
    parser.add_argument(
        "runs",
        nargs="+",
        help="Run names (subdirectories under --runs-root).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    results: list[str] = ["# Stage Reducer Output", ""]
    for run_name in args.runs:
        run_root = args.runs_root / run_name
        if not run_root.exists():
            results.append(f"**{run_name}: NOT FOUND**\n")
            continue
        try:
            run = load_run_metrics(run_root)
        except Exception as exc:
            results.append(f"**{run_name}: ERROR loading — {exc}**\n")
            continue
        results.append(format_run(run))

    print("\n".join(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
