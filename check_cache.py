#!/usr/bin/env python3
import sqlite3

db_path = 'P:/.data/yt-is/transcripts/transcripts.sqlite'
conn = sqlite3.connect(db_path)

# FIX: Ensure tables exist before querying (prevents empty database bug)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("""
    CREATE TABLE IF NOT EXISTS transcript_cache (
        cache_key TEXT PRIMARY KEY,
        video_id TEXT NOT NULL,
        lang TEXT NOT NULL,
        source TEXT NOT NULL,
        transcript TEXT NOT NULL,
        cached_at TEXT NOT NULL,
        terminal_id TEXT NOT NULL
    )
""")

cursor = conn.execute('SELECT COUNT(DISTINCT video_id) FROM transcript_cache')
print(f'Transcripts cached: {cursor.fetchone()[0]}')

cursor = conn.execute('SELECT video_id, lang, source, cached_at FROM transcript_cache ORDER BY cached_at DESC')
print('Transcript entries:')
for row in cursor.fetchall():
    print(f'  {row[0]}: lang={row[1]}, source={row[2]}, cached_at={row[3]}')
conn.close()

