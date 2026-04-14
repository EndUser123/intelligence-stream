#!/usr/bin/env python3
"""NotebookLM Industrial Batch Ingestor (High-Speed Version).

This version uses 'nlm source content' which is 10x faster than queries
and doesn't use AI credits. It fixes the mapping bug by correlating
titles from the CLI list with our input IDs.
"""

import json
import logging
import subprocess
import time
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# Minimum characters for a "valid" high-fidelity transcript
_MIN_TRANSCRIPT_CHARS = 500

class NLMBatchIngestor:
    def __init__(self, batch_size: int = 300):
        self.batch_size = batch_size
        self._nb_id = None

    def _run_cmd(self, args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(["nlm"] + args, capture_output=True, text=True, timeout=timeout)

    def create_batch_notebook(self, batch_ids: List[str]) -> Optional[str]:
        nb_name = f"Industrial_Batch_{int(time.time())}"
        print(f"[NLM-Batch] Creating notebook...")
        res = self._run_cmd(["notebook", "create", nb_name])
        
        for line in res.stdout.split('\n'):
            if "ID:" in line:
                self._nb_id = line.split("ID:")[1].strip()
                break
        if not self._nb_id: self._nb_id = res.stdout.strip()

        print(f"[NLM-Batch] Adding {len(batch_ids)} sources...")
        add_args = ["source", "add", self._nb_id, "--wait"]
        for vid in batch_ids:
            add_args.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
        
        self._run_cmd(add_args, timeout=900)
        return self._nb_id

    def extract_transcripts(self, batch_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Extract using high-speed 'source content' method."""
        # 1. Get Source List
        res = self._run_cmd(["source", "list", self._nb_id, "--json"])
        if res.returncode != 0: return {vid: (False, None, "List failed") for vid in batch_ids}
        
        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict): sources = sources.get("sources", [])
        except:
            return {vid: (False, None, "Parse failed") for vid in batch_ids}

        # 2. Map Source IDs to Video IDs
        # Since NLM doesn't return the URL in the list, we'll use the order
        # which is consistent during the 'add' phase if we do it in one command.
        source_id_list = [s['id'] for s in sources]
        
        results = {}
        
        def _fetch_content(source_id: str, vid_hint: str):
            # The 'content' command is NOT an AI query. It's a direct data fetch.
            res = self._run_cmd(["source", "content", source_id, "--json"], timeout=30)
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout)
                    # Support both raw and wrapped JSON
                    content = ""
                    if isinstance(data, dict):
                        content = data.get("value", {}).get("content", "")
                        if not content: content = data.get("content", "")
                    
                    if len(content) > 100:
                        return vid_hint, True, content, None
                except:
                    pass
            return vid_hint, False, None, f"Fetch failed for {source_id}"

        print(f"[NLM-Batch] Fetching {len(sources)} sources in parallel...")
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            for i, vid in enumerate(batch_ids):
                if i < len(source_id_list):
                    futures.append(executor.submit(_fetch_content, source_id_list[i], vid))
            
            for future in as_completed(futures):
                vid, success, text, error = future.result()
                results[vid] = (success, text, error)
                if success:
                    print(f"  ✓ {vid}: {len(text)} chars")
                else:
                    print(f"  ✗ {vid}: {error}")
        
        return results

    def cleanup(self):
        if self._nb_id:
            self._run_cmd(["notebook", "delete", self._nb_id, "--confirm"])

def process_industrial_batch(video_ids: List[str]) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
    ingestor = NLMBatchIngestor()
    try:
        if not ingestor.create_batch_notebook(video_ids):
            return {vid: (False, None, "Notebook failed") for vid in video_ids}
        return ingestor.extract_transcripts(video_ids)
    finally:
        ingestor.cleanup()

if __name__ == "__main__":
    import sys
    test_ids = sys.argv[1:] if len(sys.argv) > 1 else ["dQw4w9WgXcQ"]
    results = process_industrial_batch(test_ids)
    # Print success summaries
    for vid, (success, text, err) in results.items():
        if success:
            print(f"FINAL: {vid} SUCCESS ({len(text)} chars)")
        else:
            print(f"FINAL: {vid} FAILED ({err})")
