# NotebookLM Canary Capacity Note

**Date:** 2026-04-20  
**Status:** Active operational note

## What the worker run showed

The current yt-is worker run is hitting a NotebookLM add failure in the worker-owned path.

Observed pattern:

- `nlm_batch_subbatch_add_started`
- about `123-124s` later `nlm_batch_subbatch_add_completed`
- `returncode: 1`
- `failure_reason: add_failed`

This happened with both `300`-source and `150`-source notebook add windows.

The live question is how close we can safely get while keeping one notebook per worker title and reusing it across batches.

The failure is in the NotebookLM add boundary, not in routing, Selenium scraping, or transcript fallback.

The current shared NotebookLM config lives in `csf/nlm_config.py` and currently resolves to:

- `notebook_batch_size = 50`
- `notebook_source_cap = 50`
- `notebook_source_materialization_timeout_s = 600`

## What this means

- The live bottleneck is NotebookLM worker-notebook reuse / add capacity, not the browser automation ADR ideas.
- The earlier browser-automation ADR is now stale for this issue and should not be used as the next action guide.
- The worker-owned notebook path needs a capacity guard, not a larger add window.

## Practical guidance

1. Keep the NotebookLM add window at the free-tier `50`.
2. Keep one notebook per worker title and clear sources between batches.
3. Log notebook id and current source count before each add attempt so failures can be correlated with notebook fullness and readiness delay.
4. Keep using completed-worker totals and transcript-cache growth as throughput truth.

## What not to do

- Do not increase the add window again until notebook-capacity behavior is understood.
- Do not switch the steady state back to a fresh-notebook rotation model.
- Do not use the stale browser-automation ADR as the primary reference for this worker-run failure.
- Do not treat backlog-derived scan rates as throughput.

## Evidence to consult

- [HANDOFF.md](P:\\\\\\packages/yt-is/HANDOFF.md)
- [DEBUGGING_PLAYBOOK.md](P:\\\\\\packages/yt-is/DEBUGGING_PLAYBOOK.md)
- [csf/nlm_batch.py](P:\\\\\\packages/yt-is/csf/nlm_batch.py)
- [term_eba27297.jsonl](P:\\\\\\packages/yt-is/.logs/term_eba27297.jsonl)
