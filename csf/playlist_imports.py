"""Append-only playlist import logging for yt-is."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from csf.batch_status import block_channel, set_channel_metadata

_DEFAULT_DB_PATH = Path("P:/.data/yt-is/playlists.sqlite")
_db_lock = threading.RLock()


def get_playlist_import_db_path() -> Path:
    """Return the active playlist-import DB path."""
    override = os.environ.get("YTIS_PLAYLIST_IMPORT_DB_PATH")
    if override:
        return Path(override)
    return _DEFAULT_DB_PATH


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or get_playlist_import_db_path(), timeout=30.0)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlist_import_run (
            run_id TEXT PRIMARY KEY,
            playlist_kind TEXT NOT NULL,
            playlist_url TEXT NOT NULL,
            command TEXT NOT NULL,
            cookie_source TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            total_items INTEGER DEFAULT 0,
            resolved_items INTEGER DEFAULT 0,
            new_channels INTEGER DEFAULT 0,
            already_tracked_channels INTEGER DEFAULT 0,
            blocked_channels INTEGER DEFAULT 0,
            failed_items INTEGER DEFAULT 0,
            notes_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlist_import_item (
            run_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            playlist_kind TEXT NOT NULL,
            playlist_url TEXT NOT NULL,
            playlist_position INTEGER,
            video_id TEXT,
            video_url TEXT,
            video_title TEXT,
            channel_id TEXT,
            channel_url TEXT,
            channel_title TEXT,
            published_at TEXT,
            duration_seconds INTEGER,
            availability TEXT,
            is_live INTEGER,
            raw_json TEXT NOT NULL DEFAULT '{}',
            resolved_channel_json TEXT NOT NULL DEFAULT '{}',
            classification TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, item_id)
        )
        """
    )


def _ensure_db(db_path: Path | None = None) -> None:
    with _db_lock:
        target = db_path or get_playlist_import_db_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with _connect(target) as conn:
            _ensure_schema(conn)
            conn.commit()


def _json_text(value: Any | None) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def record_playlist_import_run(
    *,
    playlist_kind: str,
    playlist_url: str,
    command: str,
    cookie_source: str | None = None,
    total_items: int = 0,
    notes: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> str:
    """Insert a new append-only playlist import run and return its run_id."""
    _ensure_db(db_path)
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    with _db_lock, _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO playlist_import_run (
                run_id, playlist_kind, playlist_url, command, cookie_source,
                started_at, status, total_items, notes_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                run_id,
                playlist_kind,
                playlist_url,
                command,
                cookie_source,
                started_at,
                total_items,
                _json_text(notes),
            ),
        )
        conn.commit()
    return run_id


def finish_playlist_import_run(
    run_id: str,
    *,
    status: str,
    total_items: int | None = None,
    resolved_items: int | None = None,
    new_channels: int | None = None,
    already_tracked_channels: int | None = None,
    blocked_channels: int | None = None,
    failed_items: int | None = None,
    db_path: Path | None = None,
) -> None:
    """Finalize a playlist import run with summary counts."""
    _ensure_db(db_path)
    finished_at = datetime.now(timezone.utc).isoformat()
    fields = [
        "status = ?",
        "finished_at = ?",
    ]
    params: list[Any] = [status, finished_at]
    for column, value in [
        ("total_items", total_items),
        ("resolved_items", resolved_items),
        ("new_channels", new_channels),
        ("already_tracked_channels", already_tracked_channels),
        ("blocked_channels", blocked_channels),
        ("failed_items", failed_items),
    ]:
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    params.append(run_id)
    with _db_lock, _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            f"UPDATE playlist_import_run SET {', '.join(fields)} WHERE run_id = ?",
            params,
        )
        conn.commit()


