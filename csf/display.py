"""Display formatting utilities for yt-is CLI.

Provides consistent table formatting for channel statistics and other output.
Separated from business logic for maintainability.
"""

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
        """Format timestamp for display."""
        if not ts:
            return "Never"
        # Convert ISO format to more readable format (remove T, truncate microseconds)
        return ts[:19].replace("T", " ")

    def format_summary(self) -> str:
        """Format compact summary: ct, tt, vt

        - ct (channel total): All videos known
        - tt (transcript total): Videos with captions available
        - vt (verified): Downloaded to disk
        """
        parts = []
        parts.append(f"{self.total} ct")
        parts.append(f"{self.main_trackable} tt")
        parts.append(f"{self.downloaded} vt")

        return ", ".join(parts)

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
        "",
    ]

    for ch in channels:
        # Channel URL (fix @handle format: remove /channel/ prefix if present)
        display_url = ch.channel_url
        if "/channel/@" in display_url:
            display_url = display_url.replace("/channel/@", "/@")

        # Single line: URL | ct, tt, vt | size | last_checked
        summary = f"{ch.format_summary()} | {ch.format_storage_size():>8} | {ch.format_last_checked()[:16]}"
        lines.append(f"{display_url:<60} | {summary}")

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

    # Build header
    lines = [
        f"{'Channel URL':<{max_url_len}} {'Videos':>6} {'New':>4} {'Last Checked':<20}",
        "-" * (max_url_len + 6 + 4 + 20 + 3),  # +3 for spaces between columns
    ]

    # Add each channel row
    for channel_url, video_count, new_count, last_checked in channels:
        last_checked_str = (last_checked[:19] if last_checked else "Never").replace("T", " ")
        lines.append(
            f"{channel_url:<{max_url_len}} {video_count:>6} {new_count:>4} {last_checked_str:<20}"
        )

    # Add summary
    lines.append("")
    lines.append(f"[source] Check complete. {total_new} new videos across {len(channels)} channels.")

    return "\n".join(lines)
