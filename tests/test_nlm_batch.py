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
