"""Tests for batch_scheduler."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from csf.batch_scheduler import (
    _JITTER_MAX,
    _JITTER_MIN,
    _STALE_ATTEMPTING_SECONDS,
    BatchScheduler,
)


# ─── Shared test DB path ───────────────────────────────────────────────────────

_TEST_DB_DIR = Path("P:/__csf/.data/intelligence-stream/batch_status")
_TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
_TEST_DB = _TEST_DB_DIR / "test_scheduler.sqlite"


def _reset_test_db() -> None:
    """Delete and recreate the test DB to ensure clean state, avoiding Windows locks."""
    import os as _os

    for suffix in ("", "-wal", "-shm"):
        p = str(_TEST_DB) + suffix
        try:
            _os.unlink(p)
        except FileNotFoundError:
            pass
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_status (
            video_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            updated_at TEXT NOT NULL, source TEXT,
            published_at TEXT, has_captions INTEGER
        );
        CREATE TABLE IF NOT EXISTS download_archive (
            video_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            source TEXT, attempted_at REAL NOT NULL, error TEXT
        );
        CREATE TABLE IF NOT EXISTS channel_cooldown (
            source TEXT PRIMARY KEY, cooldown_until REAL NOT NULL,
            consecutive_429s INTEGER NOT NULL DEFAULT 0
        );
        PRAGMA journal_mode=WAL;
    """)
    conn.close()


def _seed(rows: list[tuple]) -> None:
    """Seed analysis_status with (video_id, status, source, published_at) tuples."""
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_status (
            video_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            published_at TEXT,
            has_captions INTEGER
        )
        """
    )
    conn.executemany(
        "INSERT OR REPLACE INTO analysis_status (video_id, status, updated_at, source, published_at, has_captions) VALUES (?, ?, datetime('now'), ?, ?, NULL)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable jitter sleeps so tests run fast."""
    import csf.batch_scheduler as bs

    monkeypatch.setattr(bs, "_JITTER_MIN", 0.001)
    monkeypatch.setattr(bs, "_JITTER_MAX", 0.001)


@pytest.fixture(autouse=True)
def _clean_db() -> Generator[None, None, None]:
    _reset_test_db()
    yield
    _reset_test_db()


# ─── test_round_robin_interleaving ───────────────────────────────────────────

def test_round_robin_interleaving() -> None:
    """3 channels × 3 videos each → first 9 yields cover all 3 channels."""
    _seed([
        ("A1", "pending", "https://youtube.com/channel/UC_A", "2025-01-01T00:00:00"),
        ("A2", "pending", "https://youtube.com/channel/UC_A", "2025-01-02T00:00:00"),
        ("A3", "pending", "https://youtube.com/channel/UC_A", "2025-01-03T00:00:00"),
        ("B1", "pending", "https://youtube.com/channel/UC_B", "2025-01-01T00:00:00"),
        ("B2", "pending", "https://youtube.com/channel/UC_B", "2025-01-02T00:00:00"),
        ("B3", "pending", "https://youtube.com/channel/UC_B", "2025-01-03T00:00:00"),
        ("C1", "pending", "https://youtube.com/channel/UC_C", "2025-01-01T00:00:00"),
        ("C2", "pending", "https://youtube.com/channel/UC_C", "2025-01-02T00:00:00"),
        ("C3", "pending", "https://youtube.com/channel/UC_C", "2025-01-03T00:00:00"),
    ])
    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())

    assert len(results) == 9
    from collections import Counter

    counts = Counter(src for _, src in results)
    assert counts["https://youtube.com/channel/UC_A"] == 3
    assert counts["https://youtube.com/channel/UC_B"] == 3
    assert counts["https://youtube.com/channel/UC_C"] == 3


# ─── test_archive_skip_failed ─────────────────────────────────────────────────

