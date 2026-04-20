# Debugging Playbook

Use this when working on the NotebookLM / transcript pipeline.

## Rules

- Check live stderr/stdout as well as structured JSONL traces.
- Treat the worker result file as the source of truth for completed work.
- Verify wrapper signatures before threading new kwargs through helpers.
- Smoke-test the exact code path that writes a new logging field.
- Prefer completed-worker totals and stage timings over backlog-derived throughput numbers.
- Do not assume a broad bucket like `no_captions` means the same thing as `audio_only`.
- Use sticky terminal skips for removed/private/unavailable videos.
- Keep negative-cache entries for dead/unavailable videos so they do not churn again.

## Signals That Mattered Most

- `fetch_worker_finished`
- `worker_completed`
- `worker_batch_metrics`
- `worker_source_profile_totals`
- `negative_cache_reason_counts`
- `add_cmd_elapsed_s` vs `materialization_wait_elapsed_s`
- `oEmbed` terminal failures
- `direct_api` terminal failures
- Whisper empty transcript with high `no_speech_prob`

## Common Failure Modes

- stdout summary shape drift
- wrapper argument mismatch
- stale notebook / profile state
- dead videos re-entering the expensive chain
- throughput numbers derived from backlog instead of completions

## Practical Workflow

1. Reproduce on a canary.
2. Check stderr/stdout first for warnings.
3. Check the structured trace for completed-worker events.
4. Verify the worker result file matches the summary.
5. If a new field was added, test the exact path that emits it.
6. If the run is slow, inspect stage timings before changing routing.

## Session Start

1. Read [HANDOFF.md](P:/packages/yt-is/HANDOFF.md).
2. Read [CODEX_MEMORY.md](P:/packages/yt-is/CODEX_MEMORY.md).
3. Run focused verification before restarting a canary.
4. Use the stopped-canary assumption unless you confirm a live process.
