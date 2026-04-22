"""Parallel batch dispatcher for isolated NotebookLM workers.

This dev-only tool fans out batches to subprocess workers. Each worker gets a
distinct reusable notebook state path and notebook title via env vars so we can
test notebook isolation without changing the production fetch path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _load_batches(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Input must be a JSON list of batches")
    batches: list[list[str]] = []
    for batch in data:
        if not isinstance(batch, list):
            raise ValueError("Each batch must be a JSON list of video IDs")
        cleaned = [str(item).strip() for item in batch if str(item).strip()]
        if cleaned:
            batches.append(cleaned)
    return batches


def _write_temp_batch(batch: list[str], workdir: Path, index: int) -> Path:
    batch_path = workdir / f"worker-batch-{index:03d}.json"
    batch_path.write_text(json.dumps(batch, indent=2), encoding="utf-8")
    return batch_path


def _group_batches_for_workers(batches: list[list[str]], workers: int) -> list[list[list[str]]]:
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if not batches:
        return []
    worker_count = min(workers, len(batches))
    grouped: list[list[list[str]]] = [[] for _ in range(worker_count)]
    for index, batch in enumerate(batches):
        grouped[index % worker_count].append(batch)
    return grouped


def _run_worker(
    batch_path: Path,
    worker_id: int,
    state_root: Path,
    notebook_prefix: str,
    run_id: str,
) -> subprocess.CompletedProcess[str]:
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / f"worker-{worker_id:02d}.json"
    notebook_title = f"{notebook_prefix}::worker-{worker_id:02d}"
    notebooklm_profile = f"ytis-worker-{worker_id:02d}"
    env = os.environ.copy()
    env["YTIS_NLM_REUSABLE_STATE_PATH"] = str(state_path)
    env["YTIS_NLM_REUSABLE_NOTEBOOK_TITLE"] = notebook_title
    env["YTIS_NLM_OWNER_STATE_PATH"] = str(state_path)
    env["YTIS_NLM_OWNER_NOTEBOOK_TITLE"] = notebook_title
    env["YTIS_INDUSTRIAL_RUN_ID"] = run_id
    env["NOTEBOOKLM_PROFILE"] = notebooklm_profile
    cmd = [
        sys.executable,
        "-m",
        "dev.worker_pool.worker_main",
        "--input",
        str(batch_path),
        "--state-path",
        str(state_path),
        "--notebook-title",
        notebook_title,
        "--notebooklm-profile",
        notebooklm_profile,
        "--worker-id",
        f"worker-{worker_id:02d}",
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch isolated NotebookLM worker batches in parallel.")
    parser.add_argument("--input", required=True, type=Path, help="JSON list of batches; each batch is a list of video IDs")
    parser.add_argument("--workers", type=int, default=2, help="Maximum parallel workers")
    parser.add_argument("--state-root", type=Path, default=Path("P:/__csf/.data/yt-is/dev-workers"))
    parser.add_argument("--notebook-prefix", default="yt-is::industrial::dev")
    args = parser.parse_args(argv)

    batches = _load_batches(args.input)
    if not batches:
        print("No batches found.")
        return 1

    run_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="ytis-workerpool-") as td:
        workdir = Path(td)
        grouped_batches = _group_batches_for_workers(batches, args.workers)
        batch_files = [_write_temp_batch(batch_group, workdir, idx) for idx, batch_group in enumerate(grouped_batches, 1)]
        results: list[tuple[int, subprocess.CompletedProcess[str]]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(batch_files)))) as executor:
            futures = {
                executor.submit(_run_worker, batch_file, idx, args.state_root, args.notebook_prefix, run_id): idx
                for idx, batch_file in enumerate(batch_files, 1)
            }
            for future in as_completed(futures):
                batch_index = futures[future]
                results.append((batch_index, future.result()))

        results.sort(key=lambda item: item[0])
        for batch_index, proc in results:
            print(f"=== batch {batch_index:03d} rc={proc.returncode} ===")
            if proc.stdout:
                print(proc.stdout.rstrip())
            if proc.stderr:
                print(proc.stderr.rstrip(), file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
