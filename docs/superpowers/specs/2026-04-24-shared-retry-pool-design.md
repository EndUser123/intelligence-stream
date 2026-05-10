# Shared Retry Pool for NotebookLM Fetch Recovery

## Problem

The current NotebookLM throughput path now separates:

- the main worker batch that adds sources and performs the first `source content` fetch
- the retry queue that re-attempts `yt-dlp = ok` failures after a waiting window

That retry queue currently runs inside the same worker and the same reusable notebook. This keeps the implementation simple, but it also means the worker is idle during the wait window unless it has other main-batch work available.

The open question is whether overall successful-video throughput improves if retryable `yt-dlp = ok` items are drained by a **shared retry pool** instead of being retried in place by the original worker.

## Goals

1. Compare the current same-worker retry queue against a shared retry pool on the same sample shape.
2. Measure whether a shared retry pool improves successful videos/hour without increasing false failures.
3. Keep terminal classifications out of the retry path:
   - `not_yet_live`
   - `removed_by_owner`
   - `private`
   - other clearly terminal yt-dlp classifications
4. Keep the main batch architecture unchanged during the experiment:
   - same reusable worker-owned notebook model
   - same notebook setup/add path
   - same NotebookLM browser/auth state
5. Preserve the current retry heuristics as the baseline so the experiment is apples-to-apples.

## Non-goals

- Spinning up a second notebook per worker as the primary solution.
- Changing the notebook batch size.
- Reworking NotebookLM browser authentication.
- Changing yt-dlp classification rules.
- Changing the terminal failure buckets.
- Replacing NotebookLM extraction with yt-dlp fallback for the main success path.

## Current Baseline

The current retry behavior is:

1. The worker adds a batch of sources to its reusable notebook.
2. The worker attempts NotebookLM `source content`.
3. If the first attempt fails and the URL looks retryable, the code consults yt-dlp classification.
4. If yt-dlp says the item is terminal, the item stops.
5. If yt-dlp says `ok`, the item is deferred into a retry queue.
6. The same worker waits for the retry queue window and retries those items.

This baseline is already implemented in:

- `P:\\\\\\packages/yt-is/csf/nlm_batch.py`
- `P:\\\\\\packages/yt-is/csf/nlm_config.py`
- `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`

## Proposed Experiment

Run a side-by-side comparison between:

### Option A: Same-worker retry queue

The current design:

- deferred `yt-dlp = ok` items remain associated with the worker that found them
- the original worker waits for the retry window and then retries them
- no shared coordination layer is needed

### Option B: Shared retry pool

Deferred `yt-dlp = ok` items are placed into a shared retry pool that any worker can drain when it becomes available.

This pool is not a separate notebook per worker. It is a coordination layer over the same reusable notebook model.

## Recommended Design

Start with **Option B: shared retry pool**, but keep notebook ownership unchanged.

The main worker path should still:

1. own source add
2. own the first NotebookLM fetch attempt
3. classify terminal vs retryable outcomes

The shared retry pool should only own:

1. retryable `yt-dlp = ok` items
2. a bounded retry delay / budget
3. final recovery or final failure after the retry window expires

This is the smallest design that answers the throughput question without introducing A/B notebook proliferation.

## Why Not A/B Notebooks Per Worker

Creating a second notebook per worker would make the architecture more complex before we know whether the retry wait is actually the limiting factor.

The current traces show that notebook setup/add work is much more expensive than the retry wait itself, so the first optimization should be about queue placement, not notebook count.

## Shared Retry Pool Shape

The shared retry pool should be a cross-worker queue keyed by:

- video ID
- NotebookLM source ID
- retry eligibility state
- retry deadline / budget
- first-failure metadata

The queue should only accept items that:

- passed yt-dlp classification as `ok`
- failed NotebookLM fetch in a retryable way
- have not already exhausted their retry budget

Terminal items should never enter the pool.

## Data Flow

### Baseline path

1. Worker adds sources to a reusable notebook.
2. Worker performs first NotebookLM fetch.
3. Worker classifies the result.
4. Terminal items stop.
5. Retryable `yt-dlp = ok` items are deferred.
6. Retry queue runs inside the same worker.

### Shared retry pool path

1. Worker adds sources to a reusable notebook.
2. Worker performs first NotebookLM fetch.
3. Worker classifies the result.
4. Terminal items stop.
5. Retryable `yt-dlp = ok` items are pushed into the shared retry pool.
6. Any worker that has spare capacity can claim the retry item.
7. The claiming worker retries NotebookLM fetch within the shared retry budget.
8. Recovery or final failure is recorded centrally.

## Coordination Model

The shared retry pool needs a durable coordination primitive so that multiple workers do not duplicate retry work.

Acceptable approaches:

### 1. Shared JSONL queue

- Simple to inspect.
- Easy to append and consume.
- Needs careful claim semantics to avoid duplicate processing.

### 2. Shared SQLite queue

- Stronger claim/update semantics.
- Easier to record attempt counts, retry deadlines, and final status.
- Slightly more implementation work, but better for a multi-worker queue.

### 3. In-memory coordinator

- Simpler conceptually.
- Not suitable if workers are separate processes or if the run needs restart resilience.

Recommended: **shared SQLite queue** if the pool is implemented for real; **shared JSONL** is acceptable only for a lightweight experiment.

## Error Handling

- If a retry item cannot be claimed, do not duplicate the retry in another worker.
- If the retry pool is unavailable, fall back to the current same-worker queue behavior rather than dropping items.
- If a claim or update fails mid-run, the item should remain recoverable rather than silently lost.
- Terminal yt-dlp classifications must continue to short-circuit immediately.

## Testing

Add coverage for:

- same-worker retry queue remains the baseline
- shared retry pool can claim exactly one worker at a time
- terminal yt-dlp classifications never enter the shared pool
- retryable `yt-dlp = ok` items can move from main queue to shared pool and back to final recovery/failure
- queue metrics report:
  - deferred count
  - recovered count
  - final failed count
  - claim count
  - duplicate-claim prevention

Recommended validation:

1. Run the baseline same-worker retry flow on the same 400-item sample.
2. Run the shared retry pool on the same 400-item sample.
3. Compare:
   - successful videos/hour
   - `yt-dlp = ok` recovery rate
   - total failed count
   - average latency to final outcome
   - notebook setup overhead

## Success Criteria

The shared retry pool is worth keeping only if it:

- improves successful videos/hour or holds throughput while recovering more `yt-dlp = ok` items
- does not increase terminal bucket counts
- does not introduce duplicate retry claims
- does not require a second notebook per worker

## Decision Rule

Use the shared retry pool only if the data shows it is actually hiding useful wait time.

If the shared pool does not improve throughput meaningfully, keep the same-worker retry queue and continue tuning only the retry window and classification rules.

