# Hot-Path Throughput Optimization Series Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and run a controlled optimization series to test whether double-buffering, source-add hardening, direct-caption routing, profile sharding, or post-change packaging retuning can raise sustained hot-path `videos/hour`.

**Architecture:** Add one small orchestration layer for comparing pipeline modes before adding deeper changes. Keep every phase anchored to the current control shape: narrow/captioned cohort, `4` workers, `--batch-size 200`, and hot-path throughput with Whisper recovery reported separately.

**Tech Stack:** Python 3.14, PowerShell, `csf/breadth_series.py`, `csf/batch_size_series.py`, `csf/load_ladder.py`, `dev/worker_pool/worker_main.py`, `csf/nlm_batch.py`, `bin/csf-fallback-crossover-benchmark`, pytest, JSON benchmark summaries.

---

## File Structure

- Modify: `P:/packages/yt-is/csf/breadth_series.py`
  - add reusable pipeline mode support to benchmark command execution and aggregation
- Modify: `P:/packages/yt-is/tests/test_breadth_series.py`
  - cover serial vs double-buffered comparison metadata
- Modify: `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
  - record completed phase results
- Modify: `P:/packages/yt-is/docs/operations/test-registry.md`
  - mark phase outcomes as `proven`, `negative`, or `pending`

No direct Phase 1 change is expected in `P:/packages/yt-is/csf/load_ladder.py` or `P:/packages/yt-is/bin/csf-fallback-crossover-benchmark`; the pipeline-mode comparison is owned by `csf/breadth_series.py`.

Do not modify Whisper admission behavior as part of this plan. Do not count Whisper recovery in sustained throughput.

---

## Task 1: Add pipeline-mode support to the benchmark series

**Files:**
- Modify: `P:/packages/yt-is/csf/breadth_series.py`
- Test: `P:/packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Write the failing test**

Add a test proving that a phase run can carry a reusable pipeline mode into its benchmark command metadata.

Example test shape:

```python
def test_breadth_series_records_reusable_pipeline_mode(monkeypatch, tmp_path):
    calls = []

    def fake_run_benchmark(**kwargs):
        calls.append(kwargs)
        return {
            "tier": {"name": "narrow"},
            "workers": kwargs["workers"],
            "videos_per_hour": 4000.0,
            "hot_path_videos_per_hour": 4000.0,
            "aggregate": {
                "videos_per_hour": 4000.0,
                "hot_path_videos_per_hour": 4000.0,
                "hot_path_success_count_total": 200,
                "transcript_fallback_success_count_total": 0,
            },
            "benchmark_summary_path": str(tmp_path / "summary.json"),
        }

    monkeypatch.setattr(breadth_series, "_run_benchmark", fake_run_benchmark)
    report = breadth_series.run_breadth_series(
        trace_root=tmp_path,
        output_root=tmp_path / "out",
        phase_a_workers=4,
        phase_b_workers=(4,),
        batch_size=200,
        limit=200,
        tiers=(breadth_series.BreadthTier("narrow", "Narrow", "captioned", "breadth_narrow"),),
        reusable_pipeline_mode="double_buffered",
    )

    assert calls[0]["reusable_pipeline_mode"] == "double_buffered"
    assert report["reusable_pipeline_mode"] == "double_buffered"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k pipeline_mode
```

Expected: fails because `run_breadth_series()` does not accept or record `reusable_pipeline_mode`.

- [ ] **Step 3: Implement the minimal support**

Add a `reusable_pipeline_mode: str = "serial"` argument to:
- `run_breadth_series(...)`
- `_run_benchmark(...)`

Store it in the report:

```python
"reusable_pipeline_mode": reusable_pipeline_mode,
```

Pass it into the benchmark subprocess environment using `YTIS_REUSABLE_PIPELINE_MODE` only when the value is not `"serial"`.

- [ ] **Step 4: Verify the test passes**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k pipeline_mode
```

Expected: pass.

---

## Task 2: Preserve pipeline env through benchmark command execution

**Files:**
- Modify: `P:/packages/yt-is/csf/breadth_series.py`
- Test: `P:/packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Write the failing env propagation test**

Add this test in `test_breadth_series.py`:

