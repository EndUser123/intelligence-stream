#!/usr/bin/env python3
"""Apply hardtail batch classifications to DB."""
import json
import sqlite3
from pathlib import Path

DB = Path("P:/__csf/.data/yt-is/batch_status.sqlite")

# Load all batches
all_items = []
for i in range(4):
    with open(f'P:/packages/yt-is/csf/batch_ht_{i}.json', 'r') as f:
        data = json.load(f)
    print(f'Batch ht_{i}: {len(data)} items')
    all_items.extend(data)

print(f'Total before dedup: {len(all_items)}')

# Deduplicate - keep first (or non-RECLASSIFY over RECLASSIFY)
seen = {}
for item in all_items:
    url = item['url']
    if url not in seen:
        seen[url] = item
    elif seen[url]['subcategory'] == 'RECLASSIFY' and item['subcategory'] != 'RECLASSIFY':
        seen[url] = item

print(f'Total after dedup: {len(seen)}')

subcategorized = {k: v for k, v in seen.items() if v['subcategory'] != 'RECLASSIFY'}
reclassify = {k: v for k, v in seen.items() if v['subcategory'] == 'RECLASSIFY'}
print(f'Subcategorized: {len(subcategorized)}, RECLASSIFY: {len(reclassify)}')

# Subcategory breakdown
from collections import Counter
subcat_counts = Counter(v['subcategory'] for v in subcategorized.values())
print('\nSubcategory breakdown:')
for subcat, cnt in subcat_counts.most_common():
    print(f'  {subcat}: {cnt}')

conn = sqlite3.connect(DB)
conn.execute("PRAGMA journal_mode=WAL")

# Apply subcategorizations
subcat_applied = 0
for url, item in subcategorized.items():
    result = conn.execute(
        "UPDATE channel_metadata SET subcategory = ? WHERE channel_url = ? AND category = 'AI/ML' AND subcategory IS NULL",
        (item['subcategory'], url)
    )
    if result.rowcount > 0:
        subcat_applied += 1

print(f'\nSubcategories applied: {subcat_applied}')

# RECLASSIFY channels - analyze video titles to determine target category
# Load original hardtail export to get video_titles
with open('P:/packages/yt-is/csf/hardtail_channels.json', 'r') as f:
    hardtail = {item['url']: item for item in json.load(f)}

