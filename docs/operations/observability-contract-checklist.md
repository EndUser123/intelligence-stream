# Observability Contract Checklist

> Use this by default before trusting any metric, summary, or analysis in `yt-is`. If the producer and consumer disagree, stop and fix the contract first.

**Purpose:** Prevent metric drift, parser drift, and summary-driven false conclusions by forcing a live producer check before any root-cause claim.

## Default Reading Order

1. Read the raw event producer code for the field or metric in question.
2. Read at least one real log or artifact emitted by the current run.
3. Confirm the analyzer/parser matches the real emitted schema.
4. Confirm the unit and meaning of the field before comparing runs.
5. Only then use summaries, reducers, or derived tables to explain the bottleneck.

## Required Checks

- Verify the producer shape, not just the summary shape.
- Verify the field unit:
  - `*_elapsed_s` is elapsed time in seconds.
  - `*_age_s` is age in seconds, not a timestamp.
  - counts are counts, not rates.
- Verify the field source:
  - log action event
  - worker JSONL
  - summary JSON
  - reducer output
- Verify the field survives one round-trip:
  - producer emits it
  - consumer reads it
  - tests assert it
- If the producer or consumer changes, update the tests and the operations docs in the same change.

## Stop Conditions

Stop and reconcile before drawing a conclusion if any of the following is true:

- A parser expects a shape that the producer no longer emits.
- A metric looks like a timestamp but is being treated like an age.
- A summary value is being interpreted without checking the raw event.
- A new field exists in code but is not visible in live logs.
- A live log shows a different unit, field name, or schema than the docs or tests claim.

## Default Rule for Future Work

For every future benchmark or observability change in `yt-is`:

- read this checklist first
- verify the live producer schema before trusting summary metrics
- add or update a focused regression test for the field contract
- update [Hot-Path Throughput Next Test Plan](hot-path-throughput-next-test-plan.md) and [Test Registry](test-registry.md) if the interpretation changes

## Current Known Failure Modes This Checklist Prevents

- treating `auth_cache_session_age_s` like a timestamp instead of an age
- assuming `source_statuses` exists when the producer emits direct per-source events
- trusting `command_failed` counts without checking `stdout`, `stderr`, and `attempts`
- concluding an age guard fired without finding the rotation event in the worker logs
- using summary-only evidence to justify retry-policy changes

