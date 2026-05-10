# Double-Buffered Reusable Notebook Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the double-buffered reusable NotebookLM pipeline from the approved design, verify that it improves sustained hot-path `videos/hour` on the same fixed cohort, and keep Whisper recovery separate from sustained throughput accounting.

**Architecture:** Keep the current serial reusable pipeline as the fallback control. Add one bounded double-buffered wrapper that overlaps staging for batch `N+1` with extraction for batch `N`. Do not expand into arbitrary pipeline parallelism unless the double-buffered shape proves out.

**Tech Stack:** Python 3.14, `csf/nlm_batch.py`, `csf/nlm_config.py`, `csf/load_ladder.py`, `bin/csf-fallback-crossover-benchmark`, `bin/csf-breadth-series`, JSON summaries, pytest.

---

## File Structure

The implementation should stay small and focused:

- `P:\\\\\\packages/yt-is/csf/nlm_batch.py`
  - add the double-buffered reusable pipeline wrapper and the stage metrics it needs
- `P:\\\\\\packages/yt-is/csf/nlm_config.py`
  - add one config flag only if the wrapper needs a runtime knob
- `P:\\\\\\packages/yt-is/csf/load_ladder.py`
  - carry a comparison mode through the benchmark command builder if needed
- `P:\\\\\\packages/yt-is/bin/csf-breadth-series`
  - add a switch so the breadth/scaling runner can compare serial vs double-buffered reusable paths
- `P:\\\\\\packages/yt-is/bin/csf-fallback-crossover-benchmark`
  - keep the existing serial reusable path available for direct comparison
- `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`
  - cover the new wrapper and the stage-swap / fallback behavior
- `P:\\\\\\packages/yt-is/tests/test_nlm_config.py`
  - cover any new config default if one is introduced
- `P:\\\\\\packages/yt-is/tests/test_breadth_series.py`
  - cover the comparison shape if the series runner is extended
- `P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
  - record the benchmark result after the run
- `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`
  - mark the comparison as proven or negative after the run

---

## Task 1: Write the failing tests for the double-buffered wrapper

**Files:**
- Modify: `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`

- [ ] **Step 1: Add a focused test for stage swapping**

The test should verify the wrapper can:
- keep an active notebook and a staging notebook
- advance the stage role after extraction
- retain correctness if the staging notebook is not ready yet

Example shape:

```python
def test_double_buffered_reusable_ingestor_swaps_stages():
    wrapper = DoubleBufferedReusableIngestor(batch_size=50)
    assert wrapper.prepare()
    result = wrapper.process_batch(["vid1", "vid2", "vid3"])
    assert result
    metrics = wrapper.get_last_process_metrics()
    assert metrics["stage_swap_count"] >= 1
    assert "staging_overlap_elapsed_s" in metrics
```

- [ ] **Step 2: Add a fallback-to-serial test**

Verify the wrapper does not fail the batch if staging cannot be prepared:

```python
def test_double_buffered_reusable_ingestor_falls_back_when_staging_fails(monkeypatch):
    wrapper = DoubleBufferedReusableIngestor(batch_size=50)
    monkeypatch.setattr(wrapper, "_prepare_staging_notebook", lambda *a, **k: False)
    assert wrapper.process_batch(["vid1"])  # should still complete serially
```

- [ ] **Step 3: Run the test file and confirm it fails**

Run:
```powershell
python -m pytest tests/test_nlm_batch.py -q -k "double_buffered"
```

Expected:
- the tests fail initially because the wrapper does not exist yet

---

## Task 2: Implement the bounded double-buffered reusable wrapper

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/nlm_batch.py`

- [ ] **Step 1: Extract the reusable serial path as the fallback control**

Before adding the new wrapper, make sure the current serial reusable path remains callable from one internal helper so the new wrapper can fall back to it without duplicating the whole process body.

The helper should:
- accept the same `video_ids`
- use the current reusable notebook state
- return the same result mapping and metrics shape

- [ ] **Step 2: Add the new wrapper**

Introduce a bounded orchestration wrapper, for example:

```python
class NLMDoubleBufferedReusableIngestor:
    def __init__(self, batch_size: int = DEFAULT_NOTEBOOKLM_BATCH_SIZE):
        ...
```

