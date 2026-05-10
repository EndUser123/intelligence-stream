# NotebookLM Fallback Crossover Benchmark Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine when NotebookLM should stop waiting and hand off to yt-dlp transcript/caption fallback so we capture all catchable videos with the highest successful-videos/hour rate.

**Architecture:** Freeze a failure-heavy sample from the latest worker-count traces, then run the same sample through a small set of fixed policies. Keep the worker count, 50-source notebook cap, and same-worker retry queue constant so the only variable is the wait-versus-fallback decision. Compare NotebookLM-only waiting, NotebookLM waiting plus fallback, and a longer NotebookLM wait on the same cohort. The decision metric is success-only throughput; the guardrail metrics are residual `yt-dlp=ok` recoveries, terminal classifications, and time to usable transcript.

**Tech Stack:** Python 3.11, PowerShell, NotebookLM CLI, `csf-worker-count-sweep`, `csf-source`, pytest, JSONL trace logs.

---

### Task 1: Freeze the evaluation cohort from a real trace

**Files:**
- Inspect: `P:\\\\\\packages/yt-is/.logs/worker_count_trials/20260424_180601/sweep_summary.json`
- Inspect: `P:\\\\\\packages/yt-is/.logs/worker_count_trials/20260424_180601/workers_02/logs/term_*.jsonl`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/cohort.json`

- [ ] **Step 1: Export the residual `yt-dlp=ok` failures**

Run:
```powershell
$runDir = "$CLAUDE_PLUGIN_ROOT/.logs\worker_count_trials\20260424_180601"
$cohortDir = "$CLAUDE_PLUGIN_ROOT/.logs\fallback_crossover_benchmark"
New-Item -ItemType Directory -Force -Path $cohortDir | Out-Null

python - <<'PY'
import json
from pathlib import Path

run_dir = Path(r"$CLAUDE_PLUGIN_ROOT/.logs\worker_count_trials\20260424_180601")
cohort_dir = Path(r"$CLAUDE_PLUGIN_ROOT/.logs\fallback_crossover_benchmark")
items = []
for path in run_dir.rglob("term_*.jsonl"):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("action") != "nlm_batch_source_content_fetch_completed":
                continue
            if row.get("youtube_ytdlp_classification") != "ok":
                continue
            items.append(
                {
                    "video_id": row.get("video_id"),
                    "source_url": row.get("source_url"),
                    "youtube_ytdlp_elapsed_s": row.get("youtube_ytdlp_elapsed_s"),
                    "youtube_page_elapsed_s": row.get("youtube_page_elapsed_s"),
                    "final_status": row.get("final_status"),
                }
            )

