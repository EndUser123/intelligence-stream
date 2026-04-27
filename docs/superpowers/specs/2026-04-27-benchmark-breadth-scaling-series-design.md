# Benchmark Breadth And Scaling Series Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove which load-shape variable actually moves sustained hot-path `videos/hour` in `yt-is`, while keeping Whisper recovery separate from the throughput metric. The series should answer two questions in one controlled family:
1. Does queue breadth matter more than the current batch label?
2. On the best breadth shape, does worker count above `2` improve hot-path throughput or only add idle time?

**Architecture:** Use one benchmark series with one fixed accounting model:
- hot-path `videos/hour` counts only NotebookLM / non-Whisper work
- Whisper recovery is recorded separately and never added into sustained throughput
- benchmark batch size stays fixed for the whole series
- worker state, profile, and routing policy stay fixed unless the phase explicitly changes them

The series is split into two phases:
- **Phase A: breadth proof** with fixed worker count
- **Phase B: scaling sweep** on the breadth shape that wins Phase A

**Tech Stack:** Python 3.14, `csf-source`, `csf-fallback-crossover-benchmark`, `csf-load-ladder`, JSON summaries, pytest, the existing shared benchmark manifest.

---

## Problem

We have two different throughput stories in the repo:
- a broad backlog sweep that reached thousands of hot-path videos/hour
- a narrower Pro benchmark shape where the same worker counts plateaued much lower and showed heavy idle time

The current evidence suggests the key variable is not Whisper, and not the benchmark batch-size label by itself. The likely lever is **queue breadth**: how many independent NotebookLM-eligible items are available for the workers to overlap. We need one combined benchmark series that isolates breadth first and then tests worker scaling on the winning breadth so we can stop guessing which knob matters.

## Proposed Behavior

- Run a single benchmark series that keeps hot-path accounting stable and excludes Whisper recovery from sustained `videos/hour`.
- Use three breadth tiers in the same series:
  - **broad**: the mixed backlog shape with no source filter
  - **mid**: a fixed medium-breadth cohort derived from the shared manifest or a documented filtered subset
  - **narrow**: a single-source-filter cohort
- For each breadth tier, hold the benchmark batch size constant.
- In Phase A, hold worker count fixed at `2` and compare the breadth tiers.
- In Phase B, take the breadth tier that produces the best hot-path `videos/hour` and sweep worker counts `2, 4, 6, 8, 10`.
- Track these metrics separately for every run:
  - `hot_path_success_count`
  - `videos_per_hour`
  - `transcript_fallback_success_count`
  - `transcript_fallback_videos_per_hour`
  - `worker_idle_wait_s`
  - `add_elapsed_s`
  - `readiness_elapsed_s`
  - `cleanup_elapsed_s`
  - `source_ready_age_s_total`
  - `fail_count`

## What This Series Is Trying To Prove

### Phase A: Breadth
We want to know whether broad queue breadth materially increases sustained hot-path throughput compared with medium and narrow breadth.

Hypothesis:
- broader cohort shapes give workers more independent hot-path work
- narrower shapes increase worker idle time
- idle time is the real reason the Pro-shaped run flattened

### Phase B: Worker Scaling On The Winner
We want to know whether the best breadth shape still benefits from more workers, or whether `2` is still the knee.

Hypothesis:
- if breadth is the real lever, the best breadth will still show a worker-count knee
- worker counts above the knee may increase failures or idle time without improving hot-path `videos/hour`

## Cohort Definitions

The series should use the same shared manifest and/or frozen trace cohort infrastructure already in the repo. The exact case ids can be recorded in the run sheet, but the tier definitions must stay stable:

- **Broad**
  - the mixed backlog shape with no source filter
  - this is the widest parallelizable queue shape

- **Mid**
  - a documented medium-breadth subset derived from the shared manifest or a fixed filtered subset of the mixed cohort
  - this should be broader than one source but narrower than the full mixed backlog

- **Narrow**
  - a single source filter or equivalent one-source cohort
  - this is the narrowest queue shape

The point is not to optimize the exact source list during the benchmark. The point is to keep the breadth tiers fixed and comparable across runs.

## Scope

In scope:
- one benchmark series with breadth-first and worker-scaling phases
- hot-path throughput accounting only
- separate Whisper recovery accounting
- fixed batch size across the series
- a documented cohort definition for broad, mid, and narrow breadth
- test-registry updates after the runs land

Out of scope:
- changing Whisper admission policy
- changing fallback concurrency
- changing NotebookLM source-cap assumptions
- changing the retry policy
- treating Whisper recovery as part of sustained `videos/hour`

## Success Criteria

- The series produces a clear ranking or plateau for broad vs mid vs narrow breadth.
- The winner breadth is obvious enough to justify the Phase B worker sweep.
- The worker sweep on the winner breadth shows whether `2` remains the knee or whether a higher count improves hot-path throughput.
- Whisper recovery remains reported separately and does not affect sustained `videos/hour`.
- The resulting conclusion is documented in the run sheet and test registry so future sweeps do not repeat the same comparison under a new name.

## Failure Modes

- If breadth does not change hot-path throughput, the queue-breadth theory is weak and the next optimization lever should be something else.
- If worker count still does not help on the winner breadth, the system is likely service-bound rather than worker-bound.
- If the benchmark shape drifts between tiers, the comparison is invalid; the manifest or source-filter selection must be frozen before running.

## Documentation Outcome

After the series completes:
- update the worker-count trial run sheet with the breadth comparison and worker-scaling results
- add the observed breadth and scaling cases to the test registry with `proven` or `negative` status
- preserve the hot-path-only `videos/hour` rule so future readers do not fold Whisper recovery into the sustained throughput number

