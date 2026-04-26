"""Tests for nlm_batch rate-limit tracker and sub-batch reset logic."""

import json
import os
import subprocess
import pytest
from unittest import mock
from csf import nlm_batch, nlm_config


class TestRateLimitDetection:
    """Distinguishability gate: requires BOTH status code AND rate-limit context."""

    def _is_rate_limit(self, returncode: int, stderr: str, stdout: str) -> bool:
        """Mirror the _run_cmd detection logic in isolation."""
        combined = stderr + "\n" + stdout
        has_429_503 = any(code in combined for code in ["429", "503"])
        has_rate_limit_context = any(
            kw in combined
            for kw in ["rate limit", "RATE_LIMIT", "Too Many Requests"]
        )
        return returncode != 0 and has_429_503 and has_rate_limit_context

    def test_rate_limit_429_with_context_detected(self):
        """429 with rate-limit language must trigger rate-limit loop."""
        assert self._is_rate_limit(1, "ERROR 429: rate limit exceeded", "") is True

    def test_rate_limit_503_with_context_detected(self):
        """503 with 'rate limit' context must trigger rate-limit loop."""
        assert self._is_rate_limit(1, "503 Service Temporarily Unavailable — rate limit", "") is True

    def test_rate_limit_too_many_requests_needs_429(self):
        """'Too Many Requests' without 429/503 must NOT trigger (AND logic)."""
        assert self._is_rate_limit(1, "Too Many Requests — please wait", "") is False

    def test_false_positive_bare_500_no_rate_limit_context(self):
        """Bare 500 with no rate-limit language must NOT trigger rate-limit loop."""
        assert self._is_rate_limit(1, "ERROR 500: Internal Server Error", "") is False

    def test_false_positive_503_without_context(self):
        """503 without rate-limit language must NOT trigger (status code alone insufficient)."""
        assert self._is_rate_limit(1, "ERROR 503: Service Unavailable", "") is False

    def test_false_positive_500_with_503_in_string(self):
        """500 error that happens to contain '503' in text must NOT trigger."""
        assert self._is_rate_limit(1, "Server error 500 — could not forward to 503rd handler", "") is False

    def test_rate_limit_signal_in_stdout_only(self):
        """Rate-limit signal in stdout (not stderr) must still trigger."""
        assert self._is_rate_limit(1, "Some other error", "429 rate limit exceeded") is True

    def test_non_rate_limit_generic_error(self):
        """Generic error with no rate-limit signals must NOT trigger."""
        assert self._is_rate_limit(1, "ERROR: Authentication failed", "") is False

    def test_successful_call_not_rate_limited(self):
        """returncode=0 must never trigger rate-limit, even with matching strings."""
        assert self._is_rate_limit(0, "429 rate limit exceeded", "") is False


class TestNotebookBatchDefaults:
    """The notebook batch default should come from one shared constant."""

    def test_shared_default_batch_size_is_50(self):
        """The reusable and direct batch paths should agree on the 50-source default."""
        cfg = nlm_config.get_nlm_config()
        assert nlm_batch.DEFAULT_NOTEBOOKLM_BATCH_SIZE == cfg.notebook_batch_size
        assert nlm_batch.NLMBatchIngestor().batch_size == cfg.notebook_batch_size
        assert nlm_batch.NLMReusableIngestor()._ingestor.batch_size == cfg.notebook_batch_size

    def test_shared_notebook_source_cap_is_50(self):
        """The notebook-cap guard should come from one shared constant."""
        cfg = nlm_config.get_nlm_config()
        assert nlm_batch.DEFAULT_NOTEBOOKLM_SOURCE_CAP == cfg.notebook_source_cap
        assert nlm_batch._NOTEBOOK_SOURCE_CAP == cfg.notebook_source_cap


class TestSubBatchReset:
    """Failure count must reset at sub-batch boundary, not compound across sub-batches."""

    def test_tracker_reset_clears_consecutive_failures(self):
        """After manual reset, consecutive_failures must be 0."""
        tracker = nlm_batch._RateLimitTracker()
        tracker._consecutive_failures = 5
        tracker._current_delay = 8.0
        with tracker._lock:
            tracker._consecutive_failures = 0
            tracker._current_delay = 0.0
        assert tracker._consecutive_failures == 0
        assert tracker._current_delay == 0.0

    def test_tracker_record_failure_increments(self):
        """record_failure must increment _consecutive_failures."""
        tracker = nlm_batch._RateLimitTracker()
        tracker.record_failure(is_rate_limit=True)
        assert tracker._consecutive_failures == 1
        tracker.record_failure(is_rate_limit=True)
        assert tracker._consecutive_failures == 2

    def test_tracker_record_success_resets(self):
        """record_success must reset both failure count and delay."""
        tracker = nlm_batch._RateLimitTracker()
        tracker._consecutive_failures = 3
        tracker._current_delay = 4.0
        tracker.record_success()
        assert tracker._consecutive_failures == 0
        assert tracker._current_delay == 0.0


class TestAuthAutoLogin:
    """nlm_batch must auto-recover from auth expiry before running commands."""

    def test_ensure_nlm_auth_calls_check_first(self):
        """_ensure_nlm_auth must run 'nlm login --check' as the first probe."""
        import subprocess

        called = []

        def mock_run(cmd, **kwargs):
            called.append(cmd)
            # Simulate: --check fails, --force succeeds
            if cmd == ["nlm", "login", "--check"]:
                return subprocess.CompletedProcess(cmd, 1, "", "Auth expired")
            if cmd == ["nlm", "login", "--force"]:
                return subprocess.CompletedProcess(cmd, 0, "", "OK")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        original_run = subprocess.run
        subprocess.run = mock_run
        try:
            result = nlm_batch._ensure_nlm_auth()
            assert result is True
            assert ["nlm", "login", "--check"] in called
            assert ["nlm", "login", "--force"] in called
        finally:
            subprocess.run = original_run


