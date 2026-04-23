"""Tests for fetch timing logs in bin/csf-source."""

from __future__ import annotations

import json
import sys
import types
from concurrent.futures import Future
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


def _load_csf_source_module():
    """Load the extensionless bin/csf-source script as a module."""
    path = Path(r"P:\packages\yt-is\bin\csf-source")
    loader = SourceFileLoader("csf_source_timing_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load csf-source")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cmd_fetch_logs_fetch_start_and_first_download_started_industrial():
    """cmd_fetch logs a run-start marker and a first-download marker for industrial backlogs."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        }
        for i in range(200)
    ]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(
                                mod,
                                "process_industrial_batch_reusable",
                                return_value={entry["video_id"]: (True, "transcript", None) for entry in pending_entries},
                            ):
                                with mock.patch.object(mod, "close_reusable_ingestor"):
                                    with mock.patch.object(mod, "set_cached_transcript"):
                                        with mock.patch.object(mod, "mark_complete"):
                                            with mock.patch.object(mod, "log_action") as mock_log:
                                                mod.cmd_fetch(
                                                    source_filter="https://www.youtube.com/@example",
                                                    dry_run=False,
                                                    workers=1,
                                                )

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert log_names[0] == "fetch_invoked"
    assert "fetch_strategy_selected" in log_names
    assert "fetch_scan_started" in log_names
    assert "fetch_scan_completed" in log_names
    assert "first_download_started" in log_names
    assert "fetch_completed" in log_names
    assert log_names.index("fetch_scan_started") < log_names.index("fetch_scan_completed")
    assert log_names.index("fetch_scan_completed") < log_names.index("first_download_started")
    first_payload = mock_log.call_args_list[log_names.index("first_download_started")].args[1]
    assert first_payload["kind"] == "industrial_cli_batch"
    assert first_payload["batch_index"] == 1
    assert first_payload["batch_size"] == 200
    assert first_payload["first_video_id"] == "vid000"
    assert "elapsed_s" in first_payload


def test_cmd_fetch_emits_elapsed_scan_status_heartbeat():
    """Long scans should emit a time-based scan status heartbeat, not only channel checkpoints."""
    mod = _load_csf_source_module()
    channel_rows = [
        ("https://www.youtube.com/@chan1", "pl-1"),
        ("https://www.youtube.com/@chan2", "pl-2"),
        ("https://www.youtube.com/@chan3", "pl-3"),
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    monotonic_value = {"current": 0.0}

    def fake_monotonic():
        monotonic_value["current"] += 31.0
        return monotonic_value["current"]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=[]):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        with mock.patch.object(mod.time, "monotonic", side_effect=fake_monotonic):
                            with mock.patch.object(mod, "log_action") as mock_log:
                                mod.cmd_fetch(dry_run=False, workers=1)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "fetch_scan_started" in log_names
    assert "fetch_scan_completed" in log_names
    assert "fetch_scan_progress" in log_names
    heartbeat_payloads = [
        call.args[1]
        for call in mock_log.call_args_list
        if call.args[0] == "fetch_scan_progress" and call.args[1].get("trigger") == "elapsed_interval"
    ]
    assert heartbeat_payloads, "expected a time-based scan heartbeat"
    assert heartbeat_payloads[0]["channels_active_total"] == 3


def test_cmd_check_all_emits_elapsed_scan_status_heartbeat():
    """/yt-is sync should emit a time-based scan heartbeat while checking channels."""
    mod = _load_csf_source_module()
    channel_rows = [
        ("https://www.youtube.com/@chan1", "pl-1", 0, None),
        ("https://www.youtube.com/@chan2", "pl-2", 0, None),
        ("https://www.youtube.com/@chan3", "pl-3", 0, None),
    ]

    summary_rows = [
        ("https://www.youtube.com/@chan1", 0, None, None),
        ("https://www.youtube.com/@chan2", 0, None, None),
        ("https://www.youtube.com/@chan3", 0, None, None),
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, channel_rows, summary_rows):
            self._channel_rows = channel_rows
            self._summary_rows = summary_rows

        def execute(self, query, *_args, **_kwargs):
            if "ORDER BY last_checked ASC" in query:
                return FakeCursor(self._channel_rows)
            if "ORDER BY CASE WHEN category IS NULL" in query:
                return FakeCursor(self._summary_rows)
            return FakeCursor([])

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, channel_rows, summary_rows):
            self._channel_rows = channel_rows
            self._summary_rows = summary_rows

        def _ensure_channel_metadata(self):
            return None

        def _get_conn(self):
            return FakeConn(self._channel_rows, self._summary_rows)

    monotonic_value = {"current": 0.0}

    def fake_monotonic():
        monotonic_value["current"] += 31.0
        return monotonic_value["current"]

    class FakeExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args, **kwargs):
            fut = Future()
            fut.set_result(fn(*args, **kwargs))
            return fut

    with mock.patch("csf.batch_status._get_batch_status_storage", return_value=FakeStorage(channel_rows, summary_rows)):
        with mock.patch.object(mod, "_process_channel_check", side_effect=[(1, 10), (0, 20), (2, 30)]):
            with mock.patch.object(mod, "get_entries_for_source", return_value=[]):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch("concurrent.futures.ThreadPoolExecutor", FakeExecutor):
                        with mock.patch("concurrent.futures.as_completed", lambda futures: list(futures)):
                            with mock.patch.object(mod.time, "monotonic", side_effect=fake_monotonic):
                                with mock.patch.object(mod, "log_action") as mock_log:
                                    mod.cmd_check_all(verbose=False)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "sync_scan_progress" in log_names
    heartbeat_payloads = [
        call.args[1]
        for call in mock_log.call_args_list
        if call.args[0] == "sync_scan_progress" and call.args[1].get("trigger") == "elapsed_interval"
    ]
    assert heartbeat_payloads, "expected a time-based sync heartbeat"
    assert heartbeat_payloads[0]["channels_total"] == 3


def test_cmd_fetch_limit_caps_selected_pending_items():
    """cmd_fetch should stop after the requested pending-item limit and log it."""
    mod = _load_csf_source_module()
    channel_rows = [("https://www.youtube.com/@example", "pl-1")]
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        }
        for i in range(200)
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(mod, "process_industrial_batch_reusable") as mock_process:
                                mock_process.return_value = {
                                    f"vid{i:03d}": (True, "transcript", None) for i in range(100)
                                }
                                with mock.patch.object(mod, "close_reusable_ingestor"):
                                    with mock.patch.object(mod, "set_cached_transcript"):
                                        with mock.patch.object(mod, "mark_complete"):
                                            with mock.patch.object(mod, "log_action") as mock_log:
                                                mod.cmd_fetch(dry_run=False, workers=1, max_items=100)

    invoked = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_invoked")
    completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_completed")
    assert invoked["max_items"] == 100
    assert completed["max_items"] == 100
    assert mock_process.call_count == 1
    queued_ids = mock_process.call_args.args[0]
    assert len(queued_ids) == 100
    assert queued_ids[0] == "vid000"
    assert queued_ids[-1] == "vid099"


def test_cmd_fetch_logs_cached_sample_and_hit_rate():
    """cmd_fetch should expose the cached backlog sample and hit rate."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": "vid-a",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        },
        {
            "video_id": "vid-b",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        },
        {
            "video_id": "vid-c",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        },
    ]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(
                        mod,
                        "has_cached_transcript",
                        side_effect=lambda video_id: video_id in {"vid-a", "vid-c"},
                    ):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(mod, "log_action") as mock_log:
                                mod.cmd_fetch(
                                    source_filter="https://www.youtube.com/@example",
                                    dry_run=True,
                                    workers=1,
                                )

    summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_scan_completed")
    triage = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_triage_summary")
    assert summary["cached_total"] == 2
    assert triage["cached_total"] == 2
    assert summary["cached_hit_rate"] == 0.6667
    assert triage["cached_hit_rate"] == 0.6667
    assert summary["cached_sample"] == [
        {"video_id": "vid-a", "source": "https://www.youtube.com/@example", "channel_index": 1},
        {"video_id": "vid-c", "source": "https://www.youtube.com/@example", "channel_index": 1},
    ]
    assert triage["cached_sample"] == summary["cached_sample"]


