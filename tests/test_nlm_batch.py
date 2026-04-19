"""Tests for nlm_batch rate-limit tracker and sub-batch reset logic."""

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
                                    with mock.patch("csf.nlm_batch.time.monotonic", side_effect=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]):
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
        assert completed["setup_elapsed_s"] == 1.0
        assert completed["extract_elapsed_s"] == 1.0
        assert completed["cleanup_elapsed_s"] == 1.0
        assert completed["total_elapsed_s"] == 7.0

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
                                        with mock.patch("csf.nlm_batch.time.monotonic", side_effect=[200.0, 201.0, 202.0, 203.0, 204.0, 205.0, 206.0, 207.0]):
                                            results = ingestor.process_batch(batch_ids)

        assert results["vid3"][0] is True
        mock_add.assert_called_once_with(batch_ids)
        mock_extract.assert_called_once_with(batch_ids)
        mock_reset.assert_called_once()

        completed = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "nlm_batch_reusable_process_completed")
        assert completed["strategy"] == "reusable"
        assert completed["notebook_reused"] is True
        assert completed["setup_mode"] == "reuse_add"
        assert completed["succeeded"] == 1
        assert completed["failed"] == 0

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
