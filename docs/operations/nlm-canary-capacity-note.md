# NotebookLM Canary Capacity Note

**Date:** 2026-04-20  
**Status:** Active operational note

## What the canary showed

The current yt-is backlog canary is hitting a NotebookLM add failure in the reusable industrial path.

Observed pattern:

- `nlm_batch_subbatch_add_started`
- about `123-124s` later `nlm_batch_subbatch_add_completed`
- `returncode: 1`
- `failure_reason: add_failed`

This happened with both `300`-source and `150`-source notebook add windows.

NotebookLM's documented notebook cap is 300 sources, so the live question is how close we can safely get before rotating to a fresh notebook.

The failure is in the NotebookLM add boundary, not in routing, Selenium scraping, or transcript fallback.

The current shared code constants are:

- `DEFAULT_NOTEBOOKLM_BATCH_SIZE = 200`
- `DEFAULT_NOTEBOOKLM_SOURCE_CAP = 225`

## What this means

- The live bottleneck is NotebookLM notebook reuse / add capacity, not the browser automation ADR ideas.
- The earlier browser-automation ADR is now stale for this issue and should not be used as the next action guide.
- The reusable notebook path needs a capacity guard, not a larger add window.

## Practical guidance

1. Keep the NotebookLM add window below the size that triggers `source_add_failed` in live canaries.
2. Rotate to a fresh notebook before the reusable notebook approaches the shared cap constant in `csf/nlm_batch.py`.
3. Log notebook id and current source count before each add attempt so failures can be correlated with notebook fullness.
4. Keep using completed-worker totals and transcript-cache growth as throughput truth.

## What not to do

- Do not increase the add window again until notebook-capacity behavior is understood.
- Do not use the stale browser-automation ADR as the primary reference for this canary failure.
- Do not treat backlog-derived scan rates as throughput.

## Evidence to consult

- [HANDOFF.md](P:/packages/yt-is/HANDOFF.md)
- [DEBUGGING_PLAYBOOK.md](P:/packages/yt-is/DEBUGGING_PLAYBOOK.md)
- [csf/nlm_batch.py](P:/packages/yt-is/csf/nlm_batch.py)
- [term_eba27297.jsonl](P:/packages/yt-is/.logs/term_eba27297.jsonl)
