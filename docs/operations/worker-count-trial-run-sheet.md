# Worker Count Trial Run Sheet

Last updated: 2026-04-26

## Purpose

Measure NotebookLM throughput under controlled load while holding batch size and notebook ownership constant.

## Three-Phase Plan

The test program is split into three phases so we do not mix discovery, validation, and lock-in.

### Phase 1: Find the best shape

Goal:
- Identify the best operating point for successful videos/hour.
- Separate the main failure source from the throughput ceiling.

Runs:
- Worker-count sweep: `1, 2, 3, 4, 6, 8` workers on the same sample family.
- CLI retry A/B: current first-failure behavior vs retry-on-`NOT_FOUND` / `too_short`.
- Batch-size check: `25` vs `50` on the free-tier notebook.

Exit criteria:
- We have a candidate winning configuration.
- We know whether worker count, retry policy, or batch size is the dominant limiter.

Execution checklist:
1. Reconfirm CLI auth with `nlm login --check`.
2. Bootstrap the persistent browser profile with `python P:\packages\yt-is\bin\nlm-playwright --bootstrap-auth`.
3. Run the eight-URL readiness matrix once to verify the DOM/browser path is still healthy.
4. Run the worker-count sweep on the same sample family with the candidate set:
   - `1`
   - `2`
   - `3`
   - `4`
   - `6`
   - `8`
5. Record success-only throughput, fail rate, `source_ready_age_s_avg`, and `content_fetch_status_counts`.
6. If the best candidate is ambiguous, rerun the top two worker counts on the same sample before moving on.
7. Compare `25` vs `50` batch size only if the worker-count sweep does not already make the answer obvious.

Pass criteria:
- One worker count is clearly ahead on successful videos/hour.
- The fail rate is not exploding at the top candidate.
- The main false-fail behavior is understood well enough to decide whether retries are needed.

Stop criteria:
- The sweep cannot be compared on the same sample family.
- DOM/browser auth becomes unstable again.
- The candidate winner changes only because the sample changed.

### Phase 2: Prove the winner is real

Goal:
- Verify the phase 1 winner is stable and not a one-off.
- Measure DOM-ready vs CLI-ready behavior on the same URLs.
- Understand whether source shape changes the answer.

Runs:
- Repeat the best config at least 3 times.
- Repeat the eight-URL readiness matrix.
- Run the four-URL comparison set with the two historical failures plus two known-good URLs.
- Stratify by source shape:
  - shorts
  - long-form
  - caption-rich vs caption-poor
  - mixed backlog
- Measure notebook fullness and warm-vs-cold reuse effects.

Exit criteria:
- The best config wins repeatedly.
- DOM readiness and CLI readiness agree well enough to use together.
- We know the remaining failure modes are stable enough to model.

Execution checklist:
1. Pick the phase 1 winner and rerun it at least 3 times on the same sample family.
2. Re-run the eight-URL readiness matrix and confirm it stays at 8/8.
3. Re-run the four-URL comparison set:
   - the two historical failure URLs
   - the two known-good control URLs
4. Repeat the same source mix at least once more:
   - shorts
   - long-form
   - caption-rich
   - caption-poor
5. Measure notebook fullness and warm-vs-cold behavior:
   - first batch on a notebook
   - later batch on the same notebook
6. If CLI false fails still occur, test the retry-enabled branch/flag against the same URLs before changing anything else.

Pass criteria:
- The phase 1 winner repeats without a large throughput drop.
- The historical false failures are explained by readiness timing, not bad inputs.
- DOM and CLI readiness signals line up closely enough to drive the production path.

Stop criteria:
- The winner only works on one source shape.
- The four-URL comparison still shows unexplained false failures.
- Notebook reuse meaningfully degrades throughput or accuracy.

### Phase 3: Lock it in

Goal:
- Turn the winning configuration into the default operating method.
- Make the result durable for the free-tier baseline and the later Pro notebook pass.

Runs:
- Repeat the winning config on a larger representative sample.
- Re-run the same shape on Pro NotebookLM with the `300`-source notebook limit.
- Update defaults, docs, and retry policy based on the validated result.

Exit criteria:
- The winning setup is codified.
- The retry and readiness rules are documented.
- The free-tier and Pro tiers have a comparable operating baseline.

Execution checklist:
1. Re-run the winner on a larger representative sample.
2. Re-run the same winner on the Pro NotebookLM `300`-source notebook limit.
3. Lock the retry and readiness rules into the defaults:
   - DOM spinner/checkmark gates probing
   - CLI `source content` retries transient `NOT_FOUND` / `too_short`
   - final failure only after the adaptive window is exceeded
