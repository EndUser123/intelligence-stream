#!/usr/bin/env python3
"""Normalize channel URLs in batch_status.sqlite.

Removes /channel/ prefix from @handle URLs for consistency.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("P:\\.data/yt-is/batch_status/batch_status.sqlite")

print(f"Migrating: {DB_PATH}")


def normalize_url(url: str) -> str:
    """Remove /channel/ prefix from @handle URLs."""
    if url and "/channel/@" in url:
        return url.replace("/channel/@", "/@")
    return url


conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA journal_mode=WAL")

# Normalize channel_metadata
print("\n1. Normalizing channel_metadata...")
cursor = conn.execute("SELECT channel_url FROM channel_metadata")
rows = cursor.fetchall()

updates = 0
for (url,) in rows:
    normalized = normalize_url(url)
    if normalized != url:
        conn.execute(
            "UPDATE channel_metadata SET channel_url = ? WHERE channel_url = ?",
            (normalized, url)
        )
        updates += 1
        print(f"  {url} -> {normalized}")

conn.commit()
print(f"  Updated {updates} rows in channel_metadata")

# Normalize analysis_status source column
print("\n2. Normalizing analysis_status...")
cursor = conn.execute("SELECT DISTINCT source FROM analysis_status WHERE source IS NOT NULL")
rows = cursor.fetchall()

updates = 0
for (url,) in rows:
    normalized = normalize_url(url)
    if normalized != url:
        conn.execute(
            "UPDATE analysis_status SET source = ? WHERE source = ?",
            (normalized, url)
        )
        updates += 1
        print(f"  {url} -> {normalized}")

conn.commit()
print(f"  Updated {updates} distinct sources in analysis_status")

# Verify normalization
print("\n3. Verification:")
cursor = conn.execute("SELECT channel_url FROM channel_metadata LIMIT 10")
print("  channel_metadata sample:")
for (url,) in cursor.fetchall():
    print(f"    {url}")

cursor = conn.execute("SELECT DISTINCT source FROM analysis_status WHERE source IS NOT NULL LIMIT 10")
print("  analysis_status sources sample:")
for (url,) in cursor.fetchall():
    print(f"    {url}")

conn.close()
print("\nMigration complete!")