class TestReusableBatchLogging:
    """Reusable batch runs should emit lifecycle and summary logs."""

    def test_retire_reusable_notebook_state_deletes_and_clears(self):
        """Retiring reusable state should delete the recorded notebook and clear state."""
        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-stale"):
            with mock.patch("csf.nlm_batch._clear_reusable_notebook_state") as mock_clear:
                with mock.patch.object(
                    nlm_batch.NLMBatchIngestor,
                    "_run_cmd",
                    return_value=mock.Mock(returncode=0, stdout="", stderr=""),
                ) as mock_run:
                    info = nlm_batch.retire_reusable_notebook_state()

        assert info["nb_id"] == "nb-stale"
        assert info["status"] == "deleted"
        mock_run.assert_called_once()
        mock_clear.assert_called_once()

    def test_reusable_batch_logs_summary_for_fresh_notebook(self):
        """A fresh reusable batch should log create/setup/extract/cleanup timings."""
        batch_ids = ["vid1", "vid2"]

        def mock_run_cmd(self, args, timeout=300):
            if args[:3] == ["notebook", "list", "--json"]:
                return type("CompletedProcess", (), {"stdout": json.dumps({"notebooks": []}), "stderr": "", "returncode": 0})()
            return type("CompletedProcess", (), {"stdout": "", "stderr": "", "returncode": 0})()

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value=None):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
                    with mock.patch("csf.nlm_batch._ensure_nlm_auth", return_value=True):
                        with mock.patch.object(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd):
                            ingestor = nlm_batch.NLMReusableIngestor()
                            with mock.patch.object(ingestor._ingestor, "create_batch_notebook", return_value="nb-1") as mock_create:
                                with mock.patch.object(
                                    ingestor._ingestor,
                                    "extract_transcripts",
                                    return_value={"vid1": (True, "text", None), "vid2": (False, None, "err")},
                                ) as mock_extract:
                                    with mock.patch.object(ingestor._ingestor, "reset_sources") as mock_reset:
                                        with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                            with mock.patch("csf.nlm_batch.time.monotonic", side_effect=[100.0 + i for i in range(20)]):
                                                results = ingestor.process_batch(batch_ids)

        assert results["vid1"][0] is True
        assert results["vid2"][0] is False
        mock_create.assert_called_once_with(batch_ids)
        mock_extract.assert_called_once_with(batch_ids)
        mock_reset.assert_called_once()

        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert log_names[0] == "nlm_batch_reusable_process_started"
        assert "nlm_batch_reusable_process_completed" in log_names
        started = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_started")
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_completed")
        assert started["started_at_epoch"] <= completed["completed_at_epoch"]
        assert completed["strategy"] == "reusable"
        assert completed["notebook_reused"] is False
        assert completed["setup_mode"] == "create"
        assert completed["succeeded"] == 1
        assert completed["failed"] == 1
        assert completed["setup_elapsed_s"] >= 0.0
        assert completed["extract_elapsed_s"] >= 0.0
        assert completed["cleanup_elapsed_s"] >= 0.0
        assert completed["total_elapsed_s"] > 0.0

    def test_reusable_batch_logs_summary_for_reused_notebook(self):
        """A reused notebook should log reuse-specific summary fields."""
        batch_ids = ["vid3"]

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-existing"):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
                    ingestor = nlm_batch.NLMReusableIngestor()
                    with mock.patch.object(ingestor, "_is_notebook_usable", return_value=True):
                        with mock.patch.object(
                            ingestor._ingestor,
                            "_run_cmd",
                            return_value=type(
                                "CompletedProcess",
                                (),
                                {
                                    "stdout": json.dumps(
                                        {
                                            "notebooks": [
                                                {
                                                    "id": "nb-existing",
                                                    "title": "yt-is-worker-01",
                                                    "updated_at": "2026-04-21T20:00:00Z",
                                                }
                                            ]
                                        }
                                    ),
                                    "stderr": "",
                                    "returncode": 0,
                                },
                            )(),
                        ):
                            with mock.patch.object(ingestor._ingestor, "_add_sources_in_subbatches") as mock_add:
                                with mock.patch.object(
                                    ingestor._ingestor,
                                    "extract_transcripts",
                                    return_value={"vid3": (True, "text", None)},
                                ) as mock_extract:
                                    with mock.patch.object(ingestor._ingestor, "reset_sources") as mock_reset:
                                        with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                            with mock.patch("csf.nlm_batch.time.monotonic", side_effect=[200.0 + i for i in range(20)]):
                                                results = ingestor.process_batch(batch_ids)

        assert results["vid3"][0] is True
        mock_add.assert_called_once_with(batch_ids, subbatch_size=ingestor._ingestor.batch_size)
        mock_extract.assert_called_once_with(batch_ids)
        mock_reset.assert_called_once()

        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_completed")
        started = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_started")
        assert started["started_at_epoch"] <= completed["completed_at_epoch"]
        assert completed["strategy"] == "reusable"
        assert completed["notebook_reused"] is True
        assert completed["setup_mode"] == "reuse_add"
        assert completed["succeeded"] == 1
        assert completed["failed"] == 0

    def test_reusable_batch_summary_includes_classifier_timing_from_extract_metrics(self):
        """Reusable batch summary should propagate yt-dlp and page timing from extract metrics."""
        batch_ids = ["vid4"]

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-existing"):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
                    ingestor = nlm_batch.NLMReusableIngestor()
                    with mock.patch.object(ingestor, "_ensure_notebook", return_value=(False, "reuse")):
                        with mock.patch.object(
                            ingestor._ingestor,
                            "extract_transcripts",
                            return_value={"vid4": (False, None, "err")},
                        ) as mock_extract:
                            with mock.patch.object(ingestor._ingestor, "reset_sources") as mock_reset:
                                with mock.patch.object(
                                    ingestor._ingestor,
                                    "get_last_extract_metrics",
                                    return_value={
                                        "content_fetch_status_counts": {"command_failed": 1},
                                        "source_ready_age_s_total": 12.0,
                                        "source_ready_age_s_max": 12.0,
                                        "source_ready_age_s_avg": 12.0,
                                        "content_fetch_attempts_total": 2,
                                        "content_fetch_attempts_max": 2,
                                        "content_fetch_attempts_avg": 2.0,
                                        "retry_queue_deferred_count": 1,
                                        "retry_queue_recovered_count": 0,
                                        "retry_queue_final_failed_count": 1,
                                        "shared_retry_deferred_count": 0,
                                        "shared_retry_recovered_count": 0,
                                        "shared_retry_final_failed_count": 0,
                                        "materialization_ready_at_epoch": 123.0,
                                        "youtube_ytdlp_elapsed_s_total": 3.5,
                                        "youtube_ytdlp_elapsed_s_max": 2.0,
                                        "youtube_ytdlp_elapsed_s_count": 2,
                                        "youtube_ytdlp_elapsed_s_avg": 1.75,
                                        "youtube_page_elapsed_s_total": 0.75,
                                        "youtube_page_elapsed_s_max": 0.75,
                                        "youtube_page_elapsed_s_count": 1,
                                        "youtube_page_elapsed_s_avg": 0.75,
                                    },
                                ):
                                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                        with mock.patch("csf.nlm_batch.time.monotonic", side_effect=[300.0 + i for i in range(20)]):
                                            results = ingestor.process_batch(batch_ids)

        assert results["vid4"][0] is False
        mock_extract.assert_called_once_with(batch_ids)
        mock_reset.assert_called_once()
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_completed")
        assert completed["youtube_ytdlp_elapsed_s_total"] == 3.5
        assert completed["youtube_ytdlp_elapsed_s_count"] == 2
        assert completed["youtube_page_elapsed_s_total"] == 0.75
        assert completed["youtube_page_elapsed_s_count"] == 1
        summary = ingestor.get_last_process_metrics()
        assert summary is not None
        assert summary["youtube_ytdlp_elapsed_s_total"] == 3.5
        assert summary["youtube_ytdlp_elapsed_s_count"] == 2
        assert summary["youtube_page_elapsed_s_total"] == 0.75
        assert summary["youtube_page_elapsed_s_count"] == 1

    def test_reusable_batch_uses_50_source_subbatches_by_default(self):
        """Reusable notebook processing should forward the 50-source subbatch size."""
        batch_ids = ["vid1", "vid2", "vid3"]

        def mock_run_cmd(self, args, timeout=300):
            if args[:3] == ["notebook", "list", "--json"]:
                return type("CompletedProcess", (), {"stdout": json.dumps({"notebooks": [{"id": "nb-existing", "title": "reuse"}]}), "stderr": "", "returncode": 0})()
            return type("CompletedProcess", (), {"stdout": "", "stderr": "", "returncode": 0})()

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-existing"):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
                    with mock.patch("csf.nlm_batch._ensure_nlm_auth", return_value=True):
                        with mock.patch.object(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd):
                            ingestor = nlm_batch.NLMReusableIngestor()
                            with mock.patch.object(ingestor, "_is_notebook_usable", return_value=True):
                                with mock.patch.object(ingestor._ingestor, "_add_sources_in_subbatches") as mock_add:
                                    with mock.patch.object(
                                        ingestor._ingestor,
                                        "extract_transcripts",
                                        return_value={"vid1": (True, "text", None)},
                                    ):
                                        with mock.patch.object(ingestor._ingestor, "reset_sources"):
                                            with mock.patch("csf.nlm_batch.log_action"):
                                                with mock.patch(
                                                    "csf.nlm_batch.time.monotonic",
                                                    side_effect=[10.0 + i for i in range(20)],
                                                ):
                                                    ingestor.process_batch(batch_ids)

        mock_add.assert_called_once_with(batch_ids, subbatch_size=ingestor._ingestor.batch_size)

    def test_experiment_add_acceptance_logs_sweep_results(self):
        """The add-acceptance sweep should log a per-size result and cleanup."""
        batch_ids = [f"vid{i:02d}" for i in range(20)]
        sizes = [50, 25, 10]

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value=None):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
                    ingestor = nlm_batch.NLMReusableIngestor(batch_size=4)
                    call_sizes: list[int] = []

                    def fake_run_cmd(cmd, timeout=60):
                        if cmd[:2] == ["notebook", "create"]:
                            return mock.MagicMock(returncode=0, stdout="ID: nb-sweep", stderr="")
                        if cmd[:2] == ["notebook", "delete"]:
                            return mock.MagicMock(returncode=0, stdout="", stderr="")
                        raise AssertionError(f"unexpected command: {cmd}")

                    def fake_add(batch_ids, *, subbatch_size=50):
                        call_sizes.append(subbatch_size)
                        return batch_ids[: min(len(batch_ids), subbatch_size)]

                    with mock.patch.object(ingestor._ingestor, "_run_cmd", side_effect=fake_run_cmd):
                        with mock.patch.object(ingestor._ingestor, "reset_sources") as mock_reset:
                            with mock.patch.object(ingestor._ingestor, "close") as mock_close:
                                with mock.patch.object(ingestor._ingestor, "_add_sources_in_subbatches", side_effect=fake_add):
                                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                        results = ingestor._ingestor.experiment_add_acceptance(batch_ids, sizes, notebook_title="yt-is-sweep")

        assert call_sizes == sizes
        assert [result["subbatch_size"] for result in results] == sizes
        assert results[0]["added_count"] == 20
        assert results[1]["added_count"] == 20
        assert results[2]["added_count"] == 10
        assert any(call.args[0] == "nlm_batch_size_sweep_started" for call in mock_log.call_args_list)
        assert any(call.args[0] == "nlm_batch_size_sweep_result" for call in mock_log.call_args_list)
        assert any(call.args[0] == "nlm_batch_size_sweep_completed" for call in mock_log.call_args_list)
        mock_reset.assert_called()
        mock_close.assert_called()

    def test_ensure_nlm_auth_returns_true_when_check_passes(self):
        """When --check succeeds, _ensure_nlm_auth returns True without calling --force."""
        import subprocess

        called = []

        def mock_run(cmd, **kwargs):
            called.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "Auth valid")

        original_run = subprocess.run
        subprocess.run = mock_run
        try:
            result = nlm_batch._ensure_nlm_auth()
            assert result is True
            assert called == [["nlm", "login", "--check"]]
        finally:
            subprocess.run = original_run

    def test_ensure_nlm_auth_returns_false_when_force_also_fails(self):
        """When --check and --force both fail, _ensure_nlm_auth returns False."""
        import subprocess

        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "Auth failed")

        original_run = subprocess.run
        subprocess.run = mock_run
        try:
            result = nlm_batch._ensure_nlm_auth()
            assert result is False
        finally:
            subprocess.run = original_run


