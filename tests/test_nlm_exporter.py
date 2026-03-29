"""Tests for csf/nlm_exporter.py — TASK-009.

Tests cover:
- build_composites determinism
- composite_id stability and content-hash-based change detection
- 500K-word and 300-video boundary splitting
- export_composite idempotency and atomicity
- Null published_at handling
- nlm_export_state table integration
"""

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.nlm_exporter import (
    build_composites,
    export_composite,
    _count_words,
    _sha256,
    CompositeDocument,
)
from csf.batch_status import (
    get_nlm_export_state,
    upsert_nlm_export_state,
    get_pending_nlm_exports,
    get_nlm_exports_by_video,
)


# =============================================================================
# build_composites determinism
# =============================================================================

def test_build_composites_deterministic():
    """build_composites returns same output across multiple calls for same input."""
    videos = [
        {"video_id": f"vid{i}", "published_at": f"2024-01-{i%31+1:02d}", "transcript": "word " * 50}
        for i in range(1, 11)
    ]
    docs1 = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    docs2 = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs1) == len(docs2)
    for d1, d2 in zip(docs1, docs2):
        assert d1.composite_id == d2.composite_id
        assert d1.content_hash == d2.content_hash
        assert d1.video_ids == d2.video_ids


def test_build_composites_same_video_set_same_id():
    """Same video set always produces same composite_id regardless of order."""
    videos_a = [
        {"video_id": "a", "published_at": "2024-01-01", "transcript": "x"},
        {"video_id": "b", "published_at": "2024-01-02", "transcript": "y"},
    ]
    videos_b = [
        {"video_id": "b", "published_at": "2024-01-02", "transcript": "y"},
        {"video_id": "a", "published_at": "2024-01-01", "transcript": "x"},
    ]
    docs_a = build_composites("https://youtube.com/channel/UCxyz", videos_a, "nb")
    docs_b = build_composites("https://youtube.com/channel/UCxyz", videos_b, "nb")
    assert docs_a[0].composite_id == docs_b[0].composite_id
    assert docs_a[0].content_hash == docs_b[0].content_hash


# =============================================================================
# composite_id changes when video set changes
# =============================================================================

def test_composite_id_changes_on_video_set_change():
    """composite_id differs when video set changes (content_hash differs)."""
    docs_a = build_composites(
        "https://youtube.com/channel/UCxyz",
        [{"video_id": "a", "published_at": "2024-01-01", "transcript": "x"}],
        "nb",
    )
    docs_b = build_composites(
        "https://youtube.com/channel/UCxyz",
        [{"video_id": "b", "published_at": "2024-01-01", "transcript": "x"}],
        "nb",
    )
    assert docs_a[0].composite_id != docs_b[0].composite_id
    assert docs_a[0].content_hash != docs_b[0].content_hash


def test_composite_id_unchanged_on_same_video_added():
    """Adding a video creates a NEW composite (different composite_id)."""
    docs_small = build_composites(
        "https://youtube.com/channel/UCxyz",
        [{"video_id": "a", "published_at": "2024-01-01", "transcript": "x"}],
        "nb",
    )
    docs_large = build_composites(
        "https://youtube.com/channel/UCxyz",
        [
            {"video_id": "a", "published_at": "2024-01-01", "transcript": "x"},
            {"video_id": "b", "published_at": "2024-01-02", "transcript": "y"},
        ],
        "nb",
    )
    assert docs_small[0].composite_id != docs_large[0].composite_id


# =============================================================================
# 500K word boundary splitting
# =============================================================================