def record_playlist_import_item(
    *,
    run_id: str,
    item_id: str,
    playlist_kind: str,
    playlist_url: str,
    playlist_position: int | None,
    video_id: str | None,
    video_url: str | None,
    video_title: str | None,
    channel_id: str | None,
    channel_url: str | None,
    channel_title: str | None,
    published_at: str | None,
    duration_seconds: int | None,
    availability: str | None,
    is_live: bool | None,
    classification: str,
    raw_json: dict[str, Any] | str | None = None,
    resolved_channel_json: dict[str, Any] | str | None = None,
    db_path: Path | None = None,
) -> None:
    """Insert an append-only playlist import item row."""
    _ensure_db(db_path)
    created_at = datetime.now(timezone.utc).isoformat()
    with _db_lock, _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO playlist_import_item (
                run_id, item_id, playlist_kind, playlist_url, playlist_position,
                video_id, video_url, video_title, channel_id, channel_url,
                channel_title, published_at, duration_seconds, availability,
                is_live, raw_json, resolved_channel_json, classification, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item_id,
                playlist_kind,
                playlist_url,
                playlist_position,
                video_id,
                video_url,
                video_title,
                channel_id,
                channel_url,
                channel_title,
                published_at,
                duration_seconds,
                availability,
                1 if is_live else 0 if is_live is not None else None,
                _json_text(raw_json),
                _json_text(resolved_channel_json),
                classification,
                created_at,
            ),
        )
        conn.commit()


def get_playlist_import_run(run_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return a playlist import run row as a dict."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT run_id, playlist_kind, playlist_url, command, cookie_source,
                   started_at, finished_at, status, total_items, resolved_items,
                   new_channels, already_tracked_channels, blocked_channels,
                   failed_items, notes_json
            FROM playlist_import_run
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    keys = [
        "run_id",
        "playlist_kind",
        "playlist_url",
        "command",
        "cookie_source",
        "started_at",
        "finished_at",
        "status",
        "total_items",
        "resolved_items",
        "new_channels",
        "already_tracked_channels",
        "blocked_channels",
        "failed_items",
        "notes_json",
    ]
    return dict(zip(keys, row, strict=False))


def get_playlist_import_item_rows(run_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return all playlist import item rows for a run."""
    _ensure_db(db_path)
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT run_id, item_id, playlist_kind, playlist_url, playlist_position,
                   video_id, video_url, video_title, channel_id, channel_url,
                   channel_title, published_at, duration_seconds, availability,
                   is_live, raw_json, resolved_channel_json, classification, created_at
            FROM playlist_import_item
            WHERE run_id = ?
            ORDER BY playlist_position ASC, item_id ASC
            """,
            (run_id,),
        ).fetchall()
    keys = [
        "run_id",
        "item_id",
        "playlist_kind",
        "playlist_url",
        "playlist_position",
        "video_id",
        "video_url",
        "video_title",
        "channel_id",
        "channel_url",
        "channel_title",
        "published_at",
        "duration_seconds",
        "availability",
        "is_live",
        "raw_json",
        "resolved_channel_json",
        "classification",
        "created_at",
    ]
    return [dict(zip(keys, row, strict=False)) for row in rows]


def replay_playlist_import_run_into_batch_status(
    run_id: str,
    *,
    batch_status_db_path: Path | None = None,
    playlist_import_db_path: Path | None = None,
) -> int:
    """Rebuild live channel state from an import run."""
    rows = get_playlist_import_item_rows(run_id, db_path=playlist_import_db_path)
    promoted = 0
    for row in rows:
        classification = str(row.get("classification") or "").lower()
        channel_url = row.get("channel_url")
        if not channel_url:
            continue
        if classification in {"accepted", "tracked", "new_channel"}:
            set_channel_metadata(
                channel_url,
                playlist_id=None,
                last_checked=datetime.now(timezone.utc).isoformat(),
                video_count_estimate=None,
                db_path=batch_status_db_path,
                channel_title=row.get("channel_title"),
                description=None,
                published_at=row.get("published_at"),
            )
            promoted += 1
        elif classification.startswith("blocked"):
            block_channel(channel_url, db_path=batch_status_db_path)
            promoted += 1
    return promoted
