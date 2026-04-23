# yt-is Handoff

Last updated: 2026-04-20

## Current state

- The current worker run is stopped.
- The current worker-owned notebook status and throughput conclusions are summarized in [docs/operations/worker-owned-notebooks-handoff.md](P:/packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md).
- The benchmark run sheet is [docs/operations/worker-count-trial-run-sheet.md](P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md).
- The routing split was changed so:
  - terminal/unavailable/private/deleted items stay sticky-skipped
  - live / live_stream / premiere items go to `transcript_fallback`
  - captioned and `no_captions` items go back to `notebooklm`
- We added a durable note at `P:/packages/yt-is/CODEX_MEMORY.md` and linked it from `README.md`.
- Whisper empty-output messages now say when the model thinks the audio was likely music or silence.

## Why this matters

- The previous worker run had been sending the broad `no_captions` backlog into the slow transcript-fallback lane.
- That was the throughput killer.
- The current split is intended to push the large recoverable backlog back into NotebookLM while keeping live content out of that lane.

## Files that matter

- `P:/packages/yt-is/csf/nlm_config.py`
  - NotebookLM batch size, source cap, materialization timeout, and auth policy defaults
- `P:/packages/yt-is/csf/nlm_batch.py`
  - worker-owned notebook rotation and source-add subbatch sizing
- `P:/packages/yt-is/bin/csf-source`
  - preflight routing split
  - logging for fallback / NotebookLM counts
  - worker-run orchestration
- `P:/packages/yt-is/csf/transcript.py`
  - oEmbed probe
  - direct_api classification
  - Whisper empty-result classification
  - negative-cache persistence
- `P:/packages/yt-is/csf/batch_status.py`
  - transcript cache / negative cache / status persistence
  - `mark_failed(..., source=...)` fix
- `P:/packages/yt-is/tests/test_csf_source_fetch_timing.py`
  - routing regression tests
- `P:/packages/yt-is/tests/test_transcript.py`
  - direct_api and Whisper regression tests

## What we learned

- NotebookLM industrial batches were much faster than the fallback lane when healthy.
- The fallback lane is mostly Selenium and is much slower.
- The backlog is large, so putting `no_captions` into fallback caused a big throughput collapse.
- Whisper empty output is not proof of a bug; it usually means no speech, maybe music or silence, and is now labeled that way.

## Validation status

- `python -m py_compile` passed on the touched files.
- `P:/packages/yt-is/tests/test_csf_source_fetch_timing.py` passed.
- `P:/packages/yt-is/tests/test_transcript.py` passed.
- The latest focused split tests passed.

## Next action for the new session

1. Restart a worker run from `P:/packages/yt-is` with:
   - `python bin/csf-source fetch --workers 4`
2. Watch the trace file under `P:/packages/yt-is/.logs/term_*.jsonl`.
3. Check whether the `notebooklm` lane now absorbs most `no_captions` items again.
4. Compare:
   - NotebookLM successes
   - transcript-fallback successes
   - negative-cache growth
   - cache row growth in `P:/__csf/.data/yt-is/transcripts.sqlite`

## Useful reminders

- A large number of `oembed unavailable: HTTP 404` items should now be skipped cheaply and cached negatively.
- `active_workers: 0` in transcript-fallback logs is expected; that lane is not the industrial NotebookLM worker pool.
- If the next worker run looks slow again, first check whether `no_captions` is still going to the wrong lane before changing batch size or retry tuning.
- The current NotebookLM worker notebook capacity note is at [docs/operations/nlm-canary-capacity-note.md](P:/packages/yt-is/docs/operations/nlm-canary-capacity-note.md).
## Debugging / Logging Rules That Matter
- Quick pointer: [DEBUGGING_PLAYBOOK.md](P:/packages/yt-is/DEBUGGING_PLAYBOOK.md)
- Do not trust the JSONL trace alone. Several important warnings surfaced only in live stderr/stdout.
- When threading a new field through a wrapper, verify the callee signature before assuming it works. The `mark_failed(..., source=...)` bug was exactly this failure mode.
- Treat the worker result file as the source of truth for completed work. Stdout summaries can be stale or incomplete.
- If a worker run emits warnings, check both structured trace events and raw terminal output because they do not always carry the same information.
- For throughput questions, prefer completed-worker totals and stage timings over scan-progress or backlog-size-derived rates.
- If a long scan looks silent, `YTIS_SCAN_STATUS_INTERVAL_S` controls the heartbeat cadence for `/yt-is sync` and fetch scans.
- The most useful live signals have been:
  - `fetch_worker_finished`
  - `worker_completed`
  - `worker_batch_metrics`
  - `worker_source_profile_totals`
  - `negative_cache_reason_counts`
  - `add_cmd_elapsed_s` vs `materialization_wait_elapsed_s`
- When a new logging field is added, smoke-test the exact path that writes it. If it only appears in one code path, the first bug is often a mismatch in another path.
- If valid videos show up as `too_short` or `command_failed`, verify NotebookLM source-to-video mapping before blaming the source itself. We already hit a bug where `source list --json` order was trusted incorrectly in `extract_transcripts()`.

## Session Bootstrap
- Read these first:
  - [HANDOFF.md](P:/packages/yt-is/HANDOFF.md)
  - [CODEX_MEMORY.md](P:/packages/yt-is/CODEX_MEMORY.md)
  - [DEBUGGING_PLAYBOOK.md](P:/packages/yt-is/DEBUGGING_PLAYBOOK.md)
- If you are touching NotebookLM throughput, check `P:/packages/yt-is/csf/nlm_config.py` first for the shared NotebookLM defaults before grepping for magic numbers.
- Key files:
  - [bin/csf-source](P:/packages/yt-is/bin/csf-source)
  - [csf/transcript.py](P:/packages/yt-is/csf/transcript.py)
  - [csf/batch_status.py](P:/packages/yt-is/csf/batch_status.py)
  - [csf/batch_scheduler.py](P:/packages/yt-is/csf/batch_scheduler.py)
- Fast verification:
  - `python -m py_compile P:\packages\yt-is\bin\csf-source P:\packages\yt-is\csf\transcript.py P:\packages\yt-is\csf\batch_status.py P:\packages\yt-is\csf\batch_scheduler.py`
  - `PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_transcript.py -q`
  - `PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_csf_source_fetch_timing.py -q`
- Current intended worker run:
  - `python P:\packages\yt-is\bin\csf-source fetch --workers <n>`
  - Worker notebook reuse is per worker; the benchmark sweep still continues through `8` workers.
