#!/usr/bin/env python3
"""Quick test of NLM import - just 3 videos."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from csf.cache import set_cached_transcript, has_cached_transcript

NOTEBOOK_ID = "54f48773-c623-4751-be2d-1b6289ff30ac"  # yt-AI Stack Studio

def import_first_3():
    # Get sources
    result = subprocess.run(
        ["nlm", "source", "list", NOTEBOOK_ID, "--json"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return

    # Parse JSON
    stdout = result.stdout.strip()
    start = stdout.find('[')
    end = stdout.rfind(']') + 1
    json_str = stdout[start:end]
    sources = json.loads(json_str)

    print(f"Found {len(sources)} sources, importing first 3...")

    for i, source in enumerate(sources[:3], 1):
        source_id = source["id"]
        title = source["title"]
        # Use simple hash for video_id
        import hashlib
        video_id = hashlib.md5(source_id.encode()).hexdigest()[:11].upper()

        print(f"\n[{i}] {title[:50]}...")
        print(f"    Source ID: {source_id}")
        print(f"    Video ID: {video_id}")

        # Query for transcript
        prompt = f"Extract the COMPLETE FULL transcript for '{title}'. Return every single word spoken in the video from beginning to end."
        query_result = subprocess.run(
            ["nlm", "notebook", "query", NOTEBOOK_ID, prompt, "--json"],
            capture_output=True,
            text=True,
        )

        if query_result.returncode != 0:
            print(f"    ERROR: {query_result.stderr}")
            continue

        try:
            data = json.loads(query_result.stdout)
            transcript = data.get("value", {}).get("answer", "")
            if not transcript or "provided sources do not contain" in transcript.lower():
                print(f"    No transcript found")
                continue

            print(f"    Transcript length: {len(transcript)} chars")
            print(f"    Preview: {transcript[:200]}...")

            # Cache it
            set_cached_transcript(
                video_id,
                "en",
                "notebooklm",
                transcript,
                metadata={
                    "notebook_id": NOTEBOOK_ID,
                    "source_id": source_id,
                    "source_title": title,
                    "importer": "csf.test_nlm_import",
                },
            )
            print(f"    ✓ Cached to database")

        except Exception as e:
            print(f"    ERROR: {e}")

if __name__ == "__main__":
    import_first_3()
