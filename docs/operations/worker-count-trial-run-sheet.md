# Worker Count Trial Run Sheet

Last updated: 2026-04-22

## Purpose

Measure NotebookLM throughput under controlled load while holding batch size and notebook ownership constant.

## Fixed Controls

- NotebookLM account/profile: keep fixed for the whole sweep
- Worker notebook model: one worker owns one notebook, reused across batches
- NotebookLM batch size: `200`
- Materialization/readiness timeout: `600s`
- Sample family: keep the same family for the whole comparison block

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

- Keep `batch_size=200` fixed.
- Keep the same sample family for all runs in the sweep.
- Do not change worker count and sample family at the same time.
- Stop the run if NotebookLM sources do not become ready within `600s`.
- Use the JSONL trace plus the worker result file to reconstruct timings.
- Treat completed-worker totals and stage timings as throughput truth.

## Preferred Command

Run the sweep with the dedicated helper:

```powershell
P:\packages\yt-is\bin\csf-worker-count-sweep --workers 1,2,3,4,5,6,7,8 --limit 1200
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

## Evidence Sources

- `P:/packages/yt-is/.logs/term_*.jsonl`
- worker result file for the run
- `P:/__csf/.data/yt-is/transcripts.sqlite`
- `P:/packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`

## Notes

- The notebook lifecycle is reuse-only in the steady state.
- Do not change batch size during this sweep.
- Do not compare backlog-derived scan rates to throughput.