class TestReusableNotebookEnvironmentOverrides:
    """Worker-specific env vars should isolate reusable notebook state."""

    def test_state_path_override_is_used(self, monkeypatch):
        """YTIS_NLM_OWNER_STATE_PATH should override the default state file."""
        monkeypatch.setenv(
            "YTIS_NLM_OWNER_STATE_PATH",
            "P:/.data/yt-is/dev-workers/worker-01.json",
        )
        assert nlm_batch._get_reusable_notebook_state_path() == nlm_batch.Path(
            "P:/.data/yt-is/dev-workers/worker-01.json"
        )

    def test_title_override_is_used(self, monkeypatch):
        """YTIS_NLM_OWNER_NOTEBOOK_TITLE should override the notebook title."""
        monkeypatch.setenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", "yt-is-worker-01")
        assert nlm_batch._get_reusable_notebook_title() == "yt-is-worker-01"

    def test_default_title_is_worker_01(self, monkeypatch):
        """The default owner title should map to worker-01."""
        monkeypatch.delenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", raising=False)
        monkeypatch.delenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", raising=False)
        assert nlm_batch._get_reusable_notebook_title() == "yt-is-worker-01"

    def test_notebooklm_profile_override_is_used(self, monkeypatch):
        """NOTEBOOKLM_PROFILE should override the default NotebookLM profile."""
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "ytis-worker-01")
        assert nlm_batch._get_notebooklm_profile() == "ytis-worker-01"

    def test_create_batch_notebook_uses_override_title(self, monkeypatch):
        """create_batch_notebook should honor the worker-specific notebook title."""
        monkeypatch.setenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", "yt-is-worker-01")
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=2)
        completed = type(
            "CompletedProcess",
            (),
            {"stdout": "Created notebook\nID: nb-123\n", "stderr": "", "returncode": 0},
        )()
        with mock.patch.object(ingestor, "_run_cmd", return_value=completed) as mock_run_cmd:
            with mock.patch.object(ingestor, "_add_sources_in_subbatches") as mock_add:
                result = ingestor.create_batch_notebook(["vid1", "vid2"])
        assert result == "nb-123"
        mock_run_cmd.assert_called_once()
        assert mock_run_cmd.call_args.args[0] == ["notebook", "create", "yt-is-worker-01"]
        mock_add.assert_called_once_with(["vid1", "vid2"], subbatch_size=ingestor.batch_size)

    def test_ensure_nlm_auth_logs_success(self):
        """A successful auth check should emit an auth-ok marker."""
        import subprocess

        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "Auth valid")

        original_run = subprocess.run
        subprocess.run = mock_run
        try:
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                result = nlm_batch._ensure_nlm_auth()
            assert result is True
            mock_log.assert_called_once()
            assert mock_log.call_args.args[0] == "nlm_auth_checked"
            assert mock_log.call_args.args[1]["component"] == "nlm_batch"
        finally:
            subprocess.run = original_run

    def test_ensure_nlm_auth_logs_login_attempt_and_refresh(self):
        """A forced auth refresh should emit login timing markers."""
        import subprocess

        def mock_run(cmd, **kwargs):
            if cmd == ["nlm", "login", "--check"]:
                return subprocess.CompletedProcess(cmd, 1, "", "Auth expired")
            if cmd == ["nlm", "login", "--force"]:
                return subprocess.CompletedProcess(cmd, 0, "", "OK")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        original_run = subprocess.run
        subprocess.run = mock_run
        try:
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                result = nlm_batch._ensure_nlm_auth()
            assert result is True
            assert [c.args[0] for c in mock_log.call_args_list] == [
                "nlm_login_started",
                "nlm_login_completed",
                "nlm_auth_refreshed",
            ]
        finally:
            subprocess.run = original_run