def test_cmd_fetch_merges_worker_source_profile_totals():
    """cmd_fetch should retain worker-level source profile totals for investigation."""
    mod = _load_csf_source_module()

    totals = mod._empty_source_profile_totals()
    mod._merge_source_profile_totals(
        totals,
        {
            "total": 1,
            "matched": 1,
            "missing": 0,
            "source_class_counts": {"captioned": 1},
            "status_counts": {"pending": 1},
            "privacy_status_counts": {"public": 1},
            "upload_status_counts": {"uploaded": 1},
            "unavailable_reason_counts": {"unknown": 1},
            "failure_reason_counts": {"unknown": 1},
        },
    )
    mod._merge_source_profile_totals(
        totals,
        {
            "total": 2,
            "matched": 2,
            "missing": 0,
            "source_class_counts": {"no_captions": 2},
            "status_counts": {"pending": 2},
            "privacy_status_counts": {"public": 2},
            "upload_status_counts": {"uploaded": 2},
            "unavailable_reason_counts": {"unknown": 2},
            "failure_reason_counts": {"unknown": 2},
        },
    )

    payload = mod._build_fetch_completed_payload(
        source_filter=None,
        strategy="industrial_cli_batch",
        backend="notebooklm_cli_batch",
        backlog_threshold=50,
        batch_size=300,
        workers=4,
        channels_tracked_total=1,
        channels_blocked_total=0,
        channels_active_total=1,
        pending_total=3,
        cached_total=0,
        negative_cache_count=0,
        cached_hit_rate=0.0,
        cached_sample=[],
        negative_cache_reason_counts={},
        negative_cache_sample=[],
        industrial_batches_processed=1,
        transcript_fallback_processed_count=0,
        transcript_fallback_queued_count=0,
        terminal_count=0,
        terminal_reason_counts={},
        worker_cleanup_deleted=0,
        worker_cleanup_failed=0,
        success_count=3,
        fail_count=0,
        skip_count=0,
        processed_count=3,
        elapsed_s=1.0,
        status="completed",
        worker_stage_totals={"batch_elapsed_s_total": 1.0},
        worker_source_profile_totals=totals,
    )

    assert payload["worker_source_profile_totals"]["total"] == 3
    assert payload["worker_source_profile_totals"]["source_class_counts"]["captioned"] == 1
    assert payload["worker_source_profile_totals"]["source_class_counts"]["no_captions"] == 2
    assert payload["industrial_batches_processed"] == 1


