"""Tests for fetch timing logs in bin/csf-source."""

from __future__ import annotations

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
    """cmd_fetch logs a run-start marker and a first-download marker for industrial runs."""
    mod = _load_csf_source_module()
    pending_entries = [(f"vid{i:02d}", "pending", None) for i in range(50)]

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_pending_by_source", return_value=[vid for vid, _, _ in pending_entries]):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch.object(
                                mod,
                                "process_industrial_batch_reusable",
                                return_value={vid: (True, "transcript", None) for vid, _, _ in pending_entries},
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
    assert first_payload["batch_size"] == 50
    assert first_payload["first_video_id"] == "vid00"
    assert "elapsed_s" in first_payload


def test_cmd_fetch_logs_preflight_scan_progress_before_downloads():
    """cmd_fetch logs the preflight channel scan before the first download marker."""
    mod = _load_csf_source_module()
    channel_rows = [(f"https://www.youtube.com/@chan{i:02d}", "pl-1") for i in range(30)]
    pending_entries = [(f"vid{i:02d}", "pending", None) for i in range(1)]

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
            with mock.patch.object(mod, "get_pending_by_source", return_value=[vid for vid, _, _ in pending_entries]):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        with mock.patch.object(mod, "process_industrial_batch_reusable", return_value={"vid00": (True, "transcript", None)}):
                            with mock.patch.object(mod, "close_reusable_ingestor"):
                                with mock.patch.object(mod, "set_cached_transcript"):
                                    with mock.patch.object(mod, "mark_complete"):
                                        with mock.patch.object(mod, "log_action") as mock_log:
                                            mod.cmd_fetch(dry_run=False, workers=1)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert log_names[0] == "fetch_invoked"
    assert "fetch_strategy_selected" in log_names
    assert "fetch_scan_started" in log_names
    assert "fetch_scan_progress" in log_names
    assert "fetch_scan_completed" in log_names
    assert "first_download_started" in log_names
    assert "fetch_completed" in log_names
    assert log_names.index("fetch_scan_started") < log_names.index("fetch_scan_completed")
    assert log_names.index("fetch_scan_completed") < log_names.index("first_download_started")


def test_cmd_fetch_starts_industrial_batch_before_scan_completes_when_buffer_is_full():
    """Industrial fetch should begin once the first batch is full, without waiting for the scan to finish."""
    mod = _load_csf_source_module()
    channel_rows = [
        ("https://www.youtube.com/@chan1", "pl-1"),
        ("https://www.youtube.com/@chan2", "pl-2"),
    ]
    first_channel_pending = [(f"vid{i:03d}", "pending", None) for i in range(301)]
    second_channel_pending = []

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
            with mock.patch.object(mod, "get_pending_by_source", side_effect=[ [vid for vid, _, _ in first_channel_pending], second_channel_pending ]):
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
                                            mod.cmd_fetch(dry_run=False, workers=1)

    log_names = [call.args[0] for call in mock_log.call_args_list]
    assert "first_download_started" in log_names
    assert "fetch_scan_completed" in log_names
    assert "fetch_completed" in log_names
    assert log_names.index("first_download_started") < log_names.index("fetch_scan_completed")


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
            with mock.patch.object(mod, "get_pending_by_source", return_value=[vid for vid, _, _ in pending_entries]) as mock_entries:
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.object(mod.subprocess, "run") as mock_run:
                        mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                        with mock.patch("csf.nlm_scraper.NLMIndustrialScraper", side_effect=FakeScraper):
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


def test_cmd_fetch_logs_fetch_start_and_first_download_started_surgical():
    """cmd_fetch logs a run-start marker and a first-download marker for surgical runs."""
    mod = _load_csf_source_module()
    pending_entries = [("vid01", "pending", None)]

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
                with mock.patch.object(mod, "get_pending_by_source", return_value=[vid for vid, _, _ in pending_entries]):
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
