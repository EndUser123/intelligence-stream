"""Tests for append-only playlist import logging and replay."""

from __future__ import annotations

import json
from pathlib import Path

from csf.batch_status import get_channel_metadata, is_channel_blocked
from csf.playlist_imports import (
    get_playlist_import_item_rows,
    get_playlist_import_run,
    record_playlist_import_item,
    record_playlist_import_run,
    replay_playlist_import_run_into_batch_status,
)


def test_playlist_import_log_records_runs_and_items(tmp_path, monkeypatch):
    import_db = tmp_path / "playlists.sqlite"
    monkeypatch.setenv("YTIS_PLAYLIST_IMPORT_DB_PATH", str(import_db))

    run_id = record_playlist_import_run(
        playlist_kind="watch_later",
        playlist_url="https://www.youtube.com/playlist?list=WL",
        command="watchlater",
        cookie_source="youtube_cookies.txt",
        total_items=2,
        notes={"dry_run": False},
    )
    record_playlist_import_item(
        run_id=run_id,
        item_id="item-1",
        playlist_kind="watch_later",
        playlist_url="https://www.youtube.com/playlist?list=WL",
        playlist_position=1,
        video_id="dQw4w9WgXcQ",
        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_title="Example",
        channel_id="UC123",
        channel_url="https://www.youtube.com/channel/UC123",
        channel_title="Channel",
        published_at="2026-04-25T00:00:00Z",
        duration_seconds=123,
        availability="public",
        is_live=False,
        raw_json=json.dumps({"id": "item-1"}),
        resolved_channel_json=json.dumps({"channel_id": "UC123"}),
        classification="accepted",
    )

    run_row = get_playlist_import_run(run_id)
    item_rows = get_playlist_import_item_rows(run_id)

    assert run_row is not None
    assert run_row["playlist_kind"] == "watch_later"
    assert run_row["playlist_url"].endswith("list=WL")
    assert item_rows[0]["video_id"] == "dQw4w9WgXcQ"
    assert item_rows[0]["classification"] == "accepted"


def test_playlist_import_replay_populates_batch_status(tmp_path, monkeypatch):
    import_db = tmp_path / "playlists.sqlite"
    live_status_db = tmp_path / "batch_status.sqlite"
    monkeypatch.setenv("YTIS_PLAYLIST_IMPORT_DB_PATH", str(import_db))

    run_id = record_playlist_import_run(
        playlist_kind="history",
        playlist_url="https://www.youtube.com/feed/history",
        command="history",
        cookie_source="youtube_cookies.txt",
        total_items=1,
    )
    record_playlist_import_item(
        run_id=run_id,
        item_id="item-1",
        playlist_kind="history",
        playlist_url="https://www.youtube.com/feed/history",
        playlist_position=1,
        video_id="dQw4w9WgXcQ",
        video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_title="Example",
        channel_id="UC123",
        channel_url="https://www.youtube.com/channel/UC123",
        channel_title="Channel",
        published_at="2026-04-25T00:00:00Z",
        duration_seconds=123,
        availability="public",
        is_live=False,
        classification="accepted",
    )

    promoted = replay_playlist_import_run_into_batch_status(
        run_id,
        batch_status_db_path=live_status_db,
    )

    assert promoted == 1
    channel_row = get_channel_metadata(
        "https://www.youtube.com/channel/UC123",
        db_path=live_status_db,
    )
    assert channel_row is not None
    assert is_channel_blocked("https://www.youtube.com/channel/UC999", db_path=live_status_db) is False