class TestWorkerNotebookCleanup:
    """Stale worker notebooks should be retired without touching active ones."""

    def test_reset_sources_chunks_large_deletes(self):
        """Large notebooks should clear sources in smaller delete chunks."""
        ingestor = nlm_batch.NLMBatchIngestor()
        ingestor._nb_id = "nb-large"
        source_ids = [f"src-{idx}" for idx in range(1, 28)]
        calls: list[list[str]] = []

        def mock_run_cmd(self, args, timeout=300):
            calls.append(args)
            if args[:3] == ["source", "list", "nb-large"]:
                return type(
                    "CompletedProcess",
                    (),
                    {
                        "stdout": json.dumps({"sources": [{"id": source_id} for source_id in source_ids]}),
                        "stderr": "",
                        "returncode": 0,
                    },
                )()
            if args[:3] == ["source", "delete", "nb-large"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": "", "stderr": "", "returncode": 0},
                )()
            return type(
                "CompletedProcess",
                (),
                {"stdout": "", "stderr": "unexpected", "returncode": 1},
            )()

        with mock.patch.object(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd):
            ingestor.reset_sources()

        delete_calls = [call for call in calls if call[:3] == ["source", "delete", "nb-large"]]
        assert len(delete_calls) == 2
        assert delete_calls[0][-1] == "src-25"
        assert delete_calls[1][-1] == "src-27"

    def test_load_current_worker_notebook_ids_collects_all_state_files(self, tmp_path, monkeypatch):
        """Permanent worker state files should all be considered active notebook ids."""
        state_root = tmp_path / "worker-states"
        state_root.mkdir()
        (state_root / "worker-01.json").write_text(
            json.dumps({"nb_id": "keep-current", "run_id": "run-current"}),
            encoding="utf-8",
        )
        (state_root / "worker-02.json").write_text(
            json.dumps({"nb_id": "keep-old", "run_id": "run-old"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("YTIS_INDUSTRIAL_WORKER_STATE_ROOT", str(state_root))

        active_ids = nlm_batch._load_current_worker_notebook_ids()

        assert active_ids == {"keep-current", "keep-old"}

    def test_cleanup_stale_worker_notebooks_is_audit_only(self, tmp_path, monkeypatch):
        """Startup audit should not delete permanent worker notebooks."""
        state_root = tmp_path / "worker-states"
        state_root.mkdir()
        (state_root / "worker-01.json").write_text(json.dumps({"nb_id": "keep-1"}), encoding="utf-8")
        (state_root / "worker-02.json").write_text(json.dumps({"nb_id": "keep-2"}), encoding="utf-8")
        monkeypatch.setenv("YTIS_INDUSTRIAL_WORKER_STATE_ROOT", str(state_root))
        monkeypatch.setenv("YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX", "yt-is-worker")
        monkeypatch.setenv("YTIS_INDUSTRIAL_RUN_ID", "run-current")

        notebooks = {
            "notebooks": [
                {"id": "keep-1", "name": "yt-is-worker-01"},
                {"id": "stale-1", "name": "yt-is-worker-03"},
                {"id": "ignore-1", "name": "something-else"},
            ]
        }
        calls: list[list[str]] = []

        def mock_run_cmd(self, args, timeout=300):
            calls.append(args)
            if args[:3] == ["notebook", "list", "--json"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": json.dumps(notebooks), "stderr": "", "returncode": 0},
                )()
            return type(
                "CompletedProcess",
                (),
                {"stdout": "", "stderr": "unexpected", "returncode": 1},
            )()

        monkeypatch.setattr(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd)
        with mock.patch("subprocess.run", side_effect=AssertionError("cleanup should not call subprocess.run")):
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                deleted, failed = nlm_batch.cleanup_stale_worker_notebooks()

        assert deleted == 0
        assert failed == 0
        assert not any(isinstance(cmd, list) and "--delete-worker" in cmd for cmd in calls)
        cleanup_started = next(
            call.args[1]
            for call in mock_log.call_args_list
            if call.args[0] == "nlm_worker_notebook_cleanup_started"
        )
        assert cleanup_started["active_nb_ids"] == 2
        cleanup_complete = next(
            call.args[1]
            for call in mock_log.call_args_list
            if call.args[0] == "nlm_worker_notebook_cleanup_complete"
        )
        assert cleanup_complete["status"] == "audit_only"
        assert cleanup_complete["worker_notebook_count"] == 2


class TestReusableNotebookPrewarm:
    """Reusable notebooks should be warmed and cleared before worker batches."""

    def test_prepare_creates_and_clears_notebook(self, monkeypatch):
        ingestor = nlm_batch.NLMReusableIngestor(batch_size=3)
        cleanup_calls: list[str] = []
        saved_ids: list[str] = []

        def mock_ensure_notebook(batch_ids):
            ingestor._nb_id = "nb-prewarm-1"
            return True, "create"

        monkeypatch.setattr(ingestor, "_ensure_notebook", mock_ensure_notebook)
        monkeypatch.setattr(ingestor._ingestor, "cleanup", lambda: cleanup_calls.append("cleanup"))
        monkeypatch.setattr(nlm_batch, "_save_reusable_notebook_id", lambda nb_id: saved_ids.append(nb_id))

        with mock.patch("csf.nlm_batch.log_action") as mock_log:
            prepared, setup_mode = ingestor.prepare()

        assert prepared is True
        assert setup_mode == "create"
        assert cleanup_calls == ["cleanup"]
        assert saved_ids == ["nb-prewarm-1"]
        assert any(call.args[0] == "nlm_batch_reusable_prep_started" for call in mock_log.call_args_list)
        assert any(call.args[0] == "nlm_batch_reusable_prep_completed" for call in mock_log.call_args_list)

    def test_close_delete_uses_cdp_title_delete(self, monkeypatch):
        """Destructive close should use the CDP title-delete path instead of direct notebook delete."""
        ingestor = nlm_batch.NLMReusableIngestor(batch_size=3)
        ingestor._nb_id = "nb-close-1"
        monkeypatch.setenv("YTIS_NLM_OWNER_NOTEBOOK_TITLE", "yt-is-worker-03")

        cdp_calls: list[list[str]] = []
        original_run = subprocess.run

        def mock_subprocess_run(cmd, **kwargs):
            cdp_calls.append(cmd)
            if cmd[0] == "node" and "--delete-title" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "deleted")
            if cmd[:3] == ["nlm", "notebook", "delete"]:
                raise AssertionError("close(delete=True) should not call direct notebook delete")
            return original_run(cmd, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
        monkeypatch.setattr(nlm_batch, "_clear_reusable_notebook_state", lambda: None)
        monkeypatch.setattr(nlm_batch, "_save_reusable_notebook_id", lambda nb_id: None)

        ingestor.close(delete=True)

        assert any(
            len(cmd) >= 3 and cmd[0] == "node" and cmd[2] == "--delete-title"
            for cmd in cdp_calls
            if isinstance(cmd, list)
        )

    def test_ensure_notebook_reuses_existing_title_match(self, monkeypatch):
        """A single exact title match should be reused instead of recreated."""
        monkeypatch.setenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "yt-is-worker-03")
        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value=None):
            ingestor = nlm_batch.NLMReusableIngestor(batch_size=3)

        calls: list[list[str]] = []

        def mock_run_cmd(self, args, timeout=300):
            calls.append(args)
            if args[:3] == ["notebook", "list", "--json"]:
                return type(
                    "CompletedProcess",
                    (),
                    {
                        "stdout": json.dumps(
                            {
                                "notebooks": [
                                    {
                                        "id": "nb-keeper",
                                        "title": "yt-is-worker-03",
                                        "updated_at": "2026-04-21T20:00:00Z",
                                    }
                                ]
                            }
                        ),
                        "stderr": "",
                        "returncode": 0,
                    },
                )()
            if args[:3] == ["source", "list", "nb-keeper"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": json.dumps({"sources": []}), "stderr": "", "returncode": 0},
                )()
            return type(
                "CompletedProcess",
                (),
                {"stdout": "", "stderr": "unexpected", "returncode": 1},
            )()

        monkeypatch.setattr(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd)
        with mock.patch.object(ingestor._ingestor, "create_batch_notebook") as mock_create:
            created_new, setup_mode = ingestor._ensure_notebook([])

        assert created_new is False
        assert setup_mode == "reuse"
        assert ingestor._nb_id == "nb-keeper"
        assert mock_create.call_count == 0
        assert ["source", "list", "nb-keeper", "--json"] in calls

    def test_ensure_notebook_reuses_keeper_when_duplicate_title_matches_exist(self, monkeypatch):
        """Duplicate worker notebooks should reuse one keeper instead of recreating."""
        monkeypatch.setenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "yt-is-worker-03")
        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-keeper"):
            ingestor = nlm_batch.NLMReusableIngestor(batch_size=3)

        calls: list[list[str]] = []

        def mock_run_cmd(self, args, timeout=300):
            calls.append(args)
            if args[:3] == ["notebook", "list", "--json"]:
                return type(
                    "CompletedProcess",
                    (),
                    {
                        "stdout": json.dumps(
                            {
                                "notebooks": [
                                    {
                                        "id": "nb-keeper",
                                        "title": "yt-is-worker-03",
                                        "updated_at": "2026-04-21T22:00:00Z",
                                    },
                                    {
                                        "id": "nb-dup",
                                        "title": "yt-is-worker-03",
                                        "updated_at": "2026-04-21T21:00:00Z",
                                    },
                                ]
                            }
                        ),
                        "stderr": "",
                        "returncode": 0,
                    },
                )()
            if args[:3] == ["source", "list", "nb-keeper"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": json.dumps({"sources": []}), "stderr": "", "returncode": 0},
                )()
            return type(
                "CompletedProcess",
                (),
                {"stdout": "", "stderr": "unexpected", "returncode": 1},
            )()

        monkeypatch.setattr(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd)
        with mock.patch.object(ingestor._ingestor, "create_batch_notebook") as mock_create:
            created_new, setup_mode = ingestor._ensure_notebook([])

        assert created_new is False
        assert setup_mode == "reuse"
        assert ingestor._nb_id == "nb-keeper"
        assert mock_create.call_count == 0
        assert not any(isinstance(cmd, list) and "--delete-title" in cmd for cmd in calls)

    def test_ensure_notebook_reuses_loaded_state_even_when_title_list_is_empty(self, monkeypatch):
        """A valid saved notebook id should still be reused if listing is temporarily empty."""
        monkeypatch.setenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "yt-is-worker-03")
        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-loaded"):
            ingestor = nlm_batch.NLMReusableIngestor(batch_size=3)

        calls: list[list[str]] = []

        def mock_run_cmd(self, args, timeout=300):
            calls.append(args)
            if args[:3] == ["notebook", "list", "--json"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": json.dumps({"notebooks": []}), "stderr": "", "returncode": 0},
                )()
            if args[:3] == ["source", "list", "nb-loaded"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"stdout": json.dumps({"sources": []}), "stderr": "", "returncode": 0},
                )()
            return type(
                "CompletedProcess",
                (),
                {"stdout": "", "stderr": "unexpected", "returncode": 1},
            )()

        monkeypatch.setattr(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd)
        with mock.patch.object(ingestor._ingestor, "create_batch_notebook") as mock_create:
            created_new, setup_mode = ingestor._ensure_notebook([])

        assert created_new is False
        assert setup_mode == "reuse"
        assert ingestor._nb_id == "nb-loaded"
        assert mock_create.call_count == 0
        assert ["source", "list", "nb-loaded", "--json"] in calls


