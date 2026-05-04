"""Post-run failure analysis for NotebookLM sharded benchmark runs."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


REQUIRED_SUMMARY_NAME = "sharded_lane_series_summary.json"


@dataclass(frozen=True)
class RunFailureAnalysis:
    run_root: Path
    summary_path: Path | None
    jsonl_file_count: int
    unique_failed_video_ids: tuple[str, ...]
    unique_failed_source_ids: tuple[str, ...]
    unique_notebook_ids: tuple[str, ...]
    not_found_count: int
    command_failed_count: int
    default_profile_reap_count: int
    recovery_event_count: int
    pre_recovery_failure_count: int
    post_recovery_failure_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_root": str(self.run_root),
            "summary_path": None if self.summary_path is None else str(self.summary_path),
            "jsonl_file_count": self.jsonl_file_count,
            "unique_failed_video_ids": list(self.unique_failed_video_ids),
            "unique_failed_source_ids": list(self.unique_failed_source_ids),
            "unique_notebook_ids": list(self.unique_notebook_ids),
            "not_found_count": self.not_found_count,
            "command_failed_count": self.command_failed_count,
            "default_profile_reap_count": self.default_profile_reap_count,
            "recovery_event_count": self.recovery_event_count,
            "pre_recovery_failure_count": self.pre_recovery_failure_count,
            "post_recovery_failure_count": self.post_recovery_failure_count,
        }


def _iter_jsonl_paths(run_root: Path) -> list[Path]:
    if not run_root.exists():
        return []
    return sorted(path for path in run_root.rglob("*.jsonl") if path.is_file())


def _flatten_text_values(value: object) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        values.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            values.extend(_flatten_text_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(_flatten_text_values(item))
    return values


def _event_contains_text(event: dict[str, object], needle: str) -> bool:
    combined = "\n".join(_flatten_text_values(event)).upper()
    return needle.upper() in combined


def _collect_ids(event: dict[str, object], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys:
                    child_value = str(child or "").strip()
                    if child_value:
                        values.append(child_value)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(event)
    return values


def analyze_run_root(run_root: Path) -> RunFailureAnalysis:
    run_root = Path(run_root)
    summary_path = run_root / REQUIRED_SUMMARY_NAME
    jsonl_paths = _iter_jsonl_paths(run_root)
    unique_failed_video_ids: set[str] = set()
    unique_failed_source_ids: set[str] = set()
    unique_notebook_ids: set[str] = set()
    not_found_count = 0
    command_failed_count = 0
    default_profile_reap_count = 0
    recovery_event_count = 0
    pre_recovery_failure_count = 0
    post_recovery_failure_count = 0

    for path in jsonl_paths:
        recovery_seen = False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            action = str(event.get("action") or "")
            data = event.get("data")
            if isinstance(data, dict):
                for key in ("video_id", "source_id", "nb_id", "notebook_id", "old_nb_id", "new_nb_id"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        if key in {"nb_id", "notebook_id", "old_nb_id", "new_nb_id"}:
                            unique_notebook_ids.add(value)
                        elif key == "video_id":
                            unique_failed_video_ids.add(value)
                        elif key == "source_id":
                            unique_failed_source_ids.add(value)

            if action == "nlm_batch_dead_notebook_recreated":
                recovery_event_count += 1
                recovery_seen = True
                if isinstance(data, dict):
                    for key in ("old_nb_id", "nb_id"):
                        value = str(data.get(key) or "").strip()
                        if value:
                            unique_notebook_ids.add(value)

            if action == "nlm_batch_source_content_fetch_completed":
                if isinstance(data, dict):
                    status = str(data.get("status") or "").strip()
                    if status == "command_failed":
                        command_failed_count += 1
                    if _event_contains_text(event, "NOT_FOUND"):
                        not_found_count += 1
                    video_id = str(data.get("video_id") or "").strip()
                    source_id = str(data.get("source_id") or "").strip()
                    nb_id = str(data.get("nb_id") or "").strip()
                    if video_id:
                        unique_failed_video_ids.add(video_id)
                    if source_id:
                        unique_failed_source_ids.add(source_id)
                    if nb_id:
                        unique_notebook_ids.add(nb_id)
                    if recovery_seen:
                        post_recovery_failure_count += 1
                    else:
                        pre_recovery_failure_count += 1

            if _event_contains_text(event, "default_profile_reaped"):
                default_profile_reap_count += 1

    return RunFailureAnalysis(
        run_root=run_root,
        summary_path=summary_path if summary_path.exists() else None,
        jsonl_file_count=len(jsonl_paths),
        unique_failed_video_ids=tuple(sorted(unique_failed_video_ids)),
        unique_failed_source_ids=tuple(sorted(unique_failed_source_ids)),
        unique_notebook_ids=tuple(sorted(unique_notebook_ids)),
        not_found_count=not_found_count,
        command_failed_count=command_failed_count,
        default_profile_reap_count=default_profile_reap_count,
        recovery_event_count=recovery_event_count,
        pre_recovery_failure_count=pre_recovery_failure_count,
        post_recovery_failure_count=post_recovery_failure_count,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize NotebookLM benchmark failures under a run root.")
    parser.add_argument("--run-root", required=True, type=Path, help="Benchmark run root to inspect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    analysis = analyze_run_root(args.run_root)
    print(json.dumps(analysis.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