```python
def test_run_benchmark_sets_double_buffered_env(monkeypatch, tmp_path):
    captured_env = {}
    summary_path = tmp_path / "run" / "benchmark_summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps({
            "batches": [{
                "policies": [{
                    "policy": breadth_series.DEFAULT_POLICY,
                    "results": [{
                        "hot_path_success_count": 10,
                        "transcript_fallback_success_count": 0,
                        "elapsed_s": 10.0,
                        "processed_count": 10,
                    }],
                }],
            }],
        }),
        encoding="utf-8",
    )

    def fake_run(command, cwd, env, check):
        captured_env.update(env)
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(breadth_series.subprocess, "run", fake_run)
    monkeypatch.setattr(breadth_series, "FALLBACK_BENCHMARK_SCRIPT", tmp_path / "bench.py")

    breadth_series._run_benchmark(
        batch_size=200,
        workers=4,
        limit=200,
        tier=breadth_series.BreadthTier("narrow", "Narrow", "captioned", "breadth_narrow"),
        trace_root=tmp_path,
        cohort_json=tmp_path / "cohort.json",
        output_root=summary_path.parent,
        source_url=breadth_series.DEFAULT_SOURCE_URL,
        policy=breadth_series.DEFAULT_POLICY,
        manifest_json=breadth_series.DEFAULT_MANIFEST_JSON,
        python_executable=None,
        reusable_pipeline_mode="double_buffered",
    )

    assert captured_env["YTIS_REUSABLE_PIPELINE_MODE"] == "double_buffered"
```

- [ ] **Step 2: Run the test and verify it fails**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k double_buffered_env
```

Expected: fails until `_run_benchmark()` sets the env.

- [ ] **Step 3: Implement env propagation**

Change the subprocess environment construction:

```python
env = os.environ.copy()
if reusable_pipeline_mode != "serial":
    env["YTIS_REUSABLE_PIPELINE_MODE"] = reusable_pipeline_mode
else:
    env.pop("YTIS_REUSABLE_PIPELINE_MODE", None)
proc = subprocess.run(command, cwd=str(REPO_ROOT), env=env, check=False)
```

- [ ] **Step 4: Verify tests**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q
python -m py_compile P:\packages\yt-is\csf\breadth_series.py
```

Expected: pass.

---

## Task 3: Add the serial-vs-double-buffered comparison runner

**Files:**
- Modify: `P:/packages/yt-is/csf/breadth_series.py`
- Test: `P:/packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Write the failing comparison test**

Add a function-level test for a helper named `run_pipeline_mode_comparison(...)`.

```python
def test_pipeline_mode_comparison_runs_serial_and_double_buffered(monkeypatch, tmp_path):
    modes = []

    def fake_run_breadth_series(**kwargs):
        mode = kwargs["reusable_pipeline_mode"]
        modes.append(mode)
        return {
            "reusable_pipeline_mode": mode,
            "phase_a": {
                "winner": {
                    "tier": "narrow",
                    "videos_per_hour": 3900.0 if mode == "serial" else 4100.0,
                    "hot_path_videos_per_hour": 3900.0 if mode == "serial" else 4100.0,
                    "transcript_fallback_success_count_total": 0,
                }
            },
            "phase_b": {"runs": []},
        }

    monkeypatch.setattr(breadth_series, "run_breadth_series", fake_run_breadth_series)
    report = breadth_series.run_pipeline_mode_comparison(
        trace_root=tmp_path,
        output_root=tmp_path / "comparison",
        modes=("serial", "double_buffered"),
        workers=4,
        batch_size=200,
        limit=200,
    )

    assert modes == ["serial", "double_buffered"]
    assert report["winner"]["reusable_pipeline_mode"] == "double_buffered"
    assert report["winner"]["videos_per_hour"] == 4100.0
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k pipeline_mode_comparison
```

Expected: fails because the helper does not exist.

- [ ] **Step 3: Implement the helper**

Implement a helper that:
- runs serial and double-buffered into separate output roots
- uses the same narrow/captioned tier
- uses `4` workers and `--batch-size 200`
- chooses the winner by `videos_per_hour`
- writes `pipeline_mode_comparison_summary.json`

The helper should return:

```python
{
    "generated_at": "2026-04-28T00:00:00Z",
    "metric_contract": "hot_path_videos_per_hour_excludes_whisper",
    "modes": ["serial", "double_buffered"],
    "runs": [{"reusable_pipeline_mode": "serial"}, {"reusable_pipeline_mode": "double_buffered"}],
    "winner": {"reusable_pipeline_mode": "double_buffered", "hot_path_videos_per_hour": 4100.0},
}
```

- [ ] **Step 4: Verify comparison tests**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k "pipeline_mode"
```

