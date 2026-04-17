#!/usr/bin/env python3
"""Backfill channel metadata (description, published_at, country) for existing channels.

Usage:
    python3 P:/packages/yt-is/scripts/backfill_channel_metadata.py [--batch=100]
"""

import os
import sys

# Set the new key 3 BEFORE importing the module (it caches keys on first call)
os.environ["YT_API_KEY_3"] = "AIzaSyAIpCVh8oamSk8-637T08ru0P4mNwv-VL0"

sys.path.insert(0, "P:/packages/yt-is")

# Force reload with fresh key state
import csf.source_enumerator as se
se._YOUTUBE_API_KEYS = None
se._key_state = {}

from csf.source_enumerator import get_upload_playlist_id, parse_channel_url
from csf.batch_status import upsert_channel

DB = "P:/__csf/.data/yt-is/batch_status.sqlite"

import sqlite3
conn = sqlite3.connect(DB)
c = conn.cursor()
c.execute("""
    SELECT cm.channel_url
    FROM channel_metadata cm
    WHERE (cm.description IS NULL OR cm.description = '')
    AND cm.channel_url NOT IN (SELECT channel_url FROM channel_blocklist)
    ORDER BY cm.video_count_estimate DESC
    LIMIT 100
""")
rows = c.fetchall()
conn.close()

print(f"Backfilling {len(rows)} channels...")
success = 0
fail = 0
for i, (channel_url,) in enumerate(rows):
    # parse_channel_url extracts the channel ID (@handle, UC..., c/name, user/name)
    # from a full URL or bare identifier
    channel_id = parse_channel_url(channel_url)
    if not channel_id:
        print(f"  [{i+1}/{len(rows)}] UNPARSEABLE {channel_url[:60]}")
        fail += 1
        continue

    info = get_upload_playlist_id(channel_id)
    if info and (info.description or info.topic_categories):
        upsert_channel(
            channel_url=channel_url,
            playlist_id=info.playlist_id,
            video_count_estimate=info.video_count,
            channel_title=info.channel_title,
            thumbnail_url=info.thumbnail_url,
            subscriber_count=info.subscriber_count,
            view_count=info.view_count,
            description=info.description,
            published_at=info.published_at,
            country=info.country,
            keywords=info.keywords,
            custom_url=info.custom_url,
            topic_categories=info.topic_categories,
        )
        print(f"  [{i+1}/{len(rows)}] OK {info.channel_title[:40]}")
        success += 1
    else:
        print(f"  [{i+1}/{len(rows)}] FAIL {channel_url[:60]}")
        fail += 1

print(f"\nDone: {success} ok, {fail} failed")