cohort_dir.mkdir(parents=True, exist_ok=True)
(cohort_dir / "cohort.json").write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")
print(f"wrote {len(items)} yt-dlp=ok items")
PY
```

Expected:
- a frozen JSON cohort containing the residual `yt-dlp=ok` items from the latest sweep
- enough items to run a meaningful comparison without re-deriving the sample later

- [ ] **Step 2: Add terminal controls from the same trace**

Create a second manifest section in `cohort.json` containing:
- `not_yet_live`
- `removed_by_owner`
- a small known-good control group

Expected:
- the benchmark sample can distinguish “terminal/unavailable” from “NotebookLM timing miss”
- the sample can also prove that the retry/fallback policy does not regress clean successes

- [ ] **Step 3: Verify the cohort is stable**

Run:
```powershell
Get-Content $CLAUDE_PLUGIN_ROOT/.logs\fallback_crossover_benchmark\cohort.json | ConvertFrom-Json | Select-Object -ExpandProperty items | Measure-Object
```

Expected:
- a non-trivial cohort size
- the sample is frozen and reproducible for the rest of the benchmark

---

### Task 2: Run the NotebookLM-only baseline on the frozen cohort

**Files:**
- Inspect: `P:\\\\\\packages/yt-is/csf/nlm_config.py`
- Inspect: `P:\\\\\\packages/yt-is/bin/csf-source`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_only/`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_only/sweep_summary.json`

- [ ] **Step 1: Set the NotebookLM wait policy**

Run:
```powershell
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_BUDGET_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED = "false"
$env:YTIS_TRANSCRIPT_FALLBACK_WORKERS = "0"
$env:YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S = "2.5"
```

Expected:
- NotebookLM gets the same 30-second retry window we already identified as the best throughput point
- fallback stays disabled so this run measures the pure NotebookLM path

- [ ] **Step 2: Run the baseline sweep**

Run:
```powershell
$env:PYTHONPATH = "P:\\\\\\packages\yt-is"
python $CLAUDE_PLUGIN_ROOT/bin\csf-worker-count-sweep --workers 2 --limit 80
```

Expected:
- one sweep summary for the frozen sample shape
- capture-only numbers for:
  - `success_count`
  - `fail_count`
  - `videos_per_hour`
  - `youtube_ytdlp_elapsed_s_total`
  - `youtube_ytdlp_elapsed_s_count`
  - `transcript_fallback_processed_count`

- [ ] **Step 3: Record the baseline decision inputs**

Capture from `sweep_summary.json`:
- total successes
- total fails
- success-only throughput
- how many failures were `yt-dlp=ok`
- how many failures were terminal (`not_yet_live`, `removed_by_owner`)

Expected:
- the baseline is the reference point for the fallback comparison

---

### Task 3: Run the NotebookLM-plus-fallback variant on the same frozen cohort

**Files:**
- Inspect: `P:\\\\\\packages/yt-is/bin/csf-source`
- Inspect: `P:\\\\\\packages/yt-is/csf/nlm_batch.py`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_plus_fallback/`
- Create: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_plus_fallback/sweep_summary.json`

- [ ] **Step 1: Keep the NotebookLM retry window fixed**

Run:
```powershell
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_BUDGET_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S = "30"
$env:YTIS_NLM_SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED = "false"
```

Expected:
- the NotebookLM hot path does not change between the baseline and fallback runs
- only the fallback branch changes

- [ ] **Step 2: Enable the transcript fallback workers**

Run:
```powershell
$env:YTIS_TRANSCRIPT_FALLBACK_WORKERS = "2"
$env:YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S = "0"
```

Expected:
- the fallback path can start immediately once the NotebookLM window is exhausted
- the benchmark measures the actual crossover instead of adding artificial delay

- [ ] **Step 3: Run the fallback-enabled sweep**

Run:
```powershell
$env:PYTHONPATH = "P:\\\\\\packages\yt-is"
python $CLAUDE_PLUGIN_ROOT/bin\csf-worker-count-sweep --workers 2 --limit 80
```

Expected:
- the same cohort shape as the baseline
- a measurable `transcript_fallback_processed_count`
- success-only throughput and total elapsed time for the fallback variant

- [ ] **Step 4: Verify the fallback path is actually being used**

Check the final summary for:
- `transcript_fallback_processed_count > 0`
- `youtube_ytdlp_elapsed_s_count > 0`
- `youtube_page_elapsed_s_count` only if the direct fallback classifier was needed

Expected:
- this run proves whether fallback recovers items that NotebookLM would otherwise keep waiting on

---

### Task 4: Compare the two policies and choose the crossover point

**Files:**
- Inspect: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_only/sweep_summary.json`
- Inspect: `P:\\\\\\packages/yt-is/.logs/fallback_crossover_benchmark/notebooklm_plus_fallback/sweep_summary.json`
- Update: `P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Update: `P:\\\\\\packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`

- [ ] **Step 1: Compare success-only throughput**

Compare:
- `videos_per_hour`
- `success_count`
- `fail_count`
- `elapsed_s`

Expected:
- the winner is the policy that maximizes successful transcript downloads per hour on the frozen sample

- [ ] **Step 2: Compare coverage**

Compare:
- number of `yt-dlp=ok` items recovered by fallback
- number of terminal items left unchanged
- number of items still requiring more NotebookLM time

Expected:
- fallback should only be promoted if it increases capture rate without collapsing throughput

- [ ] **Step 3: Decide the operational threshold**

If fallback wins:
- define a hard NotebookLM wait cutoff, likely at the current `30s` budget
- allow immediate fallback for residual `yt-dlp=ok` items

If longer waiting wins:
- raise the NotebookLM budget only to the measured crossover point
- keep the same-worker retry queue as the default

If neither wins:
- keep NotebookLM as the main path and restrict fallback to terminal/deferred cases only

Expected:
- a single explicit operating policy instead of an open-ended “wait longer forever” rule

---

### Task 5: Document the final benchmark result

**Files:**
- Modify: `P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:\\\\\\packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`
- Optional: `P:\\\\\\packages/yt-is/docs/superpowers/plans/2026-04-24-fallback-crossover-benchmark.md`

- [ ] **Step 1: Write the result in plain language**

Include:
- the baseline policy
- the fallback policy
- the crossover threshold, if one exists
- any terminal buckets that should never consume NotebookLM wait time again

Expected:
- the next operator does not have to reconstruct the decision from logs

- [ ] **Step 2: Preserve the sample provenance**

Include:
- source run directory
- frozen cohort file path
- worker count
- NotebookLM retry budget
- fallback worker settings

Expected:
- the benchmark is repeatable later without guessing which sample was used

- [ ] **Step 3: Keep the 50-source cap explicit**

State clearly:
- this benchmark does not change the 50-source notebook cap
- any fallback policy must still respect the notebook source cap and the current worker-notebook reuse model

Expected:
- no one confuses fallback policy with notebook-cap policy

---

## Self-Review

**Spec coverage:** This plan freezes a real sample, compares NotebookLM-only waiting against NotebookLM-plus-fallback on the same cohort, and records the crossover decision in the repo docs.

**Placeholder scan:** No TBD or vague “add appropriate handling” language remains.

**Type consistency:** The plan uses the actual runtime knobs and command surfaces already present in the repo:
- `YTIS_NLM_SOURCE_CONTENT_RETRY_BUDGET_S`
- `YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_DELAY_S`
- `YTIS_NLM_SOURCE_CONTENT_RETRY_QUEUE_BUDGET_S`
- `YTIS_NLM_SOURCE_CONTENT_SHARED_RETRY_POOL_ENABLED`
- `YTIS_TRANSCRIPT_FALLBACK_WORKERS`
- `YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S`
- `python $CLAUDE_PLUGIN_ROOT/bin\csf-worker-count-sweep --workers 2 --limit 80`

