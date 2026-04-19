#!/usr/bin/env python3
"""NotebookLM add-acceptance sweep for experiment runs.

Usage:
    python dev/nlm_batch_size_sweep.py --sizes 50,25,10,5,1 VID1 VID2 ...

This is a disposable experiment path. It measures how many sources NotebookLM
accepts at each requested batch size without changing the live fetch routing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from csf.nlm_batch import NLMBatchIngestor  # noqa: E402


def _parse_sizes(raw: str) -> list[int]:
    sizes: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        sizes.append(int(part))
    if not sizes:
        raise argparse.ArgumentTypeError("sizes must contain at least one positive integer")
    return sizes


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure NotebookLM add acceptance across batch sizes.")
    parser.add_argument("video_ids", nargs="+", help="Video IDs to sweep")
    parser.add_argument("--sizes", default="50,25,10,5,1", type=_parse_sizes, help="Comma-separated batch sizes to test")
    parser.add_argument("--title", default=None, help="Optional notebook title to use for the sweep")
    args = parser.parse_args()

    notebooklm_profile = os.environ.get("NOTEBOOKLM_PROFILE", "").strip()
    if not notebooklm_profile:
        notebooklm_profile = f"ytis-sweep-{os.getpid()}"
        os.environ["NOTEBOOKLM_PROFILE"] = notebooklm_profile

    ingestor = NLMBatchIngestor()
    results = ingestor.experiment_add_acceptance(
        args.video_ids,
        args.sizes,
        notebook_title=args.title,
    )
    print(json.dumps({"sizes": args.sizes, "results": results, "notebooklm_profile": notebooklm_profile}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
