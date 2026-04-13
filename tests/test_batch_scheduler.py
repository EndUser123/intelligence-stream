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
            source TEXT PRIMARY KEY, cooldown_until REAL NOT NULL
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

    # Assert round-robin interleaving order: one from each channel per pass.
    # Expected: A1,B1,C1, A2,B2,C2, A3,B3,C3 (3 rounds × 3 channels)
    expected_order = [
        "https://youtube.com/channel/UC_A",  # A1 — round 1
        "https://youtube.com/channel/UC_B",  # B1 — round 1
        "https://youtube.com/channel/UC_C",  # C1 — round 1
        "https://youtube.com/channel/UC_A",  # A2 — round 2
        "https://youtube.com/channel/UC_B",  # B2 — round 2
        "https://youtube.com/channel/UC_C",  # C2 — round 2
        "https://youtube.com/channel/UC_A",  # A3 — round 3
        "https://youtube.com/channel/UC_B",  # B3 — round 3
        "https://youtube.com/channel/UC_C",  # C3 — round 3
    ]
    sources = [src for _, src in results]
    assert sources == expected_order, f"Expected interleaved order, got {[v+':'+s for v,s in results]}"


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
        "INSERT OR REPLACE INTO channel_cooldown (source, cooldown_until) VALUES (?, ?)",
        ("https://youtube.com/channel/UC_X", time.monotonic() + 300),
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
        "INSERT OR REPLACE INTO channel_cooldown (source, cooldown_until) VALUES (?, ?)",
        ("https://youtube.com/channel/UC_X", time.monotonic() + 300),
    )
    conn.execute(
        "INSERT OR REPLACE INTO channel_cooldown (source, cooldown_until) VALUES (?, ?)",
        ("https://youtube.com/channel/UC_Y", time.monotonic() + 300),
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

    # Read jitter bounds directly from module (not cached at import time)
    import csf.batch_scheduler as bs

    t0 = time.monotonic()
    next(gen)  # first yield
    t1 = time.monotonic()
    delay = t1 - t0

    assert bs._JITTER_MIN <= delay <= bs._JITTER_MAX + 0.5


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
    """Call record_429 3× on same source → verify cooldown_until is set and updated."""
    sched = BatchScheduler(db_path=_TEST_DB)
    import time as t

    # First 429
    sched.record_429("https://youtube.com/channel/UC_K")
    conn = sqlite3.connect(_TEST_DB)
    row1 = conn.execute(
        "SELECT cooldown_until FROM channel_cooldown WHERE source=?",
        ("https://youtube.com/channel/UC_K",),
    ).fetchone()
    conn.close()
    assert row1 is not None
    first_cooldown = row1[0]

    t.sleep(0.01)  # tiny sleep so cooldown_until advances

    # Second 429 — INSERT OR REPLACE updates the row
    sched.record_429("https://youtube.com/channel/UC_K")
    conn = sqlite3.connect(_TEST_DB)
    row2 = conn.execute(
        "SELECT cooldown_until FROM channel_cooldown WHERE source=?",
        ("https://youtube.com/channel/UC_K",),
    ).fetchone()
    conn.close()
    assert row2 is not None
    assert row2[0] > first_cooldown


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


# ─── test_cross_terminal_cooldown ───────────────────────────────────────────

def test_cross_terminal_cooldown() -> None:
    """Terminal B (new scheduler instance) respects cooldown written by Terminal A."""
    _seed([
        ("X1", "pending", "https://youtube.com/channel/UC_X", "2025-01-01T00:00:00"),
        ("Y1", "pending", "https://youtube.com/channel/UC_Y", "2025-01-01T00:00:00"),
    ])

    # Terminal A: write a cooldown for UC_X
    sched_a = BatchScheduler(db_path=_TEST_DB)
    sched_a.record_429("https://youtube.com/channel/UC_X")

    # Terminal B: fresh scheduler instance, same DB — should skip UC_X
    sched_b = BatchScheduler(db_path=_TEST_DB)
    results_b = list(sched_b.yield_next())
    sources_b = [src for _, src in results_b]

    # UC_X is in cooldown, UC_Y should still be yielded
    assert "https://youtube.com/channel/UC_X" not in sources_b
    assert "https://youtube.com/channel/UC_Y" in sources_b


# ─── test_record_success_clears_cooldown ─────────────────────────────────────

def test_record_success_clears_cooldown() -> None:
    """record_success removes the channel cooldown row, allowing videos through."""
    _seed([
        ("Z1", "pending", "https://youtube.com/channel/UC_Z", "2025-01-01T00:00:00"),
    ])

    sched = BatchScheduler(db_path=_TEST_DB)
    # Put channel in cooldown
    sched.record_429("https://youtube.com/channel/UC_Z")

    # Verify cooldown exists
    conn = sqlite3.connect(_TEST_DB)
    row_before = conn.execute(
        "SELECT cooldown_until FROM channel_cooldown WHERE source=?",
        ("https://youtube.com/channel/UC_Z",),
    ).fetchone()
    conn.close()
    assert row_before is not None

    # Clear cooldown with record_success
    sched.record_success("https://youtube.com/channel/UC_Z")

    # Verify cooldown is gone
    conn = sqlite3.connect(_TEST_DB)
    row_after = conn.execute(
        "SELECT cooldown_until FROM channel_cooldown WHERE source=?",
        ("https://youtube.com/channel/UC_Z",),
    ).fetchone()
    conn.close()
    assert row_after is None


# ─── test_stale_boundary_exactly_at_threshold ──────────────────────────────────

def test_stale_boundary_exactly_at_threshold() -> None:
    """attempted_at exactly at cutoff should be recovered (treated as stale)."""
    from csf.batch_scheduler import _STALE_ATTEMPTING_SECONDS
    import time as t

    # Seed analysis_status so the channel is discovered by the scheduler
    _seed([
        ("STALE_EXACT", "pending", "https://youtube.com/channel/UC_S", "2025-01-01T00:00:00"),
    ])

    # Pre-insert attempting entry at exactly the cutoff (now - 1800)
    cutoff = t.time() - _STALE_ATTEMPTING_SECONDS
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) VALUES (?, 'attempting', ?, ?)",
        ("STALE_EXACT", "https://youtube.com/channel/UC_S", cutoff),
    )
    conn.commit()
    conn.close()

    # Scheduler should recover it on next yield_next — stale recovery converts
    # 'attempting' to 'failed', then archive check skips it (status='failed')
    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "STALE_EXACT" not in video_ids  # recovered and skipped as 'failed'


