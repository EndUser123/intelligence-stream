# Hot-Path Throughput Optimization Series Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Find and validate any remaining path to raise sustained hot-path `videos/hour` beyond the current best result while keeping Whisper recovery out of the throughput score.

## Current Baseline

The current best recorded sustained hot-path throughput is:

- `3928.18 videos/hour`
- `4` NotebookLM workers
- narrow/captioned cohort
- `--batch-size 200`
- reusable NotebookLM path
- Whisper excluded from sustained throughput

The current batch-size control is `200` benchmark items per batch. This is not NotebookLM source capacity.

## Metric Contract

The score for this series is hot-path `videos/hour` only.

Count in hot-path throughput:
- successful NotebookLM hot-path transcript completions
- direct-caption completions only in a separate direct-caption phase, and only after parity is reviewed

Do not count in hot-path throughput:
- Whisper recovery
- transcript fallback completions
- skipped terminal items
- raw processed count
- backlog scan rate

Every run must report:
- `hot_path_success_count`
- `videos_per_hour`
- `transcript_fallback_success_count`
- `transcript_fallback_videos_per_hour`
- `fail_count`
- `worker_idle_wait_s`
- `add_elapsed_s`
- `readiness_elapsed_s`
- `extract_elapsed_s`
- `cleanup_elapsed_s`
- `source_ready_age_s_total`

Pipeline-shape runs must also report:
- `pipeline_strategy`
- `staging_overlap_elapsed_s_total`
- `staging_wait_elapsed_s_total`
- `stage_swap_count_total`
- source-add failure counts

## Non-Goals

- Do not retest broad worker counts unless a pipeline shape changes.
- Do not retest benchmark batch sizes below/above `200` unless a pipeline shape wins.
- Do not retest cleanup cadence unless cleanup semantics change.
- Do not fold Whisper recovery into the hot-path metric.
- Do not run a Pro `300`-source-cap test; that is not the question.
- Do not treat a smoke test as a throughput result.

## Phase 1: Double-Buffered Pipeline Comparison

Purpose: test whether overlapping staging work with extraction improves sustained throughput.

Compare:
- serial reusable NotebookLM path
- `YTIS_REUSABLE_PIPELINE_MODE=double_buffered`

Hold fixed:
- narrow/captioned cohort
- `4` workers
- `--batch-size 200`
- same policy: `notebooklm_route_plus_fallback_30s_1w`
- same trace root and source selection
- same Pro profile setup

Decision rule:
- keep double-buffering only if it materially beats `3928.18 videos/hour` or materially beats a same-day serial control on the same cohort without increasing failures
- if double-buffering loses because of source-add failures, run Phase 2 before discarding it
- if double-buffering loses without source-add failures, mark the pipeline-shape attempt negative

## Phase 2: Source-Add Failure Hardening

Purpose: remove wasted hot-path time caused by failed NotebookLM source adds or materialization failures.

Run this only if Phase 1 exposes `source_add_failed`, materialization timeout, or readiness instability.

Test candidates:
- auth/profile readiness preflight before worker start
- bounded source-add retry for transient add failures
- clearer failure-stage classification in worker summaries
- rerun of serial and double-buffered comparison after hardening

Decision rule:
- keep only changes that reduce source-add failures without reducing hot-path throughput
- do not mask terminal failures as transient retries

## Phase 3: Direct Caption Fast Path

Purpose: determine whether captioned videos can bypass NotebookLM and still produce acceptable transcript output.

Compare:
- current NotebookLM hot path
- direct-caption retrieval path
- direct-caption-first with NotebookLM fallback for misses

Keep this phase separately labeled because it changes the transcript acquisition path.

Measure:
- direct-caption throughput
- transcript length and quality parity
- miss rate
- fallback-to-NotebookLM rate
- hot-path vph under the hybrid route

Decision rule:
- direct captions can join the main hot path only if transcript parity is acceptable on the same cohort
- otherwise keep it as a separate fast-path option with separate metrics

## Phase 4: Profile Sharding

Purpose: test whether the throughput ceiling is bound to one NotebookLM browser/profile session.

Compare:
- current single-profile `4`-worker control
- two independent profiles, each running the winning worker shape at half load
- optional four-profile pass only if the two-profile pass scales cleanly

Measure:
- aggregate hot-path vph
- per-profile vph
- auth/session failures
- source-add failures
- cleanup cost

Decision rule:
- keep profile sharding only if aggregate vph scales without unacceptable auth or cleanup overhead

## Phase 5: Retune Packaging After A Winning Shape

Purpose: retune benchmark packaging only after a previous phase changes the pipeline behavior.

Candidates:
- benchmark batch sizes around `200`: `175`, `200`, `225`, `250`
- NotebookLM add subbatch sizes: revisit `75` and `100` only after materialization timeout cause is understood

Decision rule:
- keep `200` as the default until another size beats it under the winning pipeline shape

## Required Output

The implementation should produce one summary document or JSON artifact that lists:
- phase
- mode
- command
- output root
- hot-path vph
- success/failure counts
- Whisper recovery counts kept separate
- decision for that phase: `keep`, `reject`, or `needs-hardening`

The run sheet and test registry must be updated after each completed phase.