Expected: pass.

---

## Task 4: Add CLI support for the comparison

**Files:**
- Modify: `P:/packages/yt-is/csf/breadth_series.py`
- Modify: `P:/packages/yt-is/bin/csf-breadth-series`
- Test: `P:/packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Add CLI parser coverage**

Add a test for parsing:

```powershell
python P:\packages\yt-is\bin\csf-breadth-series --comparison pipeline-mode --pipeline-modes serial,double_buffered --workers 4 --batch-size 200
```

The test should assert that `main()` dispatches to `run_pipeline_mode_comparison(...)` with:
- `modes=("serial", "double_buffered")`
- `workers=4`
- `batch_size=200`

- [ ] **Step 2: Run the parser test and verify it fails**

Run:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q -k comparison_cli
```

Expected: fails because the CLI switch does not exist.

- [ ] **Step 3: Implement CLI arguments**

Add arguments:
- `--comparison`, choices: `breadth-scaling`, `pipeline-mode`, default: `breadth-scaling`
- `--pipeline-modes`, default: `serial,double_buffered`

When `--comparison pipeline-mode`, call `run_pipeline_mode_comparison(...)`.

- [ ] **Step 4: Verify CLI help**

Run:

```powershell
python P:\packages\yt-is\bin\csf-breadth-series --help
```

Expected: help text includes `--comparison` and `--pipeline-modes`.

---

## Task 5: Run Phase 1 live validation

**Files:**
- Output: `P:/packages/yt-is/.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json`

- [ ] **Step 1: Run the comparison**

Run:

```powershell
python P:\packages\yt-is\bin\csf-breadth-series `
  --comparison pipeline-mode `
  --pipeline-modes serial,double_buffered `
  --trace-root P:\packages\yt-is\.logs\worker_count_trials `
  --output-root P:\packages\yt-is\.logs\pipeline_mode_comparison_v1 `
  --workers 4 `
  --batch-size 200 `
  --limit 400
```

Expected:
- serial and double-buffered runs both complete
- `pipeline_mode_comparison_summary.json` exists
- each run reports hot-path vph and Whisper recovery separately

- [ ] **Step 2: Inspect the summary**

Run:

```powershell
Get-Content P:\packages\yt-is\.logs\pipeline_mode_comparison_v1\pipeline_mode_comparison_summary.json -Raw
```

Record:
- serial hot-path vph
- double-buffered hot-path vph
- success/failure counts
- source-add failure count
- `staging_overlap_elapsed_s_total`
- `stage_swap_count_total`

- [ ] **Step 3: Apply the decision rule**

Decision:
- If double-buffered beats serial with similar or better failure rate, keep it as the next control.
- If double-buffered loses only because source-add failures increase, move to Phase 2.
- If double-buffered loses without a clear failure-stage explanation, mark it negative and keep serial.

---

## Task 6: Document Phase 1 result

**Files:**
- Modify: `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:/packages/yt-is/docs/operations/test-registry.md`

- [ ] **Step 1: Update the run sheet**

Add a section under the throughput conclusions:

```markdown
### Pipeline Mode Comparison

