"""Concurrent NotebookLM lane sharding benchmark runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from csf.breadth_series import _aggregate_summary
from csf.load_ladder import build_fallback_benchmark_command
from csf import nlm_auth_guard
from csf.nlm_worker_auth import (
    expected_email_for_profile,
    doctor_lane_setup,
    family_for_profile,
    refresh_source_profile,
    sync_worker_profiles,
)

run_nlm = nlm_auth_guard.run_nlm


REPO_ROOT = Path(__file__).resolve().parents[1]
FALLBACK_BENCHMARK_SCRIPT = REPO_ROOT / "bin" / "csf-fallback-crossover-benchmark"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".logs" / "sharded_lane_series"
DEFAULT_TRACE_ROOT = REPO_ROOT / ".logs" / "worker_count_trials"
DEFAULT_COHORT_JSON = DEFAULT_OUTPUT_ROOT / "cohort.json"
DEFAULT_SOURCE_URL = "https://www.youtube.com/channel/UCYTISFALLBACKBMK"
DEFAULT_POLICY = "notebooklm_route_plus_fallback_30s_1w"
DEFAULT_LIMIT = 400
DEFAULT_BATCH_SIZE = 200
DEFAULT_MANIFEST_JSON = REPO_ROOT / "tests" / "fixtures" / "shared_benchmark_manifest.json"
DEFAULT_REUSABLE_PIPELINE_MODE = "serial"
DEFAULT_NLM_CHROME_PROFILE_ROOT = nlm_auth_guard.DEFAULT_NLM_CHROME_PROFILE_ROOT


@dataclass(frozen=True, slots=True)
class LaneConfig:
    """A single independent NotebookLM execution lane."""

    lane: str
    account_class: str
    workers: int
    notebooklm_profile_prefix: str
    browser_profile_root: Path
    worker_state_root: Path
    notebook_prefix: str
    notebooklm_profiles: tuple[str, ...] = ()
    expected_email: str = ""
    browser_profile_directory: str = ""
    coordinator_notebooklm_profile: str | None = None
    startup_delay_s: float = 0.0

    @property
    def coordinator_profile(self) -> str:
        if self.coordinator_notebooklm_profile:
            return self.coordinator_notebooklm_profile
        if self.notebooklm_profiles:
            return self.notebooklm_profiles[0]
        return f"{self.notebooklm_profile_prefix}-01"


def _normalize_path(value: object) -> Path:
    return Path(str(value or "").strip())


def _lane_from_dict(raw: dict[str, object]) -> LaneConfig:
    lane = str(raw.get("lane") or "").strip()
    if not lane:
        raise ValueError("lane is required")
    workers = int(raw.get("workers") or 0)
    if workers < 1:
        raise ValueError(f"lane {lane}: workers must be >= 1")
    profile_prefix = str(raw.get("notebooklm_profile_prefix") or "").strip()
    raw_profiles = raw.get("notebooklm_profiles") or []
    if not isinstance(raw_profiles, list):
        raise ValueError(f"lane {lane}: notebooklm_profiles must be a list")
    profiles = tuple(str(item).strip() for item in raw_profiles if str(item).strip())
    if not profile_prefix and not profiles:
        raise ValueError(f"lane {lane}: notebooklm_profile_prefix or notebooklm_profiles is required")
    if profiles and len(profiles) < workers:
        raise ValueError(f"lane {lane}: notebooklm_profiles must include at least {workers} profiles")
    notebook_prefix = str(raw.get("notebook_prefix") or "").strip()
    if not notebook_prefix:
        raise ValueError(f"lane {lane}: notebook_prefix is required")
    browser_profile_root = _normalize_path(raw.get("browser_profile_root"))
    if not str(browser_profile_root):
        raise ValueError(f"lane {lane}: browser_profile_root is required")
    worker_state_root = _normalize_path(raw.get("worker_state_root"))
    if not str(worker_state_root):
        raise ValueError(f"lane {lane}: worker_state_root is required")
    coordinator_profile = str(raw.get("coordinator_notebooklm_profile") or "").strip() or None
    expected_email = str(raw.get("expected_email") or "").strip().lower()
    startup_delay_s = float(raw.get("startup_delay_s") or 0.0)
    if startup_delay_s < 0:
        raise ValueError(f"lane {lane}: startup_delay_s must be >= 0")
    return LaneConfig(
        lane=lane,
        account_class=str(raw.get("account_class") or lane).strip(),
        workers=workers,
        notebooklm_profile_prefix=profile_prefix,
        notebooklm_profiles=profiles,
        browser_profile_root=browser_profile_root,
        worker_state_root=worker_state_root,
        notebook_prefix=notebook_prefix,
        browser_profile_directory=str(raw.get("browser_profile_directory") or "").strip(),
        expected_email=expected_email,
        coordinator_notebooklm_profile=coordinator_profile,
        startup_delay_s=startup_delay_s,
    )


def _validate_lanes(lanes: Iterable[LaneConfig]) -> tuple[LaneConfig, ...]:
    lane_tuple = tuple(lanes)
    if not lane_tuple:
        raise ValueError("at least one lane is required")
    seen: dict[str, set[str]] = {
        "lane": set(),
        "notebooklm_profile_namespace": set(),
        "browser_profile_namespace": set(),
        "worker_state_root": set(),
        "notebook_prefix": set(),
    }
    for lane in lane_tuple:
        profile_namespace = ",".join(lane.notebooklm_profiles) if lane.notebooklm_profiles else lane.notebooklm_profile_prefix
        browser_namespace = str(lane.browser_profile_root / lane.browser_profile_directory) if lane.browser_profile_directory else str(lane.browser_profile_root)
        values = {
            "lane": lane.lane,
            "notebooklm_profile_namespace": profile_namespace,
            "browser_profile_namespace": browser_namespace,
            "worker_state_root": str(lane.worker_state_root),
            "notebook_prefix": lane.notebook_prefix,
        }
        for field, value in values.items():
            if value in seen[field]:
                raise ValueError(f"duplicate lane {field}: {value}")
            seen[field].add(value)
    return lane_tuple


def _extract_account(stdout: str, stderr: str = "") -> str:
    for line in f"{stdout}\n{stderr}".splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("account:"):
            return stripped.split(":", 1)[1].strip().lower()
    return ""


def _is_nlm_auth_noninteractive() -> bool:
    value = os.getenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _default_chrome_profile_pids() -> set[int]:
    return nlm_auth_guard.default_chrome_profile_pids()


def _stop_chrome_pids(pids: set[int]) -> None:
    nlm_auth_guard.stop_chrome_pids(pids)


def _stop_default_chrome_profile_if_running(*, stage: str) -> bool:
    """Close the shared legacy NLM Chrome profile and report whether cleanup happened."""
    pids = _default_chrome_profile_pids()
    if not pids:
        return False
    _stop_chrome_pids(pids)
    print(
        f"[sharded] closed default NotebookLM chrome-profile at {stage}: "
        f"{DEFAULT_NLM_CHROME_PROFILE_ROOT} pids={sorted(pids)}",
        file=sys.stderr,
    )
    return True


def _lane_auth_profiles(lane: LaneConfig) -> list[str]:
    profiles: list[str] = [lane.coordinator_profile]
    if lane.notebooklm_profiles:
        profiles.extend(lane.notebooklm_profiles[: lane.workers])
    else:
        profiles.extend(f"{lane.notebooklm_profile_prefix}-{idx:02d}" for idx in range(1, lane.workers + 1))
    unique: list[str] = []
    for profile in profiles:
        if profile and profile not in unique:
            unique.append(profile)
    return unique


def _lane_expected_email(lane: LaneConfig, profile: str) -> str:
    explicit = lane.expected_email.strip().lower()
    if explicit:
        return explicit
    return expected_email_for_profile(profile).strip().lower()


def preflight_lane_auth_profiles(lanes: Iterable[LaneConfig], *, timeout_s: float = 30.0) -> None:
    """Validate all lane NotebookLM profiles before starting a benchmark run."""
    _stop_default_chrome_profile_if_running(stage="preflight_start")
    checked: set[str] = set()
    for lane in _validate_lanes(lanes):
        for profile in _lane_auth_profiles(lane):
            if profile in checked:
                continue
            checked.add(profile)
            expected_email = _lane_expected_email(lane, profile)
            if not expected_email:
                raise RuntimeError(
                    f"lane {lane.lane}: profile {profile} has no expected email mapping; "
                    "add expected_email to the lane config or update the auth-family map"
                )
            if _profile_auth_check(profile, expected_email=expected_email, timeout_s=timeout_s):
                continue
            if not _profile_auth_force_refresh(profile, expected_email=expected_email, timeout_s=max(120.0, timeout_s)):
                raise RuntimeError(f"NotebookLM auth expired for profile {profile} and force refresh failed")
            if _stop_default_chrome_profile_if_running(stage=f"preflight_refresh_{profile}"):
                raise RuntimeError(
                    f"NotebookLM auth refresh for profile {profile} opened the default chrome-profile"
                )


def _profile_auth_check(profile: str, *, expected_email: str, timeout_s: float) -> bool:
    if _stop_default_chrome_profile_if_running(stage=f"auth_check_before_{profile}"):
        return False
    res = run_nlm(["login", "--check", "--profile", profile], timeout_s=timeout_s)
    if _stop_default_chrome_profile_if_running(stage=f"auth_check_after_{profile}"):
        return False
    if res.returncode != 0:
        return False
    if not expected_email:
        return False
    return _extract_account(res.stdout or "", res.stderr or "") == expected_email.lower()


def _profile_auth_force_refresh(profile: str, *, expected_email: str, timeout_s: float) -> bool:
    family = family_for_profile(profile)
    if family is not None:
        try:
            if not refresh_source_profile(family, timeout_s=timeout_s):
                return False
            sync_worker_profiles(families=(family,), backup=True)
        except Exception:
            return False
        return _profile_auth_check(profile, expected_email=expected_email, timeout_s=timeout_s)

    if _stop_default_chrome_profile_if_running(stage=f"auth_refresh_before_{profile}"):
        return False
    res = run_nlm(["login", "--force", "--profile", profile], timeout_s=timeout_s)
    if _stop_default_chrome_profile_if_running(stage=f"auth_refresh_after_{profile}"):
        return False
    if res.returncode != 0:
        return False
    if not expected_email:
        return False
    return _extract_account(res.stdout or "", res.stderr or "") == expected_email.lower()


def _iter_jsonl_events(root: Path) -> Iterable[tuple[Path, int, dict[str, Any]]]:
    for path in root.rglob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield path, lineno, event


def _find_invalid_lane_artifacts(lane_output_root: Path) -> list[str]:
    """Find hard invalidation markers that make benchmark throughput untrustworthy."""
    findings: list[str] = []
    for path, lineno, event in _iter_jsonl_events(lane_output_root):
        action = str(event.get("action") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        status = str(data.get("status") or "")
        if action == "nlm_auth_failed" and status == "default_profile_running":
            findings.append(
                f"{path.relative_to(lane_output_root)}:{lineno}: default_profile_running "
                f"profile={data.get('notebooklm_profile') or '<unknown>'}"
            )
        if action == "nlm_batch_subbatch_add_failed" and data.get("failure_reason") in {"source_add_failed", "source_count_probe_failed"}:
            findings.append(
                f"{path.relative_to(lane_output_root)}:{lineno}: {data.get('failure_reason')} "
                f"subbatch_size={data.get('subbatch_size') or '<unknown>'}"
            )
        if action == "nlm_batch_subbatch_zero_growth_terminal":
            findings.append(
                f"{path.relative_to(lane_output_root)}:{lineno}: zero_growth_source_add "
                f"subbatch_size={data.get('subbatch_size') or '<unknown>'} "
                f"sources={data.get('source_count_before') or 0}->{data.get('source_count_after') or 0}"
            )
        if action == "nlm_batch_subbatch_source_count_probe_terminal":
            findings.append(
                f"{path.relative_to(lane_output_root)}:{lineno}: source_count_probe_failed "
                f"subbatch_size={data.get('subbatch_size') or '<unknown>'} "
                f"sources={data.get('source_count_before') or 0}->{data.get('source_count_after') or 0}"
            )
    return findings


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a temporary file so interrupted runs do not leave partial output."""
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _tail_text(path: Path, max_chars: int = 2000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _write_lane_process_snapshot(path: Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(path, payload)


def load_lane_configs(path: Path) -> tuple[LaneConfig, ...]:
    """Load and validate lane configs from JSON."""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Older lane config files encode Windows paths with over-escaped backslashes.
        # Normalize any 3+ run of backslashes down to a valid JSON escape sequence.
        repaired_text = re.sub(r"\\{3,}", r"\\\\", text)
        data = json.loads(repaired_text)
    if not isinstance(data, list):
        raise ValueError("lane config must be a JSON list")
    lanes = [_lane_from_dict(item) for item in data if isinstance(item, dict)]
    if len(lanes) != len(data):
        raise ValueError("each lane config item must be an object")
    return _validate_lanes(lanes)


def _lane_env(
    base_env: dict[str, str],
    lane: LaneConfig,
    reusable_pipeline_mode: str,
    *,
    lane_output_root: Path,
) -> dict[str, str]:
    # Defense-in-depth: strip vars that would contaminate per-run auth behavior.
    # The current run01/run02/run03 auth-regression diagnosis is still evidence-gated,
    # but stripping known stress knobs prevents accidental cross-run inheritance.
    _AMBUSH_VARS = {"YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS"}
    for var in _AMBUSH_VARS:
        base_env = {k: v for k, v in base_env.items() if k != var}
    env = dict(base_env)
    env["NOTEBOOKLM_PROFILE"] = lane.coordinator_profile
    env["INTELLIGENCE_STREAM_LOG_DIR"] = str(lane_output_root / "logs")
    env["YTIS_NLM_BROWSER_PROFILE_ROOT"] = str(lane.browser_profile_root)
    if lane.browser_profile_directory:
        env["YTIS_NLM_BROWSER_PROFILE_DIRECTORY"] = lane.browser_profile_directory
    else:
        env.pop("YTIS_NLM_BROWSER_PROFILE_DIRECTORY", None)
    env["YTIS_BATCH_STATUS_DB_PATH"] = str(lane_output_root / "batch_status.sqlite")
    if lane.expected_email:
        env["YTIS_NLM_EXPECTED_EMAIL"] = lane.expected_email
    else:
        env.pop("YTIS_NLM_EXPECTED_EMAIL", None)
    env["YTIS_INDUSTRIAL_WORKER_STATE_ROOT"] = str(lane.worker_state_root)
    env["YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX"] = lane.notebook_prefix
    env["YTIS_BENCHMARK_WORKER_NOTEBOOK_PREFIX"] = lane.notebook_prefix
    if lane.notebooklm_profile_prefix:
        env["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX"] = lane.notebooklm_profile_prefix
    else:
        env.pop("YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILE_PREFIX", None)
    if lane.notebooklm_profiles:
        env["YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES"] = ",".join(lane.notebooklm_profiles)
    else:
        env.pop("YTIS_INDUSTRIAL_WORKER_NOTEBOOKLM_PROFILES", None)
    if reusable_pipeline_mode:
        env["YTIS_REUSABLE_PIPELINE_MODE"] = reusable_pipeline_mode
    env["YTIS_NLM_AUTH_NONINTERACTIVE"] = "1"
    return env


def _run_lane(
    *,
    lane: LaneConfig,
    trace_root: Path,
    output_root: Path,
    cohort_json: Path,
    source_url: str,
    policy: str,
    limit: int,
    batch_size: int,
    manifest_json: Path,
    python_executable: str | None,
    reusable_pipeline_mode: str,
    env: dict[str, str],
) -> dict[str, Any]:
    lane_output_root = output_root / lane.lane
    lane_output_root.mkdir(parents=True, exist_ok=True)
    lane_cohort_json = cohort_json.parent / f"{cohort_json.stem}.{lane.lane}{cohort_json.suffix}"
    lane_stdout_path = lane_output_root / "lane.stdout.txt"
    lane_stderr_path = lane_output_root / "lane.stderr.txt"
    lane_process_path = lane_output_root / "lane_process.json"
    started_at = time.monotonic()
    if lane.startup_delay_s > 0:
        time.sleep(lane.startup_delay_s)
    _stop_default_chrome_profile_if_running(stage=f"lane_start_{lane.lane}")
    command = build_fallback_benchmark_command(
        python_executable=python_executable or sys.executable,
        fallback_benchmark_script=FALLBACK_BENCHMARK_SCRIPT,
        trace_root=trace_root,
        cohort_json=lane_cohort_json,
        output_root=lane_output_root,
        source_url=source_url,
        workers=lane.workers,
        limit=limit,
        batch_size=batch_size,
        policy=policy,
        cohort_shape="captioned",
        sample_label=f"shard_{lane.lane}",
        manifest_json=None,
        manifest_families=None,
        worker_state_root=lane.worker_state_root,
        preserve_worker_state_root=False,
    )
    process_snapshot: dict[str, Any] = {
        "lane": lane.lane,
        "command": command,
        "cwd": str(REPO_ROOT),
        "output_root": str(lane_output_root),
        "started_at": round(started_at, 3),
        "status": "starting",
        "pid": None,
        "returncode": None,
    }
    _write_lane_process_snapshot(lane_process_path, process_snapshot)
    proc: subprocess.Popen[str] | None = None
    returncode: int | None = None
    try:
        with lane_stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_handle, lane_stderr_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stderr_handle:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(REPO_ROOT),
                    env=env,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                )
            except BaseException as exc:
                process_snapshot.update(
                    {
                        "status": "launch_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "finished_at": round(time.monotonic(), 3),
                    }
                )
                _write_lane_process_snapshot(lane_process_path, process_snapshot)
                raise
            process_snapshot.update({"status": "running", "pid": proc.pid})
            _write_lane_process_snapshot(lane_process_path, process_snapshot)
            try:
                returncode = proc.wait()
            except BaseException as exc:
                process_snapshot.update(
                    {
                        "status": "wait_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "finished_at": round(time.monotonic(), 3),
                        "pid": proc.pid,
                    }
                )
                _write_lane_process_snapshot(lane_process_path, process_snapshot)
                raise
            finally:
                stdout_handle.flush()
                stderr_handle.flush()
    finally:
        _stop_default_chrome_profile_if_running(stage=f"lane_complete_{lane.lane}")
    finished_at = time.monotonic()
    process_snapshot.update(
        {
            "status": "completed" if returncode == 0 else "failed",
            "returncode": returncode,
            "finished_at": round(finished_at, 3),
            "wall_elapsed_s": round(finished_at - started_at, 3),
            "pid": proc.pid if proc is not None else process_snapshot.get("pid"),
        }
    )
    _write_lane_process_snapshot(lane_process_path, process_snapshot)
    summary_path = lane_output_root / "benchmark_summary.json"
    if returncode != 0:
        raise RuntimeError(f"lane {lane.lane} failed with returncode={returncode}")
    if not summary_path.exists():
        raise RuntimeError(f"lane {lane.lane} missing benchmark summary: {summary_path}")
    invalid_artifacts = _find_invalid_lane_artifacts(lane_output_root)
    if invalid_artifacts:
        sample = "; ".join(invalid_artifacts[:5])
        raise RuntimeError(
            f"lane {lane.lane} invalidated by NotebookLM auth/source failures: {sample}"
        )
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    aggregate = _aggregate_summary(summary, policy)
    return {
        "status": "ok",
        "lane": lane.lane,
        "account_class": lane.account_class,
        "workers": lane.workers,
        "notebooklm_profile_prefix": lane.notebooklm_profile_prefix,
        "notebooklm_profiles": list(lane.notebooklm_profiles),
        "coordinator_notebooklm_profile": lane.coordinator_profile,
        "browser_profile_root": str(lane.browser_profile_root),
        "browser_profile_directory": lane.browser_profile_directory,
        "worker_state_root": str(lane.worker_state_root),
        "notebook_prefix": lane.notebook_prefix,
        "startup_delay_s": lane.startup_delay_s,
        "started_at": round(started_at, 3),
        "finished_at": round(finished_at, 3),
        "wall_elapsed_s": round(finished_at - started_at, 3),
        "returncode": proc.returncode,
        "command": command,
        "output_root": str(lane_output_root),
        "stdout_path": str(lane_stdout_path),
        "stderr_path": str(lane_stderr_path),
        "lane_process_path": str(lane_process_path),
        "benchmark_summary_path": str(summary_path),
        "aggregate": aggregate,
        **aggregate,
    }


def _invalidated_lane_report(
    *,
    lane: LaneConfig,
    output_root: Path,
    exc: BaseException,
    traceback_text: str,
) -> dict[str, Any]:
    lane_output_root = output_root / lane.lane
    return {
        "report_version": 1,
        "status": "invalidated",
        "lane": lane.lane,
        "account_class": lane.account_class,
        "workers": lane.workers,
        "notebooklm_profile_prefix": lane.notebooklm_profile_prefix,
        "notebooklm_profiles": list(lane.notebooklm_profiles),
        "coordinator_notebooklm_profile": lane.coordinator_profile,
        "browser_profile_root": str(lane.browser_profile_root),
        "browser_profile_directory": lane.browser_profile_directory,
        "worker_state_root": str(lane.worker_state_root),
        "notebook_prefix": lane.notebook_prefix,
        "startup_delay_s": lane.startup_delay_s,
        "output_root": str(lane_output_root),
        "stdout_path": str(lane_output_root / "lane.stdout.txt"),
        "stderr_path": str(lane_output_root / "lane.stderr.txt"),
        "lane_process_path": str(lane_output_root / "lane_process.json"),
        "benchmark_summary_path": str(lane_output_root / "benchmark_summary.json"),
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback_text,
        "stderr_tail": _tail_text(lane_output_root / "lane.stderr.txt"),
        "hot_path_success_count_total": 0,
        "transcript_fallback_success_count_total": 0,
        "fail_count_total": 0,
        "processed_count_total": 0,
    }


def compute_combined_hot_path_vph(lane_reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compute combined sharded throughput using concurrent wall-clock span."""
    reports = list(lane_reports)
    if not reports:
        raise ValueError("at least one lane report is required")
    started_at = min(float(report["started_at"]) for report in reports)
    finished_at = max(float(report["finished_at"]) for report in reports)
    wall_elapsed_s = round(finished_at - started_at, 3)
    hot_path_success = sum(int(report.get("hot_path_success_count_total") or 0) for report in reports)
    fallback_success = sum(int(report.get("transcript_fallback_success_count_total") or 0) for report in reports)
    fail_count = sum(int(report.get("fail_count_total") or 0) for report in reports)
    processed_count = sum(int(report.get("processed_count_total") or 0) for report in reports)
    return {
        "lane_count": len(reports),
        "started_at": round(started_at, 3),
        "finished_at": round(finished_at, 3),
        "wall_elapsed_s": wall_elapsed_s,
        "hot_path_success_count_total": hot_path_success,
        "transcript_fallback_success_count_total": fallback_success,
        "fail_count_total": fail_count,
        "processed_count_total": processed_count,
        "hot_path_videos_per_hour": round(hot_path_success / wall_elapsed_s * 3600.0, 2) if wall_elapsed_s > 0 else 0.0,
        "transcript_fallback_videos_per_hour": round(fallback_success / wall_elapsed_s * 3600.0, 2) if wall_elapsed_s > 0 else 0.0,
        "processed_per_hour": round(processed_count / wall_elapsed_s * 3600.0, 2) if wall_elapsed_s > 0 else 0.0,
    }


def _empty_combined_hot_path_vph() -> dict[str, Any]:
    return {
        "lane_count": 0,
        "started_at": 0.0,
        "finished_at": 0.0,
        "wall_elapsed_s": 0.0,
        "hot_path_success_count_total": 0,
        "transcript_fallback_success_count_total": 0,
        "fail_count_total": 0,
        "processed_count_total": 0,
        "hot_path_videos_per_hour": 0.0,
        "transcript_fallback_videos_per_hour": 0.0,
        "processed_per_hour": 0.0,
    }


def run_sharded_lane_series(
    *,
    lanes: Iterable[LaneConfig],
    trace_root: Path = DEFAULT_TRACE_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    cohort_json: Path = DEFAULT_COHORT_JSON,
    source_url: str = DEFAULT_SOURCE_URL,
    policy: str = DEFAULT_POLICY,
    limit: int = DEFAULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    manifest_json: Path = DEFAULT_MANIFEST_JSON,
    python_executable: str | None = None,
    reusable_pipeline_mode: str = DEFAULT_REUSABLE_PIPELINE_MODE,
) -> dict[str, Any]:
    """Run all NotebookLM lanes concurrently and aggregate hot-path VPH."""
    lane_configs = _validate_lanes(lanes)
    output_root.mkdir(parents=True, exist_ok=True)
    cohort_json.parent.mkdir(parents=True, exist_ok=True)
    report_path = output_root / "sharded_lane_series_summary.json"
    base_env = os.environ.copy()

    lane_reports_by_name: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(lane_configs)) as executor:
        futures = {
            executor.submit(
                _run_lane,
                lane=lane,
                trace_root=trace_root,
                output_root=output_root,
                cohort_json=cohort_json,
                source_url=source_url,
                policy=policy,
                limit=limit,
                batch_size=batch_size,
                manifest_json=manifest_json,
                python_executable=python_executable,
                reusable_pipeline_mode=reusable_pipeline_mode,
                env=_lane_env(
                    base_env,
                    lane,
                    reusable_pipeline_mode,
                    lane_output_root=output_root / lane.lane,
                ),
            ): lane
            for lane in lane_configs
        }
        for future in as_completed(futures):
            lane = futures[future]
            try:
                lane_reports_by_name[lane.lane] = future.result()
            except Exception as exc:
                traceback_text = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).strip()
                lane_reports_by_name[lane.lane] = _invalidated_lane_report(
                    lane=lane,
                    output_root=output_root,
                    exc=exc,
                    traceback_text=traceback_text,
                )
                failures.append(
                    {
                        "lane": lane.lane,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback_text,
                        "stderr_tail": _tail_text(output_root / lane.lane / "lane.stderr.txt"),
                    }
                )

    lane_reports = [lane_reports_by_name[lane.lane] for lane in lane_configs]
    successful_lane_reports = [report for report in lane_reports if report.get("status", "ok") == "ok"]
    combined = (
        compute_combined_hot_path_vph(successful_lane_reports)
        if successful_lane_reports
        else _empty_combined_hot_path_vph()
    )
    status = "ok" if not failures else "invalidated"
    report = {
        "report_version": 1,
        "status": status,
        "invalidated": bool(failures),
        "attempted_lane_count": len(lane_reports),
        "successful_lane_count": len(successful_lane_reports),
        "failure_count": len(failures),
        "failures": failures,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metric_contract": "combined_hot_path_videos_per_hour_excludes_whisper",
        "trace_root": str(trace_root),
        "cohort_json": str(cohort_json),
        "source_url": source_url,
        "policy": policy,
        "limit": limit,
        "batch_size": batch_size,
        "reusable_pipeline_mode": reusable_pipeline_mode,
        "lanes": [asdict(lane) | {"browser_profile_root": str(lane.browser_profile_root), "worker_state_root": str(lane.worker_state_root)} for lane in lane_configs],
        "runs": lane_reports,
        "combined": combined,
    }
    report["report_path"] = str(report_path)
    _write_json_atomic(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run concurrent NotebookLM lane sharding benchmark")
    parser.add_argument("--lane-config", required=True, type=Path, help="JSON list of lane configs")
    parser.add_argument("--trace-root", type=Path, default=DEFAULT_TRACE_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--cohort-json", type=Path, default=DEFAULT_COHORT_JSON)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--policy", default=DEFAULT_POLICY)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--manifest-json", type=Path, default=DEFAULT_MANIFEST_JSON)
    parser.add_argument("--python-executable", default=None)
    parser.add_argument("--reusable-pipeline-mode", default=DEFAULT_REUSABLE_PIPELINE_MODE)
    args = parser.parse_args(argv)

    try:
        lanes = doctor_lane_setup(args.lane_config, args.output_root)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[sharded] ERROR: {exc}")
        return 1
    report = run_sharded_lane_series(
        lanes=lanes,
        trace_root=args.trace_root,
        output_root=args.output_root,
        cohort_json=args.cohort_json,
        source_url=args.source_url,
        policy=args.policy,
        limit=args.limit,
        batch_size=args.batch_size,
        manifest_json=args.manifest_json,
        python_executable=args.python_executable,
        reusable_pipeline_mode=args.reusable_pipeline_mode,
    )
    combined = report["combined"]
    print(
        "[sharded] combined_hot_vph={vph:.2f} hot_success={success} "
        "fail={fail} wall_elapsed_s={elapsed:.1f}".format(
            vph=float(combined["hot_path_videos_per_hour"]),
            success=int(combined["hot_path_success_count_total"]),
            fail=int(combined["fail_count_total"]),
            elapsed=float(combined["wall_elapsed_s"]),
        )
    )
    print(f"[sharded] summary={report['report_path']}")
    if report.get("status") != "ok":
        first_failure = report.get("failures", [{}])[0] if report.get("failures") else {}
        print(
            "[sharded] status={status} failures={failures} first_failure={lane}:{error}".format(
                status=report.get("status"),
                failures=int(report.get("failure_count") or 0),
                lane=str(first_failure.get("lane") or ""),
                error=str(first_failure.get("error") or ""),
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