4. Update the run sheet and handoff with the final winner and the final stop conditions.
5. Archive the obsolete benchmark variants so future runs do not drift back to old paths.

Pass criteria:
- The same configuration still wins on the larger sample.
- Free-tier and Pro results are comparable.
- The defaults and docs match the validated operating method.

Stop criteria:
- Pro behavior diverges enough that the free-tier winner does not translate.
- The retry policy changes the ranking materially enough that a new phase 1 is needed.

## Recent Execution Notes

The following results were collected after the plan was written and should be treated as the current working evidence:

- DOM/browser preflight is healthy again on the stable profile:
  - eight-URL readiness matrix: `8/8 succeeded`
  - four-URL comparison: `4/4 succeeded`
- Batch-size experiment on the free-tier notebook:
  - `--sizes 25,50 --count 300`
  - `25`-source subbatches timed out at the current `600s` materialization cap on subbatch 3
  - this is a negative result for lowering the batch size below `50`
- Throughput repeat on the current winner:
  - `--workers 2 --limit 400`
  - `369 succeeded / 31 failed`
  - `1472.1 successful videos/hour`
- Larger free-tier confirmation run:
  - `--workers 2 --limit 800`
  - `770 succeeded / 30 failed`
  - `1421.5 successful videos/hour`
- Phase 3 Pro rerun remains pending until a Pro NotebookLM profile/account is available.
- The fallback tail now reaches Whisper for `yt-dlp = ok` videos with no captions:
  - audio download includes `--js-runtimes node` when `node` is available
  - Whisper now runs on the downloaded audio instead of stopping at the audio stage
  - successful transcripts are saved to `P:/.data/yt-is/transcripts.sqlite`
- Verified live example:
  - `zgf2d8gsy70`
  - source: `whisper`
  - transcript length: `15419`
  - cached at `2026-04-24T23:06:39.164905`

## Routing Conclusion

The current working conclusion for source-shape routing is:

- Caption-rich cohort, isolated to the synthetic benchmark source:
  - `10/10` succeeded
  - baseline: `53.0s`, `680.76 videos/hour`, `worker_idle_wait_s = 27.82`
  - `route_no_captions_to_fallback`: `52.1s`, `692.25 videos/hour`, `worker_idle_wait_s = 27.55`
  - meaning: the routing split does not hurt caption-rich throughput.
- No-caption cohort:
  - baseline NotebookLM-first: `0/10` succeeded, about `291s`
  - route-to-fallback: `0/10` succeeded, about `4s`
  - meaning: no-caption items should not burn NotebookLM time when the fallback lane can take them immediately.
- Operating rule:
  - keep caption-rich items on NotebookLM
  - route no-caption items away from NotebookLM earlier
  - keep the split in place unless a later benchmark shows a real regression on caption-rich throughput

## Robustness Matrix

Use this after the fallback tail fix is in place and before any more large throughput sweeps.

### A. Classification boundaries

Goal:
- Prove the routing buckets are cleanly separated before measuring throughput.

Sample set:
- `yt-dlp = ok`, no captions:
  - `zgf2d8gsy70`
  - `4qTixOM76EQ`
  - `gL9fq9ybx_Q`
- `removed_by_owner`:
  - `VdunqscAV5Q`
- `not_yet_live`:
  - `jGKeNYIh3eI`
  - `HUIoPtQ1e6Q`
  - `0aJ23HTEuH0`

Pass criteria:
- The three `yt-dlp = ok` items reach Whisper and save transcripts.
- The terminal items stay terminal and do not get cached as successes.
- The failure labels stay stable across a rerun of the same IDs.

### B. Fallback tail correctness

Goal:
- Prove the no-caption tail can actually produce saved transcripts.

Sample set:
- The same three `yt-dlp = ok` no-caption items above.
- Add 2 to 4 more `yt-dlp = ok` no-caption items from the latest backlog run if available.

Pass criteria:
- The tail items return `source = whisper` or an equally successful fallback source.
- The transcript text is written to `P:/.data/yt-is/transcripts.sqlite`.
- The audio stage no longer fails first because of the YouTube challenge.

### C. Cache idempotency

Goal:
- Prove a successful transcript is saved once and reused cleanly.

Sample set:
- `zgf2d8gsy70`
- `4qTixOM76EQ`

Pass criteria:
- First run writes a cache row.
- Second run hits cache or reuses the saved transcript.
- The transcript text and source metadata remain consistent.

### D. Small mixed batch

