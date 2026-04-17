"""Display formatting utilities for yt-is CLI.

Provides consistent table formatting for channel statistics and other output.
Separated from business logic for maintainability.
"""

import datetime
from typing import NamedTuple


class ChannelStats(NamedTuple):
    """Channel statistics for display."""

    channel_url: str
    total: int  # db_count
    main_trackable: int  # mt = total - shorts - unavailable - no_subs
    downloaded: int  # dt (cached transcripts)
    available: int  # vt (has subtitles, not downloaded)
    unavailable: int  # nt (no subtitles)
    shorts: int
    playlists: int
    subscribers: str | None
    last_checked: str | None
    last_full_enumeration: str | None
    last_download: str | None
    success_rate: str
    languages: str
    storage_size_mb: float

    def format_timestamp(self, ts: str | None) -> str:
        """Format timestamp for display in Calgary (MDT/MST) time."""
        if not ts:
            return "Never"
        # Parse UTC timestamp and convert to Calgary time
        try:
            # Handle both Z-suffix and +00:00 offset formats
            normalized = ts.replace("Z", "+00:00")
            dt_utc = datetime.datetime.fromisoformat(normalized)
            # Calgary/Mountain: UTC-7 (MDT) or UTC-8 (MST) depending on DST
            utc_now = datetime.datetime.now(datetime.timezone.utc)
            is_dst = (utc_now.month > 3 or (utc_now.month == 3 and utc_now.day >= 8)) and utc_now.month < 11
            calgary_offset = -7 if is_dst else -8
            calgary_tz = datetime.timezone(datetime.timedelta(hours=calgary_offset))
            dt_calgary = dt_utc.astimezone(calgary_tz)
            return dt_calgary.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            # Fallback to raw UTC display
            return ts[:19].replace("T", " ")

    def format_summary(self, widths: tuple[int, int, int] | None = None) -> str:
        """Format compact summary: ct, tt, vt.

        - ct (channel total): All videos known
        - tt (transcript total): Videos with captions available
        - vt (verified): Downloaded to disk
        """
        ct_width, tt_width, vt_width = widths or (
            len(str(self.total)),
            len(str(self.main_trackable)),
            len(str(self.downloaded)),
        )
        return (
            f"{self.total:>{ct_width}} ct, "
            f"{self.main_trackable:>{tt_width}} tt, "
            f"{self.downloaded:>{vt_width}} vt"
        )

    def format_metadata(self) -> str:
        """Format optional metadata: playlists, subscribers"""
        parts = []
        if self.playlists > 0:
            parts.append(f"{self.playlists} pl")
        if self.subscribers:
            parts.append(f"{self.subscribers} subs")
        return ", ".join(parts) if parts else "-"

    def format_last_checked(self) -> str:
        """Format last_checked timestamp for display."""
        return self.format_timestamp(self.last_checked)

    def format_last_enum(self) -> str:
        """Format last_full_enumeration timestamp for display."""
        return self.format_timestamp(self.last_full_enumeration)

    def format_last_download(self) -> str:
        """Format last_download timestamp for display."""
        return self.format_timestamp(self.last_download)

    def format_success_rate(self) -> str:
        """Format success rate as percentage."""
        return self.success_rate

    def format_languages(self) -> str:
        """Format languages list."""
        return self.languages if self.languages else "-"

    def format_storage_size(self) -> str:
        """Format storage size in MB."""
        if self.storage_size_mb < 1:
            return f"{self.storage_size_mb * 1024:.1f}KB"
        return f"{self.storage_size_mb:.1f}MB"


def format_channel_list(channels: list[ChannelStats]) -> str:
    """Format channel statistics in the compact summary style.

    Format:
        ```
        Legend:
        ct = Channel total (all videos)
        tt = Transcript total (videos with captions)
        vt = Verified transcripts (downloaded to disk)

        Channel URL                                              ct   tt   vt   | Size      | Last Checked
        ```

    Args:
        channels: List of channel statistics

    Returns:
        Formatted output string
    """
    if not channels:
        return "[source] No sources tracked. Use 'add' to add a channel or playlist."

    lines = [
        "[source] Tracked sources:",
        "",
        "```",
        "Legend:",
        "  ct = Channel total (all videos)",
        "  tt = Transcript total (videos with captions)",
        "  vt = Verified transcripts (downloaded to disk)",
        "  lc = Last checked (timestamp)",
        "",
    ]

    normalized_urls = []
    for ch in channels:
        display_url = ch.channel_url
        if "/channel/@" in display_url:
            display_url = display_url.replace("/channel/@", "/@")
        normalized_urls.append(display_url)

    url_width = min(80, max(60, max(len(url) for url in normalized_urls)))
    summary_widths = (
        max(len(str(ch.total)) for ch in channels),
        max(len(str(ch.main_trackable)) for ch in channels),
        max(len(str(ch.downloaded)) for ch in channels),
    )

    for ch, display_url in zip(channels, normalized_urls):
        # Single line: URL | ct, tt, vt | size | last_checked
        summary = (
            f"{ch.format_summary(summary_widths)}"
            f" | {ch.format_storage_size():>8}"
            f" | {ch.format_last_checked()[:16]}"
        )
        lines.append(f"{display_url:<{url_width}} | {summary}")

    lines.append("```")
    return "\n".join(lines)


def format_sync_results(
    channels: list[tuple[str, int, int, str]], total_new: int
) -> str:
    """Format sync results as a table.

    Args:
        channels: List of (channel_url, video_count, new_count, last_checked) tuples
        total_new: Total number of new videos found

    Returns:
        Formatted table output
    """
    if not channels:
        return "[source] No channels to check."

    # Find max channel URL length for column width
    max_url_len = min(70, max(50, max((len(ch[0]) for ch in channels), default=50)))
    video_width = max(len("Videos"), max(len(str(ch[1])) for ch in channels))
    new_width = max(len("New"), max(len(str(ch[2])) for ch in channels))
    last_checked_width = max(
        len("Last Checked"),
        max(len((ch[3][:19] if ch[3] else "Never").replace("T", " ")) for ch in channels),
    )

    # Build header
    lines = [
        f"{'Channel URL':<{max_url_len}} {'Videos':>{video_width}} {'New':>{new_width}} {'Last Checked':<{last_checked_width}}",
        "-" * (max_url_len + video_width + new_width + last_checked_width + 3),  # +3 for spaces between columns
    ]

    # Add each channel row
    for channel_url, video_count, new_count, last_checked in channels:
        last_checked_str = (last_checked[:19] if last_checked else "Never").replace("T", " ")
        lines.append(
            f"{channel_url:<{max_url_len}} {video_count:>{video_width}} {new_count:>{new_width}} {last_checked_str:<{last_checked_width}}"
        )

    # Add summary
    lines.append("")
    lines.append(f"[source] Check complete. {total_new} new videos across {len(channels)} channels.")

    return "\n".join(lines)


def format_kv_block(title: str, rows: list[tuple[str, str | int]]) -> str:
    """Format a compact aligned key/value block."""
    if not rows:
        return title

    label_width = max(len(label) for label, _ in rows)
    lines = [title]
    for label, value in rows:
        lines.append(f"{label:<{label_width}} : {value}")
    return "\n".join(lines)


def format_result_row(
    video_id: str, success: bool, detail: str, width: int, indent: str = "  "
) -> str:
    """Format a single aligned result row for batch-style output."""
    symbol = "✓" if success else "✗"
    return f"{indent}{symbol} {video_id:<{width}} | {detail}"
