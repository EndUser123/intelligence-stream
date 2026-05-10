"""Execute one NotebookLM industrial batch in an isolated worker process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from csf.batch_status import mark_complete, summarize_video_ids
from csf.cache import set_cached_transcript
from csf.csf_logging import log_action
from csf.nlm_config import get_nlm_config
from csf.nlm_batch import (
    close_reusable_ingestor,
    get_last_prepare_metrics,
    get_last_reusable_process_metrics,
    DoubleBufferedReusableIngestor,
    process_industrial_batch_reusable,
    set_reusable_ingestor,
    NLMReusableIngestor,
)
from csf.shared_retry_pool import claim_ready as claim_shared_retry_ready
from csf.shared_retry_pool import mark_complete as mark_shared_retry_complete
from csf.shared_retry_pool import mark_permanent_failure as mark_shared_retry_permanent_failure
from csf.shared_retry_pool import pending_count as shared_retry_pending_count


def _load_batches(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON input must be a list")
        if not data:
            return []
        if all(isinstance(item, list) for item in data):
            batches: list[list[str]] = []
            for batch in data:
                cleaned = [str(item).strip() for item in batch if str(item).strip()]
                if cleaned:
                    batches.append(cleaned)
            return batches
        cleaned = [str(item).strip() for item in data if str(item).strip()]
        return [cleaned] if cleaned else []
    cleaned = [line.strip() for line in text.splitlines() if line.strip()]
    return [cleaned] if cleaned else []


def _get_reusable_pipeline_mode() -> str:
    value = os.getenv("YTIS_REUSABLE_PIPELINE_MODE", "").strip().lower().replace("-", "_")
    if value == "double_buffered":
        return "double_buffered"
    return "serial"


def _write_result_file(result_path: Path | None, data: dict[str, object]) -> None:
    if result_path is None:
        return
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = result_path.with_suffix(result_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(result_path)


def _empty_source_profile_totals() -> dict[str, object]:
    return {
        "total": 0,
        "matched": 0,
        "missing": 0,
        "source_class_counts": {},
        "status_counts": {},
        "privacy_status_counts": {},
        "upload_status_counts": {},
        "unavailable_reason_counts": {},
        "failure_reason_counts": {},
    }


def _merge_source_profile_totals(
    target: dict[str, object],
    source: dict[str, object] | None,
) -> dict[str, object]:
    if not source:
        return target
    for key in ("total", "matched", "missing"):
        target[key] = int(target.get(key, 0) or 0) + int(source.get(key, 0) or 0)
    for key in (
        "source_class_counts",
        "status_counts",
        "privacy_status_counts",
        "upload_status_counts",
        "unavailable_reason_counts",
        "failure_reason_counts",
    ):
        merged = Counter(target.get(key, {}) or {})
        merged.update(source.get(key, {}) or {})
        target[key] = dict(merged)
    return target


def _parent_alive_windows(ppid: int) -> bool:
    if ppid <= 0:
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, ppid)
        if not handle:
            return False
        WAIT_OBJECT_0 = 0
        WAIT_TIMEOUT = 0x00000102
        result = kernel32.WaitForSingleObject(handle, 0)
        kernel32.CloseHandle(handle)
        return result == WAIT_TIMEOUT
    except Exception:
        return True


def _start_parent_watchdog() -> threading.Event:
    stop_event = threading.Event()
    parent_pid = os.getppid()

    def _watch() -> None:
        while not stop_event.is_set():
            if not _parent_alive_windows(parent_pid):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                except Exception:
                    pass
                os._exit(1)
            stop_event.wait(5.0)

    thread = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    thread.start()
    return stop_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated yt-is worker batch.")
    parser.add_argument("--input", required=True, type=Path, help="JSON array or newline file of video IDs")
    parser.add_argument("--state-path", required=True, help="Worker-specific reusable notebook state file")
    parser.add_argument("--notebook-title", required=True, help="Worker-specific reusable notebook title")
    parser.add_argument(
        "--result-path",
        type=Path,
        default=None,
        help="Structured JSON result path for the parent coordinator",
    )
    parser.add_argument(
        "--notebooklm-profile",
        default=None,
        help="NotebookLM profile name for this worker process",
    )
    parser.add_argument("--worker-id", required=True, help="Worker label for logging")
    args = parser.parse_args(argv)

    os.environ["YTIS_NLM_REUSABLE_STATE_PATH"] = args.state_path
    os.environ["YTIS_NLM_REUSABLE_NOTEBOOK_TITLE"] = args.notebook_title
    os.environ["YTIS_NLM_OWNER_STATE_PATH"] = args.state_path
    os.environ["YTIS_NLM_OWNER_NOTEBOOK_TITLE"] = args.notebook_title
    notebooklm_profile = args.notebooklm_profile or f"ytis-{args.worker_id}"
    os.environ["NOTEBOOKLM_PROFILE"] = notebooklm_profile
    watchdog_stop = _start_parent_watchdog()
    worker_result: dict[str, object] = {
        "worker_id": args.worker_id,
        "input": str(args.input),
        "batch_count": 0,
        "video_count": 0,
        "succeeded": 0,
        "failed": 0,
        "source_profile": _empty_source_profile_totals(),
        "subbatch_metrics": [],
        "startup_retire_elapsed_s": 0.0,
        "startup_notebook_check_elapsed_s": 0.0,
        "startup_notebook_create_elapsed_s": 0.0,
        "startup_prepare_cleanup_elapsed_s": 0.0,
        "startup_prepare_total_elapsed_s": 0.0,
        "setup_elapsed_s_total": 0.0,
        "notebook_check_elapsed_s_total": 0.0,
        "notebook_create_elapsed_s_total": 0.0,
        "notebook_retire_elapsed_s_total": 0.0,
        "add_sources_elapsed_s_total": 0.0,
        "add_cmd_elapsed_s_total": 0.0,
        "materialization_wait_elapsed_s_total": 0.0,
        "extract_elapsed_s_total": 0.0,
        "cleanup_elapsed_s_total": 0.0,
        "batch_elapsed_s_total": 0.0,
        "staging_overlap_elapsed_s_total": 0.0,
        "staging_wait_elapsed_s_total": 0.0,
        "stage_swap_count_total": 0,
        "content_fetch_status_counts_total": {},
        "content_fetch_command_elapsed_s_total": 0.0,
        "content_fetch_command_elapsed_s_max": 0.0,
        "content_fetch_command_elapsed_s_count": 0,
        "content_fetch_command_elapsed_s_avg": 0.0,
        "content_fetch_retry_sleep_elapsed_s_total": 0.0,
        "content_fetch_retry_queue_sleep_elapsed_s_total": 0.0,
        "source_list_probe_elapsed_s_total": 0.0,
        "source_list_probe_elapsed_s_max": 0.0,
        "source_list_probe_count": 0,
        "source_content_readiness_probe_elapsed_s_total": 0.0,
        "source_content_readiness_probe_elapsed_s_max": 0.0,
        "source_content_readiness_probe_count": 0,
        "source_content_readiness_probe_sleep_elapsed_s_total": 0.0,
        "source_ready_age_s_total": 0.0,
        "source_ready_age_s_max": 0.0,
        "source_ready_age_s_avg": 0.0,
        "youtube_ytdlp_elapsed_s_total": 0.0,
        "youtube_ytdlp_elapsed_s_max": 0.0,
        "youtube_ytdlp_elapsed_s_count": 0,
        "youtube_ytdlp_elapsed_s_avg": 0.0,
        "youtube_page_elapsed_s_total": 0.0,
        "youtube_page_elapsed_s_max": 0.0,
        "youtube_page_elapsed_s_count": 0,
        "youtube_page_elapsed_s_avg": 0.0,
        "shared_retry_deferred_count": 0,
        "shared_retry_recovered_count": 0,
        "shared_retry_final_failed_count": 0,
        "shared_retry_processed_count": 0,
        "pipeline_strategy": "reusable",
        "notebooklm_profile": notebooklm_profile,
        "state_path": args.state_path,
        "notebook_title": args.notebook_title,
    }

    worker_source_profile = _empty_source_profile_totals()
    worker_subbatch_metrics: list[dict[str, object]] = []
    try:
        prewarm_started = time.monotonic()
        cleanup_info = {
            "status": "owner_title_reuse",
            "notebook_title": args.notebook_title,
            "state_path": args.state_path,
        }
        log_action(
            "worker_notebook_reset_started",
            {
                "worker_id": args.worker_id,
                "notebooklm_profile": notebooklm_profile,
                "state_path": args.state_path,
                "notebook_title": args.notebook_title,
                "cleanup_info": cleanup_info,
            },
        )
        print(
            json.dumps(
                {
                    "worker_id": args.worker_id,
                    "event": "worker_notebook_reset_started",
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "cleanup_info": cleanup_info,
                },
                separators=(",", ":"),
            )
        )
        pipeline_mode = _get_reusable_pipeline_mode()
        ingestor = DoubleBufferedReusableIngestor() if pipeline_mode == "double_buffered" else NLMReusableIngestor()
        log_action(
            "worker_notebook_reset_completed",
            {
                "worker_id": args.worker_id,
                "notebooklm_profile": notebooklm_profile,
                "state_path": args.state_path,
                "notebook_title": args.notebook_title,
                "cleanup_info": cleanup_info,
            },
        )
        print(
            json.dumps(
                {
                    "worker_id": args.worker_id,
                    "event": "worker_notebook_reset_completed",
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "cleanup_info": cleanup_info,
                },
                separators=(",", ":"),
            )
        )
        prepared, setup_mode = ingestor.prepare()
        set_reusable_ingestor(ingestor)
        log_action(
            "worker_notebook_prewarm",
            {
                "worker_id": args.worker_id,
                "prepared": prepared,
                "setup_mode": setup_mode,
                "startup_retire_elapsed_s": 0.0,
                "startup_notebook_check_elapsed_s": 0.0,
                "startup_notebook_create_elapsed_s": 0.0,
                "startup_prepare_cleanup_elapsed_s": 0.0,
                "startup_prepare_total_elapsed_s": round(time.monotonic() - prewarm_started, 3),
                "notebooklm_profile": notebooklm_profile,
                "state_path": args.state_path,
                "notebook_title": args.notebook_title,
                "elapsed_s": round(time.monotonic() - prewarm_started, 3),
            },
        )
        print(
            json.dumps(
                {
                    "worker_id": args.worker_id,
                    "event": "notebook_prewarm",
                    "prepared": prepared,
                    "setup_mode": setup_mode,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "elapsed_s": round(time.monotonic() - prewarm_started, 3),
                },
                separators=(",", ":"),
            )
        )
        prepare_metrics = get_last_prepare_metrics() or {}
        worker_result["startup_retire_elapsed_s"] = float(prepare_metrics.get("retire_elapsed_s") or 0.0)
        worker_result["startup_notebook_check_elapsed_s"] = float(prepare_metrics.get("notebook_check_elapsed_s") or 0.0)
        worker_result["startup_notebook_create_elapsed_s"] = float(prepare_metrics.get("create_elapsed_s") or 0.0)
        worker_result["startup_prepare_cleanup_elapsed_s"] = float(prepare_metrics.get("cleanup_elapsed_s") or 0.0)
        worker_result["startup_prepare_total_elapsed_s"] = float(prepare_metrics.get("total_elapsed_s") or 0.0)
        batches = _load_batches(args.input)
        total_video_count = 0
        total_succeeded = 0
        total_failed = 0
        double_buffered_batch_results: list[dict[str, tuple[bool, Optional[str], Optional[str]]]] | None = None
        double_buffered_batch_metrics: list[dict[str, object]] | None = None
        double_buffered_pipeline_metrics: dict[str, object] = {}

        cfg = get_nlm_config()
        if pipeline_mode == "double_buffered" and len(batches) > 1:
            double_buffered_batch_results = ingestor.process_batches(batches)
            double_buffered_batch_metrics = ingestor.get_last_batch_metrics() or []
            double_buffered_pipeline_metrics = ingestor.get_last_process_metrics() or {}
            worker_result["pipeline_strategy"] = str(double_buffered_pipeline_metrics.get("strategy") or "double_buffered_reusable")

        def _drain_shared_retry_pool() -> None:
            nonlocal total_succeeded, total_failed
            if not cfg.source_content_shared_retry_pool_enabled:
                return
            drain_started_at = time.monotonic()
            drain_budget_s = max(0.0, float(cfg.source_content_retry_queue_budget_s))
            drain_poll_s = max(1.0, min(float(cfg.source_content_retry_queue_delay_s) / 2.0, 5.0))
            shared_retry_deferred = 0
            shared_retry_recovered = 0
            shared_retry_final_failed = 0
            shared_retry_processed = 0
            log_action(
                "worker_shared_retry_drain_started",
                {
                    "worker_id": args.worker_id,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "drain_budget_s": drain_budget_s,
                    "drain_poll_s": drain_poll_s,
                },
            )
            print(
                json.dumps(
                    {
                        "worker_id": args.worker_id,
                        "event": "worker_shared_retry_drain_started",
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "notebook_title": args.notebook_title,
                        "drain_budget_s": drain_budget_s,
                        "drain_poll_s": drain_poll_s,
                    },
                    separators=(",", ":"),
                )
            )
            while time.monotonic() - drain_started_at < drain_budget_s:
                claimed = claim_shared_retry_ready(limit=8, claimant_id=args.worker_id)
                if not claimed:
                    try:
                        remaining_pending = shared_retry_pending_count()
                    except Exception as exc:
                        log_action(
                            "worker_shared_retry_pending_count_error",
                            {
                                "worker_id": args.worker_id,
                                "notebooklm_profile": notebooklm_profile,
                                "state_path": args.state_path,
                                "notebook_title": args.notebook_title,
                                "error": str(exc),
                            },
                        )
                        remaining_pending = 0
                    if remaining_pending <= 0:
                        break
                    time.sleep(drain_poll_s)
                    continue
                claimed_video_ids = [entry.video_id for entry in claimed]
                shared_retry_processed += len(claimed_video_ids)
                shared_results = process_industrial_batch_reusable(claimed_video_ids)
                shared_metrics = get_last_reusable_process_metrics() or {}
                deferred = int(shared_metrics.get("shared_retry_deferred_count") or 0)
                success_in_round = sum(1 for ok, transcript, _ in shared_results.values() if ok and transcript)
                final_failed = max(0, len(claimed_video_ids) - success_in_round - deferred)
                shared_retry_deferred += deferred
                shared_retry_recovered += success_in_round
                shared_retry_final_failed += final_failed
                total_succeeded += success_in_round
                total_failed += final_failed
                for video_id, (success, transcript, _error) in shared_results.items():
                    if success and transcript:
                        set_cached_transcript(video_id, "en", "notebooklm", transcript)
                        mark_complete(video_id, last_stage="notebooklm")
                        mark_shared_retry_complete(video_id)
                    else:
                        mark_shared_retry_permanent_failure(video_id, str(_error or "shared retry failed"))
                log_action(
                    "worker_shared_retry_drain_batch_completed",
                    {
                        "worker_id": args.worker_id,
                        "claimed_count": len(claimed_video_ids),
                        "succeeded": success_in_round,
                        "failed": final_failed,
                        "deferred": deferred,
                        "processed": shared_retry_processed,
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "notebook_title": args.notebook_title,
                    },
                )
                print(
                    json.dumps(
                        {
                            "worker_id": args.worker_id,
                            "event": "worker_shared_retry_drain_batch_completed",
                            "claimed_count": len(claimed_video_ids),
                            "succeeded": success_in_round,
                            "failed": final_failed,
                            "deferred": deferred,
                            "processed": shared_retry_processed,
                            "notebooklm_profile": notebooklm_profile,
                            "state_path": args.state_path,
                            "notebook_title": args.notebook_title,
                        },
                        separators=(",", ":"),
                    )
                )

            worker_result["shared_retry_deferred_count"] = int(worker_result["shared_retry_deferred_count"]) + shared_retry_deferred
            worker_result["shared_retry_recovered_count"] = int(worker_result["shared_retry_recovered_count"]) + shared_retry_recovered
            worker_result["shared_retry_final_failed_count"] = int(worker_result["shared_retry_final_failed_count"]) + shared_retry_final_failed
            worker_result["shared_retry_processed_count"] = int(worker_result["shared_retry_processed_count"]) + shared_retry_processed
            log_action(
                "worker_shared_retry_drain_completed",
                {
                    "worker_id": args.worker_id,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "deferred": shared_retry_deferred,
                    "recovered": shared_retry_recovered,
                    "final_failed": shared_retry_final_failed,
                    "processed": shared_retry_processed,
                    "elapsed_s": round(time.monotonic() - drain_started_at, 3),
                },
            )
            print(
                json.dumps(
                    {
                        "worker_id": args.worker_id,
                        "event": "worker_shared_retry_drain_completed",
                        "deferred": shared_retry_deferred,
                        "recovered": shared_retry_recovered,
                        "final_failed": shared_retry_final_failed,
                        "processed": shared_retry_processed,
                        "elapsed_s": round(time.monotonic() - drain_started_at, 3),
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "notebook_title": args.notebook_title,
                    },
                    separators=(",", ":"),
                )
                )

        def _record_batch_completion(
            batch_index: int,
            video_ids: list[str],
            batch_started_at: float,
            batch_started_at_epoch: float,
            batch_succeeded: int,
            batch_failed: int,
            source_profile: dict[str, object],
            metrics: dict[str, object],
        ) -> None:
            nonlocal total_succeeded, total_failed
            batch_elapsed_s = float(
                metrics.get("batch_elapsed_s")
                or metrics.get("total_elapsed_s")
                or round(time.monotonic() - batch_started_at, 3)
            )
            setup_elapsed_s = float(metrics.get("setup_elapsed_s") or 0.0)
            notebook_check_elapsed_s = float(metrics.get("notebook_check_elapsed_s") or 0.0)
            notebook_create_elapsed_s = float(metrics.get("notebook_create_elapsed_s") or 0.0)
            notebook_retire_elapsed_s = float(metrics.get("notebook_retire_elapsed_s") or 0.0)
            add_sources_elapsed_s = float(metrics.get("add_sources_elapsed_s") or 0.0)
            add_cmd_elapsed_s = float(metrics.get("add_cmd_elapsed_s") or 0.0)
            materialization_wait_elapsed_s = float(metrics.get("materialization_wait_elapsed_s") or 0.0)
            extract_elapsed_s = float(metrics.get("extract_elapsed_s") or 0.0)
            cleanup_elapsed_s = float(metrics.get("cleanup_elapsed_s") or 0.0)
            content_fetch_status_counts = dict(metrics.get("content_fetch_status_counts") or {})
            content_fetch_command_elapsed_s_total = float(metrics.get("content_fetch_command_elapsed_s_total") or 0.0)
            content_fetch_command_elapsed_s_max = float(metrics.get("content_fetch_command_elapsed_s_max") or 0.0)
            content_fetch_command_elapsed_s_count = int(metrics.get("content_fetch_command_elapsed_s_count") or 0)
            content_fetch_command_elapsed_s_avg = float(metrics.get("content_fetch_command_elapsed_s_avg") or 0.0)
            content_fetch_retry_sleep_elapsed_s_total = float(metrics.get("content_fetch_retry_sleep_elapsed_s_total") or 0.0)
            content_fetch_retry_queue_sleep_elapsed_s_total = float(metrics.get("content_fetch_retry_queue_sleep_elapsed_s_total") or 0.0)
            source_list_probe_elapsed_s_total = float(metrics.get("source_list_probe_elapsed_s_total") or 0.0)
            source_list_probe_elapsed_s_max = float(metrics.get("source_list_probe_elapsed_s_max") or 0.0)
            source_list_probe_count = int(metrics.get("source_list_probe_count") or 0)
            source_content_readiness_probe_elapsed_s_total = float(metrics.get("source_content_readiness_probe_elapsed_s_total") or 0.0)
            source_content_readiness_probe_elapsed_s_max = float(metrics.get("source_content_readiness_probe_elapsed_s_max") or 0.0)
            source_content_readiness_probe_count = int(metrics.get("source_content_readiness_probe_count") or 0)
            source_content_readiness_probe_sleep_elapsed_s_total = float(metrics.get("source_content_readiness_probe_sleep_elapsed_s_total") or 0.0)
            source_ready_age_s_total = float(metrics.get("source_ready_age_s_total") or 0.0)
            source_ready_age_s_max = float(metrics.get("source_ready_age_s_max") or 0.0)
            source_ready_age_s_avg = float(metrics.get("source_ready_age_s_avg") or 0.0)
            youtube_ytdlp_elapsed_s_total = float(metrics.get("youtube_ytdlp_elapsed_s_total") or 0.0)
            youtube_ytdlp_elapsed_s_max = float(metrics.get("youtube_ytdlp_elapsed_s_max") or 0.0)
            youtube_ytdlp_elapsed_s_count = int(metrics.get("youtube_ytdlp_elapsed_s_count") or 0)
            youtube_ytdlp_elapsed_s_avg = float(metrics.get("youtube_ytdlp_elapsed_s_avg") or 0.0)
            youtube_page_elapsed_s_total = float(metrics.get("youtube_page_elapsed_s_total") or 0.0)
            youtube_page_elapsed_s_max = float(metrics.get("youtube_page_elapsed_s_max") or 0.0)
            youtube_page_elapsed_s_count = int(metrics.get("youtube_page_elapsed_s_count") or 0)
            youtube_page_elapsed_s_avg = float(metrics.get("youtube_page_elapsed_s_avg") or 0.0)
            shared_retry_deferred_count = int(metrics.get("shared_retry_deferred_count") or 0)
            shared_retry_recovered_count = int(metrics.get("shared_retry_recovered_count") or 0)
            shared_retry_final_failed_count = int(metrics.get("shared_retry_final_failed_count") or 0)
            shared_retry_processed_count = int(metrics.get("shared_retry_processed_count") or 0)
            staging_overlap_elapsed_s = float(metrics.get("staging_overlap_elapsed_s") or 0.0)
            staging_wait_elapsed_s = float(metrics.get("staging_wait_elapsed_s") or 0.0)
            stage_swap_count = int(metrics.get("stage_swap_count") or 0)
            pipeline_strategy = str(metrics.get("strategy") or worker_result.get("pipeline_strategy") or "reusable")
            subbatch_metrics = list(metrics.get("subbatch_metrics") or [])
            worker_subbatch_metrics.extend([dict(item) for item in subbatch_metrics if isinstance(item, dict)])
            worker_result["setup_elapsed_s_total"] = float(worker_result["setup_elapsed_s_total"]) + setup_elapsed_s
            worker_result["notebook_check_elapsed_s_total"] = float(worker_result["notebook_check_elapsed_s_total"]) + notebook_check_elapsed_s
            worker_result["notebook_create_elapsed_s_total"] = float(worker_result["notebook_create_elapsed_s_total"]) + notebook_create_elapsed_s
            worker_result["notebook_retire_elapsed_s_total"] = float(worker_result["notebook_retire_elapsed_s_total"]) + notebook_retire_elapsed_s
            worker_result["add_sources_elapsed_s_total"] = float(worker_result["add_sources_elapsed_s_total"]) + add_sources_elapsed_s
            worker_result["add_cmd_elapsed_s_total"] = float(worker_result.get("add_cmd_elapsed_s_total", 0.0)) + add_cmd_elapsed_s
            worker_result["materialization_wait_elapsed_s_total"] = float(worker_result.get("materialization_wait_elapsed_s_total", 0.0)) + materialization_wait_elapsed_s
            worker_result["extract_elapsed_s_total"] = float(worker_result["extract_elapsed_s_total"]) + extract_elapsed_s
            worker_result["cleanup_elapsed_s_total"] = float(worker_result["cleanup_elapsed_s_total"]) + cleanup_elapsed_s
            worker_result["batch_elapsed_s_total"] = float(worker_result["batch_elapsed_s_total"]) + batch_elapsed_s
            worker_result["staging_overlap_elapsed_s_total"] = float(worker_result["staging_overlap_elapsed_s_total"]) + staging_overlap_elapsed_s
            worker_result["staging_wait_elapsed_s_total"] = float(worker_result["staging_wait_elapsed_s_total"]) + staging_wait_elapsed_s
            worker_result["stage_swap_count_total"] = int(worker_result["stage_swap_count_total"]) + stage_swap_count
            worker_result["pipeline_strategy"] = pipeline_strategy
            worker_result["content_fetch_status_counts_total"] = dict(
                Counter(worker_result.get("content_fetch_status_counts_total", {}) or {})
                + Counter(content_fetch_status_counts)
            )
            worker_result["content_fetch_command_elapsed_s_total"] = float(worker_result.get("content_fetch_command_elapsed_s_total", 0.0)) + content_fetch_command_elapsed_s_total
            worker_result["content_fetch_command_elapsed_s_max"] = max(
                float(worker_result.get("content_fetch_command_elapsed_s_max", 0.0)),
                content_fetch_command_elapsed_s_max,
            )
            worker_result["content_fetch_command_elapsed_s_count"] = int(worker_result.get("content_fetch_command_elapsed_s_count", 0)) + content_fetch_command_elapsed_s_count
            worker_result["content_fetch_command_elapsed_s_avg"] = round(
                float(worker_result["content_fetch_command_elapsed_s_total"]) / max(int(worker_result["content_fetch_command_elapsed_s_count"]), 1),
                3,
            )
            worker_result["content_fetch_retry_sleep_elapsed_s_total"] = float(worker_result.get("content_fetch_retry_sleep_elapsed_s_total", 0.0)) + content_fetch_retry_sleep_elapsed_s_total
            worker_result["content_fetch_retry_queue_sleep_elapsed_s_total"] = float(worker_result.get("content_fetch_retry_queue_sleep_elapsed_s_total", 0.0)) + content_fetch_retry_queue_sleep_elapsed_s_total
            worker_result["source_list_probe_elapsed_s_total"] = float(worker_result.get("source_list_probe_elapsed_s_total", 0.0)) + source_list_probe_elapsed_s_total
            worker_result["source_list_probe_elapsed_s_max"] = max(
                float(worker_result.get("source_list_probe_elapsed_s_max", 0.0)),
                source_list_probe_elapsed_s_max,
            )
            worker_result["source_list_probe_count"] = int(worker_result.get("source_list_probe_count", 0)) + source_list_probe_count
            worker_result["source_content_readiness_probe_elapsed_s_total"] = float(worker_result.get("source_content_readiness_probe_elapsed_s_total", 0.0)) + source_content_readiness_probe_elapsed_s_total
            worker_result["source_content_readiness_probe_elapsed_s_max"] = max(
                float(worker_result.get("source_content_readiness_probe_elapsed_s_max", 0.0)),
                source_content_readiness_probe_elapsed_s_max,
            )
            worker_result["source_content_readiness_probe_count"] = int(worker_result.get("source_content_readiness_probe_count", 0)) + source_content_readiness_probe_count
            worker_result["source_content_readiness_probe_sleep_elapsed_s_total"] = float(worker_result.get("source_content_readiness_probe_sleep_elapsed_s_total", 0.0)) + source_content_readiness_probe_sleep_elapsed_s_total
            worker_result["source_ready_age_s_total"] = float(worker_result.get("source_ready_age_s_total", 0.0)) + source_ready_age_s_total
            worker_result["source_ready_age_s_max"] = max(
                float(worker_result.get("source_ready_age_s_max", 0.0)),
                source_ready_age_s_max,
            )
            worker_result["shared_retry_deferred_count"] = int(worker_result["shared_retry_deferred_count"]) + shared_retry_deferred_count
            worker_result["shared_retry_recovered_count"] = int(worker_result["shared_retry_recovered_count"]) + shared_retry_recovered_count
            worker_result["shared_retry_final_failed_count"] = int(worker_result["shared_retry_final_failed_count"]) + shared_retry_final_failed_count
            worker_result["shared_retry_processed_count"] = int(worker_result["shared_retry_processed_count"]) + shared_retry_processed_count
            worker_result["youtube_ytdlp_elapsed_s_total"] = float(worker_result["youtube_ytdlp_elapsed_s_total"]) + youtube_ytdlp_elapsed_s_total
            worker_result["youtube_ytdlp_elapsed_s_max"] = max(
                float(worker_result["youtube_ytdlp_elapsed_s_max"]),
                youtube_ytdlp_elapsed_s_max,
            )
            worker_result["youtube_ytdlp_elapsed_s_count"] = int(worker_result["youtube_ytdlp_elapsed_s_count"]) + youtube_ytdlp_elapsed_s_count
            worker_result["youtube_ytdlp_elapsed_s_avg"] = round(
                float(worker_result["youtube_ytdlp_elapsed_s_total"]) / max(int(worker_result["youtube_ytdlp_elapsed_s_count"]), 1),
                3,
            )
            worker_result["youtube_page_elapsed_s_total"] = float(worker_result["youtube_page_elapsed_s_total"]) + youtube_page_elapsed_s_total
            worker_result["youtube_page_elapsed_s_max"] = max(
                float(worker_result["youtube_page_elapsed_s_max"]),
                youtube_page_elapsed_s_max,
            )
            worker_result["youtube_page_elapsed_s_count"] = int(worker_result["youtube_page_elapsed_s_count"]) + youtube_page_elapsed_s_count
            worker_result["youtube_page_elapsed_s_avg"] = round(
                float(worker_result["youtube_page_elapsed_s_total"]) / max(int(worker_result["youtube_page_elapsed_s_count"]), 1),
                3,
            )
            counts_total = dict(worker_result.get("content_fetch_status_counts_total", {}) or {})
            count_sum = sum(int(v) for v in counts_total.values())
            worker_result["source_ready_age_s_avg"] = round(
                float(worker_result["source_ready_age_s_total"]) / max(count_sum, 1),
                3,
            )
            log_action(
                "worker_batch_metrics",
                {
                    "worker_id": args.worker_id,
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "batch_size": len(video_ids),
                    "setup_mode": metrics.get("setup_mode"),
                    "notebook_reused": metrics.get("notebook_reused"),
                    "setup_elapsed_s": setup_elapsed_s,
                    "notebook_check_elapsed_s": notebook_check_elapsed_s,
                    "notebook_create_elapsed_s": notebook_create_elapsed_s,
                    "notebook_retire_elapsed_s": notebook_retire_elapsed_s,
                    "add_sources_elapsed_s": add_sources_elapsed_s,
                    "add_cmd_elapsed_s": add_cmd_elapsed_s,
                    "materialization_wait_elapsed_s": materialization_wait_elapsed_s,
                    "extract_elapsed_s": extract_elapsed_s,
                    "cleanup_elapsed_s": cleanup_elapsed_s,
                    "batch_elapsed_s": batch_elapsed_s,
                    "succeeded": batch_succeeded,
                    "failed": batch_failed,
                    "content_fetch_status_counts": content_fetch_status_counts,
                    "source_ready_age_s_total": source_ready_age_s_total,
                    "source_ready_age_s_max": source_ready_age_s_max,
                    "source_ready_age_s_avg": source_ready_age_s_avg,
                    "youtube_ytdlp_elapsed_s_total": youtube_ytdlp_elapsed_s_total,
                    "youtube_ytdlp_elapsed_s_max": youtube_ytdlp_elapsed_s_max,
                    "youtube_ytdlp_elapsed_s_count": youtube_ytdlp_elapsed_s_count,
                    "youtube_ytdlp_elapsed_s_avg": youtube_ytdlp_elapsed_s_avg,
                    "youtube_page_elapsed_s_total": youtube_page_elapsed_s_total,
                    "youtube_page_elapsed_s_max": youtube_page_elapsed_s_max,
                    "youtube_page_elapsed_s_count": youtube_page_elapsed_s_count,
                    "youtube_page_elapsed_s_avg": youtube_page_elapsed_s_avg,
                    "shared_retry_deferred_count": shared_retry_deferred_count,
                    "shared_retry_recovered_count": shared_retry_recovered_count,
                    "shared_retry_final_failed_count": shared_retry_final_failed_count,
                    "shared_retry_processed_count": shared_retry_processed_count,
                    "staging_overlap_elapsed_s": staging_overlap_elapsed_s,
                    "staging_wait_elapsed_s": staging_wait_elapsed_s,
                    "stage_swap_count": stage_swap_count,
                    "pipeline_strategy": pipeline_strategy,
                    "source_profile": source_profile,
                    "subbatch_count": len(subbatch_metrics),
                    "subbatch_metrics": subbatch_metrics,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "started_at_epoch": batch_started_at_epoch,
                    "completed_at_epoch": time.time(),
                },
            )

        for batch_index, video_ids in enumerate(batches, 1):
            batch_started_at = time.monotonic()
            batch_started_at_epoch = time.time()
            total_video_count += len(video_ids)
            source_profile = summarize_video_ids(video_ids)
            _merge_source_profile_totals(worker_source_profile, source_profile)
            log_action(
                "worker_batch_started",
                {
                    "worker_id": args.worker_id,
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "batch_size": len(video_ids),
                    "video_count": len(video_ids),
                    "source_profile": source_profile,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "started_at_epoch": batch_started_at_epoch,
                },
            )
            if pipeline_mode == "double_buffered" and len(batches) > 1:
                results = double_buffered_batch_results[batch_index - 1] if double_buffered_batch_results is not None else {}
                batch_succeeded = 0
                batch_failed = 0
                for vid, (success, transcript, err) in results.items():
                    if success and transcript:
                        set_cached_transcript(vid, "en", "notebooklm", transcript)
                        mark_complete(vid, last_stage="notebooklm")
                        total_succeeded += 1
                        batch_succeeded += 1
                    else:
                        total_failed += 1
                        batch_failed += 1
                metrics = (
                    double_buffered_batch_metrics[batch_index - 1]
                    if double_buffered_batch_metrics is not None and batch_index - 1 < len(double_buffered_batch_metrics)
                    else (get_last_reusable_process_metrics() or {})
                )
                batch_completed_elapsed_s = float(
                    metrics.get("batch_elapsed_s")
                    or metrics.get("total_elapsed_s")
                    or round(time.monotonic() - batch_started_at, 3)
                )
                log_action(
                    "worker_batch_completed",
                    {
                        "worker_id": args.worker_id,
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "batch_size": len(video_ids),
                        "video_count": len(video_ids),
                        "succeeded": batch_succeeded,
                        "failed": batch_failed,
                        "elapsed_s": batch_completed_elapsed_s,
                        "source_profile": source_profile,
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "notebook_title": args.notebook_title,
                        "started_at_epoch": batch_started_at_epoch,
                        "completed_at_epoch": time.time(),
                    },
                )
                _record_batch_completion(
                    batch_index,
                    video_ids,
                    batch_started_at,
                    batch_started_at_epoch,
                    batch_succeeded,
                    batch_failed,
                    source_profile,
                    metrics,
                )
            else:
                results = process_industrial_batch_reusable(video_ids)
                batch_succeeded = 0
                batch_failed = 0
                for vid, (success, transcript, err) in results.items():
                    if success and transcript:
                        set_cached_transcript(vid, "en", "notebooklm", transcript)
                        mark_complete(vid, last_stage="notebooklm")
                        total_succeeded += 1
                        batch_succeeded += 1
                    else:
                        total_failed += 1
                        batch_failed += 1
                log_action(
                    "worker_batch_completed",
                    {
                        "worker_id": args.worker_id,
                        "batch_index": batch_index,
                        "batch_count": len(batches),
                        "batch_size": len(video_ids),
                        "video_count": len(video_ids),
                        "succeeded": batch_succeeded,
                        "failed": batch_failed,
                        "elapsed_s": round(time.monotonic() - batch_started_at, 3),
                        "source_profile": source_profile,
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "notebook_title": args.notebook_title,
                        "started_at_epoch": batch_started_at_epoch,
                        "completed_at_epoch": time.time(),
                    },
                )
                metrics = get_last_reusable_process_metrics() or {}
                _record_batch_completion(
                    batch_index,
                    video_ids,
                    batch_started_at,
                    batch_started_at_epoch,
                    batch_succeeded,
                    batch_failed,
                    source_profile,
                    metrics,
                )
            log_action(
                "worker_completed",
                {
                    "worker_id": args.worker_id,
                    "batch_count": len(batches),
                    "video_count": total_video_count,
                    "succeeded": total_succeeded,
                    "failed": total_failed,
                    "source_profile": worker_source_profile,
                    "subbatch_metrics": worker_subbatch_metrics,
                    "startup_retire_elapsed_s": worker_result["startup_retire_elapsed_s"],
                    "startup_notebook_check_elapsed_s": worker_result["startup_notebook_check_elapsed_s"],
                    "startup_notebook_create_elapsed_s": worker_result["startup_notebook_create_elapsed_s"],
                    "startup_prepare_cleanup_elapsed_s": worker_result["startup_prepare_cleanup_elapsed_s"],
                    "startup_prepare_total_elapsed_s": worker_result["startup_prepare_total_elapsed_s"],
                    "setup_elapsed_s_total": worker_result["setup_elapsed_s_total"],
                    "notebook_check_elapsed_s_total": worker_result["notebook_check_elapsed_s_total"],
                    "notebook_create_elapsed_s_total": worker_result["notebook_create_elapsed_s_total"],
                    "notebook_retire_elapsed_s_total": worker_result["notebook_retire_elapsed_s_total"],
                    "add_sources_elapsed_s_total": worker_result["add_sources_elapsed_s_total"],
                    "add_cmd_elapsed_s_total": worker_result["add_cmd_elapsed_s_total"],
                    "materialization_wait_elapsed_s_total": worker_result["materialization_wait_elapsed_s_total"],
                    "extract_elapsed_s_total": worker_result["extract_elapsed_s_total"],
                    "cleanup_elapsed_s_total": worker_result["cleanup_elapsed_s_total"],
                    "batch_elapsed_s_total": worker_result["batch_elapsed_s_total"],
                "staging_overlap_elapsed_s_total": worker_result["staging_overlap_elapsed_s_total"],
                "staging_wait_elapsed_s_total": worker_result["staging_wait_elapsed_s_total"],
                "stage_swap_count_total": worker_result["stage_swap_count_total"],
                "content_fetch_status_counts_total": worker_result["content_fetch_status_counts_total"],
                "content_fetch_command_elapsed_s_total": worker_result["content_fetch_command_elapsed_s_total"],
                "content_fetch_command_elapsed_s_max": worker_result["content_fetch_command_elapsed_s_max"],
                "content_fetch_command_elapsed_s_count": worker_result["content_fetch_command_elapsed_s_count"],
                "content_fetch_command_elapsed_s_avg": worker_result["content_fetch_command_elapsed_s_avg"],
                "content_fetch_retry_sleep_elapsed_s_total": worker_result["content_fetch_retry_sleep_elapsed_s_total"],
                "content_fetch_retry_queue_sleep_elapsed_s_total": worker_result["content_fetch_retry_queue_sleep_elapsed_s_total"],
                "source_list_probe_elapsed_s_total": worker_result["source_list_probe_elapsed_s_total"],
                "source_list_probe_elapsed_s_max": worker_result["source_list_probe_elapsed_s_max"],
                "source_list_probe_count": worker_result["source_list_probe_count"],
                "source_content_readiness_probe_elapsed_s_total": worker_result["source_content_readiness_probe_elapsed_s_total"],
                "source_content_readiness_probe_elapsed_s_max": worker_result["source_content_readiness_probe_elapsed_s_max"],
                "source_content_readiness_probe_count": worker_result["source_content_readiness_probe_count"],
                "source_content_readiness_probe_sleep_elapsed_s_total": worker_result["source_content_readiness_probe_sleep_elapsed_s_total"],
                "source_ready_age_s_total": worker_result["source_ready_age_s_total"],
                "source_ready_age_s_max": worker_result["source_ready_age_s_max"],
                "source_ready_age_s_avg": worker_result["source_ready_age_s_avg"],
                    "youtube_ytdlp_elapsed_s_total": worker_result["youtube_ytdlp_elapsed_s_total"],
                    "youtube_ytdlp_elapsed_s_max": worker_result["youtube_ytdlp_elapsed_s_max"],
                    "youtube_ytdlp_elapsed_s_count": worker_result["youtube_ytdlp_elapsed_s_count"],
                    "youtube_ytdlp_elapsed_s_avg": worker_result["youtube_ytdlp_elapsed_s_avg"],
                    "youtube_page_elapsed_s_total": worker_result["youtube_page_elapsed_s_total"],
                    "youtube_page_elapsed_s_max": worker_result["youtube_page_elapsed_s_max"],
                    "youtube_page_elapsed_s_count": worker_result["youtube_page_elapsed_s_count"],
                    "youtube_page_elapsed_s_avg": worker_result["youtube_page_elapsed_s_avg"],
                    "pipeline_strategy": worker_result["pipeline_strategy"],
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                },
            )
        _drain_shared_retry_pool()
        if pipeline_mode == "double_buffered" and len(batches) > 1:
            worker_result["staging_overlap_elapsed_s_total"] = float(
                double_buffered_pipeline_metrics.get("staging_overlap_elapsed_s")
                or worker_result["staging_overlap_elapsed_s_total"]
                or 0.0
            )
            worker_result["staging_wait_elapsed_s_total"] = float(
                double_buffered_pipeline_metrics.get("staging_wait_elapsed_s")
                or worker_result["staging_wait_elapsed_s_total"]
                or 0.0
            )
            worker_result["stage_swap_count_total"] = int(
                double_buffered_pipeline_metrics.get("stage_swap_count")
                or worker_result["stage_swap_count_total"]
                or 0
            )
            worker_result["pipeline_strategy"] = str(
                double_buffered_pipeline_metrics.get("strategy")
                or worker_result["pipeline_strategy"]
                or "double_buffered_reusable"
            )
        worker_result.update(
            {
                "batch_count": len(batches),
                "video_count": total_video_count,
                "succeeded": total_succeeded,
                "failed": total_failed,
                "source_profile": worker_source_profile,
                "subbatch_metrics": worker_subbatch_metrics,
                "startup_retire_elapsed_s": worker_result["startup_retire_elapsed_s"],
                "startup_notebook_check_elapsed_s": worker_result["startup_notebook_check_elapsed_s"],
                "startup_notebook_create_elapsed_s": worker_result["startup_notebook_create_elapsed_s"],
                "startup_prepare_cleanup_elapsed_s": worker_result["startup_prepare_cleanup_elapsed_s"],
                "startup_prepare_total_elapsed_s": worker_result["startup_prepare_total_elapsed_s"],
                "setup_elapsed_s_total": worker_result["setup_elapsed_s_total"],
                "notebook_check_elapsed_s_total": worker_result["notebook_check_elapsed_s_total"],
                "notebook_create_elapsed_s_total": worker_result["notebook_create_elapsed_s_total"],
                "notebook_retire_elapsed_s_total": worker_result["notebook_retire_elapsed_s_total"],
                "add_sources_elapsed_s_total": worker_result["add_sources_elapsed_s_total"],
                "add_cmd_elapsed_s_total": worker_result["add_cmd_elapsed_s_total"],
                "materialization_wait_elapsed_s_total": worker_result["materialization_wait_elapsed_s_total"],
                "extract_elapsed_s_total": worker_result["extract_elapsed_s_total"],
                "cleanup_elapsed_s_total": worker_result["cleanup_elapsed_s_total"],
                "batch_elapsed_s_total": worker_result["batch_elapsed_s_total"],
                "staging_overlap_elapsed_s_total": worker_result["staging_overlap_elapsed_s_total"],
                "staging_wait_elapsed_s_total": worker_result["staging_wait_elapsed_s_total"],
                "stage_swap_count_total": worker_result["stage_swap_count_total"],
                "content_fetch_status_counts_total": worker_result["content_fetch_status_counts_total"],
                "content_fetch_command_elapsed_s_total": worker_result["content_fetch_command_elapsed_s_total"],
                "content_fetch_command_elapsed_s_max": worker_result["content_fetch_command_elapsed_s_max"],
                "content_fetch_command_elapsed_s_count": worker_result["content_fetch_command_elapsed_s_count"],
                "content_fetch_command_elapsed_s_avg": worker_result["content_fetch_command_elapsed_s_avg"],
                "content_fetch_retry_sleep_elapsed_s_total": worker_result["content_fetch_retry_sleep_elapsed_s_total"],
                "content_fetch_retry_queue_sleep_elapsed_s_total": worker_result["content_fetch_retry_queue_sleep_elapsed_s_total"],
                "source_list_probe_elapsed_s_total": worker_result["source_list_probe_elapsed_s_total"],
                "source_list_probe_elapsed_s_max": worker_result["source_list_probe_elapsed_s_max"],
                "source_list_probe_count": worker_result["source_list_probe_count"],
                "source_content_readiness_probe_elapsed_s_total": worker_result["source_content_readiness_probe_elapsed_s_total"],
                "source_content_readiness_probe_elapsed_s_max": worker_result["source_content_readiness_probe_elapsed_s_max"],
                "source_content_readiness_probe_count": worker_result["source_content_readiness_probe_count"],
                "source_content_readiness_probe_sleep_elapsed_s_total": worker_result["source_content_readiness_probe_sleep_elapsed_s_total"],
                "source_ready_age_s_total": worker_result["source_ready_age_s_total"],
                "source_ready_age_s_max": worker_result["source_ready_age_s_max"],
                "source_ready_age_s_avg": worker_result["source_ready_age_s_avg"],
                "youtube_ytdlp_elapsed_s_total": worker_result["youtube_ytdlp_elapsed_s_total"],
                "youtube_ytdlp_elapsed_s_max": worker_result["youtube_ytdlp_elapsed_s_max"],
                "youtube_ytdlp_elapsed_s_count": worker_result["youtube_ytdlp_elapsed_s_count"],
                "youtube_ytdlp_elapsed_s_avg": worker_result["youtube_ytdlp_elapsed_s_avg"],
                "youtube_page_elapsed_s_total": worker_result["youtube_page_elapsed_s_total"],
                "youtube_page_elapsed_s_max": worker_result["youtube_page_elapsed_s_max"],
                "youtube_page_elapsed_s_count": worker_result["youtube_page_elapsed_s_count"],
                "youtube_page_elapsed_s_avg": worker_result["youtube_page_elapsed_s_avg"],
                "pipeline_strategy": worker_result["pipeline_strategy"],
                "shared_retry_deferred_count": worker_result["shared_retry_deferred_count"],
                "shared_retry_recovered_count": worker_result["shared_retry_recovered_count"],
                "shared_retry_final_failed_count": worker_result["shared_retry_final_failed_count"],
                "shared_retry_processed_count": worker_result["shared_retry_processed_count"],
                "status": "ok",
                "returncode": 0,
            }
        )
        print(json.dumps(worker_result, separators=(",", ":")))
        return 0
    except Exception as exc:
        worker_result.update(
            {
                "status": "error",
                "returncode": 1,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        print(json.dumps(worker_result, separators=(",", ":")))
        return 1
    finally:
        log_action(
            "worker_cleanup_started",
            {
                "worker_id": args.worker_id,
                "batch_count": worker_result.get("batch_count", 0),
                "video_count": worker_result.get("video_count", 0),
                "succeeded": worker_result.get("succeeded", 0),
                "failed": worker_result.get("failed", 0),
                "status": worker_result.get("status", "unknown"),
                "returncode": worker_result.get("returncode", None),
                "notebooklm_profile": notebooklm_profile,
                "state_path": args.state_path,
                "notebook_title": args.notebook_title,
            },
        )
        state_path = Path(args.state_path)
        try:
            _write_result_file(args.result_path, worker_result)
            log_action(
                "worker_result_written",
                {
                    "worker_id": args.worker_id,
                    "result_path": str(args.result_path) if args.result_path is not None else None,
                    "status": worker_result.get("status", "unknown"),
                    "returncode": worker_result.get("returncode", None),
                    "notebooklm_profile": notebooklm_profile,
                },
            )
        except Exception:
            pass
        try:
            cleanup_ingestor_started = time.monotonic()
            log_action(
                "worker_cleanup_ingestor_close_started",
                {
                    "worker_id": args.worker_id,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "delete": True,
                },
            )
            close_reusable_ingestor(delete=True)
            log_action(
                "worker_cleanup_ingestor_close_completed",
                {
                    "worker_id": args.worker_id,
                    "elapsed_s": round(time.monotonic() - cleanup_ingestor_started, 3),
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                    "delete": True,
                },
            )
        finally:
            try:
                state_path.unlink(missing_ok=True)
                log_action(
                    "worker_cleanup_state_cleared",
                    {
                        "worker_id": args.worker_id,
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "removed": True,
                    },
                )
            except Exception as exc:
                log_action(
                    "worker_cleanup_state_clear_failed",
                    {
                        "worker_id": args.worker_id,
                        "notebooklm_profile": notebooklm_profile,
                        "state_path": args.state_path,
                        "error": str(exc),
                    },
                )
            log_action(
                "worker_cleanup_completed",
                {
                    "worker_id": args.worker_id,
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                },
            )
            watchdog_stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