Goal:
- Confirm the fix only changes the recoverable tail and not the terminal buckets.

Sample set:
- `zgf2d8gsy70`
- `4qTixOM76EQ`
- `gL9fq9ybx_Q`
- `VdunqscAV5Q`
- `jGKeNYIh3eI`

Pass criteria:
- The three tail items save transcripts.
- The two terminal items remain terminal.
- The batch does not reclassify terminal items as recoverable.

### E. Lightweight throughput sanity

Goal:
- Measure whether the fixed fallback tail changes the hot path enough to matter.

Sample set:
- A small mixed backlog batch after the above passes.

Pass criteria:
- Throughput remains in the same rough band as the current `2-worker` baseline.
- Any throughput change is explainable by the percentage of fallback-tail items in the batch.
- No new regression appears in NotebookLM add/readiness timing.

### Order

Run these in order:
1. Classification boundaries
2. Fallback tail correctness
3. Cache idempotency
4. Small mixed batch
5. Lightweight throughput sanity

Stop after step 3 if the tail is still not saving transcripts correctly.
Stop after step 4 if terminal buckets are leaking into success.
Only do step 5 after the earlier checks are clean.

## Fixed Controls

- NotebookLM account/profile: keep fixed for the whole sweep
- DOM readiness browser profile: use one persistent Chrome profile signed into the same NotebookLM account
- Worker notebook model: one worker owns one notebook, reused across batches
- NotebookLM batch size: `50`
- Materialization/readiness timeout: `600s`
- Sample family: keep the same family for the whole comparison block
- Fallback audio path: use yt-dlp with a real JS runtime (`node`) before Whisper when captions are absent.

## First Preflight

Before any DOM readiness work:

1. Run `nlm login --check` for the CLI profile.
2. Bootstrap the persistent browser profile with `python P:\packages\yt-is\bin\nlm-playwright --bootstrap-auth`.
3. Confirm NotebookLM loads without `Request access`.
4. Only then run the readiness matrix.
5. If the immediate goal is backlog progress, also rerun the current failing cohort so the now-fixed Whisper tail can save transcripts.

## What To Measure

- Successful videos/hour
- Wall-clock elapsed time
- Success count
- Failure count
- NotebookLM add time
- NotebookLM readiness wait time
- Whether materialization started before timeout
- Cleanup time
- Failure stage counts

## Trial Order

Run the worker-count sweep in this order:

1. `1` worker
2. `2` workers
3. `3` workers
4. `4` workers
5. `5` workers
6. `6` workers
7. `7` workers
8. `8` workers

## Run Rules

- Keep `batch_size=50` fixed.
- Keep the same sample family for all runs in the sweep.
- Do not change worker count and sample family at the same time.
- Stop the run if NotebookLM sources do not become ready within `600s`.
- If the DOM profile shows `Request access`, stop and re-bootstrap the browser profile before continuing.
- Use the JSONL trace plus the worker result file to reconstruct timings.
- Treat completed-worker totals and stage timings as throughput truth.

## Preferred Command

Run the sweep with the dedicated helper:

```powershell
P:\packages\yt-is\bin\csf-worker-count-sweep --workers 1,2,3,4,5,6,7,8 --limit 400
```

Use `--output-root` if you want the artifacts somewhere other than `.logs/worker_count_trials`.

## Results Template

| Workers | Sample | Succeeded | Failed | Elapsed_s | Videos/hour | Add_s | Readiness_s | Materialization Started | Timeout | Cleanup_s | Notes |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---|
| 1 |  |  |  |  |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |  |  |  |
| 3 |  |  |  |  |  |  |  |  |  |  |  |
| 4 |  |  |  |  |  |  |  |  |  |  |  |
| 5 |  |  |  |  |  |  |  |  |  |  |  |
| 6 |  |  |  |  |  |  |  |  |  |  |  |
| 7 |  |  |  |  |  |  |  |  |  |  |  |
| 8 |  |  |  |  |  |  |  |  |  |  |  |

## Follow-Up Analysis Passes

After the main worker-count sweep, use these narrower passes to explain the result.

### 1. Replicate the best few worker counts

- Repeat the best observed worker counts 3 times each.
- Keep the exact same sample IDs if possible.
- Measure median throughput and variance.

### 2. Compare overlap vs staggered NotebookLM pressure

- Run one pass with normal worker overlap.
- Run one pass with staggered NotebookLM access, where only one worker is in add/readiness at a time.
- Compare readiness wait, failure rate, and videos/hour.

### 3. Stratify by workload mix