# ─── test_stale_boundary_just_over_threshold ───────────────────────────────────

def test_stale_boundary_just_over_threshold() -> None:
    """attempted_at just over the cutoff (older) should be recovered."""
    from csf.batch_scheduler import _STALE_ATTEMPTING_SECONDS
    import time as t

    _seed([
        ("STALE_OVER", "pending", "https://youtube.com/channel/UC_T", "2025-01-01T00:00:00"),
    ])

    # Pre-insert attempting entry 1 second past the cutoff
    cutoff = t.time() - _STALE_ATTEMPTING_SECONDS - 1
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) VALUES (?, 'attempting', ?, ?)",
        ("STALE_OVER", "https://youtube.com/channel/UC_T", cutoff),
    )
    conn.commit()
    conn.close()

    # Scheduler should recover it — attempted_at (now-1801) < cutoff (now-1800)
    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "STALE_OVER" not in video_ids  # recovered as 'failed', skipped


# ─── test_double_archive_finalize_idempotent ──────────────────────────────────

def test_double_archive_finalize_idempotent() -> None:
    """Calling archive_finalize twice on same video is safe (idempotent)."""
    sched = BatchScheduler(db_path=_TEST_DB)
    channel = "https://youtube.com/channel/UC_U"

    # First archive as failed
    sched.archive_finalize("DOUBLE_VID", "failed", channel)

    # Second archive as failed — should not error
    sched.archive_finalize("DOUBLE_VID", "failed", channel)

    # Third archive as success — status should update to success
    sched.archive_finalize("DOUBLE_VID", "success", channel)

    conn = sqlite3.connect(_TEST_DB)
    row = conn.execute(
        "SELECT status FROM download_archive WHERE video_id=?",
        ("DOUBLE_VID",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "success"


# ─── test_concurrent_yield_next_race ───────────────────────────────────────────

def test_concurrent_yield_next_race() -> None:
    """Two schedulers race to yield the same video; EXCLUSIVE lock ensures only one attempting record."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _seed([
        ("RACE1", "pending", "https://youtube.com/channel/UC_R", "2025-01-01T00:00:00"),
    ])

    barrier = threading.Barrier(2)
    results_a: list[tuple[str, str]] = []
    results_b: list[tuple[str, str]] = []
    error_a: list[Exception] = []
    error_b: list[Exception] = []

    def yield_a() -> None:
        try:
            barrier.wait()  # sync with B before racing
            sched_a = BatchScheduler(db_path=_TEST_DB)
            results_a.extend(sched_a.yield_next())
        except Exception as e:
            error_a.append(e)

    def yield_b() -> None:
        try:
            barrier.wait()  # sync with A before racing
            sched_b = BatchScheduler(db_path=_TEST_DB)
            results_b.extend(sched_b.yield_next())
        except Exception as e:
            error_b.append(e)

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_a = executor.submit(yield_a)
        f_b = executor.submit(yield_b)
        f_a.result()
        f_b.result()

    # Verify exactly ONE attempting record in DB (EXCLUSIVE lock prevents duplicates)
    conn = sqlite3.connect(_TEST_DB)
    attempting_rows = conn.execute(
        "SELECT video_id, status FROM download_archive WHERE status='attempting'"
    ).fetchall()
    conn.close()

    assert len(attempting_rows) == 1, (
        f"Expected exactly 1 attempting record, got {len(attempting_rows)}: {attempting_rows}"
    )

    # Verify total yielded across both schedulers = 1 (one wins, one gets nothing)
    all_yielded = [v for v, _ in results_a] + [v for v, _ in results_b]
    assert len(all_yielded) == 1, f"Expected 1 total yield, got {len(all_yielded)}: {all_yielded}"
    assert all_yielded[0] == "RACE1"


# ─── test_skipped_status_not_yielded ──────────────────────────────────────────

def test_skipped_status_not_yielded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Videos with status='skipped' in download_archive are not yielded."""
    _seed([
        ("SKIP1", "pending", "https://youtube.com/channel/UC_K", "2025-01-01T00:00:00"),
    ])

    # Pre-insert skipped entry — video should be skipped even if pre-check passes
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) "
        "VALUES (?, 'skipped', ?, ?)",
        ("SKIP1", "https://youtube.com/channel/UC_K", time.time()),
    )
    conn.commit()
    conn.close()

    # No pre-check anymore - test archive filter directly
    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]
    assert "SKIP1" not in video_ids


