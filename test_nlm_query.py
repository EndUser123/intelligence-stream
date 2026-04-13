#!/usr/bin/env python3
import subprocess
import json

# Get yt-Chase AI notebook ID
result = subprocess.run(['nlm', 'notebook', 'list', '--json'], capture_output=True, text=True)
notebooks = json.loads(result.stdout)
yt_chase = [n for n in notebooks if n['title'] == 'yt-Chase AI'][0]
print(f'Notebook ID: {yt_chase["id"]}')

# List sources
result = subprocess.run(['nlm', 'source', 'list', yt_chase['id'], '--json'], capture_output=True, text=True)
sources = json.loads(result.stdout)
print(f'Total sources: {len(sources)}')

# Test query on first source
if sources:
    first_source = sources[0]
    print(f'\nTesting query on: {first_source["title"][:50]}...')
    prompt = f'Extract COMPLETE FULL transcript for "{first_source["title"]}" from this video. Return ONLY the transcript text, no summaries, no analysis, no introductions. Include ALL spoken words including repetitions, fillers, and hesitations. If there is no transcript available, respond with exactly: NO_TRANSCRIPT_AVAILABLE'

    result = subprocess.run(['nlm', 'notebook', 'query', yt_chase['id'], prompt], capture_output=True, text=True, timeout=120)
    print(f'Exit code: {result.returncode}')
    print(f'Stdout length: {len(result.stdout)}')
    print(f'Stderr: {result.stderr[:500] if result.stderr else "None"}')
    print(f'Output preview: {result.stdout[:500] if result.stdout else "Empty"}')