def test_cmd_fetch_skips_active_negative_cache_before_routing():
    """cmd_fetch should skip active negative-cache videos before routing them again."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": "vid-good",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        },
        {
            "video_id": "vid-negative",
            "status": "pending",
            "has_captions": False,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        },
    ]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(mod, "get_negative_cache") as mock_negative_cache:
                        mock_negative_cache.side_effect = lambda video_id, db_path=None: (
                            {"video_id": video_id, "reason": "no_transcript", "source": None, "last_stage": "direct_api"}
                            if video_id == "vid-negative"
                            else None
                        )
                        with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                            with mock.patch.object(mod.subprocess, "run") as mock_run:
                                mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                                with mock.patch.object(mod, "log_action") as mock_log:
                                    mod.cmd_fetch(
                                        source_filter="https://www.youtube.com/@example",
                                        dry_run=True,
                                        workers=1,
                                    )

    triage = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_triage_summary")
    scan = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_scan_completed")
    assert triage["negative_cache_count"] == 1
    assert scan["negative_cache_count"] == 1
    assert triage["notebooklm_pending_count"] == 1
    assert triage["transcript_fallback_processed_count"] == 0
    assert triage["transcript_fallback_queued_count"] == 0


def test_cmd_fetch_uses_transcript_fallback_env_names():
    """cmd_fetch should prefer the new transcript-fallback env names and keep aliases working."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        }
        for i in range(300)
    ]

    with mock.patch.dict(
        mod.os.environ,
        {
            "YTIS_TRANSCRIPT_FALLBACK_WORKERS": "3",
            "YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S": "7.5",
        },
        clear=False,
    ):
        with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
            with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
                with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                    with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                        with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                            with mock.patch.object(mod.subprocess, "run") as mock_run:
                                mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                                with mock.patch.object(
                                    mod,
                                    "process_industrial_batch_reusable",
                                    return_value={entry["video_id"]: (True, "transcript", None) for entry in pending_entries},
                                ):
                                    with mock.patch.object(mod, "close_reusable_ingestor"):
                                        with mock.patch.object(mod, "set_cached_transcript"):
                                            with mock.patch.object(mod, "mark_complete"):
                                                with mock.patch.object(mod, "log_action") as mock_log:
                                                    mod.cmd_fetch(
                                                        source_filter="https://www.youtube.com/@example",
                                                        dry_run=False,
                                                        workers=4,
                                                    )

    fetch_invoked = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_invoked")
    assert fetch_invoked["transcript_fallback_workers"] == 3
    assert fetch_invoked["transcript_fallback_min_start_interval_s"] == 7.5