- Run the best worker counts on caption-rich long-form videos.
- Run the best worker counts on shorts.
- Run the best worker counts on mixed backlog.
- Compare throughput and failure-stage distribution.

### 4. Measure readiness distribution

- Use the readiness logs to capture median and tail wait times.
- Track whether `materialization_started` was true before timeout.
- Compare readiness wait against notebook source count.

### 5. Correlate notebook fullness with failure rate

- Group runs by source count before add.
- Compare add failures, readiness delays, and throughput as notebook fullness changes.

### 6. Compare cold vs warm worker notebooks

- Record the first batch on each worker notebook.
- Compare it with later batches on the same notebook.
- Measure startup penalty versus steady-state reuse.

## Load-Shaping Matrix

Use this matrix when the question is not just "which worker count wins?" but
"what kind of load is keeping workers idle?" The goal is to separate notebook
contention from queue composition.

### Batch labels

Tag each 400-item sample before the run as one of:

- `fast_lane`
  - caption-rich, ready-to-process, low-fallback items
- `slow_lane`
  - no-caption, fallback-heavy, or readiness-sensitive items
- `mixed_lane`
  - a representative mix of both shapes
- `terminal_lane`
  - mostly unavailable / terminal items, used only as a control

If you want the simplest split, use:

- `200` fast-lane items
- `100` mixed items
- `100` slow-lane items

### Worker-count matrix

Run the same sample family at:

- `2` workers
- `4` workers
- `8` workers

Keep the notebook/profile setup fixed so the only changing variables are worker count and load mix.

### Per-run metrics to log

Add these to the normal sweep output or the run notes:

- `worker_idle_wait_s`
- `source_count_before_add`
- `add_elapsed_s`
- `source_list_wait_elapsed_s`
- `dom_wait_elapsed_s`
- `content_readiness_probe_elapsed_s`
- `materialization_started`
- `retry_queue_depth`
- `fallback_queue_depth`
- `caption_rich_count`
- `caption_poor_count`
- `no_caption_count`
- `short_form_count`
- `long_form_count`
- `terminal_count`

### What the matrix should tell you

- If `worker_idle_wait_s` grows with worker count, NotebookLM is the bottleneck.
- If `source_list_wait_elapsed_s` dominates, notebook materialization is the bottleneck.
- If `content_readiness_probe_elapsed_s` dominates on slow-lane items, the tail needs a separate queue.
- If fast-lane items keep workers busy but slow-lane items leave workers idle, split the queues instead of adding more workers.
- If `8` workers only helps when the sample is fast-lane heavy, the current winner is sample-dependent rather than load-independent.

### Run order

1. `2` workers on `fast_lane`
2. `2` workers on `slow_lane`
3. `4` workers on the same two lanes
4. `8` workers on the same two lanes
5. `mixed_lane` as the sanity check

### Stop rules

- Stop if NotebookLM readiness becomes unstable again.
- Stop if the split-lane runs materially change the routing balance.
- Stop if worker idle time cannot be explained by notebook fullness or queue composition.

## Readiness Calibration Matrix

Use this when you want to compare the DOM spinner/checkmark signal against the CLI `source content` readiness probe on the exact same URLs.

### Current calibration set

Known failures from the last run:

- [https://www.youtube.com/watch?v=KvC7ct1UVBs](https://www.youtube.com/watch?v=KvC7ct1UVBs)
- [https://www.youtube.com/watch?v=cbfnFt9lLV4](https://www.youtube.com/watch?v=cbfnFt9lLV4)
- [https://www.youtube.com/watch?v=mzKV2BoSPvs](https://www.youtube.com/watch?v=mzKV2BoSPvs)
- [https://www.youtube.com/watch?v=XA-dIgErCi8](https://www.youtube.com/watch?v=XA-dIgErCi8)

Known-good controls from the same batch:

- [https://www.youtube.com/watch?v=tduRayavmJI](https://www.youtube.com/watch?v=tduRayavmJI)
- [https://www.youtube.com/watch?v=a7HW4SicO5M](https://www.youtube.com/watch?v=a7HW4SicO5M)
- [https://www.youtube.com/watch?v=pMTpWGA64aM](https://www.youtube.com/watch?v=pMTpWGA64aM)
- [https://www.youtube.com/watch?v=opRhPRMOFYs](https://www.youtube.com/watch?v=opRhPRMOFYs)

### Preferred command

Run the scraper in readiness-matrix mode with the same eight URLs every time:

```powershell
python P:\packages\yt-is\csf\nlm_scraper.py --readiness-matrix --video-ids KvC7ct1UVBs,cbfnFt9lLV4,mzKV2BoSPvs,XA-dIgErCi8,tduRayavmJI,a7HW4SicO5M,pMTpWGA64aM,opRhPRMOFYs
```

### What it logs

- `staging_source_readiness_snapshot`
- `staging_source_content_readiness_probe_window_started`
- `staging_source_content_readiness_probe_started`
- `staging_source_content_readiness_probe_completed`
- `staging_source_content_readiness_probe_window_completed`
- existing DOM source-materialization logs from `nlm_scraper.py`
- the browser-auth preflight marker:
  - `selenium_browser_auth_checked`
  - `selenium_browser_auth_failed`

### Bootstrap the DOM profile

This step is already required by the first preflight above. Repeat it any time the browser profile starts showing `Request access`.

### What to compare

- DOM spinner active vs inactive
- DOM checkmark inferred as spinner inactive
- CLI `source content` status:
  - `ready`
  - `too_short`
  - `command_failed`
  - `parse_failed`
- elapsed age from source materialization to first ready probe

### Known-good DOM baseline

Use this successful run as the current reference point for the eight-URL readiness matrix:

| Metric | Value |
|---|---:|
| Run output | `P:\packages\yt-is\.logs\readiness_trials\latest_matrix_run_3.txt` |
| Shell wall time | `121.867s` |
| In-process matrix span | `99.094s` |
| Sources | `8/8 succeeded` |
| Transcript size | `13581 chars` for each URL |
| First source log after start | `22.744s` |
| DOM buttons ready after notebook open | effectively immediate in the captured trace |
| Per-source completion cadence | about `12s` to `14s` between results |

The successful trace shows the browser session is now valid, the DOM sources panel is reachable, and the scraper can read all eight calibration URLs from the NotebookLM UI.
It also confirms the DOM spinner/checkmark readiness path is exercised end-to-end in the live matrix run, but it does not prove the spinner predicate is a perfect semantic oracle for every NotebookLM UI state.

### Recommended next tests

Use this order so the next passes build on the known-good baseline instead of jumping straight to load testing:

1. Re-run the same eight-URL readiness matrix once more to confirm the DOM/browser auth path stays stable and the CLI readiness probes still agree on the same URLs.
2. Run a four-URL comparison pass using the two previously troublesome URLs plus two known-good URLs, so we can compare spinner/checkmark timing against CLI `source content` timing on the exact items that used to fail.
3. After the browser path is stable on repeated runs, go back to the free-tier throughput comparison: one worker versus two workers, same sample, same readiness logging.

## Backup Step

- Before any sweep or cleanup that could touch transcript state, run:
  - `python P:/packages/yt-is/bin/csf-backup-transcripts`
- This snapshots `P:/.data/yt-is/transcripts.sqlite` into `P:/.data/yt-is/backups/`.
- Treat this as normal preflight, not an optional extra.
- For staged backlog runs, set `YTIS_TRANSCRIPT_CACHE_DB_PATH=P:/.data/yt-is/transcripts-staging.sqlite`, run the batch, then promote the results with `python P:/packages/yt-is/bin/csf-promote-transcripts`. The promote command is blocking and fail-closed, so it will stop on a missing source DB, an empty staging DB, or a source/destination path collision.
- Before any tracked-channel sync or blocklist change, run `python P:/packages/yt-is/bin/csf-backup-channel-state`.
- This snapshots `P:/.data/yt-is/batch_status.sqlite` into `P:/.data/yt-is/backups/`.
- For staged channel-state changes, set `YTIS_BATCH_STATUS_DB_PATH=P:/.data/yt-is/batch-status-staging.sqlite`, run `yt-is sync` against that staging DB, then promote the results with `python P:/packages/yt-is/bin/csf-promote-channel-state`. The promote command is blocking and fail-closed, so it will stop on a missing source DB, an empty staging DB, or a source/destination path collision.

## Evidence Sources

- `P:/packages/yt-is/.logs/term_*.jsonl`
- worker result file for the run
- `P:/.data/yt-is/transcripts.sqlite`
- `P:/packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`

## Notes

- The notebook lifecycle is reuse-only in the steady state.
- Do not change batch size during this sweep.
- Do not compare backlog-derived scan rates to throughput.

## Future Phase: Pro NotebookLM

After the free-tier `50`-source baseline is understood, repeat the same readiness and throughput matrix on a Pro NotebookLM subscription with the `300`-source notebook limit.

- Keep the same logging fields and run order.
- Keep the same worker-owned notebook model.
- Compare the UI-ready vs CLI-ready lag and the failure mix against the free-tier baseline.
