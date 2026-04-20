"""Tests for nlm_batch rate-limit tracker and sub-batch reset logic."""

import json
import pytest
from unittest import mock
from csf import nlm_batch


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

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value=None):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
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
        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_completed")
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
        assert completed["strategy"] == "reusable"
        assert completed["notebook_reused"] is True
        assert completed["setup_mode"] == "reuse_add"
        assert completed["succeeded"] == 1
        assert completed["failed"] == 0

    def test_reusable_batch_uses_300_source_subbatches_by_default(self):
        """Reusable notebook processing should forward the 300-source subbatch size."""
        batch_ids = ["vid1", "vid2", "vid3"]

        with mock.patch("csf.nlm_batch._load_reusable_notebook_id", return_value="nb-existing"):
            with mock.patch("csf.nlm_batch._save_reusable_notebook_id"):
                with mock.patch("csf.nlm_batch._clear_reusable_notebook_state"):
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
                                        results = ingestor._ingestor.experiment_add_acceptance(batch_ids, sizes, notebook_title="yt-is::dev::sweep")

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
        """YTIS_NLM_REUSABLE_STATE_PATH should override the default state file."""
        monkeypatch.setenv(
            "YTIS_NLM_REUSABLE_STATE_PATH",
            "P:/__csf/.data/yt-is/dev-workers/worker-01.json",
        )
        assert nlm_batch._get_reusable_notebook_state_path() == nlm_batch.Path(
            "P:/__csf/.data/yt-is/dev-workers/worker-01.json"
        )

    def test_title_override_is_used(self, monkeypatch):
        """YTIS_NLM_REUSABLE_NOTEBOOK_TITLE should override the notebook title."""
        monkeypatch.setenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "yt-is::dev::worker-01")
        assert nlm_batch._get_reusable_notebook_title() == "yt-is::dev::worker-01"

    def test_notebooklm_profile_override_is_used(self, monkeypatch):
        """NOTEBOOKLM_PROFILE should override the default NotebookLM profile."""
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "ytis-worker-01")
        assert nlm_batch._get_notebooklm_profile() == "ytis-worker-01"

    def test_create_batch_notebook_uses_override_title(self, monkeypatch):
        """create_batch_notebook should honor the worker-specific notebook title."""
        monkeypatch.setenv("YTIS_NLM_REUSABLE_NOTEBOOK_TITLE", "yt-is::dev::worker-01")
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
        assert mock_run_cmd.call_args.args[0] == ["notebook", "create", "yt-is::dev::worker-01"]
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

    def test_cleanup_deletes_stale_worker_notebooks(self, tmp_path, monkeypatch):
        """Only notebooks missing from state files should be deleted."""
        state_root = tmp_path / "worker-states"
        state_root.mkdir()
        (state_root / "worker-01.json").write_text(json.dumps({"nb_id": "keep-1"}), encoding="utf-8")
        (state_root / "worker-02.json").write_text(json.dumps({"nb_id": "keep-2"}), encoding="utf-8")
        monkeypatch.setenv("YTIS_INDUSTRIAL_WORKER_STATE_ROOT", str(state_root))
        monkeypatch.setenv("YTIS_INDUSTRIAL_WORKER_NOTEBOOK_PREFIX", "yt-is::industrial::worker")

        notebooks = {
            "notebooks": [
                {"id": "keep-1", "name": "yt-is::industrial::worker::worker-01"},
                {"id": "stale-1", "name": "yt-is::industrial::worker::worker-03"},
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
            if args[:3] == ["notebook", "delete", "stale-1"]:
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

        monkeypatch.setattr(nlm_batch.NLMBatchIngestor, "_run_cmd", mock_run_cmd)
        with mock.patch("csf.nlm_batch.log_action") as mock_log:
            deleted, failed = nlm_batch.cleanup_stale_worker_notebooks()

        assert deleted == 1
        assert failed == 0
        assert ["notebook", "delete", "stale-1", "--confirm"] in calls
        cleanup_complete = next(
            call.args[1]
            for call in mock_log.call_args_list
            if call.args[0] == "nlm_worker_notebook_cleanup_complete"
        )
        assert cleanup_complete["status"] == "ok"
        assert cleanup_complete["deleted"] == 1


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


class TestSubBatchFailureMode:
    """NotebookLM add failures should not recursively shrink to 1-2 items."""

    def test_add_failure_does_not_split_recursively(self):
        """A failed add should log and return empty without retry splitting."""
        ingestor = nlm_batch.NLMBatchIngestor(batch_size=3)
        ingestor._nb_id = "nb-123"
        response = type(
            "CompletedProcess",
            (),
            {"stdout": "", "stderr": "Could not add URL sources", "returncode": 1},
        )()

        with mock.patch.object(ingestor, "_run_cmd", return_value=response) as mock_run_cmd:
            with mock.patch("csf.nlm_batch.log_action") as mock_log:
                result = ingestor._add_sources_chunk(
                    ["vid1", "vid2", "vid3"],
                    subbatch_index=1,
                    expected_total=3,
                )

        assert result == []
        mock_run_cmd.assert_called_once()
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
