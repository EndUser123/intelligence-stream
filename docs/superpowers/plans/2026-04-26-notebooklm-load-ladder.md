# NotebookLM Load Ladder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a benchmark runner that can execute the next NotebookLM throughput experiments back-to-back, with explicit control over notebook fullness, notebook reuse, staggered access, routing split, and rotation thresholds.

**Architecture:** Keep the current `yt-is` fetch code unchanged where possible, but add one small configuration surface for worker jitter so staggered-access testing can be controlled from the environment. Build a thin ladder runner around the existing sweep machinery instead of replacing it: the runner should run a fixed sequence of scenarios, preserve per-scenario artifacts, and emit one combined JSON summary so results can be compared without manual cleanup between passes.

**Tech Stack:** Python 3.14, existing `csf` package, existing `bin/csf-source` sweep entrypoint, pytest, JSON/CSV log artifacts.

---

### Task 1: Add env-controlled stagger knobs

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/nlm_config.py`
- Modify: `P:\\\\\\packages/yt-is/csf/transcript.py`
- Modify: `P:\\\\\\packages/yt-is/csf/batch.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_config.py`

- [ ] **Step 1: Write the failing test**

Add a config test that proves `get_nlm_config()` reads `YTIS_TRANSCRIPT_WORKER_JITTER_MIN_S` and `YTIS_TRANSCRIPT_WORKER_JITTER_MAX_S`, and that the transcript/batch paths use the same values instead of hardcoded jitter bounds.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py -q`

Expected: FAIL because the new jitter fields do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add two frozen config fields:
- `transcript_worker_jitter_min_s`
- `transcript_worker_jitter_max_s`

Read them from:
- `YTIS_TRANSCRIPT_WORKER_JITTER_MIN_S`
- `YTIS_TRANSCRIPT_WORKER_JITTER_MAX_S`

Wire the transcript and batch round-robin sleep calls to those config values instead of fixed module constants.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py -q`

Expected: PASS with the new jitter fields and no regression in existing config defaults.

- [ ] **Step 5: Commit**

```bash
git add P:\\\\\\packages/yt-is/csf/nlm_config.py P:\\\\\\packages/yt-is/csf/transcript.py P:\\\\\\packages/yt-is/csf/batch.py P:\\\\\\packages/yt-is/tests/test_nlm_config.py
git commit -m "feat: make worker jitter configurable for stagger tests"
```

### Task 2: Add a load-ladder benchmark runner

**Files:**
- Create: `P:\\\\\\packages/yt-is/bin/csf-load-ladder`
- Create: `P:\\\\\\packages/yt-is/csf/load_ladder.py`
- Modify: `P:\\\\\\packages/yt-is/bin/csf-fallback-crossover-benchmark`
- Test: `P:\\\\\\packages/yt-is/tests/test_load_ladder.py`

- [ ] **Step 1: Write the failing test**

Add a unit test that asserts the ladder builder returns the expected ordered scenarios:
- baseline
- fullness_25
- fresh_state
- reuse_state
- staggered_off
- staggered_on
- rotation_75

The test should also verify that each scenario includes:
- a human-readable label
- an env override map
- an artifact output subdirectory name
- a short note describing what the scenario is trying to prove

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_load_ladder.py -q`

Expected: FAIL because the ladder helper and runner do not exist yet.

- [ ] **Step 3: Write minimal implementation**

Create a small helper module that defines the scenario list and a thin CLI wrapper that:
- loads the same frozen cohort pattern used by `csf-fallback-crossover-benchmark`
- runs the notebook-state scenarios in order without manual intervention
- preserves or resets the shared worker-state root depending on the scenario
- writes one combined `benchmark_summary.json`
- keeps the current `run_worker_count_sweep` machinery for the actual throughput runs

The routing-split follow-up should stay out of this phase until we have a cohort labeled for that experiment.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_load_ladder.py -q`

Expected: PASS with stable scenario ordering and env merge behavior.

- [ ] **Step 5: Commit**

```bash
git add P:\\\\\\packages/yt-is/bin/csf-load-ladder P:\\\\\\packages/yt-is/csf/load_ladder.py P:\\\\\\packages/yt-is/tests/test_load_ladder.py
git commit -m "feat: add notebooklm benchmark load ladder"
```

### Task 3: Document the new benchmark sequence

**Files:**
- Modify: `P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:\\\\\\packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`

- [ ] **Step 1: Update the run sheet**

Document the recommended execution order:
- notebook fullness
- notebook reuse vs fresh notebook
- staggered access
- routing split
- rotation threshold

Include the key interpretation rule:
- if retry/fallback does not change the no-caption cohort outcome, stop tuning retry and move to notebook-state or routing-shape tests.

- [ ] **Step 2: Update the handoff**

Add the new CLI invocation and the exact artifacts the next agent should inspect first:
- `benchmark_summary.json`
- per-scenario `sweep_summary.json`
- per-scenario `stdout.txt` and `term_*.jsonl`

- [ ] **Step 3: Commit**

```bash
git add P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md P:\\\\\\packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md
git commit -m "docs: add notebooklm benchmark load ladder"
```

### Task 4: Validate the ladder end-to-end

**Files:**
- Test: `P:\\\\\\packages/yt-is/tests/test_nlm_config.py`
- Test: `P:\\\\\\packages/yt-is/tests/test_load_ladder.py`

- [ ] **Step 1: Run the focused unit tests**

Run:
`python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_nlm_config.py $CLAUDE_PLUGIN_ROOT/tests\test_load_ladder.py -q`

Expected: PASS.

- [ ] **Step 2: Run a dry benchmark on the current no-caption cohort**

Run:
`python $CLAUDE_PLUGIN_ROOT/bin\csf-load-ladder --limit 10 --workers 2`

Expected: per-scenario summaries for the first 10-item cohort slice, with the retry-only policy still showing the current no-caption failure pattern and the stagger/reuse/fullness comparisons writing clean artifacts.

- [ ] **Step 3: Confirm the output**

Verify that the combined summary includes:
- scenario name
- worker count
- elapsed time
- success/fail counts
- `worker_idle_wait_s`
- `source_ready_age_s_*`

Only after those artifacts look sane should we spend time on the source-shape routing follow-on.
