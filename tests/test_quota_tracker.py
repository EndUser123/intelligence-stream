"""Tests for csf/quota_tracker.py - LOGIC-004: Quota kill trigger.

RED Phase: Tests are written BEFORE implementation to define expected behavior.
Verifies: CLI call count tracking, free-only mode switch, threshold enforcement.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.quota_tracker import (
    _DEFAULT_DAILY_QUOTA,
    _THRESHOLD_FRACTION,
    get_cli_calls_today,
    get_free_only_mode,
    increment_cli_calls,
    is_free_only_mode,
    reset_daily_quota,
    set_free_only_mode,
)

# Shared DB path for testing
_TEST_DB_PATH = Path("P:/__csf/.data/intelligence-stream/quota/test_quota.sqlite")


class TestQuotaTracking:
    """Test CLI call count tracking in shared DB."""

    def setup_method(self):
        """Reset quota state before each test."""
        set_free_only_mode(False)
        reset_daily_quota()

    def test_increment_cli_calls_increments_count(self):
        """Calling increment_cli_calls increases the count."""
        initial = get_cli_calls_today()
        increment_cli_calls()
        assert get_cli_calls_today() == initial + 1

    def test_multiple_increments(self):
        """Multiple calls accumulate correctly."""
        for _ in range(5):
            increment_cli_calls()
        assert get_cli_calls_today() == 5

    def test_get_cli_calls_today_returns_int(self):
        """get_cli_calls_today returns an integer."""
        result = get_cli_calls_today()
        assert isinstance(result, int)
        assert result >= 0

    def test_free_only_mode_default_is_false(self):
        """Free-only mode is False by default."""
        assert is_free_only_mode() is False

    def test_set_free_only_mode_to_true(self):
        """set_free_only_mode(True) enables free-only mode."""
        set_free_only_mode(True)
        assert is_free_only_mode() is True

    def test_set_free_only_mode_to_false(self):
        """set_free_only_mode(False) disables free-only mode."""
        set_free_only_mode(True)
        set_free_only_mode(False)
        assert is_free_only_mode() is False

    def test_free_only_mode_persists(self):
        """Free-only mode persists across calls (DB-backed)."""
        set_free_only_mode(True)
        # Simulate new storage instance
        from csf.quota_tracker import _get_quota_storage

        storage = _get_quota_storage()
        assert storage._is_free_only() is True


class TestQuotaThreshold:
    """Test automatic free-only mode switch at threshold."""

    def setup_method(self):
        set_free_only_mode(False)
        reset_daily_quota()

    def test_threshold_is_half_of_daily_quota(self):
        """Free-only triggers when CLI calls exceed 50% of daily quota."""
        threshold = int(_DEFAULT_DAILY_QUOTA * _THRESHOLD_FRACTION)
        assert threshold == _DEFAULT_DAILY_QUOTA // 2

    def test_free_only_triggers_at_threshold(self):
        """is_free_only_mode returns True when CLI calls exceed threshold."""
        threshold = int(_DEFAULT_DAILY_QUOTA * _THRESHOLD_FRACTION)
        # Set calls to threshold (one over triggers)
        for _ in range(threshold + 1):
            increment_cli_calls()
        assert is_free_only_mode() is True

    def test_free_only_not_triggered_below_threshold(self):
        """Free-only mode stays False when below threshold."""
        threshold = int(_DEFAULT_DAILY_QUOTA * _THRESHOLD_FRACTION)
        # Set calls to just below threshold
        for _ in range(threshold):
            increment_cli_calls()
        assert is_free_only_mode() is False


class TestDailyQuotaReset:
    """Test daily quota reset mechanism."""

    def setup_method(self):
        set_free_only_mode(False)
        reset_daily_quota()

    def test_reset_clears_cli_call_count(self):
        """reset_daily_quota clears the CLI call count."""
        for _ in range(10):
            increment_cli_calls()
        reset_daily_quota()
        assert get_cli_calls_today() == 0

    def test_reset_does_not_change_free_only_mode(self):
        """reset_daily_quota preserves free_only mode setting."""
        set_free_only_mode(True)
        reset_daily_quota()
        assert is_free_only_mode() is True


class TestGetFreeOnlyMode:
    """Test get_free_only_mode returns correct state."""

    def setup_method(self):
        set_free_only_mode(False)
        reset_daily_quota()

    def test_returns_false_when_under_threshold(self):
        """get_free_only_mode returns False when under quota threshold."""
        # Add some calls but not enough to trigger
        for _ in range(5):
            increment_cli_calls()
        assert get_free_only_mode() is False

    def test_returns_true_when_over_threshold(self):
        """get_free_only_mode returns True when over quota threshold."""
        threshold = int(_DEFAULT_DAILY_QUOTA * _THRESHOLD_FRACTION)
        for _ in range(threshold + 1):
            increment_cli_calls()
        assert get_free_only_mode() is True
