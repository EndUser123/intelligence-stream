# Double-Buffered Reusable Notebook Pipeline Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Raise sustained hot-path `videos/hour` by overlapping NotebookLM stage work instead of only tuning the same serial reusable-notebook pipeline. Whisper recovery must remain separate from sustained throughput accounting.

## Context

The current throughput work has already established that:
- cleanup cadence changes did not improve sustained throughput
- `200` benchmark batch size is the best-supported control among the tested values
- `4` workers is the current local peak on the narrow/captioned cohort
- Whisper recovery is real, but it is not counted in sustained hot-path `videos/hour`

The remaining promising lever is **stage shape**. The reusable NotebookLM path still performs add/materialize, extract, and cleanup as mostly serial work on one notebook at a time. That leaves wall-clock time on the table when the next batch could be staged while the current batch is still being extracted.

## Proposed Change

Introduce a **double-buffered reusable notebook pipeline**:

- one notebook is the current **active** notebook
- one notebook is the **staging** notebook for the next batch
- while batch `N` is extracting from the active notebook, batch `N+1` is already being added and materialized on the staging notebook
- when batch `N` finishes, the stage roles swap

This is intentionally narrower than a full producer/consumer rewrite. It only overlaps the parts that are currently the main wall-clock sink:

- add sources
- materialization wait
- extract transcripts
- cleanup

## Non-Goals

- Do not change Whisper admission policy.
- Do not count Whisper recovery in sustained `videos/hour`.
- Do not change the benchmark batch-size control for the validation run.
- Do not change worker count as part of the double-buffering experiment.
- Do not change the notebook source cap assumptions.
- Do not retry the cleanup cadence experiment.
- Do not generalize into arbitrary multi-stage parallelism unless the double-buffered version proves out.

## Proposed Architecture

The implementation should be a small orchestration layer around the existing reusable NotebookLM flow.

Recommended shape:

- Keep `NLMBatchIngestor` as the low-level add/extract/cleanup engine.
- Add a wrapper that owns two reusable notebook slots:
  - `active`
  - `staging`
- The wrapper manages a bounded handoff:
  - `active` is used for the current batch
  - `staging` is prepared for the next batch in the background
  - after extraction, the notebooks swap roles
- If staging is unavailable or fails, the run must fall back to the current serial path instead of failing the batch.

Important boundary:
- do not overlap two mutating operations on the same notebook
- do not let the staging notebook contaminate the active notebook’s source list
- keep the notebook state explicit so cleanup and reuse remain deterministic

## Behavioral Rules

1. The current hot-path accounting stays the same.
2. The active notebook always finishes extraction before being cleaned or retired.
3. The staging notebook may be created or refreshed while the active notebook is extracting.
4. If staging falls behind, the system may degrade to serial behavior, but correctness must be preserved.
5. Cleanup must remain bounded and observable.
6. The combined benchmark summary must still record Whisper recovery separately from hot-path throughput.

## Metrics To Capture

For every batch and for the combined run, record:
- `hot_path_success_count`
- `videos_per_hour`
- `worker_idle_wait_s`
- `add_elapsed_s`
- `readiness_elapsed_s`
- `extract_elapsed_s`
- `cleanup_elapsed_s`
- `source_ready_age_s_total`
- `staging_overlap_elapsed_s`
- `staging_wait_elapsed_s`
- `stage_swap_count`
- `fail_count`
- `transcript_fallback_success_count`
- `transcript_fallback_videos_per_hour`

The two new metrics that matter most here are:
- `staging_overlap_elapsed_s`
- `staging_wait_elapsed_s`

These tell us whether overlap is actually hiding wall-clock time or just adding bookkeeping.

## Validation Shape

Use the already-proven control family:
- narrow/captioned cohort
- fixed `200` benchmark batch size
- fixed `4` workers for the first proof run

Compare:
- current serial reusable path
- double-buffered reusable path

Pass criteria:
- the double-buffered path improves sustained hot-path `videos/hour` over the current serial reusable path on the same cohort
- the improvement is not caused by changing Whisper accounting
- the new overlap metrics show actual hidden stage time, not just extra bookkeeping

Failure criteria:
- no sustained `videos/hour` improvement
- higher failure rate or unstable notebook reuse
- overlap metrics show that the pipeline is still effectively serial

## Implementation Shape

The implementation should likely touch:
- the reusable NotebookLM orchestration layer
- the benchmark runner so the serial and double-buffered shapes can be compared
- targeted tests for state transitions and metrics
- the run sheet and registry after validation

The implementation should not rewrite the low-level NotebookLM command runner unless the wrapper needs a tiny helper extracted for reuse.

## Decision Rule

Keep this change only if it moves the sustained hot-path number on the same fixed cohort. If the double-buffered version does not beat the current serial reusable control, stop here and do not expand the parallelism further.