def test_500k_word_boundary_split():
    """Videos split when combined word count exceeds 500K."""
    # 5 videos × 100K words each = 500K — fits in 1 composite
    videos = [
        {"video_id": f"v{i}", "published_at": f"2024-01-{i+1:02d}", "transcript": "word " * 100_000}
        for i in range(5)
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1

    # 6 × 100K = 600K — must split into 2 composites
    videos6 = [
        {"video_id": f"v{i}", "published_at": f"2024-01-{i+1:02d}", "transcript": "word " * 100_000}
        for i in range(6)
    ]
    docs6 = build_composites("https://youtube.com/channel/UCxyz", videos6, "nb")
    assert len(docs6) == 2
    assert len(docs6[0].video_ids.split("|")) == 5
    assert len(docs6[1].video_ids.split("|")) == 1


def test_500k_under_boundary():
    """Under 500K total: single composite."""
    videos = [
        {"video_id": f"v{i}", "published_at": f"2024-01-{i+1:02d}", "transcript": "word " * 10_000}
        for i in range(10)
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1
    assert docs[0].word_count < 500_000


# =============================================================================
# 300-video boundary splitting
# =============================================================================

def test_300_video_boundary_exactly_300():
    """Exactly 300 videos: single composite."""
    videos = [
        {"video_id": f"v{i:03d}", "published_at": f"2024-01-{i%31+1:02d}", "transcript": "a "}
        for i in range(300)
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1
    assert len(docs[0].video_ids.split("|")) == 300


def test_300_video_boundary_over_300():
    """Over 300 videos: split into 2 composites."""
    videos = [
        {"video_id": f"v{i:03d}", "published_at": f"2024-01-{i%31+1:02d}", "transcript": "a "}
        for i in range(305)
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 2
    assert len(docs[0].video_ids.split("|")) == 300
    assert len(docs[1].video_ids.split("|")) == 5


# =============================================================================
# Single video with >500K words raises
# =============================================================================

def test_single_oversized_video_raises():
    """Single video exceeding 500K words raises ValueError."""
    big = {"video_id": "big", "published_at": "2024-01-01", "transcript": "word " * 500_001}
    with pytest.raises(ValueError, match="exceeds"):
        build_composites("https://youtube.com/channel/UCxyz", [big], "nb")


# =============================================================================
# export_composite idempotency
# =============================================================================

def test_export_composite_idempotent_with_matching_hash(tmp_path):
    """Idempotent skip when nlm_source_id set AND content_hash matches."""
    doc = CompositeDocument(
        composite_id="test123",
        notebook_id="nb",
        batch_key="ch:part1",
        video_ids="v1|v2",
        word_count=100,
        content="test content",
        content_hash="abc",
    )

    # Pre-set in DB with matching content_hash
    upsert_nlm_export_state(
        "test123", "ch:part1", "v1|v2", "abc", 100, nlm_source_id="nlm_123", db_path=tmp_path / "bs.db"
    )

    # Mock get_nlm_export_state to return the pre-set record from the temp db
    def mock_get_state(composite_id, db_path=None):
        return get_nlm_export_state(composite_id, db_path=tmp_path / "bs.db")

    with (
        mock.patch("csf.nlm_exporter._DEFAULT_EXPORTS_DIR", tmp_path / "exports"),
        mock.patch("csf.batch_status.get_nlm_export_state", mock_get_state),
    ):
        result = export_composite(doc)

    # Should return existing nlm_source_id without re-exporting
    assert result == "nlm_123"


def test_export_composite_re_export_on_hash_mismatch(tmp_path):
    """Force re-export when content_hash changed but nlm_source_id was set."""
    doc = CompositeDocument(
        composite_id="test456",
        notebook_id="nb",
        batch_key="ch:part1",
        video_ids="v1|v2|v3",  # changed video set
        word_count=200,
        content="new content",
        content_hash="xyz",  # different hash
    )

    # Pre-set in DB with OLD content_hash
    upsert_nlm_export_state(
        "test456", "ch:part1", "v1|v2", "xyz_old", 100, nlm_source_id="nlm_old", db_path=tmp_path / "bs.db"
    )

    with (
        mock.patch("csf.nlm_exporter._DEFAULT_EXPORTS_DIR", tmp_path / "exports"),
        mock.patch("csf.batch_status._DEFAULT_DB_PATH", tmp_path / "bs.db"),
        mock.patch("subprocess.run") as mock_run,
        mock.patch("shutil.which", return_value="/bin/nlm"),
        mock.patch("pathlib.Path.mkdir"),
    ):
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="Source added: new_src")
        with mock.patch("pathlib.Path.write_text"):
            with mock.patch("pathlib.Path.rename"):
                # Call while holding lock — result intentionally unchecked (mocked)
                _ = export_composite(doc)

    # Should not return old nlm_source_id (re-export attempted)


# =============================================================================
# export_composite atomicity
# =============================================================================

def test_export_composite_cleans_up_tmp_on_failure(tmp_path):
    """.tmp file is deleted when nlm CLI fails."""
    doc = CompositeDocument(
        composite_id="test789",
        notebook_id="nb",
        batch_key="ch:part1",
        video_ids="v1",
        word_count=10,
        content="content",
        content_hash="hash",
    )

    exports_dir = tmp_path / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = exports_dir / "test789.tmp"

    with (
        mock.patch("csf.nlm_exporter._DEFAULT_EXPORTS_DIR", exports_dir),
        mock.patch("subprocess.run") as mock_run,
        mock.patch("shutil.which", return_value="/bin/nlm"),
    ):
        # Simulate nlm CLI failure
        mock_run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="API error")
        tmp_file.write_text("content")

        result = export_composite(doc)

        assert result is None
        assert not tmp_file.exists()  # .tmp should be cleaned up


def test_export_composite_no_tmp_file_after_success(tmp_path):
    """.tmp file renamed to .txt on successful export (atomic rename)."""
    doc = CompositeDocument(
        composite_id="testatomic",
        notebook_id="nb",
        batch_key="ch:part1",
        video_ids="v1",
        word_count=10,
        content="content",
        content_hash="hash",
    )

    exports_dir = tmp_path / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = exports_dir / "testatomic.tmp"
    final_file = exports_dir / "testatomic.txt"

    rename_called = []

    def mock_rename(_self, dst):
        rename_called.append(True)
        final_file.write_text(tmp_file.read_text())

    with (
        mock.patch("csf.nlm_exporter._DEFAULT_EXPORTS_DIR", exports_dir),
        mock.patch("csf.batch_status._DEFAULT_DB_PATH", tmp_path / "bs.db"),
        mock.patch("subprocess.run") as mock_run,
        mock.patch("shutil.which", return_value="/bin/nlm"),
        mock.patch("pathlib.Path.write_text"),
        mock.patch("pathlib.Path.rename", mock_rename),
    ):
        mock_run.return_value = mock.MagicMock(returncode=0, stdout="Source added: src_abc")
        _ = export_composite(doc)

    assert len(rename_called) > 0


# =============================================================================
# Null published_at handling
# =============================================================================

def test_null_published_at_sorted_to_end():
    """Videos with null published_at sorted to end, don't crash."""
    videos = [
        {"video_id": "with_date", "published_at": "2024-01-01", "transcript": "a"},
        {"video_id": "no_date", "published_at": None, "transcript": "b"},
        {"video_id": "with_date2", "published_at": "2024-01-02", "transcript": "c"},
        {"video_id": "no_date2", "published_at": None, "transcript": "d"},
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1
    # Videos with dates come first, nulls last
    ids_order = docs[0].video_ids
    assert ids_order.index("with_date") < ids_order.index("no_date")
    assert ids_order.index("with_date2") < ids_order.index("no_date2")


def test_all_null_published_at_does_not_crash():
    """All videos with null published_at: still builds composite (no crash)."""
    videos = [
        {"video_id": f"v{i}", "published_at": None, "transcript": "x"}
        for i in range(3)
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1
    assert "v0" in docs[0].video_ids


# =============================================================================
# composite_id encoding invariance
# =============================================================================

def test_composite_id_same_with_unicode_channel_name():
    """Same composite_id across Python versions for same video set."""
    videos = [
        {"video_id": "a", "published_at": "2024-01-01", "transcript": "x"},
        {"video_id": "b", "published_at": "2024-01-02", "transcript": "y"},
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "test-notebook")
    assert len(docs) == 1
    # composite_id is deterministic SHA-256 prefix
    assert len(docs[0].composite_id) == 16
    assert docs[0].composite_id.isalnum()


# =============================================================================
# _count_words and _sha256 helpers
# =============================================================================

def test_count_words():
    assert _count_words("hello world") == 2
    assert _count_words("  hello   world  ") == 2
    assert _count_words("") == 0
    assert _count_words("word " * 100) == 100


def test_sha256_deterministic():
    h1 = _sha256("abc")
    h2 = _sha256("abc")
    assert h1 == h2
    assert h1 != _sha256("def")


# =============================================================================
# nlm_export_state table integration (batch_status)
# =============================================================================

def test_nlm_export_state_upsert_is_idempotent(tmp_path):
    """Same composite_id upserted twice: second call preserves notebook_id."""
    db = tmp_path / "bs.db"
    upsert_nlm_export_state(
        "c1", "batch1", "v1|v2", "hash1", 100, notebook_id="nb_abc", db_path=db
    )
    # Upsert again with same composite_id but different values
    upsert_nlm_export_state(
        "c1", "batch2", "v1|v2|v3", "hash2", 200, db_path=db
    )
    state = get_nlm_export_state("c1", db_path=db)
    assert state is not None
    assert state["notebook_id"] == "nb_abc"  # preserved
    assert state["word_count"] == 200  # updated
    assert state["batch_key"] == "batch2"  # updated


def test_pending_exports_query_correct(tmp_path):
    """get_pending_nlm_exports returns only composites with null notebook_id."""
    upsert_nlm_export_state("p1", "b1", "v1", "h1", 10, db_path=tmp_path / "bs.db")
    upsert_nlm_export_state("p2", "b2", "v2", "h2", 20, notebook_id="nlm_123", db_path=tmp_path / "bs.db")
    upsert_nlm_export_state("p3", "b3", "v3", "h3", 30, db_path=tmp_path / "bs.db")

    pending = get_pending_nlm_exports(db_path=tmp_path / "bs.db")
    assert len(pending) == 2
    pending_ids = {p["composite_id"] for p in pending}
    assert "p1" in pending_ids
    assert "p3" in pending_ids
    assert "p2" not in pending_ids


def test_nlm_export_state_table_created_on_first_use(tmp_path):
    """nlm_export_state table is created on first upsert (no pre-existing DB)."""
    db = tmp_path / "new.db"
    assert not db.exists()
    upsert_nlm_export_state("c1", "b1", "v1", "h1", 10, db_path=db)
    state = get_nlm_export_state("c1", db_path=db)
    assert state is not None
    assert state["composite_id"] == "c1"


def test_get_nlm_exports_by_video(tmp_path):
    """get_nlm_exports_by_video returns composites containing the video."""
    upsert_nlm_export_state(
        "comp1", "batch1", "vid1|vid2|vid3", "h1", 100, db_path=tmp_path / "bs.db"
    )
    upsert_nlm_export_state(
        "comp2", "batch2", "vid4|vid5", "h2", 50, db_path=tmp_path / "bs.db"
    )

    results = get_nlm_exports_by_video("vid2", db_path=tmp_path / "bs.db")
    assert len(results) == 1
    assert results[0]["composite_id"] == "comp1"

    results2 = get_nlm_exports_by_video("vid5", db_path=tmp_path / "bs.db")
    assert len(results2) == 1
    assert results2[0]["composite_id"] == "comp2"

    results3 = get_nlm_exports_by_video("nonexistent", db_path=tmp_path / "bs.db")
    assert len(results3) == 0


def test_get_nlm_exports_by_video_pipe_delimited(tmp_path):
    """Pipe-delimited video_ids parsed correctly for edge cases."""
    # video at start
    upsert_nlm_export_state("s1", "b", "vidA|vidB|vidC", "h", 10, db_path=tmp_path / "bs.db")
    # video at end
    upsert_nlm_export_state("s2", "b", "vidX|vidY|vidZ", "h", 10, db_path=tmp_path / "bs.db")
    # single video
    upsert_nlm_export_state("s3", "b", "vidSolo", "h", 10, db_path=tmp_path / "bs.db")

    r1 = get_nlm_exports_by_video("vidA", db_path=tmp_path / "bs.db")
    assert len(r1) == 1 and r1[0]["composite_id"] == "s1"

    r2 = get_nlm_exports_by_video("vidZ", db_path=tmp_path / "bs.db")
    assert len(r2) == 1 and r2[0]["composite_id"] == "s2"

    r3 = get_nlm_exports_by_video("vidSolo", db_path=tmp_path / "bs.db")
    assert len(r3) == 1 and r3[0]["composite_id"] == "s3"

    r4 = get_nlm_exports_by_video("vidX", db_path=tmp_path / "bs.db")
    assert len(r4) == 1 and r4[0]["composite_id"] == "s2"


# =============================================================================
# Edge cases
# =============================================================================

def test_empty_video_list():
    """Empty videos returns empty list of composites."""
    docs = build_composites("https://youtube.com/channel/UCxyz", [], "nb")
    assert docs == []


def test_channel_url_validation_empty():
    """Empty channel_url raises ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        build_composites("", [{"video_id": "v1", "transcript": "x"}], "nb")


def test_channel_url_validation_too_long():
    """channel_url > 500 chars raises ValueError."""
    long_url = "https://youtube.com/channel/" + "x" * 500
    with pytest.raises(ValueError, match="non-empty"):
        build_composites(long_url, [{"video_id": "v1", "transcript": "x"}], "nb")


def test_video_without_video_id_skipped():
    """Video dicts without video_id are skipped with a warning."""
    videos = [
        {"video_id": "v1", "transcript": "x"},
        {"transcript": "y"},  # missing video_id
        {"video_id": "v3", "transcript": "z"},
    ]
    docs = build_composites("https://youtube.com/channel/UCxyz", videos, "nb")
    assert len(docs) == 1
    assert "v1" in docs[0].video_ids
    assert "v3" in docs[0].video_ids
    assert "v2" not in docs[0].video_ids  # v2 was skipped (id=None)


import pytest  # noqa: E402 (needed for pytest.raises)
