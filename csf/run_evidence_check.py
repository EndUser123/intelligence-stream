"""Post-run evidence validation for NotebookLM sharded benchmarks."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


REQUIRED_SUMMARY_NAME = "sharded_lane_series_summary.json"
FORBIDDEN_MARKERS = (
    "default_profile_running",
    "source_add_failed",
    "nlm_batch_subbatch_add_split_circuit_opened",
)
OPTIONAL_REQUIRED_MARKERS = ("nlm_auth_forced_refresh_scheduled",)


@dataclass(frozen=True)
class EvidenceCheckResult:
    ok: bool
    summary_path: Path | None
    reasons: tuple[str, ...]


def _iter_jsonl_paths(run_root: Path) -> list[Path]:
    if not run_root.exists():
        return []
    return sorted(path for path in run_root.rglob("*.jsonl") if path.is_file())


def _event_values(value: object) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(_event_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_event_values(item))
    return values


def _event_has_marker(event: dict[str, object], marker: str) -> bool:
    values = _event_values(event.get("action"))
    values.extend(_event_values(event.get("data")))
    values.extend(_event_values(event.get("status")))
    return any(value == marker for value in values)


def _jsonl_path_has_marker(path: Path, marker: str) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and _event_has_marker(event, marker):
                return True
    except OSError:
        return False
    return False


def inspect_run_root(run_root: Path, *, require_forced_refresh_marker: bool = False) -> EvidenceCheckResult:
    run_root = Path(run_root)
    summary_path = run_root / REQUIRED_SUMMARY_NAME
    reasons: list[str] = []

    if not run_root.exists():
        reasons.append(f"run root does not exist: {run_root}")
        return EvidenceCheckResult(False, None, tuple(reasons))

    if not summary_path.exists():
        reasons.append(f"missing summary: {summary_path}")

    jsonl_paths = _iter_jsonl_paths(run_root)
    if not jsonl_paths:
        reasons.append(f"no jsonl logs found under {run_root}")
    else:
        for marker in FORBIDDEN_MARKERS:
            marker_hits = [path for path in jsonl_paths if _jsonl_path_has_marker(path, marker)]
            if marker_hits:
                reasons.append(f"forbidden marker {marker} found in {len(marker_hits)} jsonl file(s)")

        if require_forced_refresh_marker:
            forced_hits = [path for path in jsonl_paths if _jsonl_path_has_marker(path, OPTIONAL_REQUIRED_MARKERS[0])]
            if not forced_hits:
                reasons.append("missing required marker nlm_auth_forced_refresh_scheduled")

    ok = not reasons
    return EvidenceCheckResult(ok, summary_path if summary_path.exists() else None, tuple(reasons))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate NotebookLM benchmark evidence under a run root.")
    parser.add_argument("--run-root", required=True, type=Path, help="Benchmark run root to inspect.")
    parser.add_argument(
        "--require-forced-refresh-marker",
        action="store_true",
        help="Require nlm_auth_forced_refresh_scheduled to appear in the run logs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = inspect_run_root(args.run_root, require_forced_refresh_marker=args.require_forced_refresh_marker)
    if result.ok:
        print(f"[evidence] ok summary={result.summary_path}")
        return 0
    for reason in result.reasons:
        print(f"[evidence] ERROR: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