def test_cmd_fetch_defaults_transcript_fallback_workers_to_requested_workers():
    """cmd_fetch should default transcript fallback concurrency to the requested worker count."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": "vid000",
            "status": "pending",
            "has_captions": False,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@example",
        }
    ]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.dict(
                            mod.os.environ,
                            {
                                "YTIS_TRANSCRIPT_FALLBACK_WORKERS": "",
                                "YTIS_AUDIO_FALLBACK_WORKERS": "",
                                "YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S": "",
                                "YTIS_AUDIO_FALLBACK_MIN_START_INTERVAL_S": "",
                            },
                            clear=False,
                        ):
                            with mock.patch.object(mod.subprocess, "run") as mock_run:
                                mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                                with mock.patch.object(mod, "log_action") as mock_log:
                                    mod.cmd_fetch(
                                        source_filter="https://www.youtube.com/@example",
                                        dry_run=True,
                                        workers=4,
                                    )

    invoked = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_invoked")
    assert invoked["transcript_fallback_workers"] == 4
    assert invoked["transcript_fallback_min_start_interval_s"] == 2.5


def test_cmd_fetch_logs_preflight_scan_progress_before_downloads():
    """cmd_fetch logs the preflight channel scan before the first download marker."""
    mod = _load_csf_source_module()
    channel_rows = [(f"https://www.youtube.com/@chan{i:02d}", "pl-1") for i in range(30)]
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@chan00",
        }
        for i in range(300)
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        with mock.patch.object(
                            mod,
                            "process_industrial_batch_reusable",
                            return_value={entry["video_id"]: (True, "transcript", None) for entry in pending_entries},
                        ):
                            with mock.patch.object(mod, "close_reusable_ingestor"):
                                with mock.patch.object(mod, "set_cached_transcript"):
                                    with mock.patch.object(mod, "mark_complete"):
                                        with mock.patch.object(mod, "log_action") as mock_log:
                                            mod.cmd_fetch(dry_run=False, workers=4)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert log_names[0] == "fetch_invoked"
    assert "fetch_strategy_selected" in log_names
    assert "fetch_scan_started" in log_names
    assert "fetch_scan_progress" in log_names
    assert "fetch_scan_completed" in log_names
    assert "fetch_worker_dispatch_started" in log_names
    assert "fetch_completed" in log_names
    assert log_names.index("fetch_scan_started") < log_names.index("fetch_scan_completed")
    assert log_names.index("fetch_scan_started") < log_names.index("fetch_worker_dispatch_started")
    assert log_names.index("fetch_worker_dispatch_started") < log_names.index("fetch_scan_completed")


def test_cmd_fetch_starts_industrial_batch_before_scan_completes_when_buffer_is_full():
    """Industrial fetch should begin once the first batch is full, without waiting for the scan to finish."""
    mod = _load_csf_source_module()
    channel_rows = [
        ("https://www.youtube.com/@chan1", "pl-1"),
        ("https://www.youtube.com/@chan2", "pl-2"),
    ]
    first_channel_pending = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
        }
        for i in range(301)
    ]
    second_channel_pending: list[dict[str, object]] = []

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", side_effect=[first_channel_pending, second_channel_pending]):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        with mock.patch.object(
                            mod,
                            "process_industrial_batch_reusable",
                            return_value={vid: (True, "transcript", None) for vid in [f"vid{i:03d}" for i in range(300)]},
                        ):
                            with mock.patch.object(mod, "close_reusable_ingestor"):
                                with mock.patch.object(mod, "set_cached_transcript"):
                                    with mock.patch.object(mod, "mark_complete"):
                                        with mock.patch.object(mod, "log_action") as mock_log:
                                            mod.cmd_fetch(dry_run=False, workers=4)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "fetch_worker_dispatch_state" in log_names
    assert "fetch_worker_dispatch_started" in log_names
    assert "fetch_scan_completed" in log_names
    assert "fetch_completed" in log_names
    state_payload = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_worker_dispatch_state")
    assert state_payload["workers_requested"] == 4
    assert state_payload["queued_batches"] >= 1
    assert state_payload["available_slots"] >= 1
    assert "last_worker_finished_at" in state_payload
    assert log_names.index("fetch_worker_dispatch_started") < log_names.index("fetch_scan_completed")


def test_take_industrial_dispatch_groups_uses_warm_batch_bundles():
    """A freed worker slot should receive a bounded bundle of batches."""
    mod = _load_csf_source_module()
    batch_queue = [[f"vid{i:03d}"] for i in range(1042)]

    groups = mod._take_industrial_dispatch_groups(batch_queue, 1, 4)

    assert len(groups) == 1
    assert len(groups[0]) == 4
    assert len(batch_queue) == 1038
    assert groups[0][0] == ["vid000"]


def test_load_worker_summary_falls_back_when_result_file_missing():
    """Worker summary parsing should fall back to stdout when the result file is missing."""
    mod = _load_csf_source_module()
    summary = mod._load_worker_summary(
        Path(r"P:\packages\yt-is\tests\missing-worker-result.json"),
        '{"worker_id":"worker-02","succeeded":7,"failed":2,"status":"ok"}',
    )

    assert summary["worker_id"] == "worker-02"
    assert summary["succeeded"] == 7
    assert summary["failed"] == 2
    assert summary["status"] == "ok"


def test_build_worker_health_warning_includes_key_context():
    """Worker health warnings should carry enough context to act on quickly."""
    mod = _load_csf_source_module()
    payload = mod._build_worker_health_warning(
        reason="no_worker_completion_after_15m",
        elapsed_s=901.2,
        active_workers=4,
        queued_batches=12,
        available_slots=0,
        first_worker_finished_at=None,
        last_worker_finished_at=None,
    )

    assert payload["reason"] == "no_worker_completion_after_15m"
    assert payload["elapsed_s"] == 901.2
    assert payload["active_workers"] == 4
    assert payload["queued_batches"] == 12
    assert payload["available_slots"] == 0
    assert payload["first_worker_finished"] is False
    assert payload["last_worker_finished"] is False

    stalled = mod._build_worker_health_warning(
        reason="no_worker_completion_after_15m_since_last_completion",
        elapsed_s=1201.2,
        active_workers=2,
        queued_batches=8,
        available_slots=2,
        first_worker_finished_at=118.0,
        last_worker_finished_at=300.5,
    )

    assert stalled["reason"] == "no_worker_completion_after_15m_since_last_completion"
    assert stalled["elapsed_s"] == 1201.2
    assert stalled["first_worker_finished"] is True
    assert stalled["last_worker_finished"] is True
    assert stalled["first_worker_finished_at"] == 118.0
    assert stalled["last_worker_finished_at"] == 300.5

    oversized = mod._build_worker_health_warning(
        reason="oversized_worker_dispatch",
        elapsed_s=120.0,
        active_workers=3,
        queued_batches=1,
        available_slots=1,
        first_worker_finished_at=118.0,
        last_worker_finished_at=118.0,
        worker_id="worker-02",
        batch_count=1042,
        video_count=312599,
        batch_size=300,
    )

    assert oversized["reason"] == "oversized_worker_dispatch"
    assert oversized["first_worker_finished"] is True
    assert oversized["first_worker_finished_at"] == 118.0
    assert oversized["last_worker_finished"] is True
    assert oversized["last_worker_finished_at"] == 118.0
    assert oversized["worker_id"] == "worker-02"
    assert oversized["batch_count"] == 1042
    assert oversized["video_count"] == 312599
    assert oversized["batch_size"] == 300


def test_cmd_fetch_skips_blocked_channels_in_preflight_scan():
    """Blocked channels should be excluded before get_entries_for_source runs."""
    mod = _load_csf_source_module()
    tracked_rows = [
        ("https://www.youtube.com/@blocked", "pl-blocked"),
        ("https://www.youtube.com/@active", "pl-active"),
    ]
    pending_entries = [("vid01", "pending", None)]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    class FakeScraper:
        def __init__(self, headless: bool = True):
            self.headless = headless

        def preflight_cleanup(self):
            return (0, 0)

        def scrape_with_staging(self, batch):
            return {vid: (True, "transcript", None) for vid in batch}

        def close(self):
            return None

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(tracked_rows)):
        with mock.patch.object(mod, "is_channel_blocked", side_effect=lambda url: url.endswith("blocked")):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=[
                {
                    "video_id": "vid01",
                    "status": "pending",
                    "has_captions": True,
                    "privacy_status": "public",
                    "upload_status": "uploaded",
                    "is_live_content": False,
                    "unavailable_reason": None,
                    "source": "https://www.youtube.com/@active",
                }
            ]) as mock_entries:
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        fake_nlm_scraper = types.ModuleType("csf.nlm_scraper")
                        fake_nlm_scraper.NLMIndustrialScraper = FakeScraper
                        with mock.patch.dict(sys.modules, {"csf.nlm_scraper": fake_nlm_scraper}):
                            with mock.patch.object(mod, "set_cached_transcript"):
                                with mock.patch.object(mod, "mark_complete"):
                                    with mock.patch.object(mod, "log_action") as mock_log:
                                        mod.cmd_fetch(dry_run=False, workers=1)

    mock_entries.assert_called_once_with("https://www.youtube.com/@active")
    started = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_scan_started")
    completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_scan_completed")
    assert started["channels_tracked_total"] == 2
    assert started["channels_blocked_total"] == 1
    assert started["channels_active_total"] == 1
    assert completed["channels_tracked_total"] == 2
    assert completed["channels_blocked_total"] == 1
    assert completed["channels_active_total"] == 1


def test_cmd_fetch_routes_non_captioned_items_to_notebooklm_first():
    """Non-captioned items should stay on the NotebookLM lane before fallback."""
    mod = _load_csf_source_module()
    channel_rows = [("https://www.youtube.com/@active", "pl-1")]
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": False,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@active",
        }
        for i in range(200)
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.dict(
                        mod.os.environ,
                        {
                            "YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S": "0",
                            "YTIS_TRANSCRIPT_FALLBACK_WORKERS": "4",
                        },
                        clear=False,
                    ):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            notebooklm_results = {
                                f"vid{i:03d}": (True, "notebooklm transcript", None)
                                for i in range(200)
                            }
                            with mock.patch.object(mod, "process_industrial_batch_reusable", return_value=notebooklm_results) as mock_process:
                                with mock.patch.object(mod, "close_reusable_ingestor"):
                                    with mock.patch.object(mod, "set_cached_transcript"):
                                        with mock.patch.object(mod, "mark_complete"):
                                            with mock.patch.object(mod, "log_action") as mock_log:
                                                mod.cmd_fetch(dry_run=False, workers=1)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "fetch_completed" in log_names
    assert mock_process.call_count == 1
    assert "transcript_fallback_queued" not in log_names


def test_cmd_fetch_routes_live_items_to_transcript_fallback_first():
    """Live items should bypass NotebookLM and go to transcript fallback."""
    mod = _load_csf_source_module()
    channel_rows = [("https://www.youtube.com/@active", "pl-1")]
    pending_entries = [
        {
            "video_id": "vid-live",
            "status": "pending",
            "has_captions": False,
            "privacy_status": "public",
            "upload_status": "live",
            "is_live_content": True,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@active",
        }
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.dict(
                        mod.os.environ,
                        {
                            "YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S": "0",
                            "YTIS_TRANSCRIPT_FALLBACK_WORKERS": "4",
                        },
                        clear=False,
                    ):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            transcript_result = mock.Mock(
                                transcript="live fallback transcript",
                                lang="en",
                                source="selenium",
                                view_count=None,
                                like_count=None,
                                comment_count=None,
                                duration=None,
                                video_title=None,
                                video_description=None,
                                error=None,
                            )
                            with mock.patch("csf.transcript.fetch_transcript_chain", return_value=transcript_result) as mock_fetch:
                                with mock.patch.object(mod, "process_industrial_batch_reusable") as mock_process:
                                    with mock.patch.object(mod, "close_reusable_ingestor"):
                                        with mock.patch.object(mod, "set_cached_transcript"):
                                            with mock.patch.object(mod, "mark_complete"):
                                                with mock.patch.object(mod, "log_action") as mock_log:
                                                    mod.cmd_fetch(dry_run=False, workers=1)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "fetch_completed" in log_names
    assert mock_process.call_count == 0
    assert mock_fetch.call_count == 1
    assert all(call.kwargs.get("skip_notebooklm") is True for call in mock_fetch.call_args_list)


def test_cmd_fetch_logs_worker_prewarm_summary_before_dispatch(tmp_path):
    """Industrial fetch should log the worker cleanup/prewarm summary before dispatch."""
    mod = _load_csf_source_module()
    channel_rows = [("https://www.youtube.com/@chan1", "pl-1")]
    pending_entries = [
        {
            "video_id": f"vid{i:03d}",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
            "source": "https://www.youtube.com/@chan1",
        }
        for i in range(200)
    ]

    class FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class FakeConn:
        def __init__(self, rows):
            self._rows = rows

        def execute(self, *_args, **_kwargs):
            return FakeCursor(self._rows)

        def close(self):
            return None

    class FakeStorage:
        def __init__(self, rows):
            self._rows = rows

        def _get_conn(self):
            return FakeConn(self._rows)

    def mock_run(cmd, **_kwargs):
        if isinstance(cmd, list) and "dev.worker_pool.worker_main" in cmd:
            result_path = Path(cmd[cmd.index("--result-path") + 1])
            result_path.write_text(
                json.dumps(
                    {
                            "worker_id": "worker-01",
                            "input": "batches.json",
                            "batch_count": 1,
                            "video_count": 200,
                            "succeeded": 200,
                        "failed": 0,
                        "startup_retire_elapsed_s": 0.25,
                        "startup_notebook_check_elapsed_s": 0.5,
                        "startup_notebook_create_elapsed_s": 1.25,
                        "startup_prepare_cleanup_elapsed_s": 0.75,
                        "startup_prepare_total_elapsed_s": 2.75,
                        "setup_elapsed_s_total": 12.5,
                        "notebook_check_elapsed_s_total": 0.5,
                        "notebook_create_elapsed_s_total": 1.25,
                        "notebook_retire_elapsed_s_total": 0.25,
                        "add_sources_elapsed_s_total": 4.75,
                        "extract_elapsed_s_total": 7.0,
                        "cleanup_elapsed_s_total": 1.5,
                        "batch_elapsed_s_total": 25.75,
                        "status": "ok",
                        "returncode": 0,
                        "state_path": "P:/__csf/.data/yt-is/industrial-worker-states/worker-01.json",
                        "notebook_title": "yt-is-worker-01",
                    }
                ),
                encoding="utf-8",
            )
            return mock.MagicMock(
                returncode=0,
                stdout='worker start\n{"worker_id":"worker-01","phase":"cleanup"}\nnot-json-final-line\n',
                stderr="",
            )
        return mock.MagicMock(returncode=0, stdout="", stderr="")

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod, "cleanup_stale_worker_notebooks", return_value=(3, 1)):
                        with mock.patch.object(mod.subprocess, "run", side_effect=mock_run):
                            with mock.patch.object(mod, "close_reusable_ingestor"):
                                with mock.patch.object(mod, "set_cached_transcript"):
                                    with mock.patch.object(mod, "mark_complete"):
                                        with mock.patch.object(mod, "log_action") as mock_log:
                                            mod.cmd_fetch(dry_run=False, workers=2)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "fetch_worker_prewarm_summary" in log_names
    summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_worker_prewarm_summary")
    assert summary["workers_requested"] == 2
    assert summary["workers_active"] == 2
    assert summary["prewarm_expected"] == 2
    assert summary["cleanup_deleted"] == 3
    assert summary["cleanup_failed"] == 1
    worker_finished = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_worker_finished")
    assert worker_finished["summary"]["succeeded"] == 200
    assert worker_finished["summary"]["failed"] == 0
    completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_completed")
    assert completed["worker_cleanup_deleted"] == 3
    assert completed["worker_cleanup_failed"] == 1
    assert completed["success_count"] == 200
    assert completed["fail_count"] == 0
    assert completed["processed_count"] == 200
    assert completed["processed_per_min"] is not None
    assert completed["worker_stage_totals"]["batch_elapsed_s_total"] == 25.75
    assert completed["worker_stage_totals"]["add_sources_elapsed_s_total"] == 4.75


def test_cmd_fetch_logs_fetch_start_and_first_download_started_surgical():
    """cmd_fetch logs a run-start marker and a first-download marker for surgical runs."""
    mod = _load_csf_source_module()
    pending_entries = [
        {
            "video_id": "vid01",
            "status": "pending",
            "has_captions": True,
            "privacy_status": "public",
            "upload_status": "uploaded",
            "is_live_content": False,
            "unavailable_reason": None,
        }
    ]

    class FakeFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

        def cancel(self):
            return True

    class FakeExecutor:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, *args, **kwargs):
            return FakeFuture(fn(*args, **kwargs))

    def fake_as_completed(futures):
        return list(futures)

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch("concurrent.futures.ThreadPoolExecutor", FakeExecutor):
                                with mock.patch("concurrent.futures.as_completed", fake_as_completed):
                                    with mock.patch("csf.transcript.fetch_transcript_chain") as mock_fetch:
                                        mock_fetch.return_value = mock.MagicMock(
                                            transcript="transcript",
                                            source="yt-dlp",
                                            lang="en",
                                            view_count=None,
                                            like_count=None,
                                            comment_count=None,
                                            duration=None,
                                            video_title=None,
                                            video_description=None,
                                            error=None,
                                        )
                                        with mock.patch.object(mod, "set_cached_transcript"):
                                                with mock.patch.object(mod, "mark_complete"):
                                                    with mock.patch.object(mod, "log_action") as mock_log:
                                                        mod.cmd_fetch(
                                                            source_filter="https://www.youtube.com/@example",
                                                            dry_run=False,
                                                            workers=1,
                                                        )

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert log_names[0] == "fetch_invoked"
    assert "fetch_scan_started" in log_names
    assert "fetch_scan_completed" in log_names
    assert "first_download_started" in log_names
    assert log_names.index("fetch_scan_started") < log_names.index("fetch_scan_completed")
    assert log_names.index("fetch_scan_completed") < log_names.index("first_download_started")
    first_payload = mock_log.call_args_list[log_names.index("first_download_started")].args[1]
    assert first_payload["kind"] == "surgical"
    assert first_payload["video_id"] == "vid01"
    assert first_payload["source_url"] == "https://www.youtube.com/@example"
    assert "elapsed_s" in first_payload
