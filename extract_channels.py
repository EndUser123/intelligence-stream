#!/usr/bin/env python3
"""Extract YouTube channel @handles from history.jsonl and add them via csf-source add.

Usage:
    python extract_channels.py [--dry-run] [--workers N]
"""

import re, os, sys, subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

# GLM resolution: try GLM for failed handles before giving up
USE_GLM_RESOLUTION = True

# Handles and patterns to skip (obvious tests/fakes despite keyword filtering)
SKIP_HANDLES = {
    'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten',
    'ali', 'Ali',  # too short / ambiguous
    'paused',
    'new',
    'testvid',
    'testnewproblematicchannel',  # split words that slipped through
}

# Regex patterns for handles to skip (beyond keyword filtering)
SKIP_PATTERNS = [
    r'^channel\d*$',          # channel, channel1, channel2, etc.
    r'^channel-',             # channel-anything
    r'^existing$',
    r'^newschannel$',
    r'^techchannel$',
    r'^anotherchannel',
    r'^d-squared',            # d-squared70, d-squared-anything
    r'^ch\d+$',               # ch1, ch2
    r'^testch',
    r'^sample',
    r'^demo',
]

hist_path = os.path.join(os.path.expanduser('~'), '.claude', 'history.jsonl')
channels = set()

with open(hist_path, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        found = re.findall(r'youtube\.com/(@[\w\-]+)', line)
        for handle in found:
            handle_lower = handle.lower()
            # Skip test/fake/invalid
            if any(x in handle_lower for x in [
                'test', 'fake', 'nonexistent', 'doesnotexist', 'notexist',
                'invalid', 'deleted', 'private', 'problematic', 'channel',
                'example', 'handle', 'user', 'thischannel', 'somechannel',
                'badchannel', 'correctname', 'properhandle', 'https', 'youtube',
                'nonexist', 'donot', 'wrong', 'broken', 'foo', 'bar', 'baz',
                'qux', 'quux', 'asdf', 'abcd', 'temp', 'tmp',
            ]):
                continue
            if handle[1:] in SKIP_HANDLES:  # strip @ prefix
                continue
            # Check skip patterns
            stripped = handle[1:]  # strip @ prefix
            if any(re.match(p, stripped, re.IGNORECASE) for p in SKIP_PATTERNS):
                continue
            channels.add(f'https://www.youtube.com/{handle}')

# Remove already-tracked channels
import sqlite3
bconn = sqlite3.connect('P:/__csf/.data/yt-is/batch_status.sqlite')
bc = bconn.cursor()
bc.execute('SELECT channel_url FROM channel_metadata')
existing = set(row[0] for row in bc.fetchall())
bconn.close()

new_channels = sorted(channels - existing)
print(f'Clean channels found: {len(channels)}')
print(f'Already tracked: {len(existing)}')
print(f'New to add: {len(new_channels)}')
print()

dry_run = '--dry-run' in sys.argv

# Parse --workers flag
workers = 1
if '--workers' in sys.argv:
    idx = sys.argv.index('--workers')
    if idx + 1 < len(sys.argv):
        workers = int(sys.argv[idx + 1])

print(f'Using {workers} parallel workers (max 8)')
print()

def resolve_via_glm(handle: str) -> str | None:
    """Use GLM to find the correct YouTube channel URL for a failed handle.

    Args:
        handle: The failed handle (e.g., '@AndrewNg')

    Returns:
        A resolved YouTube URL (https://youtube.com/@correctHandle or
        https://youtube.com/c/CustomName) or None if GLM can't find it.
    """
    # Extract person/organization name from handle
    name = handle[1:]  # strip @
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)  # CamelCase -> words
    name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', name)  # acronym -> words
    name = name.replace('-', ' ').replace('_', ' ')

    query = f'"{name}" official YouTube channel handle or URL'

    try:
        result = subprocess.run(
            [sys.executable, '-m', 'search_research.cli', query, '--mode', 'glm',
             '--max-results', '3'],
            capture_output=True, text=True, timeout=60,
            cwd='P:/packages/search-research'
        )
        if result.returncode != 0:
            return None

        output = result.stdout + result.stderr

        # Look for YouTube URLs in output
        yt_patterns = [
            r'youtube\.com/(@[\w\-]+)',
            r'youtube\.com/c/([\w\-]+)',
            r'youtube\.com/channel/(UC[\w\-]+)',
            r'youtube\.com/user/([\w\-]+)',
        ]
        for pattern in yt_patterns:
            m = re.search(pattern, output)
            if m:
                suffix = m.group(0).split('youtube.com/')[1]
                return f'https://www.youtube.com/{suffix}'

        return None
    except Exception:
        return None


def add_channel(url, handle_forglm=None):
    """Add a single channel. Returns (url, success, error_msg, resolved_url).

    If initial add fails and handle_forglm is provided, tries GLM resolution
    and retries once with the resolved URL.
    """
    result = subprocess.run(
        [sys.executable, 'bin/csf-source', 'add', url],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        return (url, True, None, None)

    error = result.stderr.strip()

    # If GLM resolution is enabled and we have a handle, try to resolve via GLM
    if USE_GLM_RESOLUTION and handle_forglm:
        resolved = resolve_via_glm(handle_forglm)
        if resolved and resolved != url:
            # Retry with resolved URL
            result2 = subprocess.run(
                [sys.executable, 'bin/csf-source', 'add', resolved],
                capture_output=True, text=True, timeout=120
            )
            if result2.returncode == 0:
                return (url, True, None, resolved)
            # GLM also failed — report original error
            error = result2.stderr.strip() if result2.stderr.strip() else error

    return (url, False, error, None)

if dry_run:
    print('--- DRY RUN: would add ---')
    for c in new_channels:
        print(c)
else:
    added = 0
    failed = 0
    glm_resolved = 0
    results = []

    with ThreadPoolExecutor(max_workers=min(workers, 8)) as executor:
        futures = {
        executor.submit(add_channel, url, handle): url
        for url, handle in ((u, re.search(r'(@[\w\-]+)', u).group(1)) for u in new_channels)
    }
        for future in as_completed(futures):
            url, success, err, resolved = future.result()
            results.append((url, success, err, resolved))
            if success:
                if resolved:
                    print(f'OK: {url} (GLM resolved to {resolved})')
                    glm_resolved += 1
                else:
                    print(f'OK: {url}')
                added += 1
            else:
                print(f'FAILED ({err[:80]}): {url}')
                failed += 1

    print(f'\nDone: {added} added, {failed} failed, {len(new_channels) - added - failed} skipped')
    if glm_resolved:
        print(f'  (GLM resolved {glm_resolved} handle(s))')
