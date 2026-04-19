"""Execute one NotebookLM industrial batch in an isolated worker process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from csf.batch_status import mark_complete
from csf.cache import set_cached_transcript
from csf.csf_logging import log_action
from csf.nlm_batch import (
    close_reusable_ingestor,
    process_industrial_batch_reusable,
    retire_reusable_notebook_state,
    NLMReusableIngestor,
)


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


def _write_result_file(result_path: Path | None, data: dict[str, object]) -> None:
    if result_path is None:
        return
    result_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = result_path.with_suffix(result_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(result_path)


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
        "notebooklm_profile": notebooklm_profile,
        "state_path": args.state_path,
        "notebook_title": args.notebook_title,
    }

    try:
        prewarm_started = time.monotonic()
        cleanup_info = retire_reusable_notebook_state()
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
        ingestor = NLMReusableIngestor()
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
        log_action(
            "worker_notebook_prewarm",
            {
                "worker_id": args.worker_id,
                "prepared": prepared,
                "setup_mode": setup_mode,
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
        batches = _load_batches(args.input)
        total_video_count = 0
        total_succeeded = 0
        total_failed = 0
        for batch_index, video_ids in enumerate(batches, 1):
            batch_started_at = time.monotonic()
            total_video_count += len(video_ids)
            log_action(
                "worker_batch_started",
                {
                    "worker_id": args.worker_id,
                    "batch_index": batch_index,
                    "batch_count": len(batches),
                    "batch_size": len(video_ids),
                    "video_count": len(video_ids),
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                },
            )
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
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                },
            )
        log_action(
            "worker_completed",
            {
                "worker_id": args.worker_id,
                "batch_count": len(batches),
                "video_count": total_video_count,
                "succeeded": total_succeeded,
                "failed": total_failed,
                "notebooklm_profile": notebooklm_profile,
                "state_path": args.state_path,
                "notebook_title": args.notebook_title,
            },
        )
        worker_result.update(
            {
                "batch_count": len(batches),
                "video_count": total_video_count,
                "succeeded": total_succeeded,
                "failed": total_failed,
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
                },
            )
            close_reusable_ingestor(delete=False)
            log_action(
                "worker_cleanup_ingestor_close_completed",
                {
                    "worker_id": args.worker_id,
                    "elapsed_s": round(time.monotonic() - cleanup_ingestor_started, 3),
                    "notebooklm_profile": notebooklm_profile,
                    "state_path": args.state_path,
                    "notebook_title": args.notebook_title,
                },
            )
        finally:
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
