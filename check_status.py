#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect('P:\\.data/yt-is/batch_status/batch_status.sqlite')
cursor = conn.execute('SELECT status, COUNT(*) FROM analysis_status GROUP BY status')
print('Analysis status counts:')
for row in cursor.fetchall():
    print(f'  {row[0]}: {row[1]}')

cursor = conn.execute('SELECT COUNT(*) FROM analysis_status')
total = cursor.fetchone()[0]
print(f'Total: {total} videos tracked')
conn.close()