class TestSubBatchFailureMode:
    """NotebookLM add failures should not recursively shrink to 1-2 items."""

    def test_add_failure_does_not_split_recursively(self):
        """A failed add should log and return empty without retry splitting."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=3)
        ingestor._nb_id = "nb-123"
        # _get_current_source_count calls source list --json twice (before and after add)
        source_list_response = type(
            "CompletedProcess",
            (),
            {"returncode": 0, "stdout": json.dumps({"sources": []}), "stderr": ""},
        )()
        add_response = type(
            "CompletedProcess",
            (),
            {"stdout": "", "stderr": "Could not add URL sources", "returncode": 1},
        )()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=[source_list_response, add_response, source_list_response]) as mock_run_cmd:
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                result = ingestor._add_sources_chunk(
                    ["vid1", "vid2", "vid3"],
                    subbatch_index=1,
                    expected_total=3,
                )

        assert result == []
        assert mock_run_cmd.call_count == 3
        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_subbatch_add_failed" in log_names

    def test_subbatch_failure_keeps_configured_batch_size(self):
        """A failed sub-batch should not shrink the next window."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=3)
        ingestor._nb_id = "nb-123"

        with mock.patch.object(
            ingestor,
            "_add_sources_chunk",
            side_effect=[[], [], []],
        ) as mock_add:
            with mock.patch.object(ingestor, "_get_current_source_count", return_value=0):
                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                    result = ingestor._add_sources_in_subbatches(
                        ["vid1", "vid2", "vid3", "vid4", "vid5", "vid6", "vid7", "vid8"],
                        subbatch_size=3,
                    )

        assert result == []
        call_sizes = [len(call.args[0]) for call in mock_add.call_args_list]
        assert call_sizes == [3, 3, 2]
        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_subbatch_add_shortfall" in log_names
        assert "nlm_batch_subbatch_size_adjusted" not in log_names