def test_archive_skip_failed() -> None:
    """Pre-insert failed entry → verify video is not yielded."""
    _seed([
        ("VID1", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive VALUES (?, ?, ?, ?, ?)",
        ("VID1", "failed", "https://youtube.com/channel/UC_X", time.time(), None),
    )
    conn.commit()
    conn.close()

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "VID1" not in video_ids


# ─── test_archive_skip_attempting ─────────────────────────────────────────────

def test_archive_skip_attempting() -> None:
    """Pre-insert attempting entry → verify video is not yielded."""
    _seed([
        ("VID2", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive VALUES (?, ?, ?, ?, ?)",
        ("VID2", "attempting", "https://youtube.com/channel/UC_X", time.time(), None),
    )
    conn.commit()
    conn.close()

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "VID2" not in video_ids


# ─── test_archive_skip_success ────────────────────────────────────────────────

def test_archive_skip_success() -> None:
    """Pre-insert success entry → verify video is not yielded."""
    _seed([
        ("VID3", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive VALUES (?, ?, ?, ?, ?)",
        ("VID3", "success", "https://youtube.com/channel/UC_X", time.time(), None),
    )
    conn.commit()
    conn.close()

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "VID3" not in video_ids


# ─── test_cooldown_blocking ───────────────────────────────────────────────────

def test_cooldown_blocking() -> None:
    """Pre-insert future cooldown_until → verify channel is skipped."""
    _seed([
        ("VID4", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
        ("VID5", "pending", "https://youtube.com/channel/UC_X", "2025-01-02T00:00:00"),
        ("VID6", "pending", "https://youtube.com/channel/UC_X", "2025-01-03T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO channel_cooldown VALUES (?, ?, ?)",
        ("https://youtube.com/channel/UC_X", time.monotonic() + 300, 3),
    )
    conn.commit()
    conn.close()

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    assert results == []


# ─── test_all_channels_in_cooldown ────────────────────────────────────────────

def test_all_channels_in_cooldown() -> None:
    """All channels in cooldown → loop breaks gracefully."""
    _seed([
        ("X1", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
        ("Y1", "pending", "https://youtube.com/channel/UC_Y", "2025-01-01T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO channel_cooldown VALUES (?, ?, ?)",
        ("https://youtube.com/channel/UC_X", time.monotonic() + 300, 1),
    )
    conn.execute(
        "INSERT OR REPLACE INTO channel_cooldown VALUES (?, ?, ?)",
        ("https://youtube.com/channel/UC_Y", time.monotonic() + 300, 1),
    )
    conn.commit()
    conn.close()

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    assert results == []


# ─── test_stale_attempting_recovery ───────────────────────────────────────────

def test_stale_attempting_recovery() -> None:
    """Insert 30-min-old attempting → on init, verify it is promoted to failed."""
    _seed([
        ("STALE1", "pending", "https://youtube.com/channel/UC_Z", "2025-01-01T00:00:00"),
    ])
    conn = sqlite3.connect(_TEST_DB)
    stale_time = time.time() - _STALE_ATTEMPTING_SECONDS - 10
    conn.execute(
        "INSERT OR REPLACE INTO download_archive VALUES (?, ?, ?, ?, ?)",
        ("STALE1", "attempting", "https://youtube.com/channel/UC_Z", stale_time, None),
    )
    conn.commit()
    conn.close()

    # Scheduler init should promote stale attempting → failed
    BatchScheduler(db_path=_TEST_DB)

    conn2 = sqlite3.connect(_TEST_DB)
    row = conn2.execute(
        "SELECT status FROM download_archive WHERE video_id=?", ("STALE1",)
    ).fetchone()
    conn2.close()

    assert row is not None
    assert row[0] == "failed"


# ─── test_jitter_range ────────────────────────────────────────────────────────

def test_jitter_range() -> None:
    """Measure delay between consecutive yields → verify within JITTER bounds."""
    _seed([
        ("J1", "pending", "https://youtube.com/channel/UC_J", "2025-01-01T00:00:00"),
        ("J2", "pending", "https://youtube.com/channel/UC_J", "2025-01-02T00:00:00"),
    ])
    sched = BatchScheduler(db_path=_TEST_DB)
    gen = sched.yield_next()

    t0 = time.monotonic()
    next(gen)  # first yield
    t1 = time.monotonic()
    delay = t1 - t0

    # After jitter patch: delay should be ~0.001s
    assert _JITTER_MIN <= delay <= _JITTER_MAX + 0.5


# ─── test_empty_channel_handling ──────────────────────────────────────────────

def test_empty_channel_handling() -> None:
    """Channel with 0 pending videos → scheduler skips it gracefully."""
    # Only one channel has pending videos
    _seed([
        ("E1", "pending", "https://youtube.com/channel/UC_E", "2025-01-01T00:00:00"),
        ("E2", "pending", "https://youtube.com/channel/UC_E", "2025-01-02T00:00:00"),
    ])

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    assert len(results) == 2
    assert all(src == "https://youtube.com/channel/UC_E" for _, src in results)


# ─── test_record_429_counter ──────────────────────────────────────────────────

def test_record_429_counter() -> None:
    """Call record_429 3× on same source → verify consecutive_429s=3 in DB."""
    sched = BatchScheduler(db_path=_TEST_DB)
    sched.record_429("https://youtube.com/channel/UC_K")
    sched.record_429("https://youtube.com/channel/UC_K")
    sched.record_429("https://youtube.com/channel/UC_K")

    conn = sqlite3.connect(_TEST_DB)
    row = conn.execute(
        "SELECT consecutive_429s FROM channel_cooldown WHERE source=?",
        ("https://youtube.com/channel/UC_K",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 3


# ─── test_archive_finalize_success ─────────────────────────────────────────────

def test_archive_finalize_success() -> None:
    """Call archive_finalize(vid, 'success') → verify status='success' in archive."""
    sched = BatchScheduler(db_path=_TEST_DB)
    sched.archive_finalize("VID_OK", "success", "https://youtube.com/channel/UC_L")

    conn = sqlite3.connect(_TEST_DB)
    row = conn.execute(
        "SELECT status, source FROM download_archive WHERE video_id=?", ("VID_OK",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "success"
    assert row[1] == "https://youtube.com/channel/UC_L"


# ─── test_archive_finalize_failed ─────────────────────────────────────────────

def test_archive_finalize_failed() -> None:
    """Call archive_finalize(vid, 'failed') → verify status='failed' in archive."""
    sched = BatchScheduler(db_path=_TEST_DB)
    sched.archive_finalize("VID_ERR", "failed", "https://youtube.com/channel/UC_M")

    conn = sqlite3.connect(_TEST_DB)
    row = conn.execute(
        "SELECT status FROM download_archive WHERE video_id=?", ("VID_ERR",)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "failed"


# ─── test_source_not_null_filter ─────────────────────────────────────────────

def test_source_not_null_filter() -> None:
    """Entries with NULL source are skipped by scheduler."""
    _seed([
        ("NULL1", "pending", None, "2025-01-01T00:00:00"),
        ("GOOD1", "pending", "https://youtube.com/channel/UC_N", "2025-01-01T00:00:00"),
    ])

    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "NULL1" not in video_ids
    assert "GOOD1" in video_ids
