# Highest Sustained VPH Investigation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Identify the next structural change or diagnostic that can raise sustained NotebookLM hot-path `videos/hour` above the current guarded best without confusing transient recovery with durable throughput.

**Architecture:** Start with one full-load scaling test of the age-capped shape that already improved throughput, then branch only if the data says the improvement does not scale. Use source-age, `command_failed`, `nlm_content_below_threshold`, auth elapsed time, and worker idle wait as the decision inputs. Do not broaden retry markers or add lane geometry before the scaling test proves a new bottleneck.

**Tech Stack:** PowerShell, `csf-sharded-lane-sequence`, `csf-nlm-worker-auth`, `csf-nlm-command-failed-classifier`, `pytest`, JSON benchmark summaries, live worker logs.

---

## File Structure

This plan touches both runtime evidence and follow-up analysis:

- Read: `P:\packages\yt-is\docs\operations\hot-path-throughput-next-test-plan.md`
- Read: `P:\packages\yt-is\docs\operations\sharded-lane-series.md`
- Read: `P:\packages\yt-is\docs\operations\test-registry.md`
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\sweep_phase3_2lane_3w_agecap_200_run02\sharded_lane_series_summary.json`
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\sweep_phase3_2lane_3w_run08\sharded_lane_series_summary.json`
- Read: `P:\packages\yt-is\.logs\nlm_content_probe\run07_age_300_probe\probe_summary.json`

Follow-up files only if a branch is triggered:

- Modify: `P:\packages\yt-is\csf\nlm_batch.py`
- Modify: `P:\packages\yt-is\tests\test_nlm_batch.py`
- Modify: `P:\packages\yt-is\docs\operations\hot-path-throughput-next-test-plan.md`
- Modify: `P:\packages\yt-is\docs\operations\sharded-lane-series.md`
- Modify: `P:\packages\yt-is\docs\operations\test-registry.md`

---

## Decision Gates

- If the next full-load age-capped run keeps `source_ready_age_s_max` below the prior cliff and `command_failed` stays near zero, treat the age cap as a viable throughput lever and investigate sparse-content residuals next.
- If batch 2 regresses and age climbs back above roughly `200s`, implement a real notebook-rotation or age-guard branch before pursuing any other throughput idea.
- If `nlm_content_below_threshold` becomes the dominant residual while `NOT_FOUND` stays suppressed, focus the next probe on sparse-content handling instead of retry policy.
- If actual auth elapsed time is a large share of wall time, treat auth scheduling or refresh cadence as a real limiter.
- If auth elapsed time is not a major share, stop investigating auth and keep the work centered on notebook freshness and source readiness.
- If the next run invalidates because of wrong account or shared-profile leakage, fix auth/profile routing before interpreting throughput.
- If the run stays clean but does not beat the age-capped result, do not widen lanes or broaden retry markers yet.

---

## Task 1: Run The Full-Load Scaling Test

**Files:**
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\pro_free_lanes.json`
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\sweep_phase3_2lane_3w_agecap_200_run02\sharded_lane_series_summary.json`
- Read: `P:\packages\yt-is\docs\operations\hot-path-throughput-next-test-plan.md`

- [ ] **Step 1: Refresh worker auth**

Run:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:\packages\yt-is\bin\csf-nlm-worker-auth sync
```

Expected: all configured worker `01` profiles validate against the intended accounts and the copy step succeeds. Stop if any profile maps to the wrong account or if the command reports an unprofiled auth path.

- [ ] **Step 2: Run the full-load scaling benchmark**

Run:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:\packages\yt-is\bin\csf-sharded-lane-sequence `
  --lane-config P:\packages\yt-is\.logs\sharded_lane_series\pro_free_lanes.json `
  --run-root P:\packages\yt-is\.logs\sharded_lane_series\highest_vph_agecap_400_run01 `
  --smoke-limit 50 `
  --smoke-batch-size 200 `
  --soak-limit 400 `
  --soak-batch-size 200 `
  --source-url https://www.youtube.com/channel/UCYTISFALLBACKBMK `
  --policy notebooklm_route_plus_fallback_30s_1w `
  --reusable-pipeline-mode serial
```

Expected: the run completes cleanly and produces `sharded_lane_series_summary.json` under the new run root.

- [ ] **Step 3: Extract the benchmark summary**

Run:

```powershell
@'
import json
from pathlib import Path

path = Path(r"P:\packages\yt-is\.logs\sharded_lane_series\highest_vph_agecap_400_run01\sharded_lane_series_summary.json")
summary = json.loads(path.read_text())
print(json.dumps({
    "artifact": str(path),
    "combined_hot_path_vph": summary["combined"]["hot_path_videos_per_hour"],
    "success": summary["combined"]["hot_path_success_count"],
    "failure": summary["combined"]["hot_path_failure_count"],
    "processed": summary["combined"]["processed_count"],
    "wall_elapsed_s": summary["combined"]["wall_elapsed_s"],
    "lanes": [
        {
            "lane": lane["lane"],
            "hot_path_vph": lane["hot_path_videos_per_hour"],
            "success": lane["hot_path_success_count"],
            "failure": lane["hot_path_failure_count"],
            "source_ready_age_s_avg": lane.get("source_ready_age_s_avg"),
            "source_ready_age_s_max": lane.get("source_ready_age_s_max"),
            "add_elapsed_s": lane.get("add_elapsed_s"),
            "cleanup_elapsed_s": lane.get("cleanup_elapsed_s"),
            "worker_idle_wait_s_total": lane.get("worker_idle_wait_s_total"),
            "content_fetch_status_counts_total": lane.get("content_fetch_status_counts_total"),
        }
        for lane in summary["lanes"]
    ],
}, indent=2))
'@ | python -
```