- Artifact: `P:/packages/yt-is/.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json`
- Control: serial reusable path, `4` workers, `--batch-size 200`, narrow/captioned cohort
- Candidate: double-buffered reusable path
- Hot-path vph excludes Whisper recovery.
- Result: state the serial hot-path vph and double-buffered hot-path vph from `pipeline_mode_comparison_summary.json`; explicitly state that Whisper recovery is excluded
- Decision: `keep double-buffered`, `move to Phase 2 hardening`, or `keep serial`
```

- [ ] **Step 2: Update the test registry**

Add exactly one row based on the Phase 1 outcome.

If double-buffered wins with a stable failure rate:

```markdown
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort | proven | [.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json) | double-buffered beat serial on hot-path vph; Whisper excluded | reusable pipeline mode or worker scheduling changes |
```

If double-buffered loses without a source-add failure explanation:

```markdown
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort | negative | [.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json) | double-buffered did not beat serial on hot-path vph; Whisper excluded | reusable pipeline mode or worker scheduling changes |
```

If the run does not complete:

```markdown
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort | pending | [.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_v1/pipeline_mode_comparison_summary.json) | comparison did not complete; rerun Phase 1 after fixing the blocker | reusable pipeline mode or worker scheduling changes |
```

- [ ] **Step 3: Verify docs mention the artifact**

Run:

```powershell
rg -n "pipeline_mode_comparison_v1|double-buffered" P:\packages\yt-is\docs\operations
```

Expected:
- both docs reference the comparison result

---

## Task 7: Run Phase 2 hardening when Phase 1 shows source-add failures

**Files:**
- Modify only after Phase 1 shows source-add failures:
  - `P:/packages/yt-is/csf/nlm_batch.py`
  - `P:/packages/yt-is/dev/worker_pool/worker_main.py`
  - relevant tests

- [ ] **Step 1: Identify the failure signature**

Inspect the Phase 1 worker stderr/stdout and summary rows.

Run:

```powershell
rg -n "source_add_failed|NotebookSourceMaterializationTimeout|command_failed|Request access|auth" P:\packages\yt-is\.logs\pipeline_mode_comparison_v1
```

- [ ] **Step 2: Write a failing regression test for the exact failure**

Choose the smallest test file that owns the failure:
- `tests/test_nlm_batch.py` for NotebookLM add/materialization behavior
- `tests/test_dev_worker_pool.py` for worker summary/reporting behavior

- [ ] **Step 3: Implement the smallest hardening change**

Allowed hardening changes:
- preserve clear failure-stage metrics
- add bounded retry only for transient source-add failures
- add auth/profile readiness preflight before worker processing

Disallowed changes:
- unbounded retry loops
- retrying terminal failures
- counting fallback recovery in hot-path throughput

- [ ] **Step 4: Rerun Phase 1 after hardening**

Use a new output root:

```powershell
P:\packages\yt-is\.logs\pipeline_mode_comparison_v2
```

Apply the same decision rule as Task 5.

---

## Task 8: Defer larger bets until Phase 1 is classified

Only after Phase 1 is classified as `proven`, `negative`, or `needs-hardening`, open follow-up specs for:
- direct caption fast path
- profile sharding
- post-change benchmark batch-size retuning

Do not implement those in this plan. They change the operating model enough to need their own focused specs and tests.

---

## Verification Checklist

- [ ] Unit tests pass:

```powershell
python -m pytest P:\packages\yt-is\tests\test_breadth_series.py -q
```

- [ ] Existing double-buffered worker tests still pass:

```powershell
python -m pytest P:\packages\yt-is\tests\test_dev_worker_pool.py -q
python -m pytest P:\packages\yt-is\tests\test_nlm_batch.py -q -k double_buffered
```

- [ ] Modified files compile:

```powershell
python -m py_compile P:\packages\yt-is\csf\breadth_series.py P:\packages\yt-is\dev\worker_pool\worker_main.py P:\packages\yt-is\csf\nlm_batch.py
```

- [ ] Live Phase 1 comparison writes:

```powershell
P:\packages\yt-is\.logs\pipeline_mode_comparison_v1\pipeline_mode_comparison_summary.json
```

- [ ] Docs record the result and decision.

---

## Current Starting Point

The double-buffered worker path already has a smoke result:

- `P:/packages/yt-is/.logs/double_buffered_smoke4/result.json`
- `pipeline_strategy = double_buffered_reusable`
- `stage_swap_count_total = 1`
- `staging_overlap_elapsed_s_total = 173.56`

That smoke proves the path can execute, but it is not a throughput result because both live IDs failed source add. The first implementation task is therefore to make the benchmark harness run serial and double-buffered modes on the real winning throughput shape.
