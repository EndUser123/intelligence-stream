# Worker-Owned Notebooks Handoff

Last updated: 2026-04-22

## Purpose

This note summarizes the current NotebookLM worker-owned-notebook work so another LLM can continue without re-deriving the same decisions.

## Current model

- One worker owns one notebook title.
- The worker notebook titles are deterministic per worker slot.
- Each worker reuses its own notebook across batches.
- The worker-owned notebook path applies to all NotebookLM touch points in `yt-is`, not just the dev worker harness.
- The benchmark axis is worker count, and the sweep still goes up to `8` workers.

## What was proved

### Auth startup

- `nlm login --check -p default` is valid after re-authentication.
- The current default NotebookLM profile is usable again.

### Throughput baseline

The best current throughput evidence is from the worker-count sweep on the same sample family.

Low-load / higher-load pair on 400 items:

- `1 worker`
  - `389 succeeded / 11 failed`
  - `835.7s`
  - about `1,676 successful videos/hour`
- `2 workers`
  - `389 succeeded / 11 failed`
  - `497.2s`
  - about `2,817 successful videos/hour`

Higher worker-count sweep on 1200 items:

- `3 workers`
  - `1082 succeeded / 118 failed`
  - `1655.8s`
  - about `2,354 successful videos/hour`
- `4 workers`
  - `1169 succeeded / 31 failed`
  - `1318.1s`
  - about `3,193 successful videos/hour`
- `5 workers`
  - `1166 succeeded / 34 failed`
  - `805.2s`
  - about `5,215 successful videos/hour`
- `6 workers`
  - `982 succeeded / 218 failed`
  - `627.7s`
  - about `5,635 successful videos/hour`

Planned extension of the worker sweep:

- `7 workers`
- `8 workers`

Observed conclusion:

- `6 workers` produced the highest observed throughput.
- `5 workers` looked like the better balance of throughput and failure rate.
- The next throughput sweep should continue through `7` and `8` workers before we decide whether the knee has moved.
- The low-load / higher-load pair is best described as concurrent NotebookLM pressure from 1 vs 2 workers, not a strict serialized handoff.

## Code state that matters

- `csf/nlm_batch.py`
  - shared NotebookLM batch size default is `200`
  - shared NotebookLM source cap guard is `225`
  - worker-owned notebook title reuse exists
  - duplicate-title cleanup uses CDP title deletion
  - reusable shutdown now uses the CDP title-delete path
- `bin/csf-source`
  - worker-owned notebook env vars are passed through
  - fetch uses the shared `200` batch default
- `bin/nlm-puppeteer.js`
  - CDP cleanup supports worker-title and exact-title delete modes
- `dev/worker_pool/worker_main.py`
  - worker startup and shutdown now use worker-owned notebook state
  - worker batch logs now carry `started_at_epoch` / `completed_at_epoch` markers
- `csf/nlm_batch.py`
  - reusable process logs and subbatch add/wait logs now carry wall-clock timestamps for overlap reconstruction

## Cleanup status

- The NotebookLM inventory cleanup pass was interrupted before I could finish removing every stale duplicate worker-title notebook.
- Before rerunning benchmarks, verify the notebook inventory is clean and make sure each worker title resolves to exactly one notebook.

## Open issue to watch

- Keep checking that startup reuse resolves the existing worker notebook title and does not create a duplicate worker notebook.

## What to read first

1. [HANDOFF.md](P:/packages/yt-is/HANDOFF.md)
2. [worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md)
3. [csf/nlm_batch.py](P:/packages/yt-is/csf/nlm_batch.py)
4. [bin/csf-source](P:/packages/yt-is/bin/csf-source)
5. [dev/worker_pool/worker_main.py](P:/packages/yt-is/dev/worker_pool/worker_main.py)
6. [tests/test_nlm_batch.py](P:/packages/yt-is/tests/test_nlm_batch.py)
7. [tests/test_dev_worker_pool.py](P:/packages/yt-is/tests/test_dev_worker_pool.py)

## Suggested next run

- Reconfirm `nlm login --check -p default`.
- Verify the NotebookLM inventory is clean.
- Run one worker-owned notebook test first.
- Then rerun the worker-count benchmark with the same sample size and batch default, extending the sweep through `8` workers using [csf-worker-count-sweep](P:/packages/yt-is/bin/csf-worker-count-sweep).
- After the sweep, use [worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md) for the replicate, staggered-load, stratified-workload, readiness, fullness, and warm-vs-cold passes.