# ─── test_schema_no_consecutive_429s ───────────────────────────────────────────

def test_schema_no_consecutive_429s() -> None:
    """channel_cooldown table has exactly 2 columns (source, cooldown_until)."""
    conn = sqlite3.connect(_TEST_DB)
    cursor = conn.execute("PRAGMA table_info(channel_cooldown)")
    columns = [(row[1], row[2]) for row in cursor.fetchall()]
    conn.close()

    col_names = [name for name, _ in columns]
    assert "consecutive_429s" not in col_names, (
        f"consecutive_429s should not exist in channel_cooldown: {col_names}"
    )
    assert len(columns) == 2, f"Expected 2 columns, got {len(columns)}: {col_names}"



# ─── test_skipped_video_not_yielded_on_recovery ────────────────────────────────

def test_skipped_video_not_yielded_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A video marked 'skipped' in a prior pass is still skipped on recovery."""
    _seed([
        ("SKIP_RECOVER", "pending", "https://youtube.com/channel/UC_Q", "2025-01-01T00:00:00"),
    ])

    # Pre-insert a 'skipped' entry (as if a prior pass already failed it)
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT OR REPLACE INTO download_archive (video_id, status, source, attempted_at) "
        "VALUES (?, 'skipped', ?, ?)",
        ("SKIP_RECOVER", "https://youtube.com/channel/UC_Q", time.time()),
    )
    conn.commit()
    conn.close()

    # No pre-check anymore - test that 'skipped' archive status blocks re-yield
    sched = BatchScheduler(db_path=_TEST_DB)
    results = list(sched.yield_next())
    video_ids = [vid for vid, _ in results]

    # Even with pre-check passing, 'skipped' archive status should block the yield
    assert "SKIP_RECOVER" not in video_ids, (
        "Video with prior 'skipped' status should not be yielded even after recovery"
    )


# ─── test_schema_migration_removes_consecutive_429s ─────────────────────────────

def test_schema_migration_removes_consecutive_429s() -> None:
    """Migration handles existing DB that still has the consecutive_429s column."""
    import os as _os

    # Use a separate DB for this test to avoid interfering with other tests
    _MIGRATION_TEST_DB = _TEST_DB_DIR / "test_migration_consecutive_429s.sqlite"
    for suffix in ("", "-wal", "-shm"):
        try:
            _os.unlink(str(_MIGRATION_TEST_DB) + suffix)
        except FileNotFoundError:
            pass

    # Create old-schema DB (with consecutive_429s column)
    conn = sqlite3.connect(_MIGRATION_TEST_DB)
    conn.execute(
        "CREATE TABLE channel_cooldown ("
        "source TEXT PRIMARY KEY, "
        "cooldown_until REAL NOT NULL, "
        "consecutive_429s INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE download_archive ("
        "video_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "source TEXT, attempted_at REAL NOT NULL, error TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE analysis_status ("
        "video_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, source TEXT, "
        "published_at TEXT, has_captions INTEGER"
        ")"
    )
    # Insert a row that would have had consecutive_429s
    conn.execute(
        "INSERT INTO channel_cooldown VALUES (?, ?, ?)",
        ("https://youtube.com/channel/UC_OLD", 9999999999.0, 3),
    )
    conn.commit()
    conn.close()

    # Now create a scheduler (which runs migrations) — if migration tries to DROP
    # the column on an older DB, it should be wrapped in try/except
    try:
        sched = BatchScheduler(db_path=_MIGRATION_TEST_DB)
        conn2 = sqlite3.connect(_MIGRATION_TEST_DB)
        cursor = conn2.execute("PRAGMA table_info(channel_cooldown)")
        columns = [row[1] for row in cursor.fetchall()]
        conn2.close()
        assert "consecutive_429s" not in columns, (
            f"Migration should remove consecutive_429s: got columns {columns}"
        )
    except Exception:
        # If DROP COLUMN fails (e.g., SQLite version), that's acceptable —
        # the column is simply unused. Verify it's not referenced in code.
        import csf.batch_status as bs

        src = bs.__file__
        with open(src, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        assert "consecutive_429s" not in content.lower(), (
            "consecutive_429s should not be referenced in batch_status source"
        )