# Category assignments based on video title patterns
reclass_map = {
    # Entertainment
    'https://www.youtube.com/@AILabs': 'Entertainment',
    'https://www.youtube.com/@CoreyMSchafer': 'Entertainment',
    'https://www.youtube.com/channel/UC-rua8-q02V1N4p_Tb5Z1rg': 'Entertainment',
    'https://www.youtube.com/channel/UC0PikOOOC708LJUI9E1oNFA': 'Entertainment',
    'https://www.youtube.com/channel/UC0TivKVOs7FmXn-y_VOdD2g': 'Entertainment',
    'https://www.youtube.com/channel/UC0rBSUxtVScQnqWqlUDMhIA': 'Entertainment',
    'https://www.youtube.com/channel/UCJ52xpIoq5aKaIU_ZP40-nQ': 'Entertainment',
    'https://www.youtube.com/channel/UCAzc_GRmAWwjyTnF0WqZSKg': 'Entertainment',
    'https://www.youtube.com/channel/UCh8se4vTdBOwZQVOpghCSwQ': 'Entertainment',
    'https://www.youtube.com/channel/UCccbc1anupXwiU5WJphbwbjA': 'Entertainment',
    'https://www.youtube.com/channel/UCC5BwUrcMVWMwUUyKM2lYDw': 'Entertainment',
    'https://www.youtube.com/channel/UC52ln4mWFbs3hOL85_iA9Ww': 'Entertainment',
    'https://www.youtube.com/channel/UCDY_LPTz6rXZZh8duZ2PhEw': 'Entertainment',
    'https://www.youtube.com/channel/UCP0k37dIxhRAZBKEpcEMNvA': 'Entertainment',
    'https://www.youtube.com/channel/UCiue5Soilcbqp3XS2c1P1PA': 'Entertainment',
    'https://www.youtube.com/channel/UCq8I7KpSfzKC_s0C-YsO0hw': 'Entertainment',
    'https://www.youtube.com/channel/UCH36NGYZifrSYCzu7uMX6VQ': 'Entertainment',
    'https://www.youtube.com/channel/UC1FLIaKDzkU5Z0knDYYKyPQ': 'Entertainment',
    'https://www.youtube.com/channel/UCsrUfLbd61Tjo3_Te1Gzuvg': 'Science',
    'https://www.youtube.com/channel/UCXxOr489_H01t2l8V2VWt4Q': 'Entertainment',
    'https://www.youtube.com/channel/UCr9fkj2h2p5osMigIOlbLqw': 'Entertainment',
    'https://www.youtube.com/channel/UCqawm_wGiJ7I_nhtNUu88Pw': 'Entertainment',
    'https://www.youtube.com/channel/UCUJXzh7K99t_KHx9bhAGVuA': 'Entertainment',
    'https://www.youtube.com/channel/UCIYPUlFzrxpIgG2akdcyUuA': 'Entertainment',
    'https://www.youtube.com/channel/UCyY0D1A3u1Ijk8_dh0GCLfw': 'Entertainment',
    'https://www.youtube.com/channel/UCFzMGOGtqQqsT_f0WASeu9w': 'Entertainment',
    'https://www.youtube.com/channel/UCJT-Q91LPXygle4q1jet18g': 'Entertainment',
    'https://www.youtube.com/channel/UCisjCCYyc59F2B4fewKRgaA': 'Entertainment',
    'https://www.youtube.com/channel/UCIuIgTeA22fOUHrkspj7Gtg': 'Entertainment',
    'https://www.youtube.com/channel/UCLH7qUqM0PLieCVaHA7RegA': 'Entertainment',
    'https://www.youtube.com/channel/UCkPeaJ9dwHTvq6OYrsSGz7Q': 'Entertainment',
    'https://www.youtube.com/channel/UCKipICKHTWUR9nmkGFUiFgg': 'Entertainment',
    'https://www.youtube.com/channel/UCERAPMuOxhqOg6WqIDR1G2w': 'Entertainment',
    'https://www.youtube.com/channel/UCH-Hgc8gGl6782_Vs6f8XHA': 'Entertainment',
    'https://www.youtube.com/channel/UCewp6Gzsw7hI3O3tz7dgbow': 'Entertainment',
    'https://www.youtube.com/channel/UCXYIDTytNEO3NgDVwdOKjWw': 'Entertainment',
    'https://www.youtube.com/channel/UCMGKdTpKlveWcF9zeQz7apw': 'Entertainment',
    'https://www.youtube.com/channel/UCwhFvS02GwuTx32B3RVKe8A': 'Entertainment',
    'https://www.youtube.com/channel/UCc3z-orHpm4i3Ptcz_6pVMw': 'Entertainment',
    'https://www.youtube.com/channel/UComIr_SYmKrR6PbX2AezIJg': 'Entertainment',
    'https://www.youtube.com/channel/UCBBrNSvlOsSS5Z3JA4IqlBQ': 'Science',
    'https://www.youtube.com/channel/UCLtF4Z4mxVuIj6Pa-88DpQQ': 'Entertainment',
    'https://www.youtube.com/channel/UC-6xx83XBwlIBuzI7D-WIcw': 'Entertainment',
    # Science
    'https://www.youtube.com/@JeffHeaton': 'Science',
    'https://www.youtube.com/channel/UCjRXYtMRouR9xHNS_egHQaQ': 'Science',
    # News
    'https://www.youtube.com/@OpenRobotics': 'News',
    'https://www.youtube.com/channel/UCPGrgwfbkjTIgPoOh2q1BAg': 'News',
    'https://www.youtube.com/channel/UCpEwIxVpuoljoLBmcBRYUdA': 'Science',
    # Unknown - no clear pattern from video titles
    'https://www.youtube.com/@samwitteveen': 'Technology',
    'https://www.youtube.com/@CAISafety': 'Science',
    'https://www.youtube.com/channel/UCKW1UkS5szOItFnKKsWzVRQ': 'Technology',
    'https://www.youtube.com/channel/UCWD0OMrAWdSUan3Hh_q9QCA': 'Technology',
    'https://www.youtube.com/channel/UCb2YDY7emI3GSquuSf4Lzcg': 'Finance',
    'https://www.youtube.com/channel/UCc1qMq2UBJD9cSKbeBwGoZQ': 'Technology',
    'https://www.youtube.com/channel/UC9uYBfW4ef9nat4sw0svWEw': 'Finance',
    'https://www.youtube.com/channel/UC4DRUh4X4lNz22WBmkdiQQ': 'Technology',
    'https://www.youtube.com/channel/UCBX__dPYqDFqAN4QcWbnUbw': 'Technology',
    'https://www.youtube.com/channel/UC9aSfHWfEHfXsQVaAk1VE7Q': 'Finance',
}

reloc_applied = 0
unmatched = []
for url in reclassify:
    cat = reclass_map.get(url)
    if cat:
        result = conn.execute(
            "UPDATE channel_metadata SET category = ?, subcategory = NULL WHERE channel_url = ? AND category = 'AI/ML'",
            (cat, url)
        )
        if result.rowcount > 0:
            reloc_applied += 1
    else:
        unmatched.append(url)

print(f'Relocations applied: {reloc_applied}')
print(f'Unmatched (stay AI/ML): {len(unmatched)}')

conn.commit()

# Final distribution
rows = conn.execute("""
    SELECT category, subcategory, COUNT(*) as cnt
    FROM channel_metadata
    GROUP BY category, subcategory
    ORDER BY CASE WHEN category IS NULL THEN 1 ELSE 0 END, category,
             CASE WHEN subcategory IS NULL THEN 1 ELSE 0 END, subcategory
""").fetchall()

print('\n--- Final Distribution ---')
for cat, sub, cnt in rows:
    label = f"{cat} > {sub}" if sub else str(cat)
    print(f"  {label}: {cnt}")
print(f"\nTotal: {sum(r[2] for r in rows)}")

remaining = conn.execute(
    "SELECT COUNT(*) FROM channel_metadata WHERE category = 'AI/ML' AND subcategory IS NULL"
).fetchone()[0]
print(f'\nAI/ML base remaining: {remaining}')
conn.close()

# Cleanup
import os
for i in range(4):
    try:
        os.remove(f'P:/packages/yt-is/csf/batch_ht_{i}.json')
    except: pass
os.remove('P:/packages/yt-is/csf/hardtail_channels.json')
print('\nCleanup done.')
