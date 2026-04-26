# Source-Shape Routing Ladder Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a benchmarkable source-shape routing switch so we can compare the current NotebookLM-first handling of no-caption items against a direct fallback route without changing the rest of the fetch pipeline.

**Architecture:** The existing `csf-source fetch` classifier already splits live items, captioned items, and no-caption items. We will make the no-caption branch configurable with one env flag, log that routing mode in the fetch summary, and add a ladder scenario that runs the same frozen cohort under both routing modes. The benchmark runner stays the same; only the routing decision and scenario list change.

**Tech Stack:** Python 3.14, `csf-source`, JSONL trace logs, existing `csf-load-ladder` / `csf-fallback-crossover-benchmark` runners, pytest.

---

## Problem

We already learned that notebook fullness, notebook reuse, and staggered access do not materially change the no-caption cohort. The remaining question is whether no-caption items should stay on the NotebookLM lane at all. The current code routes no-caption items to NotebookLM first; this design adds a switch so we can benchmark that against direct fallback routing without pausing for a separate harness.

## Proposed Behavior

- Keep live/premiere items on transcript fallback.
- Keep captioned items on NotebookLM.
- Make no-caption items configurable:
  - default: NotebookLM-first, as today
  - opt-in: transcript fallback first
- Record the active mode in the fetch invocation and summary logs.
- Add a route-split benchmark scenario that compares the two modes on the same frozen cohort.

## Scope

In scope:
- One env flag that controls no-caption routing.
- Fetch log fields that expose the active routing mode.
- A benchmark scenario that exercises the new route.
- Tests for both code paths.

Out of scope:
- A new queueing backend.
- Reworking the NotebookLM add path.
- Reclassifying which rows are no-caption versus captioned.

## Success Criteria

- The routing mode is visible in the fetch logs and benchmark summary.
- The default path is unchanged when the env flag is unset.
- When the env flag is set, no-caption rows bypass the NotebookLM-first path and enter the fallback lane.
- The route-split benchmark can be run alongside the existing ladder without manual edits.
- Tests prove the classifier behavior in both modes.

## Risks

- If the fallback lane is still slow, the benchmark may confirm that routing split alone is not enough. That is acceptable; the goal is to measure the effect cleanly.
- Mixed historical trace data may not provide perfect caption-rich cohorts. The current ladder should still compare the current no-caption behavior against the route-split behavior using the frozen cohort we already have.

