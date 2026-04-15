"""Tests for csf.display formatting."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\packages\yt-is").absolute()))

from csf.display import (
    ChannelStats,
    format_channel_list,
    format_kv_block,
    format_result_row,
    format_sync_results,
)


def _channel(
    url: str,
    total: int,
    main_trackable: int,
    downloaded: int,
    last_checked: str = "2026-04-13T21:30:00Z",
) -> ChannelStats:
    return ChannelStats(
        channel_url=url,
        total=total,
        main_trackable=main_trackable,
        downloaded=downloaded,
        available=0,
        unavailable=0,
        shorts=0,
        playlists=0,
        subscribers=None,
        last_checked=last_checked,
        last_full_enumeration=None,
        last_download=None,
        success_rate="-",
        languages="",
        storage_size_mb=0.0,
    )


def test_format_channel_list_aligns_summary_counts():
    channels = [
        _channel("https://www.youtube.com/@statquest", 293, 0, 0),
        _channel("https://www.youtube.com/@freeCodeCamp", 2201, 12, 7),
    ]

    output = format_channel_list(channels)
    rows = [line for line in output.splitlines() if line.startswith("https://")]
    summaries = [row.split("|")[1] for row in rows]

    assert len(summaries) == 2
    assert summaries[0].index(",") == summaries[1].index(",")
    assert summaries[0].rindex(",") == summaries[1].rindex(",")


def test_format_channel_list_uses_handle_form_for_channel_urls():
    channel = _channel("https://www.youtube.com/channel/@statquest", 1, 1, 1)

    output = format_channel_list([channel])

    assert "https://www.youtube.com/@statquest" in output
    assert "/channel/@" not in output


def test_format_sync_results_aligns_numeric_columns():
    output = format_sync_results(
        [
            ("https://www.youtube.com/@statquest", 293, 7, "2026-04-13T21:30:00Z"),
            ("https://www.youtube.com/@freeCodeCamp", 2201, 123, "2026-04-13T20:21:00Z"),
        ],
        total_new=130,
    )

    rows = [line for line in output.splitlines() if line.startswith("https://")]
    videos_end = [row.index("293") + len("293") if "293" in row else row.index("2201") + len("2201") for row in rows]
    new_end = [row.index(" 7 ") + 2 if " 7 " in row else row.index("123") + len("123") for row in rows]

    assert len(set(videos_end)) == 1
    assert len(set(new_end)) == 1


def test_format_kv_block_aligns_colons():
    output = format_kv_block(
        "=== Selenium Transcript Extractor ===",
        [("Channels", 9), ("Firefox Profile", "Default"), ("Mode", "DRY RUN")],
    )

    rows = output.splitlines()[1:]
    colon_positions = [row.index(":") for row in rows]

    assert len(set(colon_positions)) == 1


def test_format_result_row_aligns_separator():
    ok_row = format_result_row("dQw4w9WgXcQ", True, "123 chars", 11)
    err_row = format_result_row("abc123", False, "Rate limited", 11)

    assert ok_row.index("|") == err_row.index("|")
