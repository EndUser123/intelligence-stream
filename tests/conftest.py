"""Pytest configuration for intelligence-stream tests."""

import pytest


@pytest.fixture(autouse=True)
def clean_shared_cache():
    """Clear the shared transcript cache before each test.

    The transcript cache is shared across terminals via a single SQLite DB.
    Each test must start with an empty cache and stopped writer threads
    to avoid cross-test contamination.
    """
    from pathlib import Path

    # 1. Stop all writer threads and clear in-memory storages FIRST.
    #    This ensures no more writes happen while we delete the DB.
    import csf.cache

    csf.cache.clear_all_storages()

    # 2. Now delete the DB files
    # CI-aware: skip if P:/__csf/ does not exist (ubuntu-latest has no P: drive)
    db_path = Path("P:/__csf/.data/intelligence-stream/transcripts/transcripts.sqlite")
    if db_path.parent.exists():
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        # Also clean up WAL and SHM files if they exist
        for suffix in ("-wal", "-shm"):
            wal_path = Path(str(db_path) + suffix)
            if wal_path.exists():
                try:
                    wal_path.unlink()
                except OSError:
                    pass
    yield
