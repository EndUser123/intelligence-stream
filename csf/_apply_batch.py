#!/usr/bin/env python3
"""Apply all batch classification results to the database."""
import json
import sqlite3
from pathlib import Path

DB = Path("P:/__csf/.data/yt-is/batch_status.sqlite")

# Load all batches and dedupe
all_items = []
for i in range(8):
    try:
        with open(f'P:/packages/yt-is/csf/batch_{i}.json', 'r') as f:
            data = json.load(f)
        all_items.extend(data)
        print(f'Batch {i}: {len(data)} items')
    except FileNotFoundError:
        pass

print(f'Total before dedup: {len(all_items)}')

seen = {}
for item in all_items:
    url = item['url']
    if url not in seen:
        seen[url] = item
    else:
        if seen[url]['subcategory'] == 'RECLASSIFY' and item['subcategory'] != 'RECLASSIFY':
            seen[url] = item

print(f'Total after dedup: {len(seen)}')

subcategorized = {k: v for k, v in seen.items() if v['subcategory'] != 'RECLASSIFY'}
reclassify = {k: v for k, v in seen.items() if v['subcategory'] == 'RECLASSIFY'}

print(f'\nSubcategorized: {len(subcategorized)}')
print(f'RECLASSIFY: {len(reclassify)}')