Responsibilities:
- maintain `active` and `staging` notebook slots
- preserve the current reusable state file behavior
- overlap staging for the next batch while the active notebook is extracting the current batch
- fall back to the serial reusable path if staging is unavailable

Implementation constraints:
- do not change NotebookLM source-cap assumptions
- do not overlap mutating operations on the same notebook
- keep cleanup deterministic and bounded
- keep Whisper accounting separate from hot-path throughput

- [ ] **Step 3: Emit overlap metrics**

Add the new metrics required by the design:
- `staging_overlap_elapsed_s`
- `staging_wait_elapsed_s`
- `stage_swap_count`

Keep the existing batch metrics unchanged.

- [ ] **Step 4: Expose the wrapper behind a small switch**

Add the smallest possible selector so the series runner can compare:
- current serial reusable path
- new double-buffered reusable path

The selector should default to the existing serial path unless the benchmark explicitly asks for the new one.

- [ ] **Step 5: Run targeted tests and confirm they pass**

Run:
```powershell
python -m pytest tests/test_nlm_batch.py -q -k "double_buffered"
python -m py_compile P:\\\\\\packages/yt-is/csf/nlm_batch.py
```

Expected:
- the new wrapper tests pass
- the module compiles

---

## Task 3: Wire the comparison into the benchmark series

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/breadth_series.py`
- Modify: `P:\\\\\\packages/yt-is/bin/csf-breadth-series`
- Modify: `P:\\\\\\packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Add a comparison-mode test**

Add a test that proves the series can compare serial vs double-buffered reusable paths on the same fixed cohort and batch size.

Expected assertions:
- the same cohort is used for both modes
- Whisper recovery is still kept separate
- the summary records both modes distinctly

- [ ] **Step 2: Update the series runner**

Add a `--reusable-mode` or equivalent selector with at least:
- `serial`
- `double_buffered`

The breadth/scaling harness should be able to run the same fixed control family through both modes and preserve separate hot-path metrics.

- [ ] **Step 3: Keep the winner selection unchanged**

The series should still choose the winning breadth tier the same way as before. The only new variable is the reusable pipeline shape.

- [ ] **Step 4: Run the tests and confirm they pass**

Run:
```powershell
python -m pytest tests/test_breadth_series.py -q
python -m py_compile P:\\\\\\packages/yt-is/csf/breadth_series.py P:\\\\\\packages/yt-is/bin/csf-breadth-series
```

Expected:
- the comparison-mode tests pass
- the new runner compiles

---

## Task 4: Run the validation benchmark and record the result

**Files:**
- Modify: `P:\\\\\\packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`

- [ ] **Step 1: Run the fixed validation cohort**

Use the already-proven control shape:
- narrow/captioned cohort
- `200` benchmark batch size
- `4` workers

Run both reusable modes:
- serial
- double-buffered

Do not count Whisper recovery in sustained `videos/hour`.

- [ ] **Step 2: Compare the results**

Record:
- `hot_path_success_count`
- `videos_per_hour`
- `worker_idle_wait_s`
- `add_elapsed_s`
- `readiness_elapsed_s`
- `extract_elapsed_s`
- `cleanup_elapsed_s`
- `staging_overlap_elapsed_s`
- `staging_wait_elapsed_s`
- `stage_swap_count`

Decision rule:
- keep the double-buffered path only if it improves sustained hot-path `videos/hour` on the same cohort
- otherwise keep the serial reusable path as the control

- [ ] **Step 3: Update the docs**

Add a concise conclusion to the run sheet and registry so future runs do not re-evaluate the same stage-shape comparison under a different name.

---

## Success Criteria

- The new wrapper is covered by focused tests.
- The benchmark runner can compare serial and double-buffered reusable modes on the same control cohort.
- Whisper recovery remains separate from sustained hot-path `videos/hour`.
- The final docs state clearly whether the double-buffered path won or lost.

## Failure Criteria

- The wrapper changes correctness or notebook reuse behavior.
- The new overlap metrics show the pipeline is still effectively serial.
- The double-buffered path does not improve sustained hot-path throughput.

