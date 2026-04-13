"""Quota tracker for Gemini API calls — LOGIC-004 fix.

Tracks CLI call count in a shared SQLite DB. When CLI calls exceed
50% of estimated daily quota (~1000 for gemini-2.5-flash), automatically
switches to free-only mode (skips CLI in transcript fallback chain).

Multi-terminal safe: all terminals share the same DB with WAL mode.
"""

import sqlite3
import threading
from datetime import date
from pathlib import Path

# Gemini daily quota for gemini-2.5-flash (free tier)
# Source: Gemini API documentation (1500 req/day = ~1000 video transcripts)
_DEFAULT_DAILY_QUOTA = 1000
_THRESHOLD_FRACTION = 0.5  # Trigger free-only at 50% of daily quota

# Shared quota DB — separate from transcript/retry DBs (isolation blast radius)
_SHARED_DB_PATH: Path = Path("P:/__csf/.data/yt-is/quota.sqlite")

_storage_lock = threading.Lock()
_quota_storage: "_QuotaStorage | None" = None


def _get_quota_storage() -> "_QuotaStorage":
    """Get or create the quota storage singleton."""
    global _quota_storage
    if _quota_storage is None:
        with _storage_lock:
            if _quota_storage is None:
                _quota_storage = _QuotaStorage()
    return _quota_storage


class _QuotaStorage:
    """Thread-safe quota state backed by SQLite with WAL mode."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create quota_state table if not exists."""
        _SHARED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a connection to the quota DB."""
        conn = sqlite3.connect(_SHARED_DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _get(self, key: str) -> str | None:
        """Get a value from quota_state."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT value FROM quota_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        """Set a value in quota_state."""
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO quota_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
        conn.close()

    def get_cli_calls_today(self) -> int:
        """Get CLI call count for today, resetting if new day."""
        today = str(date.today())
        last_reset = self._get("last_reset_date")

        if last_reset != today:
            # New day — reset counter but preserve free_only mode
            self._set("cli_calls_today", "0")
            self._set("last_reset_date", today)
            return 0

        value = self._get("cli_calls_today")
        return int(value) if value else 0

    def increment_cli_calls(self) -> int:
        """Increment CLI call count. Auto-enables free-only if threshold exceeded."""
        today = str(date.today())
        last_reset = self._get("last_reset_date")

        if last_reset != today:
            # New day — reset counter
            self._set("cli_calls_today", "0")
            self._set("last_reset_date", today)

        current = self.get_cli_calls_today()
        new_count = current + 1
        self._set("cli_calls_today", str(new_count))

        # Auto-switch to free-only if threshold exceeded
        threshold = int(_DEFAULT_DAILY_QUOTA * _THRESHOLD_FRACTION)
        if new_count > threshold:
            self._set("free_only_mode", "true")

        return new_count

    def _is_free_only(self) -> bool:
        """Check free_only_mode flag directly from storage."""
        value = self._get("free_only_mode")
        return value == "true"

    def is_free_only_mode(self) -> bool:
        """Check if free-only mode is active."""
        return self._is_free_only()

    def set_free_only_mode(self, enabled: bool) -> None:
        """Set free-only mode flag."""
        self._set("free_only_mode", "true" if enabled else "false")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_cli_calls_today() -> int:
    """Get number of CLI calls made today."""
    return _get_quota_storage().get_cli_calls_today()


def increment_cli_calls() -> int:
    """Increment CLI call count. Auto-enables free-only if threshold exceeded.

    Call this after every Gemini CLI transcript fetch.

    Returns:
        New CLI call count after increment.
    """
    return _get_quota_storage().increment_cli_calls()


def is_free_only_mode() -> bool:
    """Return True if quota threshold exceeded and free-only mode is active."""
    return _get_quota_storage().is_free_only_mode()


def get_free_only_mode() -> bool:
    """Return True if free-only mode is active (auto or manual).

    Shortcut that checks both auto-trigger and manual setting.
    """
    return _get_quota_storage().is_free_only_mode()


def set_free_only_mode(enabled: bool) -> None:
    """Manually enable or disable free-only mode."""
    _get_quota_storage().set_free_only_mode(enabled)


def reset_daily_quota() -> None:
    """Reset CLI call counter for today (preserves free_only mode).

    Called by test fixtures. In production, auto-resets at midnight.
    """
    storage = _get_quota_storage()
    today = str(date.today())
    storage._set("cli_calls_today", "0")
    storage._set("last_reset_date", today)