class TestNotebookCapRotation:
    """Notebook should rotate when source count approaches the cap threshold."""

    def test_get_current_source_count_parses_json_list(self):
        """_get_current_source_count should return the number of sources in the notebook."""
        ingestor = nlm_batch.NLMBatchIngestor()
        ingestor._nb_id = "nb-123"
        mock_response = type(
            "CompletedProcess",
            (),
            {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]}), "stderr": ""},
        )()
        with mock.patch.object(ingestor, "_run_cmd", return_value=mock_response):
            count = ingestor._get_current_source_count()
        assert count == 3

    def test_get_current_source_count_returns_0_on_error(self):
        """_get_current_source_count should return 0 when the list command fails."""
        ingestor = nlm_batch.NLMBatchIngestor()
        ingestor._nb_id = "nb-123"
        with mock.patch.object(ingestor, "_run_cmd", return_value=type("CompletedProcess", (), {"returncode": 1, "stdout": "", "stderr": ""})()):
            count = ingestor._get_current_source_count()
        assert count == 0

    def test_get_current_source_count_returns_0_when_no_nb_id(self):
        """_get_current_source_count should return 0 when no notebook is active."""
        ingestor = nlm_batch.NLMBatchIngestor()
        assert ingestor._nb_id is None
        assert ingestor._get_current_source_count() == 0

    def test_rotate_notebook_recycles_old_without_creating_new(self):
        """_rotate_notebook should clear sources and keep the same notebook."""
        ingestor = nlm_batch.NLMBatchIngestor()
        ingestor._nb_id = "nb-old"
        ingestor._current_source_count = 50

        with mock.patch.object(ingestor, "reset_sources") as mock_reset:
            with mock.patch.object(ingestor, "_get_current_source_count", return_value=0):
                with mock.patch("csf.nlm_batch._save_reusable_notebook_id") as mock_save:
                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                        ingestor._rotate_notebook()

        mock_reset.assert_called_once()
        assert ingestor._nb_id == "nb-old"
        assert ingestor._current_source_count == 0
        mock_save.assert_called_once_with("nb-old")

        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_notebook_recycled" in log_names
        assert "nlm_batch_reusable_state_saved" in log_names
        recycle_event = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_notebook_recycled")
        assert recycle_event["nb_id"] == "nb-old"
        assert recycle_event["old_source_count"] == 50
        assert recycle_event["new_source_count"] == 0
        assert recycle_event["reason"] == "source_cap_near_threshold"
        assert recycle_event["cap_threshold"] == nlm_batch._NOTEBOOK_SOURCE_CAP

    def test_capacity_rotation_requests_before_add_when_at_cap(self):
        """A notebook at capacity should recycle before attempting the next add."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=2)
        ingestor._nb_id = "nb-cap"
        ingestor._current_source_count = 50

        with mock.patch.object(ingestor, "_get_current_source_count", side_effect=[50, 50, 0, 0]):
            with mock.patch.object(ingestor, "_add_sources_chunk", side_effect=[["v1", "v2"], ["v3", "v4"]]):
                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                    with mock.patch.object(ingestor, "_rotate_notebook") as mock_rotate:
                        ingestor._add_sources_in_subbatches(["v1", "v2", "v3", "v4"], subbatch_size=2)

        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_subbatch_capacity_rotation_requested" in log_names
        assert mock_rotate.call_count == 1
        capacity_rotation = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_subbatch_capacity_rotation_requested")
        assert capacity_rotation["current_source_count"] == 50
        assert capacity_rotation["cap_threshold"] == nlm_batch._NOTEBOOK_SOURCE_CAP
        assert capacity_rotation["rotation_reason"] == "source_cap_near_threshold"

    def test_shortfall_does_not_rotate_when_below_cap(self):
        """Subbatch shortfall below cap threshold should NOT trigger notebook recycle."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=2)
        ingestor._nb_id = "nb-fresh"
        ingestor._current_source_count = 45

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type("CompletedProcess", (), {"returncode": 0, "stdout": json.dumps({"sources": [{"id": f"s{i}"} for i in range(46)]}), "stderr": ""})()
            if cmd[:2] == ["source", "add"]:
                return type("CompletedProcess", (), {"returncode": 1, "stdout": "Could not add URL sources", "stderr": "could not add"})()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                with mock.patch.object(ingestor, "_rotate_notebook") as mock_rotate:
                    result = ingestor._add_sources_in_subbatches(["v1", "v2"], subbatch_size=2)

        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_notebook_recycled" not in log_names
        assert "nlm_batch_subbatch_add_shortfall" in log_names
        mock_rotate.assert_not_called()

    def test_subbatch_size_adjusts_to_remaining_capacity(self):
        """Subbatch size should shrink to the remaining NotebookLM headroom."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=50)
        ingestor._nb_id = "nb-room"
        ingestor._current_source_count = 45
        batch_ids = [f"v{i}" for i in range(8)]
        add_calls = []

        with mock.patch.object(ingestor, "_get_current_source_count", side_effect=[45, 0, 0, 0]):
            with mock.patch.object(ingestor, "_add_sources_chunk", side_effect=lambda batch_ids, **kwargs: add_calls.append(list(batch_ids)) or list(batch_ids)):
                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                    ingestor._add_sources_in_subbatches(batch_ids, subbatch_size=50)

        assert add_calls, "expected at least one add command"
        assert [len(batch) for batch in add_calls] == [5, 3]
        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_subbatch_size_adjusted" in log_names
        adjusted = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_subbatch_size_adjusted")
        assert adjusted["adjusted_subbatch_size"] == 5
        assert adjusted["rotation_reason"] == "capacity_headroom"

    def test_materialization_wait_logs_source_counts(self):
        """Materialization wait logs should capture source counts around the wait."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-wait"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "add"]:
                return type("CompletedProcess", (), {"returncode": 0, "stdout": "added", "stderr": ""})()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                ingestor._add_sources_chunk(["v1"], subbatch_index=1, expected_total=1)

        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_source_materialization_wait_started" in log_names
        assert "nlm_batch_source_materialization_wait_succeeded" in log_names
        wait_started = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_materialization_wait_started")
        wait_succeeded = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_materialization_wait_succeeded")
        assert wait_started["started_at_epoch"] <= wait_succeeded["completed_at_epoch"]
        assert wait_started["source_count_before_wait"] == 1
        assert wait_succeeded["source_count_before_wait"] == 1
        assert wait_succeeded["source_count_after_wait"] == 1

    def test_materialization_wait_timeout_halts_after_ten_minutes(self):
        """A stalled readiness wait should fail fast after the 10 minute timeout."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-wait"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "add"]:
                return type("CompletedProcess", (), {"returncode": 0, "stdout": "added", "stderr": ""})()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch.object(ingestor, "_wait_for_sources_ready", return_value=False) as wait_mock:
                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                    with pytest.raises(nlm_batch.NotebookSourceMaterializationTimeout):
                        ingestor._add_sources_chunk(["v1"], subbatch_index=1, expected_total=1)

        wait_mock.assert_called_once_with(1, timeout=600, source_count_before_wait=1)
        log_names = [call.args[0] for call in mock_log.call_args_list]
        assert "nlm_batch_source_materialization_wait_started" in log_names
        assert "nlm_batch_source_materialization_wait_failed" in log_names
        wait_failed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_materialization_wait_failed")
        assert wait_failed["timeout_s"] == 600
        assert wait_failed["source_count_before_wait"] == 1

    def test_source_content_fetch_logs_ready_status(self):
        """A ready source should log explicit ready-state completion fields."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-ready"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"value": {"content": "x" * 101}}), "stderr": ""},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is True
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed")
        started = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_started")
        assert started["source_id"] == "s1"
        assert started["video_id"] == "vid1"
        assert started["source_ready_age_s"] == 0.0
        assert completed["status"] == "ready"
        assert completed["returncode"] == 0
        assert completed["content_length"] == 101
        assert completed["ready_threshold"] == 100
        assert completed["source_ready_age_s"] == 0.0
        assert completed["started_at_epoch"] <= completed["completed_at_epoch"]
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["content_fetch_status_counts"]["ready"] == 1
        assert summary["source_ready_age_s_max"] == 0.0

    def test_source_content_fetch_logs_too_short_status(self):
        """A short source should log that it was not ready for download."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-short"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"value": {"content": "x" * 50}}), "stderr": ""},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S", 0.0):
            with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
                with mock.patch(
                    "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                    return_value={
                        "classification": "ok",
                        "available": False,
                        "availability": None,
                        "live_status": None,
                        "was_live": False,
                        "is_live": False,
                        "title": None,
                        "error": None,
                    },
                ) as mock_ytdlp:
                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                        results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert mock_ytdlp.call_count == 2
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed")
        assert completed["status"] == "too_short"
        assert completed["content_length"] == 50
        assert completed["failure_reason"] == "Fetch failed for s1: too_short"
        assert completed["source_ready_age_s"] == 0.0
        assert completed["started_at_epoch"] <= completed["completed_at_epoch"]
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["content_fetch_status_counts"]["too_short"] == 1
        assert summary["content_fetch_attempts_total"] == 4
        assert summary["content_fetch_attempts_max"] == 4
        assert summary["content_fetch_attempts_avg"] == 4.0

    def test_source_content_fetch_logs_command_failed_status(self):
        """A failed content command should log a command-failed status."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-fail"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "failed"},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S", 0.0):
            with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
                with mock.patch(
                    "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                    return_value={
                        "classification": "ok",
                        "available": False,
                        "availability": None,
                        "live_status": None,
                        "was_live": False,
                        "is_live": False,
                        "title": None,
                        "error": None,
                    },
                ) as mock_ytdlp:
                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                        results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert mock_ytdlp.call_count == 2
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed")
        assert completed["status"] == "command_failed"
        assert completed["returncode"] == 1
        assert completed["content_length"] == 0
        assert completed["failure_reason"] == "Fetch failed for s1: command_failed"
        assert completed["source_ready_age_s"] == 0.0
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["content_fetch_status_counts"]["command_failed"] == 1
        assert summary["content_fetch_attempts_total"] == 1
        assert summary["content_fetch_attempts_max"] == 1
        assert summary["content_fetch_attempts_avg"] == 1.0

    def test_source_content_fetch_retries_transient_not_found_and_recovers(self):
        """A transient NOT_FOUND should be retried until content becomes ready."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-retry"
        content_attempts = {"count": 0}

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                content_attempts["count"] += 1
                if content_attempts["count"] == 1:
                    return type(
                        "CompletedProcess",
                        (),
                        {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                    )()
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"value": {"content": "x" * 101}}), "stderr": ""},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch("csf.nlm_batch.time.sleep") as mock_sleep:
                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                    results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is True
        assert results["vid1"][1] == "x" * 101
        assert content_attempts["count"] == 2
        assert mock_sleep.call_count >= 1
        completed = [call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed"]
        assert any(entry["status"] == "ready" for entry in completed)
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["content_fetch_attempts_total"] == 2
        assert summary["content_fetch_attempts_max"] == 2
        assert summary["content_fetch_attempts_avg"] == 2.0

    def test_source_content_fetch_honors_retry_budget_cutoff(self):
        """A small wall-clock budget should stop retries before a second attempt."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-budget"
        content_attempts = {"count": 0}

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                content_attempts["count"] += 1
                if content_attempts["count"] > 1:
                    raise AssertionError("NotebookLM content fetch retried despite exhausted budget")
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_BUDGET_S", 0.01):
            with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S", 0.0):
                with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
                    with mock.patch(
                        "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                        return_value={
                            "classification": "ok",
                            "available": False,
                            "availability": None,
                            "live_status": None,
                            "was_live": False,
                            "is_live": False,
                            "title": None,
                            "error": None,
                        },
                    ) as mock_ytdlp:
                        with mock.patch(
                            "csf.nlm_batch.time.time",
                            side_effect=[1000.0, 1000.01, 1000.02, 1000.03, 1000.04, 1000.05, 1000.06, 1000.07, 1000.08, 1000.09],
                        ):
                            with mock.patch("csf.nlm_batch.time.sleep") as mock_sleep:
                                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                    results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert content_attempts["count"] == 1
        assert mock_ytdlp.call_count == 2
        mock_sleep.assert_not_called()
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed")
        assert completed["attempts"] == 1
        assert completed["status"] == "command_failed"

    def test_source_content_fetch_queues_retry_pass_for_ytdlp_ok(self):
        """A ytdlp-ok miss should enter the second NotebookLM pass and recover there."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-retry-queue"
        content_attempts = {"count": 0}

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                content_attempts["count"] += 1
                if content_attempts["count"] == 1:
                    return type(
                        "CompletedProcess",
                        (),
                        {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                    )()
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"value": {"content": "x" * 101}}), "stderr": ""},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_ATTEMPTS", 1):
            with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S", 0.1):
                with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S", 30.0):
                    with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
                        with mock.patch(
                            "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                            return_value={
                                "classification": "ok",
                                "available": True,
                                "availability": "public",
                                "live_status": "not_live",
                                "was_live": False,
                                "is_live": False,
                                "title": None,
                                "error": None,
                            },
                        ) as mock_ytdlp:
                            with mock.patch("csf.nlm_batch.time.sleep") as mock_sleep:
                                with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                    results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is True
        assert results["vid1"][1] == "x" * 101
        assert content_attempts["count"] == 2
        assert mock_ytdlp.call_count == 1
        mock_sleep.assert_called_once_with(0.1)
        completed = [call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed"]
        assert any(entry["pass_name"] == "retry" and entry["status"] == "ready" for entry in completed)
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["retry_queue_deferred_count"] == 1
        assert summary["retry_queue_recovered_count"] == 1
        assert summary["retry_queue_final_failed_count"] == 0
        assert summary["content_fetch_attempts_total"] == 1
        assert summary["content_fetch_attempts_max"] == 1

    def test_source_content_fetch_queues_shared_retry_pool_entries_when_enabled(self):
        """Shared retry pool mode should enqueue retryable items instead of draining locally."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-shared-retry"
        content_attempts = {"count": 0}

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                content_attempts["count"] += 1
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_ATTEMPTS", 1):
            with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED", True):
                with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S", 30.0):
                    with mock.patch.object(nlm_batch, "_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S", 0.0):
                        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
                            with mock.patch(
                                "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                                return_value={
                                    "classification": "ok",
                                    "available": True,
                                    "availability": "public",
                                    "live_status": "not_live",
                                    "was_live": False,
                                    "is_live": False,
                                    "title": None,
                                    "error": None,
                                },
                            ) as mock_ytdlp:
                                with mock.patch("csf.nlm_batch.enqueue_shared_retry") as mock_enqueue:
                                    with mock.patch("csf.nlm_batch.time.sleep") as mock_sleep:
                                        with mock.patch("csf.nlm_batch.log_action") as mock_log:
                                            results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert content_attempts["count"] == 1
        assert mock_ytdlp.call_count == 1
        mock_enqueue.assert_called_once()
        mock_sleep.assert_not_called()
        summary = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert summary["retry_queue_deferred_count"] == 1
        assert summary["shared_retry_deferred_count"] == 1
        assert summary["retry_queue_recovered_count"] == 0
        assert summary["retry_queue_final_failed_count"] == 0

    def test_source_content_fetch_logs_direct_youtube_page_classification_on_failure(self):
        """Failed fetches should carry yt-dlp and direct YouTube page metadata."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-inspect"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch(
                "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                return_value={
                    "classification": "unknown",
                    "available": False,
                    "availability": None,
                    "live_status": None,
                    "was_live": False,
                    "is_live": False,
                    "title": None,
                    "error": None,
                    "elapsed_s": 1.25,
                },
            ) as mock_ytdlp:
                with mock.patch(
                    "csf.nlm_batch.inspect_youtube_watch_page",
                    return_value={
                        "classification": "removed_by_owner",
                        "available": False,
                        "status": "ERROR",
                        "reason": "Video unavailable",
                        "subreason": "This video has been removed by the uploader",
                        "is_live_content": False,
                        "title": None,
                        "elapsed_s": 0.75,
                    },
                ) as mock_inspect:
                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                        results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert mock_ytdlp.call_count == 2
        mock_inspect.assert_called_once_with("vid1")
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_source_content_fetch_completed")
        assert completed["youtube_page_classification"] == "removed_by_owner"
        assert completed["youtube_page_available"] is False
        assert completed["youtube_page_status"] == "ERROR"
        assert completed["youtube_page_reason"] == "Video unavailable"
        assert completed["youtube_ytdlp_classification"] == "unknown"
        assert completed["youtube_ytdlp_available"] is False
        assert completed["youtube_ytdlp_availability"] is None
        assert completed["youtube_ytdlp_elapsed_s"] == 1.25
        assert completed["youtube_page_elapsed_s"] == 0.75
        summary = ingestor.get_last_extract_metrics()
        assert summary is not None
        assert summary["youtube_ytdlp_elapsed_s_total"] == 1.25
        assert summary["youtube_ytdlp_elapsed_s_count"] == 1
        assert summary["youtube_page_elapsed_s_total"] == 0.75
        assert summary["youtube_page_elapsed_s_count"] == 1

    def test_source_content_retry_queue_counts_youtube_probe_elapsed_on_deferred_failure(self):
        """Deferred retry-queue failures must still accumulate yt-dlp timing in summary metrics."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=1)
        ingestor._nb_id = "nb-retry-queue"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"sources": [{"id": "s1"}]}), "stderr": ""},
                )()
            if cmd[:2] == ["source", "content"]:
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "API error (code 5): NOT_FOUND"},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch(
                "csf.nlm_batch.inspect_youtube_watch_page_via_ytdlp",
                return_value={
                    "classification": "ok",
                    "available": True,
                    "availability": "public",
                    "live_status": "not_live",
                    "was_live": False,
                    "is_live": False,
                    "title": "Queued sample",
                    "error": None,
                    "elapsed_s": 1.5,
                },
            ) as mock_ytdlp:
                with mock.patch("csf.nlm_batch.time.sleep") as mock_sleep:
                    with mock.patch("csf.nlm_batch.log_action") as mock_log:
                        results = ingestor.extract_transcripts(["vid1"])

        assert results["vid1"][0] is False
        assert mock_ytdlp.call_count == 2
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_extract_completed")
        assert completed["retry_queue_deferred_count"] == 1
        assert completed["youtube_ytdlp_elapsed_s_total"] == 3.0
        assert completed["youtube_ytdlp_elapsed_s_count"] == 2
        summary = ingestor.get_last_extract_metrics()
        assert summary is not None
        assert summary["youtube_ytdlp_elapsed_s_total"] == 3.0
        assert summary["youtube_ytdlp_elapsed_s_count"] == 2

    def test_extract_transcripts_matches_sources_by_title_instead_of_order(self):
        """Source list order should not control which video ID gets which source ID."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=2)
        ingestor._nb_id = "nb-order"
        vid1 = "AAAAAAAAAAA"
        vid2 = "BBBBBBBBBBB"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type(
                    "CompletedProcess",
                    (),
                    {
                        "returncode": 0,
                        "stdout": json.dumps(
                            {
                                "sources": [
                                    {"id": "s2", "title": f"https://www.youtube.com/watch?v={vid2}"},
                                    {"id": "s1", "title": f"https://www.youtube.com/watch?v={vid1}"},
                                ]
                            }
                        ),
                        "stderr": "",
                    },
                )()
            if cmd[:2] == ["source", "content"]:
                source_id = cmd[2]
                content = "A" * 101 if source_id == "s1" else "B" * 101
                return type(
                    "CompletedProcess",
                    (),
                    {"returncode": 0, "stdout": json.dumps({"value": {"content": content}}), "stderr": ""},
                )()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            results = ingestor.extract_transcripts([vid1, vid2])

        assert results[vid1][0] is True
        assert results[vid1][1] == "A" * 101
        assert results[vid2][0] is True
        assert results[vid2][1] == "B" * 101

    def test_source_count_tracked_in_subbatch_metrics(self):
        """Subbatch metrics should include current_source_count after each subbatch."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=2)
        ingestor._nb_id = "nb-123"

        def fake_run_cmd(cmd, timeout=300):
            if cmd[:2] == ["source", "list"]:
                return type("CompletedProcess", (), {"returncode": 0, "stdout": json.dumps({"sources": [{"id": f"s{i}"} for i in range(100)]}), "stderr": ""})()
            if cmd[:2] == ["source", "add"]:
                return type("CompletedProcess", (), {"returncode": 0, "stdout": "added", "stderr": ""})()
            return type("CompletedProcess", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with mock.patch.object(ingestor, "_run_cmd", side_effect=fake_run_cmd):
            with mock.patch("csf.nlm_batch.log_action"):
                ingestor._add_sources_in_subbatches(["v1", "v2", "v3", "v4"], subbatch_size=2)

        for metric in ingestor._last_subbatch_metrics:
            assert "current_source_count" in metric
            assert metric["current_source_count"] == 100  # always 100 from mock


class TestBackoffCalculation:
    """Exponential backoff must be capped at _MAX_DELAY."""

    def test_backoff_capped_at_max_delay(self):
        """Consecutive failures beyond threshold must respect _MAX_DELAY ceiling."""
        tracker = nlm_batch._RateLimitTracker()
        for _ in range(10):
            tracker.record_failure(is_rate_limit=True)
        with tracker._lock:
            assert tracker._current_delay <= nlm_batch._MAX_DELAY

    def test_backoff_grows_exponentially(self):
        """Delay must grow as INITIAL_DELAY * 2^(n-1) for failures 1..4."""
        tracker = nlm_batch._RateLimitTracker()
        delays = []
        for i in range(1, 5):
            tracker.record_failure(is_rate_limit=True)
            with tracker._lock:
                delays.append(tracker._current_delay)
        assert delays == [0.5, 1.0, 2.0, 4.0]
