"""Classifier for nlm_batch source-content-fetch command_failed events.

Maps individual command_failed events to error classes and retry recommendations
using only evidence from artifact logs — does not alter runtime behavior.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Error-class definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ErrorClass:
    name: str
    markers: tuple[str, ...]
    retry_recommendation: str  # "do_not_retry" | "candidate_retry" | "candidate_retry_once" | "unknown_do_not_change_policy"


ERROR_CLASSES: tuple[ErrorClass, ...] = tuple([
    ErrorClass(
        name="not_found_transient",
        markers=("NOT_FOUND",),
        retry_recommendation="candidate_retry",
    ),
    ErrorClass(
        name="rate_limited",
        markers=("RATE LIMIT", "RATE_LIMIT", "TOO MANY REQUESTS", "429"),
        retry_recommendation="candidate_retry",
    ),
    ErrorClass(
        name="service_unavailable",
        markers=(
            "TEMPORARILY UNAVAILABLE",
            "SERVICE UNAVAILABLE",
            "503",
            "502",
            "BAD GATEWAY",
            "GATEWAY TIMEOUT",
            "504",
        ),
        retry_recommendation="candidate_retry",
    ),
    ErrorClass(
        name="network_transient",
        markers=(
            "ECONNRESET",
            "ETIMEDOUT",
            "ECONNREFUSED",
            "CONNECTION RESET",
            "CONNECTION TIMED OUT",
            "DEADLINE EXCEEDED",
            "REQUEST TIMED OUT",
        ),
        retry_recommendation="candidate_retry",
    ),
    ErrorClass(
        name="tls_transient",
        markers=("TLS", "SSL", "CERTIFICATE"),
        retry_recommendation="candidate_retry",
    ),
    ErrorClass(
        name="empty_output",
        markers=(),
        retry_recommendation="candidate_retry_once",
    ),
    ErrorClass(
        name="auth_or_permission",
        markers=(
            "PERMISSION_DENIED",
            "UNAUTHORIZED",
            "AUTH",
            "LOGIN",
            "CREDENTIAL",
            "PROFILE",
        ),
        retry_recommendation="do_not_retry",
    ),
    ErrorClass(
        name="unknown",
        markers=(),
        retry_recommendation="unknown_do_not_change_policy",
    ),
])


def _classify_by_marker(combined_upper: str, returncode: int, stdout: str, stderr: str) -> ErrorClass:
    """Classify a command_failed event by scanning for known markers.

    Marker checks are case-insensitive. Auth/permission is checked first
    to ensure non-retryable classification takes priority.
    """
    # Auth/permission — non-retryable by definition
    for cls in ERROR_CLASSES:
        if cls.name == "auth_or_permission":
            for marker in cls.markers:
                if marker in combined_upper:
                    return cls
            break

    # Empty output — nonzero returncode with empty stdout and empty stderr
    if returncode != 0 and not stdout.strip() and not stderr.strip():
        for cls in ERROR_CLASSES:
            if cls.name == "empty_output":
                return cls

    # Transient markers
    for cls in ERROR_CLASSES:
        if cls.name in ("not_found_transient", "rate_limited", "service_unavailable",
                        "network_transient", "tls_transient"):
            for marker in cls.markers:
                if marker in combined_upper:
                    return cls

    # Unknown
    for cls in ERROR_CLASSES:
        if cls.name == "unknown":
            return cls

    # Fallback (shouldn't be reached)
    return ERROR_CLASSES[-1]


def classify_command_failed_event(event: dict) -> dict:
    """Classify a single command_failed content-fetch event.

    Expected event shape (from nlm_batch or term JSONL):
        {
            "status": "command_failed",
            "video_id": "...",
            "source_id": "...",
            "returncode": 1,
            "stdout": "...",
            "stderr": "...",
            "attempts": 1,
            "batch_timestamp": "..."
        }

    Returns a dict with:
        - error_class: str
        - retry_recommendation: str
        - matched_marker: str or None
        - is_auth_or_permission: bool
        - is_transient: bool
    """
    status = str(event.get("status", ""))
    if status != "command_failed":
        return {
            "error_class": "not_command_failed",
            "retry_recommendation": "unknown_do_not_change_policy",
            "matched_marker": None,
            "is_auth_or_permission": False,
            "is_transient": False,
        }

    returncode = event.get("returncode", 0)
    stdout = str(event.get("stdout") or "")
    stderr = str(event.get("stderr") or "")
    combined_upper = (stdout + "\n" + stderr).upper()

    error_class = _classify_by_marker(combined_upper, returncode, stdout, stderr)

    # Find which marker triggered classification
    matched_marker = None
    if error_class.name != "empty_output":
        for marker in error_class.markers:
            if marker in combined_upper:
                matched_marker = marker
                break

    is_auth_or_permission = error_class.name == "auth_or_permission"
    is_transient = error_class.retry_recommendation in ("candidate_retry", "candidate_retry_once")

    return {
        "error_class": error_class.name,
        "retry_recommendation": error_class.retry_recommendation,
        "matched_marker": matched_marker,
        "is_auth_or_permission": is_auth_or_permission,
        "is_transient": is_transient,
    }


# ---------------------------------------------------------------------------
# Artifact scanning helpers
# ---------------------------------------------------------------------------

def _scan_term_jsonl_for_content_fetch_events(term_path: Path) -> list[dict]:
    """Extract command_failed content-fetch events from a term JSONL log.

    Returns a list of event dicts with video_id, source_id, returncode,
    stdout, stderr, and batch context fields.

    Returns an empty list if the file does not contain event-level data
    (e.g., only summary counts or worker stdout without per-source events).
    """
    events: list[dict] = []
    found_events_key = False
    found_source_data = False

    with term_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Check for nlm_batch content_fetch_completed events with per-source data
            action = obj.get("action", "")

            # nlm_batch-style event with status breakdown per source
            if action in (
                "nlm_batch_source_content_fetch_completed",
                "nlm_batch_content_fetch_completed",
                "content_fetch_completed",
            ):
                found_events_key = True
                data = obj.get("data", {})
                if not isinstance(data, dict):
                    continue

                if str(data.get("status", "")) == "command_failed":
                    found_source_data = True
                    events.append({
                        "status": "command_failed",
                        "video_id": data.get("video_id", data.get("source_id", "")),
                        "source_id": data.get("source_id", ""),
                        "returncode": data.get("returncode", 0),
                        "stdout": (data.get("stdout") or data.get("stdout_excerpt") or "")[:500],
                        "stderr": (data.get("stderr") or data.get("stderr_excerpt") or "")[:500],
                        "attempts": data.get("attempts", 1),
                        "batch_timestamp": obj.get("timestamp", ""),
                        "source_id_validated_after_not_found": data.get("source_id_validated_after_not_found"),
                        "source_list_probe_returncode": data.get("source_list_probe_returncode"),
                        "source_list_probe_count": data.get("source_list_probe_count"),
                        "source_list_probe_elapsed_s": data.get("source_list_probe_elapsed_s"),
                        "source_list_probe_match_index": data.get("source_list_probe_match_index"),
                        "source_list_probe_match_title": data.get("source_list_probe_match_title"),
                        "source_list_probe_match_url": data.get("source_list_probe_match_url"),
                    })
                    continue

                # Look for source-level status arrays or breakdowns
                # The nlm_batch worker emits per-source events when detailed logging is enabled
                source_statuses = data.get("source_statuses", data.get("source_results", []))
                if isinstance(source_statuses, list) and len(source_statuses) > 0:
                    found_source_data = True
                    for src in source_statuses:
                        status = str(src.get("status", ""))
                        if status == "command_failed":
                            events.append({
                                "status": status,
                                "video_id": src.get("video_id", src.get("source_id", "")),
                                "source_id": src.get("source_id", ""),
                                "returncode": src.get("returncode", 0),
                                "stdout": (src.get("stdout") or src.get("stdout_excerpt") or "")[:500],
                                "stderr": (src.get("stderr") or src.get("stderr_excerpt") or "")[:500],
                                "attempts": src.get("attempts", 1),
                                "batch_timestamp": obj.get("timestamp", ""),
                                "source_id_validated_after_not_found": src.get("source_id_validated_after_not_found"),
                                "source_list_probe_returncode": src.get("source_list_probe_returncode"),
                                "source_list_probe_count": src.get("source_list_probe_count"),
                                "source_list_probe_elapsed_s": src.get("source_list_probe_elapsed_s"),
                                "source_list_probe_match_index": src.get("source_list_probe_match_index"),
                                "source_list_probe_match_title": src.get("source_list_probe_match_title"),
                                "source_list_probe_match_url": src.get("source_list_probe_match_url"),
                            })

    # If we scanned a file but found no event-level data, return empty
    # with a marker so the caller knows the file was scanned but insufficient
    if not found_events_key and not found_source_data:
        return []  # No content-fetch events at all in this file

    return events


def _scan_sweep_summary_for_content_fetch_events(sweep_path: Path) -> list[dict]:
    """Try to extract command_failed events from a sweep_summary.json result entry.

    Returns an empty list if sweep_summary.json contains only summary-level
    content_fetch_status_counts without per-source event data.
    """
    events: list[dict] = []
    try:
        data = json.loads(sweep_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return events

    for result in data.get("results", []):
        # Check if per-source event data exists in the result
        if "source_statuses" in result or "source_results" in result:
            for src in result.get("source_statuses", result.get("source_results", [])):
                status = str(src.get("status", ""))
                if status == "command_failed":
                    events.append({
                        "status": status,
                        "video_id": src.get("video_id", src.get("source_id", "")),
                        "source_id": src.get("source_id", ""),
                        "returncode": src.get("returncode", 0),
                        "stdout": (src.get("stdout") or src.get("stdout_excerpt") or "")[:500],
                        "stderr": (src.get("stderr") or src.get("stderr_excerpt") or "")[:500],
                        "attempts": src.get("attempts", 1),
                        "batch_timestamp": result.get("batch_timestamp", sweep_path.parent.name),
                        "source_id_validated_after_not_found": src.get("source_id_validated_after_not_found"),
                        "source_list_probe_returncode": src.get("source_list_probe_returncode"),
                        "source_list_probe_count": src.get("source_list_probe_count"),
                        "source_list_probe_elapsed_s": src.get("source_list_probe_elapsed_s"),
                    })
        # If only summary counts exist, return empty (caller handles this)
        elif "content_fetch_status_counts" in result:
            # Has summary but not event-level data
            return []

    return events


def _scan_for_events_in_run(run_root: Path) -> tuple[list[dict], str]:
    """Scan a run directory for content-fetch command_failed events.

    Returns (events, sufficiency_status) where:
        - events: list of classified event dicts
        - sufficiency_status: one of
            - "has_event_level_data": found detailed event records with stdout/stderr
            - "summary_counts_only": found content_fetch_status_counts but no per-source detail
            - "no_content_fetch_events": no content-fetch events found
    """
    all_events: list[dict] = []
    sufficiency = "no_content_fetch_events"

    # Check each lane's smoke and soak subdirectories
    for subdir_name in ("smoke", "soak"):
        subdir = run_root / subdir_name
        if not subdir.is_dir():
            continue

        for lane_dir in subdir.iterdir():
            if not lane_dir.is_dir():
                continue

            for batch_dir in sorted(lane_dir.iterdir()):
                if not batch_dir.is_dir() or not batch_dir.name.startswith("batch_"):
                    continue

                for sweep_candidate in batch_dir.iterdir():
                    if not sweep_candidate.is_dir() or not sweep_candidate.name.startswith("notebooklm_route"):
                        continue

                    # Try to find timestamp subdirectory with sweep_summary.json
                    ts_dirs = sorted(
                        d for d in sweep_candidate.iterdir()
                        if d.is_dir() and re.match(r"\d{8}_\d{6}", d.name)
                    )
                    if not ts_dirs:
                        continue

                    ts_dir = ts_dirs[-1]
                    sweep_summary = ts_dir / "sweep_summary.json"

                    if sweep_summary.exists():
                        sweep_events = _scan_sweep_summary_for_content_fetch_events(sweep_summary)
                        if sweep_events:
                            all_events.extend(sweep_events)
                            sufficiency = "has_event_level_data"
                        elif sufficiency == "no_content_fetch_events":
                            # Check if summary counts exist
                            try:
                                data = json.loads(sweep_summary.read_text(encoding="utf-8"))
                                for result in data.get("results", []):
                                    if "content_fetch_status_counts" in result:
                                        sufficiency = "summary_counts_only"
                                        break
                            except (json.JSONDecodeError, OSError):
                                pass

                    # Also scan term JSONL logs
                    for worker_dir in ts_dir.iterdir():
                        if not worker_dir.is_dir() or not worker_dir.name.startswith("workers_"):
                            continue
                        logs_dir = worker_dir / "logs"
                        if not logs_dir.is_dir():
                            continue
                        for term_file in logs_dir.glob("term_*.jsonl"):
                            term_events = _scan_term_jsonl_for_content_fetch_events(term_file)
                            if term_events:
                                all_events.extend(term_events)
                                sufficiency = "has_event_level_data"
                            elif sufficiency == "no_content_fetch_events":
                                sufficiency = "summary_counts_only"

    return all_events, sufficiency


def classify_run(run_root: Path, run_name: str) -> dict:
    """Scan a run directory and produce a classification report.

    Returns a dict with:
        - run_name
        - run_root
        - sufficiency
        - event_count
        - class_counts: dict[str, int]
        - retry_counts: dict[str, int]
        - auth_or_permission_count: int
        - transient_retry_candidate_count: int
        - events: list of (event, classification) tuples
        - summary_counts: dict of any aggregate content_fetch_status_counts found
    """
    events, sufficiency = _scan_for_events_in_run(run_root)

    class_counts: dict[str, int] = {}
    retry_counts: dict[str, int] = {}
    auth_count = 0
    transient_count = 0
    classified_events: list[tuple[dict, dict]] = []

    for event in events:
        classification = classify_command_failed_event(event)
        classified_events.append((event, classification))

        ec = classification["error_class"]
        class_counts[ec] = class_counts.get(ec, 0) + 1

        rec = classification["retry_recommendation"]
        retry_counts[rec] = retry_counts.get(rec, 0) + 1

        if classification["is_auth_or_permission"]:
            auth_count += 1
        if classification["is_transient"]:
            transient_count += 1

    # Gather summary-level counts for runs without event-level data
    summary_counts: dict[str, int] = {}
    for subdir_name in ("smoke", "soak"):
        subdir = run_root / subdir_name
        if not subdir.is_dir():
            continue
        for lane_dir in subdir.iterdir():
            if not lane_dir.is_dir():
                continue
            for batch_dir in sorted(lane_dir.iterdir()):
                if not batch_dir.is_dir() or not batch_dir.name.startswith("batch_"):
                    continue
                for sweep_candidate in batch_dir.iterdir():
                    if not sweep_candidate.is_dir() or not sweep_candidate.name.startswith("notebooklm_route"):
                        continue
                    ts_dirs = sorted(d for d in sweep_candidate.iterdir()
                                    if d.is_dir() and re.match(r"\d{8}_\d{6}", d.name))
                    if not ts_dirs:
                        continue
                    ts_dir = ts_dirs[-1]
                    sweep_summary = ts_dir / "sweep_summary.json"
                    if not sweep_summary.exists():
                        continue
                    try:
                        data = json.loads(sweep_summary.read_text(encoding="utf-8"))
                        for result in data.get("results", []):
                            cfc = result.get("content_fetch_status_counts", {})
                            if isinstance(cfc, dict):
                                for k, v in cfc.items():
                                    summary_counts[k] = summary_counts.get(k, 0) + int(v)
                    except (json.JSONDecodeError, OSError):
                        pass

    return {
        "run_name": run_name,
        "run_root": str(run_root),
        "sufficiency": sufficiency,
        "event_count": len(events),
        "class_counts": class_counts,
        "retry_counts": retry_counts,
        "auth_or_permission_count": auth_count,
        "transient_retry_candidate_count": transient_count,
        "events": classified_events,
        "summary_counts": summary_counts,
    }


def format_report(runs: list[dict], fmt: str = "markdown") -> str:
    """Format classification reports into markdown or text."""
    if fmt == "markdown":
        return _format_markdown(runs)
    elif fmt == "text":
        return _format_text(runs)
    else:
        raise ValueError(f"Unknown format: {fmt}")


def _format_markdown(runs: list[dict]) -> str:
    lines = [
        "# nlm_batch command_failed Classifier Report",
        "",
        "## Run Summary",
        "",
        "| Run | Sufficiency | Events | Auth/Permission | Transient Retry Candidates | Class Counts |",
        "|---|---|---|---|---|---|",
    ]

    for run in runs:
        sufficiency = run["sufficiency"]
        event_count = run["event_count"]
        auth = run["auth_or_permission_count"]
        transient = run["transient_retry_candidate_count"]
        class_str = ", ".join(f"{k}={v}" for k, v in sorted(run["class_counts"].items()))
        if not class_str:
            class_str = "none"
        lines.append(
            f"| {run['run_name']} | {sufficiency} | {event_count} | {auth} | {transient} | {class_str} |"
        )

    lines.append("")

    for run in runs:
        lines.append(f"## {run['run_name']}")
        lines.append(f"**Sufficiency:** `{run['sufficiency']}`")
        lines.append(f"**Event count:** {run['event_count']}")
        lines.append(f"**Auth/permission (non-retryable):** {run['auth_or_permission_count']}")
        lines.append(f"**Transient retry candidates:** {run['transient_retry_candidate_count']}")

        if run["summary_counts"]:
            lines.append(f"**Summary-level counts (no per-source events):** `{run['summary_counts']}`")

        if run["class_counts"]:
            lines.append("")
            lines.append("**Error class distribution:**")
            for cls, cnt in sorted(run["class_counts"].items(), key=lambda x: -x[1]):
                rec = run["retry_counts"].get(
                    next(
                        (ec.retry_recommendation for ec in ERROR_CLASSES if ec.name == cls),
                        "unknown_do_not_change_policy"
                    ),
                    0
                )
                lines.append(f"- `{cls}`: {cnt} events → retry: `{rec}`")

        if run["events"]:
            lines.append("")
            lines.append("### Event Details (up to 20 samples)")
            lines.append("")
            lines.append(
                "| # | video_id | source_id | returncode | attempts | source_validated | probe_rc | match_idx | match_title | matched_marker | class | retry |"
            )
            lines.append(
                "|---|---|---|---|---|---|---|---|---|---|---|---|"
            )
            for i, (event, classification) in enumerate(run["events"][:20]):
                vid = str(event.get("video_id", "") or event.get("source_id", ""))[:20]
                sid = str(event.get("source_id", ""))[:20]
                rc = event.get("returncode", 0)
                att = event.get("attempts", 1)
                source_validated = event.get("source_id_validated_after_not_found")
                probe_rc = event.get("source_list_probe_returncode")
                match_idx = event.get("source_list_probe_match_index")
                match_title = str(event.get("source_list_probe_match_title") or "")[:24]
                marker = classification.get("matched_marker") or ""
                ec = classification["error_class"]
                rec = classification["retry_recommendation"]
                lines.append(f"| {i+1} | {vid} | {sid} | {rc} | {att} | {source_validated} | {probe_rc} | {match_idx} | {match_title} | {marker} | {ec} | {rec} |")

            if len(run["events"]) > 20:
                lines.append(f"_... and {len(run['events']) - 20} more events_")

        if run["sufficiency"] != "has_event_level_data":
            lines.append("")
            if run["sufficiency"] == "summary_counts_only":
                lines.append(
                    "> **Insufficient data for retry-policy changes.** "
                    "Runs contain only aggregate content_fetch_status_counts, "
                    "not per-source event-level data with stdout/stderr. "
                    "Run a new instrumented probe to collect detailed events."
                )
            elif run["sufficiency"] == "no_content_fetch_events":
                lines.append(
                    "> **No content-fetch events found.** "
                    "Check that the run actually processed sources through nlm_batch."
                )

        lines.append("")

    return "\n".join(lines)


def _format_text(runs: list[dict]) -> str:
    lines = []
    for run in runs:
        lines.append(f"Run: {run['run_name']}")
        lines.append(f"  Sufficiency: {run['sufficiency']}")
        lines.append(f"  Events: {run['event_count']}")
        lines.append(f"  Auth/permission: {run['auth_or_permission_count']}")
        lines.append(f"  Transient retry candidates: {run['transient_retry_candidate_count']}")
        if run["summary_counts"]:
            lines.append(f"  Summary counts: {run['summary_counts']}")
        if run["class_counts"]:
            lines.append("  Class distribution:")
            for cls, cnt in sorted(run["class_counts"].items(), key=lambda x: -x[1]):
                lines.append(f"    {cls}: {cnt}")
        lines.append("")
    return "\n".join(lines)