# Relocations based on manual analysis of descriptions
relocations = {
    # Entertainment (sci-fi, movies,HFY)
    'https://www.youtube.com/@Anthropic': 'Entertainment',
    'https://www.youtube.com/channel/UC0-NGw63cT3R8HFC9h6t0UA': 'Entertainment',
    'https://www.youtube.com/channel/UC057bZ2YLcUaWSp_joOQEtw': 'Entertainment',
    'https://www.youtube.com/channel/UC0_k6dWofw5H8rrS5J8Ha9g': 'Entertainment',
    'https://www.youtube.com/channel/UC7AmpcfPhHDODvRdu5gU0vw': 'Entertainment',
    'https://www.youtube.com/channel/UC16ghuHlBjdV_2Fmdg3CECA': 'Entertainment',
    'https://www.youtube.com/channel/UC2YenozOQfOUayPoYSELqqg': 'Entertainment',
    'https://www.youtube.com/channel/UC-1HMfiQ3G7IcCgs4lVxa0Q': 'Entertainment',
    'https://www.youtube.com/channel/UC9XCVQR-940xwhcqAadyJ_w': 'Entertainment',
    'https://www.youtube.com/channel/UCiu9hJpuBJMCEIotUFYYV0A': 'Entertainment',
    'https://www.youtube.com/channel/UCXGEeuntD28QDKURrg8M3Aw': 'Entertainment',
    'https://www.youtube.com/channel/UC_p6dpUZ477yY7lvXz7839g': 'Entertainment',
    'https://www.youtube.com/channel/UCwIIC34Mt6oDxvCA5zQ4Jdw': 'Entertainment',
    'https://www.youtube.com/channel/UCP7R1fRqHafzKYhXh2zyxbg': 'Entertainment',
    'https://www.youtube.com/channel/UC9to9MJHFod_HCb5QVRN3uQ': 'Entertainment',
    'https://www.youtube.com/channel/UCR4if3TFE8RvtFf-HFlMu9w': 'Entertainment',
    'https://www.youtube.com/channel/UCHOmVzoDq1SdAi4vKS0AT8g': 'Entertainment',
    'https://www.youtube.com/channel/UCvV2LZZGhxzTK2pSumTArEg': 'Entertainment',
    'https://www.youtube.com/channel/UCfICtzAKg9jmhHYLhSJ5l1w': 'Entertainment',
    'https://www.youtube.com/channel/UCfdXEZUCOI9QM398VhMCh-g': 'Entertainment',
    'https://www.youtube.com/channel/UC-pSd_2_4vXqJVbDAsZbuIg': 'Entertainment',
    'https://www.youtube.com/channel/UCKsJLnRbT_J9uSkkTu1B5vw': 'Entertainment',
    'https://www.youtube.com/channel/UCZH4Q0ceiasMczUY3tTR0_A': 'Entertainment',
    'https://www.youtube.com/channel/UCp6dpUZ477yY7lvXz7839g': 'Entertainment',
    'https://www.youtube.com/channel/UCBN0GrxE7780vr5O2U5sO2w': 'Entertainment',
    'https://www.youtube.com/channel/UCnb_FL8lFw8AA9EqcoXH3eQ': 'Entertainment',
    'https://www.youtube.com/channel/UCpvELajcA27wAxe9stE9mog': 'Entertainment',
    'https://www.youtube.com/channel/UCfFDIa-yhj80tl8WQ5e2IMQ': 'Entertainment',
    'https://www.youtube.com/channel/UCI-dntNg2KMrhpgUSv1gAzQ': 'Entertainment',
    'https://www.youtube.com/channel/UC4zwmkEUOynCy0RH4SqNuqQ': 'Entertainment',
    'https://www.youtube.com/channel/UCkHvmqDMjyMzIcEWljRQRMw': 'Entertainment',
    'https://www.youtube.com/channel/UC1h5O-Fk_afTUMS8jML9tAw': 'Entertainment',
    'https://www.youtube.com/channel/UCkvZ7S_Vcp33qsEGrkvG3yw': 'Entertainment',
    'https://www.youtube.com/channel/UCnRmC8k3qzn0Qxq-nQOWmOA': 'Entertainment',
    'https://www.youtube.com/channel/UCy8LfEjAzL-5ZHgLUmeExUA': 'Entertainment',
    'https://www.youtube.com/channel/UC8k89cowowEWHRjZbHxd6SQ': 'Entertainment',
    'https://www.youtube.com/channel/UC4prFExACdE3FN_eOkTGb9w': 'Entertainment',
    'https://www.youtube.com/channel/UCKOSo2LhIatu2kkbh0s7PAA': 'Entertainment',
    'https://www.youtube.com/channel/UC-TxxtiLZrO6GTfSQc8j6SA': 'Entertainment',
    'https://www.youtube.com/channel/UCx7S9dqhjwz3NYcXTq7kAjA': 'Entertainment',
    'https://www.youtube.com/channel/UC-BxxtiLZrO6GTfSQc8j6SA': 'Entertainment',
    # HFY channels
    'https://www.youtube.com/channel/UCP7R1fRqHafzKYhXh2zyxbg': 'Entertainment',
    'https://www.youtube.com/channel/UCdIxMHBw9e95-jOb2GFzUKg': 'Entertainment',
    'https://www.youtube.com/channel/UCJgCmATDcJSdqApj6Tx1YZQ': 'Entertainment',
    'https://www.youtube.com/channel/UCSjPYBmRW5ZhkMlAlim_RBw': 'Entertainment',
    'https://www.youtube.com/channel/UC-q8I7KpSfzKC_s0C-YsO0hw': 'Entertainment',
    'https://www.youtube.com/channel/UCSTJxIqM-mIT705EIQ4PlRA': 'Entertainment',
    'https://www.youtube.com/channel/UCRFKpXFV6L_XN5Q9u0Ul4WQ': 'Entertainment',
    # News
    'https://www.youtube.com/channel/UC2X6M-GBOx8nV1X5RRMVTmw': 'News',
    'https://www.youtube.com/channel/UC-QoQKZap5aedvx38yyh1yw': 'News',
    'https://www.youtube.com/channel/UC0NSuvrwL98JSParYBI8-yQ': 'News',
    'https://www.youtube.com/channel/UC0W2f-rD8PAdn12ZiTZ2bHA': 'News',
    'https://www.youtube.com/channel/UC1uav1I7c_YHbJZa1Tc1Cnw': 'News',
    'https://www.youtube.com/channel/UC790WPHI_Sewr5LqiITAz-g': 'News',
    'https://www.youtube.com/channel/UC33G3U8TibLhYuNuMXl8Gmw': 'News',
    'https://www.youtube.com/channel/UCim-FAfKt-zb5v_CYUgpXrg': 'News',
    'https://www.youtube.com/channel/UCjexdAdXwed_wh32r_vrjFQ': 'News',
    'https://www.youtube.com/channel/UCTQs-6DP7Vu0uWwFa9KHYCA': 'News',
    'https://www.youtube.com/channel/UCa6fbbob-4N9Nq2bZjhGm6Q': 'News',
    'https://www.youtube.com/channel/UCZrXbiKCUkRNd0Dgn3sDXqw': 'News',
    'https://www.youtube.com/channel/UCMR4SQfGkEoiNfyaG_22Baw': 'News',
    'https://www.youtube.com/channel/UCEATT6H3U5lu20eKPuHVN8A': 'News',
    'https://www.youtube.com/channel/UCP37ZqE3gN9Jxl2jtnvO8eA': 'News',
    'https://www.youtube.com/channel/UCzurQNk_ywERLGBzkcDjWRA': 'News',
    'https://www.youtube.com/channel/UCzQc6wml-wAQCM4YQIHv6Cw': 'News',
    'https://www.youtube.com/channel/UCCInRC_-mCbDo0XvLArVV3Q': 'News',
    'https://www.youtube.com/channel/UCdjcwx5Kgbnfhn8FGH2sG3Q': 'News',
    'https://www.youtube.com/channel/UCCgvzAQRHmrdO7vMxb6gs_A': 'News',
    'https://www.youtube.com/channel/UCZ9fLu_8Ix8ccfQ_SgYMztQ': 'News',
    'https://www.youtube.com/channel/UCcPUHgdc31tyChkN2gvvDIA': 'News',
    'https://www.youtube.com/channel/UC-BxxtiLZrO6GTfSQc8j6SA': 'News',
    # Health
    'https://www.youtube.com/@apneareset': 'Health',
    'https://www.youtube.com/channel/UCpnQVN493fEsjJsR-6a_vzA': 'Health',
    'https://www.youtube.com/channel/UCPn4FsiQP15nudug9FDhluA': 'Health',
    'https://www.youtube.com/channel/UCNpz0ec0jWJlFO7ptYdlAJw': 'Health',
    'https://www.youtube.com/channel/UCIjuQ2TctuVARcZGnrr9tTA': 'Health',
    'https://www.youtube.com/channel/UCaaBu83ylgVnjOkUF6kzFSA': 'Health',
    'https://www.youtube.com/channel/UCCeaTmtzfjbrZdMG4jGRWUw': 'Health',
    'https://www.youtube.com/channel/UChw8okCo93q1Y37arH2Ym3g': 'Health',
    # Science
    'https://www.youtube.com/channel/UC-EjFMzCvkFXY5AJl5Zmm3g': 'Science',
    'https://www.youtube.com/channel/UC-yTuB4aJO_XKDH2h5FbBIQ': 'Science',
    'https://www.youtube.com/channel/UC114-6qnobat-sRzNsJJU_g': 'Science',
    'https://www.youtube.com/channel/UC1AtJIwlt9tI-Z-QKQJJ_Vw': 'Science',
    'https://www.youtube.com/channel/UC0RQ0xqm3WHqa5ilpUe1FAg': 'Science',
    'https://www.youtube.com/channel/UCLmZyirq7EaKhseDeTGLamw': 'Science',
    'https://www.youtube.com/channel/UCIg2taLnC9X6LRP1k3kukOA': 'Science',
    'https://www.youtube.com/channel/UCUdoEHxd9k4ssej-w0_8KtQ': 'Science',
    'https://www.youtube.com/channel/UCHl1DjBfvd1U2UWFhAe_TgQ': 'Science',
    'https://www.youtube.com/channel/UC9Ryt3XOGYBoAJVsBHNGDzA': 'Science',
    'https://www.youtube.com/channel/UCSjlTWo8-Nbo6koK8df1y5w': 'Science',
    'https://www.youtube.com/channel/UCB8HkJoXl5XNJWzA5q_iPxg': 'Science',
    'https://www.youtube.com/channel/UCKkJ6AzefsMCvvaR5YY2hxA': 'Science',
    'https://www.youtube.com/channel/UCxLrvjGBzYmj8W1rJToPasg': 'Science',
    'https://www.youtube.com/channel/UCV_aVa7t6zk4yv2tcZlLmYA': 'Science',
    'https://www.youtube.com/channel/UCtVr-DLQ4xAKpk9d7MOYPLg': 'Science',
    'https://www.youtube.com/channel/UCSrUfLbd61Tjo3_Te1Gzuvg': 'Science',
    # Business
    'https://www.youtube.com/channel/UC4SgqYQmdTCKXUoer2U-lcg': 'Business',
    'https://www.youtube.com/channel/UCBgPxTfodXMa_zavgl0DX7A': 'Business',
    'https://www.youtube.com/channel/UCPR4DV8qMQVs_YBa-CQZQBQ': 'Business',
    'https://www.youtube.com/channel/UCUb26GfZqw6S0RhNYTqd2ag': 'Business',
    # Technology
    'https://www.youtube.com/channel/UC-5at4izid5vmBmbMd0RylQ': 'Technology',
    'https://www.youtube.com/channel/UCBhTxsFP2VvjO87RsQY75Tw': 'Technology',
    'https://www.youtube.com/channel/UChk6TQce1EJMn6_liKdHDog': 'Technology',
    'https://www.youtube.com/channel/UCuudpdbKmQWq2PPzYgVCWlA': 'Technology',
    'https://www.youtube.com/channel/UCv8GBt-zqWIDfXr_JJyKQsA': 'Technology',
    'https://www.youtube.com/channel/UCTWxGlm5C9YNpkNlkAGV2-w': 'Technology',
    'https://www.youtube.com/channel/UC__d7jgOunIVFz6TU_t1QOQ': 'Technology',
    'https://www.youtube.com/channel/UC0gjVbm7HY5GzDTo5NbQruA': 'Technology',
    # Finance
    'https://www.youtube.com/channel/UCDl5YK8CD2fY267VdDHLgfg': 'Finance',
    'https://www.youtube.com/channel/UCAiadqtIuxMOBbzMHGk_aYQ': 'Finance',
    'https://www.youtube.com/channel/UCUaKVnXjE7pa4-CRgEQCJkw': 'Finance',
    # History
    'https://www.youtube.com/channel/UCQDl5IREJC6zcNIZWeRj_rg': 'History',
    # Entertainment
    'https://www.youtube.com/@tachesteaches': 'Entertainment',
    'https://www.youtube.com/@MetaAI': 'Entertainment',
    'https://www.youtube.com/channel/UCnovyyqO1Q-s-dxOs5bUEbQ': 'News',
    'https://www.youtube.com/channel/UCHsThxa9HvDpSywv4bP55NA': 'Technology',
    'https://www.youtube.com/channel/UCj2zirDn1hkPKSARbARfeQw': 'Entertainment',
    'https://www.youtube.com/channel/UCGtXqPiNV8YC0GMUzY-EUFg': 'Technology',
    'https://www.youtube.com/@TwoBitDaVinci': 'Technology',
    'https://www.youtube.com/@betterstack': 'Technology',
    'https://www.youtube.com/@SamiSabirIdrissi': 'News',
    'https://www.youtube.com/channel/UCGig3QaIoPvxNuF_rHWAFkA': 'Entertainment',
    'https://www.youtube.com/channel/UC7L9dQ59FFcwjkVwfG7c_hg': 'Entertainment',
    'https://www.youtube.com/channel/UC1U36iHJheBzQjAzM-ADtDQ': 'Entertainment',
    'https://www.youtube.com/channel/UC0iyBNula7PMx1HhCq5h-Xg': 'Entertainment',
    'https://www.youtube.com/@Nuro': 'Technology',
    'https://www.youtube.com/channel/UC7_n7OLgwOsVQzPzXnapprw': 'Technology',
}

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

# Apply relocations
reloc_applied = 0
for url, cat in relocations.items():
    if url in reclassify:
        result = conn.execute(
            "UPDATE channel_metadata SET category = ?, subcategory = NULL WHERE channel_url = ? AND category = 'AI/ML'",
            (cat, url)
        )
        if result.rowcount > 0:
            reloc_applied += 1

print(f'Relocations applied: {reloc_applied}')
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

# AI/ML base remaining
remaining = conn.execute(
    "SELECT COUNT(*) FROM channel_metadata WHERE category = 'AI/ML' AND subcategory IS NULL"
).fetchone()[0]
print(f'\nAI/ML base remaining (unclassified): {remaining}')
conn.close()