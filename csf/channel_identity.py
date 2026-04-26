"""Canonical YouTube channel identity helpers for yt-is."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelIdentity:
    """Resolved channel identity plus a canonical display URL."""

    channel_id: str
    canonical_url: str
    source_ref: str


def normalize_channel_url(channel_ref: str) -> str:
    """Normalize a YouTube channel reference for stable storage and lookup."""
    if not channel_ref:
        return channel_ref

    normalized = channel_ref.strip()
    if "/channel/@" in normalized:
        normalized = normalized.replace("/channel/@", "/@")
    normalized = re.sub(
        r"^(https?://(?:[\w-]+\.)*youtube\.com)@",
        r"\1/@",
        normalized,
    )
    if normalized.startswith("@"):
        normalized = f"https://www.youtube.com/{normalized}"
    return normalized


def channel_lookup_candidates(channel_ref: str) -> list[str]:
    """Return lookup candidates from most-specific to least-specific."""
    normalized = normalize_channel_url(channel_ref)
    candidates: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in candidates:
            candidates.append(value)

    add(normalized)

    try:
        from csf.source_enumerator import parse_channel_url, resolve_to_uc_channel_id
    except Exception:
        return candidates

    parsed = parse_channel_url(normalized)
    if not parsed:
        return candidates

    if parsed.startswith("UC"):
        add(parsed)
        add(f"https://www.youtube.com/channel/{parsed}")
        return candidates

    add(parsed)
    if parsed.startswith("@"):
        add(f"https://www.youtube.com/{parsed}")
    elif parsed.startswith("c/"):
        add(f"https://www.youtube.com/{parsed}")
    elif parsed.startswith("user/"):
        add(f"https://www.youtube.com/{parsed}")

    uc_id = resolve_to_uc_channel_id(parsed)
    add(uc_id)
    if uc_id:
        add(f"https://www.youtube.com/channel/{uc_id}")
    return candidates


def resolve_channel_identity(channel_ref: str) -> ChannelIdentity | None:
    """Resolve a channel reference into a stable channel ID and display URL."""
    normalized = normalize_channel_url(channel_ref)

    try:
        from csf.source_enumerator import parse_channel_url, resolve_to_uc_channel_id
    except Exception:
        return None

    parsed = parse_channel_url(normalized)
    if not parsed:
        return None

    if parsed.startswith("UC"):
        return ChannelIdentity(
            channel_id=parsed,
            canonical_url=f"https://www.youtube.com/channel/{parsed}",
            source_ref=normalized,
        )

    uc_id = resolve_to_uc_channel_id(parsed)
    if not uc_id:
        return None

    if parsed.startswith("@"):
        canonical_url = f"https://www.youtube.com/{parsed}"
    elif parsed.startswith("c/"):
        canonical_url = f"https://www.youtube.com/{parsed}"
    elif parsed.startswith("user/"):
        canonical_url = f"https://www.youtube.com/{parsed}"
    else:
        canonical_url = normalized

    return ChannelIdentity(
        channel_id=uc_id,
        canonical_url=canonical_url,
        source_ref=normalized,
    )
