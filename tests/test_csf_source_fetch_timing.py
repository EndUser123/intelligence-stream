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

    class FakeScraper:
        def __init__(self, headless: bool = True):
            self.headless = headless

        def preflight_cleanup(self):
            return (0, 0)

        def scrape_with_staging(self, batch):
            return {vid: (True, "transcript", None) for vid in batch}

        def close(self):
            return None

    with mock.patch.object(mod, "_get_batch_status_storage", return_value=mock.MagicMock()):
        with mock.patch.object(mod, "get_channel_metadata", return_value={"playlist_id": "pl-1"}):
            with mock.patch.object(mod, "is_channel_blocked", return_value=False):
                with mock.patch.object(mod, "get_entries_for_source", return_value=pending_entries):
                    with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch("csf.nlm_scraper.NLMIndustrialScraper", side_effect=FakeScraper):
                                with mock.patch.object(mod, "set_cached_transcript"):
                                    with mock.patch.object(mod, "mark_complete"):
                                        with mock.patch.object(mod, "log_action") as mock_log:
                                            mod.cmd_fetch(
                                                source_filter="https://www.youtube.com/@example",
                                                dry_run=False,
                                                workers=1,
                                            )

    assert mock_log.call_args_list[0].args[0] == "fetch_invoked"
    assert mock_log.call_args_list[1].args[0] == "first_download_started"
    first_payload = mock_log.call_args_list[1].args[1]
    assert first_payload["kind"] == "industrial"
    assert first_payload["batch_index"] == 1
    assert first_payload["batch_size"] == 50
    assert first_payload["first_video_id"] == "vid00"
    assert "elapsed_s" in first_payload


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
                with mock.patch.object(mod, "get_entries_for_source", return_value=pending_entries):
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

    assert mock_log.call_args_list[0].args[0] == "fetch_invoked"
    assert mock_log.call_args_list[1].args[0] == "first_download_started"
    first_payload = mock_log.call_args_list[1].args[1]
    assert first_payload["kind"] == "surgical"
    assert first_payload["video_id"] == "vid01"
    assert first_payload["source_url"] == "https://www.youtube.com/@example"
    assert "elapsed_s" in first_payload
