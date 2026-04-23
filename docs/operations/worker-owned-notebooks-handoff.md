# Worker-Owned Notebooks Handoff

Last updated: 2026-04-23

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

### Recent execution notes

- DOM/browser readiness is stable again on the dedicated profile:
  - eight-URL matrix: `8/8 succeeded`
  - four-URL comparison: `4/4 succeeded`
- The free-tier batch-size check did not support lowering below `50`:
  - `nlm-subbatch-sweep --sizes 25,50 --count 300`
  - `25`-source subbatches hit the current `600s` materialization timeout on subbatch 3
- The current winner still looks like `2 workers`, but throughput varies by sample:
  - `--workers 2 --limit 400`
  - `369 succeeded / 31 failed`
  - `1472.1 successful videos/hour`
  - `--workers 2 --limit 800`
  - `770 succeeded / 30 failed`
  - `1421.5 successful videos/hour`
- The Pro NotebookLM rerun is still pending because we do not yet have a Pro profile/account wired into this workspace.

## Code state that matters

- `csf/nlm_config.py`
  - shared NotebookLM batch size default is `50`
  - shared NotebookLM source cap guard is `50`
  - NotebookLM materialization timeout is `600s`
  - NotebookLM auth policy defaults live here too
  - browser auth defaults live here too:
    - persistent Selenium profile mode
    - persistent browser profile name
- `csf/nlm_batch.py`
  - worker-owned notebook title reuse exists
  - duplicate-title cleanup uses CDP title deletion
  - reusable shutdown now uses the CDP title-delete path
- `csf/nlm_scraper.py`
  - DOM readiness tests use a persistent Selenium Chrome profile by default
  - `Request access` now fails fast as a browser-auth problem instead of silently continuing
- `bin/csf-source`
  - worker-owned notebook env vars are passed through
  - fetch uses the shared `50` batch default
- `bin/nlm-puppeteer.js`
  - CDP cleanup supports worker-title and exact-title delete modes
- `bin/nlm-playwright`
  - `--bootstrap-auth` opens NotebookLM in the dedicated automation profile and is the manual browser-login bootstrap for DOM tests
- `dev/worker_pool/worker_main.py`
  - worker startup and shutdown now use worker-owned notebook state
  - worker batch logs now carry `started_at_epoch` / `completed_at_epoch` markers
- `csf/nlm_batch.py`
  - reusable process logs and subbatch add/wait logs now carry wall-clock timestamps for overlap reconstruction

## Cleanup status

- The NotebookLM inventory cleanup pass was interrupted before I could finish removing every stale duplicate worker-title notebook.
- Before rerunning benchmarks, verify the notebook inventory is clean and make sure each worker title resolves to exactly one notebook.

## Future phase

- After the free-tier `50`-source baseline is understood, repeat the same readiness and throughput tests on a Pro NotebookLM subscription with the `300`-source notebook limit.
- Keep the same worker-owned notebook model and the same logging fields so the free-tier and Pro results can be compared directly.
- Use the readiness calibration matrix in [worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md) for the fixed eight-URL comparison set when you want DOM spinner/checkmark and CLI `source content` readiness side by side.

## Open issue to watch

- Keep checking that startup reuse resolves the existing worker notebook title and does not create a duplicate worker notebook.
- Keep the CLI and browser auth profiles aligned to the same NotebookLM account. `nlm login` covers the CLI session, but DOM tests still need the persistent browser profile bootstrapped once.

## What to read first

1. [HANDOFF.md](P:/packages/yt-is/HANDOFF.md)
2. [worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md)
3. [csf/nlm_config.py](P:/packages/yt-is/csf/nlm_config.py)
4. [bin/csf-source](P:/packages/yt-is/bin/csf-source)
5. [dev/worker_pool/worker_main.py](P:/packages/yt-is/dev/worker_pool/worker_main.py)
6. [tests/test_nlm_batch.py](P:/packages/yt-is/tests/test_nlm_batch.py)
7. [tests/test_dev_worker_pool.py](P:/packages/yt-is/tests/test_dev_worker_pool.py)

## DOM Preflight

Before any browser/DOM readiness test:

1. Run `nlm login --check` for the CLI profile.
2. Open the persistent browser profile with `python P:\packages\yt-is\bin\nlm-playwright --bootstrap-auth`.
3. Confirm NotebookLM loads without `Request access`.
4. Then run the readiness matrix.

## Suggested next run

- Reconfirm `nlm login --check -p default`.
- Bootstrap the persistent browser profile before any DOM readiness run.
- Verify the NotebookLM inventory is clean.
- Follow the three-phase plan in [worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md):
  1. phase 1: find the best shape
  2. phase 2: prove the winner is real
  3. phase 3: lock it in
- Phase 1 starts with:
  - the eight-URL readiness matrix
  - the `1, 2, 3, 4, 6, 8` worker-count sweep
  - the `25` vs `50` batch-size check only if needed
- Phase 2 starts with:
  - the repeat run of the phase 1 winner
  - the four-URL comparison pass
  - source-shape and notebook-fullness stratification
- Phase 3 starts with:
  - the larger-sample confirmation
  - the Pro notebook rerun
  - the final default/retry policy lock-in