Expected: the output includes per-lane age, failure, add, cleanup, and idle metrics.

- [ ] **Step 4: Decide the next branch**

Use the gates above:

```text
if batch_2 source_ready_age_s_max <= 220 and command_failed ~= 0:
    keep age-capped shape as the current best branch
elif batch_2 source_ready_age_s_max rises above the cliff or command_failed returns:
    implement real notebook rotation / age guard before any other investigation
elif nlm_content_below_threshold dominates:
    investigate sparse-content handling next
elif auth elapsed time is a major wall-time share:
    investigate auth scheduling / refresh cadence next
else:
    record the run as informative but not the new ceiling
```

Stop if the run is invalidated, if the shared profile leaks, or if the benchmark root is dirty before soak starts.

---

## Task 2: Add A Real Age Guard Only If The Scaling Test Fails

**Files:**
- Modify: `P:\packages\yt-is\csf\nlm_batch.py`
- Test: `P:\packages\yt-is\tests\test_nlm_batch.py`

- [ ] **Step 1: Write the failing test**

Add a test that proves a notebook or source batch is rotated before the source age crosses the cliff. The test should exercise the age-guard decision boundary, not the old `NOT_FOUND` retry path.

```python
def test_age_guard_rotates_before_cliff(monkeypatch):
    # Arrange a batch with source_ready_age_s just above the configured cliff.
    # Assert the code chooses notebook rotation instead of another fetch attempt.
    ...
```

- [ ] **Step 2: Run the test and confirm the current code does not satisfy it**

Run:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
pytest P:\packages\yt-is\tests\test_nlm_batch.py -q -k age_guard_rotates_before_cliff
```

Expected: fail until the age guard exists.

- [ ] **Step 3: Implement the rotation boundary**

Implement the smallest notebook-rotation or source-refresh check that prevents source age from crossing the cliff during the full-load run.

- [ ] **Step 4: Re-run the focused test**

Run:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
pytest P:\packages\yt-is\tests\test_nlm_batch.py -q -k age_guard_rotates_before_cliff
```

Expected: pass.

- [ ] **Step 5: Re-run the full-load scaling benchmark**

Use the same `highest_vph_agecap_400_run01` shape under a fresh run root.

Decision: keep the guard only if it improves sustained VPH without increasing `command_failed` or breaking auth hygiene.

---

## Task 3: Measure Auth Cost Precisely

**Files:**
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\highest_vph_agecap_400_run01\sharded_lane_series_summary.json`
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\highest_vph_agecap_400_run01\soak\*\benchmark_summary.json`

- [ ] **Step 1: Sum actual auth elapsed time**

Use the worker traces and the `nlm_family_refresh_completed` events to compute real auth elapsed time per lane and per batch.

- [ ] **Step 2: Compare auth elapsed against wall time**

If auth elapsed is a major share of wall time, record auth as a real limiter. If it is not, stop blaming auth for the throughput gap.

- [ ] **Step 3: Branch accordingly**

If auth is a real limiter, the next investigation is auth cadence or refresh serialization.
If auth is not a real limiter, do not touch auth again until another branch proves it matters.

---

## Task 4: Investigate Sparse-Content Residuals Only After Age Is Stable

**Files:**
- Read: `P:\packages\yt-is\.logs\nlm_content_probe\run07_age_300_probe\probe_summary.json`
- Read: `P:\packages\yt-is\.logs\sharded_lane_series\sweep_phase3_2lane_3w_agecap_200_run02\sharded_lane_series_summary.json`

- [ ] **Step 1: Extract the residual `nlm_content_below_threshold` IDs**

Use the age-capped run artifacts to isolate the videos that still fail only on sparse content.

- [ ] **Step 2: Probe those IDs directly**

Run the targeted NotebookLM probe on the residual IDs and keep delayed retries if needed.

- [ ] **Step 3: Decide the sparse-content path**

If the residual IDs consistently remain below threshold, route them to fallback or separate handling.
If they recover on delay, tune the delay instead of the threshold.

---

## Task 5: Revisit Lane Geometry Only After The Mechanism Is Stable

**Files:**
- Read: `P:\packages\yt-is\docs\operations\sharded-lane-series.md`
- Read: `P:\packages\yt-is\docs\operations\test-registry.md`

- [ ] **Step 1: Compare only after the age / sparse-content branch is resolved**

Do not move to a third lane, worker-count changes, or stagger changes until the full-load branch has a stable interpretation.

- [ ] **Step 2: Re-evaluate the best lane shape**

Only then compare the current `4+4` guarded run, the age-capped run, and the historical `3+3` leader.

- [ ] **Step 3: Update registry and operations docs**

Record the new proven or negative outcome in:

- `P:\packages\yt-is\docs\operations\test-registry.md`
- `P:\packages\yt-is\docs\operations\sharded-lane-series.md`
- `P:\packages\yt-is\docs\operations\hot-path-throughput-next-test-plan.md`

---

## What Not To Do

- Do not rerun the same uncapped `3+3` shape again without a code change.
- Do not broaden retry markers until event-level evidence proves a missing marker.
- Do not treat the one-batch age-capped result as the ceiling without the full-load scaling test.
- Do not add a third lane before the per-lane age cliff is understood.
- Do not conflate `command_failed` with terminal video loss.

