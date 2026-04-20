# Codex Memory Notes

Last updated: 2026-04-20

## Things to remember for future work

- When threading new keyword arguments through `csf.batch_status` wrappers, verify the callee signature first and add a focused regression test.
- The `mark_failed(...)` path must preserve `source` attribution; terminal failures need to be persisted correctly so dead/unavailable videos stop churning.
- Live warnings in stderr/stdout can reveal bugs that structured JSONL trace tails do not show immediately.
- `no_captions` is not the same thing as `audio_only`; treat it as a routing hint, not a hard truth.
- Terminal/unavailable transcript outcomes should be sticky and should feed the negative cache so the same dead videos do not keep re-entering the expensive fallback chain.

## Verified bug we hit

- `mark_failed()` in `P:/packages/yt-is/csf/batch_status.py` originally did not accept `source`.
- `P:/packages/yt-is/csf/transcript.py` called `_mark_failed_video(video_id, source=source, failure_reason="unavailable")`.
- That mismatch caused live warnings and prevented terminal failures from being recorded correctly until the wrapper was fixed.
# Debugging and logging reminders

- See [DEBUGGING_PLAYBOOK.md](P:/packages/yt-is/DEBUGGING_PLAYBOOK.md) for the compact reusable version.
- Do not trust the JSONL trace alone; check live stderr/stdout too.
- Verify wrapper signatures when threading new kwargs through a helper.
- Treat the worker result file as the source of truth for completed work.
- Prefer completed-worker totals and stage timings over backlog-derived throughput numbers.
- The most useful live signals were:
  - `fetch_worker_finished`
  - `worker_completed`
  - `worker_batch_metrics`
  - `worker_source_profile_totals`
  - `negative_cache_reason_counts`
  - `add_cmd_elapsed_s` vs `materialization_wait_elapsed_s`
- Smoke-test the exact path that emits new logging fields so wrapper/API mismatches show up immediately.
