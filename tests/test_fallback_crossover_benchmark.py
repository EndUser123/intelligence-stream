"""Tests for the fallback crossover benchmark harness."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path


def _load_benchmark_module():
    script = Path("P:/packages/yt-is/bin/csf-fallback-crossover-benchmark")
    loader = importlib.machinery.SourceFileLoader("csf_fallback_crossover_benchmark", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _seed_captioned_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE analysis_status (
                video_id TEXT PRIMARY KEY,
                source TEXT,
                status TEXT,
                published_at TEXT,
                has_captions INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO analysis_status (video_id, source, status, published_at, has_captions) VALUES (?, ?, ?, ?, ?)",
            [
                ("vid-b", "https://www.youtube.com/channel/UC1", "complete", "2026-04-02T00:00:00Z", 1),
                ("vid-a", "https://www.youtube.com/channel/UC2", "pending", "2026-04-03T00:00:00Z", 1),
                ("vid-no", "https://www.youtube.com/channel/UC3", "pending", "2026-04-04T00:00:00Z", 0),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_route_plus_fallback_policy_enables_both_route_and_fallback_workers():
    mod = _load_benchmark_module()

    policy = mod.POLICY_ENV["notebooklm_route_plus_fallback_30s"]

    assert policy["YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK"] == "true"
    assert policy["YTIS_TRANSCRIPT_FALLBACK_WORKERS"] == "2"
    assert policy["YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S"] == "0"


def test_load_cohort_from_trace_include_ready_collects_ready_events(tmp_path):
    trace_root = tmp_path / "trace-root"
    trace_root.mkdir()
    trace_path = trace_root / "term_00000000.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {"video_id": "vid-fetch", "status": "ready", "source_url": "https://example.invalid/1"},
                    }
                ),
                json.dumps(
                    {
                        "action": "staging_source_content_readiness_probe_completed",
                        "data": {"video_id": "vid-probe", "status": "ready", "source_url": "https://example.invalid/2"},
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {"video_id": "vid-skip", "status": "pending", "source_url": "https://example.invalid/3"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    mod = _load_benchmark_module()
    items = mod._load_cohort_from_trace(trace_root, include_ready=True)

    assert [item["video_id"] for item in items] == ["vid-fetch", "vid-probe"]
    assert all(item["source_url"].startswith("https://example.invalid/") for item in items)
    assert all(item["has_captions"] is True for item in items)


def test_load_captioned_cohort_from_db_filters_and_orders_by_publish_date(tmp_path, monkeypatch):
    db_path = tmp_path / "batch_status.sqlite"
    _seed_captioned_db(db_path)
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(db_path))

    mod = _load_benchmark_module()
    items = mod._load_captioned_cohort_from_db()

    assert [item["video_id"] for item in items] == ["vid-a", "vid-b"]
    assert all(item["has_captions"] is True for item in items)
    assert all(item["source_url"].startswith("https://www.youtube.com/channel/") for item in items)


def test_load_or_build_cohort_uses_captioned_shape(tmp_path, monkeypatch):
    db_path = tmp_path / "batch_status.sqlite"
    _seed_captioned_db(db_path)
    monkeypatch.setenv("YTIS_BATCH_STATUS_DB_PATH", str(db_path))

    mod = _load_benchmark_module()
    cohort_path = tmp_path / "captioned-cohort.json"
    cohort = mod._load_or_build_cohort(cohort_path, tmp_path / "trace-root", "captioned")

    assert cohort["cohort_shape"] == "captioned"
    assert cohort["batch_status_db_path"] == str(db_path)
    assert [item["video_id"] for item in cohort["items"]] == ["vid-a", "vid-b"]
    saved = json.loads(cohort_path.read_text(encoding="utf-8"))
    assert saved["cohort_shape"] == "captioned"


def test_load_or_build_cohort_mixed_shape_combines_ready_and_non_ready_trace_items(tmp_path):
    trace_root = tmp_path / "trace-root"
    trace_root.mkdir()
    trace_path = trace_root / "term_00000000.jsonl"
    trace_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {"video_id": "vid-ready-1", "status": "ready", "source_url": ""},
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {
                            "video_id": "vid-pending-1",
                            "status": "pending",
                            "source_url": "",
                            "youtube_ytdlp_classification": "ok",
                        },
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {"video_id": "vid-ready-2", "status": "ready", "source_url": ""},
                    }
                ),
                json.dumps(
                    {
                        "action": "nlm_batch_source_content_fetch_completed",
                        "data": {
                            "video_id": "vid-pending-2",
                            "status": "too_short",
                            "source_url": "",
                            "youtube_ytdlp_classification": "ok",
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    mod = _load_benchmark_module()
    cohort_path = tmp_path / "mixed-cohort.json"
    cohort = mod._load_or_build_cohort(cohort_path, trace_root, "mixed")

    assert cohort["cohort_shape"] == "mixed"
    assert [item["video_id"] for item in cohort["items"]] == [
        "vid-ready-1",
        "vid-pending-1",
        "vid-ready-2",
        "vid-pending-2",
    ]
    assert [item["has_captions"] for item in cohort["items"]] == [True, False, True, False]
