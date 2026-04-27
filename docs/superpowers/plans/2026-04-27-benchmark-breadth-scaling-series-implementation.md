# Benchmark Breadth And Scaling Series Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement one combined benchmark series that proves whether queue breadth or worker count is the real driver of sustained hot-path `videos/hour`, while keeping Whisper recovery separate from the throughput metric.

**Architecture:** Add a small orchestration layer that runs the same frozen benchmark family in two phases. Phase A compares breadth tiers at a fixed worker count and fixed batch size. Phase B takes the winning breadth tier and sweeps worker counts `2, 4, 6, 8, 10`. The existing hot-path accounting stays unchanged: Whisper recovery is recorded separately and never folded into sustained `videos/hour`.

**Tech Stack:** Python 3.14, `csf-source`, `csf-fallback-crossover-benchmark`, `csf-load-ladder`, JSON summaries, pytest, the shared benchmark manifest, NotebookLM worker-state roots.

---

## File Structure

The plan keeps the new code small and focused:

- `P:/packages/yt-is/csf/load_ladder.py`
  - extend the benchmark command builder so the breadth-series runner can pass manifest family filters without duplicating subprocess construction
- `P:/packages/yt-is/csf/breadth_series.py`
  - new orchestration module for the breadth proof and worker-scaling sweep
- `P:/packages/yt-is/bin/csf-breadth-series`
  - new CLI entrypoint for the combined series
- `P:/packages/yt-is/tests/test_load_ladder.py`
  - cover the extended command builder
- `P:/packages/yt-is/tests/test_breadth_series.py`
  - cover breadth selection, phase ordering, hot-path accounting, and summary shape
- `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
  - document the final combined result after it runs
- `P:/packages/yt-is/docs/operations/test-registry.md`
  - mark the new breadth cases as proven or negative after the run

---

### Task 1: Extend the command builder for manifest family selection

**Files:**
- Modify: `P:/packages/yt-is/csf/load_ladder.py`
- Modify: `P:/packages/yt-is/tests/test_load_ladder.py`

- [ ] **Step 1: Write the failing test**

```python
def test_command_builder_includes_manifest_selection(tmp_path):
    command = build_fallback_benchmark_command(
        python_executable="python",
        fallback_benchmark_script=Path("P:/packages/yt-is/bin/csf-fallback-crossover-benchmark"),
        trace_root=Path("P:/packages/yt-is/.logs/worker_count_trials"),
        cohort_json=Path("P:/packages/yt-is/.logs/breadth_series/cohort.json"),
        output_root=Path("P:/packages/yt-is/.logs/breadth_series/broad"),
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        workers=2,
        limit=400,
        batch_size=200,
        policy="notebooklm_only_30s",
        cohort_shape="manifest",
        sample_label="breadth_broad",
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
        manifest_families="routing,hot_path_control",
        worker_state_root=tmp_path / "worker_states",
        preserve_worker_state_root=False,
    )
    assert "--manifest-json" in command
    assert "shared_benchmark_manifest.json" in command
    assert "--manifest-families" in command
    assert "routing,hot_path_control" in command
```

- [ ] **Step 2: Run the test and confirm it fails**

Run:
```powershell
python -m pytest tests/test_load_ladder.py -q
```
Expected: the new test fails because `build_fallback_benchmark_command()` does not yet accept `manifest_json` or `manifest_families`.

- [ ] **Step 3: Implement the minimal code**

Update `build_fallback_benchmark_command()` so it accepts `manifest_json: Path | None = None` and `manifest_families: str | None = None`, and append the corresponding CLI flags only when they are provided:

```python
    if manifest_json is not None:
        command.extend(["--manifest-json", str(manifest_json)])
    if manifest_families is not None:
        command.extend(["--manifest-families", manifest_families])
```

Keep the existing `source_url`, `cohort_shape`, `sample_label`, and worker-state arguments unchanged.

- [ ] **Step 4: Run the test and confirm it passes**

Run:
```powershell
python -m pytest tests/test_load_ladder.py -q
```
Expected: pass, including the new manifest-selection assertion.

- [ ] **Step 5: Commit**

```bash
git add P:/packages/yt-is/csf/load_ladder.py P:/packages/yt-is/tests/test_load_ladder.py
git commit -m "feat: carry manifest selection through ladder command builder"
```

---

### Task 2: Add the breadth-series orchestration module and CLI

**Files:**
- Create: `P:/packages/yt-is/csf/breadth_series.py`
- Create: `P:/packages/yt-is/bin/csf-breadth-series`
- Modify: `P:/packages/yt-is/tests/test_breadth_series.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_selects_best_breadth_tier_by_hot_path_vph():
    phase_a_rows = [
        {"tier": "broad", "videos_per_hour": 2466.87, "worker_idle_wait_s": 120.0},
        {"tier": "mid", "videos_per_hour": 1800.0, "worker_idle_wait_s": 80.0},
        {"tier": "narrow", "videos_per_hour": 327.7, "worker_idle_wait_s": 2239.2},
    ]
    assert choose_best_breadth_tier(phase_a_rows)["tier"] == "broad"


def test_builds_two_phase_series_plan():
    plan = build_breadth_series_plan(
        phase_a_workers=2,
        phase_b_workers=(2, 4, 6, 8, 10),
        batch_size=200,
        limit=400,
    )
    assert [phase["name"] for phase in plan["phases"]] == ["breadth", "scaling"]
    assert plan["phases"][0]["workers"] == 2
    assert plan["phases"][1]["worker_counts"] == [2, 4, 6, 8, 10]
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:
```powershell
python -m pytest tests/test_breadth_series.py -q
```
Expected: fail because the new module and helper functions do not exist yet.

- [ ] **Step 3: Implement the orchestration module**

Create `csf/breadth_series.py` with the following responsibilities:

```python
@dataclass(frozen=True, slots=True)
class BreadthTier:
    name: str
    description: str
    cohort_shape: str
    sample_label: str
    manifest_families: str | None = None


def choose_best_breadth_tier(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda row: float(row.get("videos_per_hour", 0) or 0.0))


def build_breadth_series_plan(
    *,
    phase_a_workers: int,
    phase_b_workers: tuple[int, ...],
    batch_size: int,
    limit: int,
) -> dict[str, Any]:
    ...
```

The module should:
- run Phase A breadth tiers with a fixed worker count and a fixed batch size
- choose the best tier by hot-path `videos/hour`
- run Phase B worker sweeps only on that winner
- keep Whisper recovery in the summary, but never add it into hot-path `videos/hour`
- write one combined summary JSON for the whole series

Use the existing benchmark runner and command-builder helpers instead of shelling out by hand in multiple places.

- [ ] **Step 4: Add the CLI wrapper**

`bin/csf-breadth-series` should parse:
- `--output-root`
- `--trace-root`
- `--cohort-json`
- `--phase-a-workers`
- `--phase-b-workers`
- `--limit`
- `--batch-size`
- `--policy`
- `--source-url`
- `--cohort-shape-broad`
- `--cohort-shape-mid`
- `--cohort-shape-narrow`
- `--manifest-json`
- `--broad-manifest-families`
- `--mid-manifest-families`
- `--narrow-manifest-families`

The wrapper should call `run_breadth_series()` and print the path to the combined summary.

- [ ] **Step 5: Run the tests and confirm they pass**

Run:
```powershell
python -m pytest tests/test_breadth_series.py -q
python -m py_compile P:/packages/yt-is/csf/breadth_series.py P:/packages/yt-is/bin/csf-breadth-series
```
Expected: tests pass and the new module compiles.

- [ ] **Step 6: Commit**

```bash
git add P:/packages/yt-is/csf/breadth_series.py P:/packages/yt-is/bin/csf-breadth-series P:/packages/yt-is/tests/test_breadth_series.py
git commit -m "feat: add breadth scaling benchmark series"
```

---

### Task 3: Run the combined breadth series and lock the result into the docs

**Files:**
- Modify: `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:/packages/yt-is/docs/operations/test-registry.md`

- [ ] **Step 1: Run the breadth proof**

Run the combined series with the fixed hot-path accounting and the fixed batch size:

```powershell
python P:/packages/yt-is/bin/csf-breadth-series `
  --output-root P:/packages/yt-is/.logs/breadth_scaling_series `
  --trace-root P:/packages/yt-is/.logs/worker_count_trials `
  --cohort-json P:/packages/yt-is/.logs/breadth_scaling_series/cohort.json `
  --phase-a-workers 2 `
  --phase-b-workers 2,4,6,8,10 `
  --limit 400 `
  --batch-size 200 `
  --policy notebooklm_only_30s `
  --source-url https://www.youtube.com/channel/UCYTISFALLBACKBMK `
  --cohort-shape-broad mixed `
  --cohort-shape-mid manifest `
  --cohort-shape-narrow manifest `
  --manifest-json P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json `
  --broad-manifest-families routing,hot_path_control `
  --mid-manifest-families routing,whisper_admission,hot_path_control `
  --narrow-manifest-families hot_path_control
```

Expected:
- one summary JSON for Phase A
- one summary JSON for Phase B
- one combined report that identifies the winning breadth tier by hot-path `videos/hour`
- Whisper recovery reported separately and not folded into hot-path throughput

- [ ] **Step 2: Update the run sheet with the result**

Record:
- the breadth tiers that were compared
- the winning tier
- the worker sweep on the winning tier
- the hot-path-only `videos/hour` conclusion
- the separate Whisper recovery note

- [ ] **Step 3: Update the test registry**

Add entries for:
- the broad breadth tier
- the mid breadth tier
- the narrow breadth tier
- the Phase B worker sweep on the winner

Use `proven` or `negative` status and name the code path that would justify rerunning the series.

- [ ] **Step 4: Commit**

```bash
git add P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md P:/packages/yt-is/docs/operations/test-registry.md
git commit -m "docs: record breadth scaling benchmark series results"
```

---

## Self-Review

Coverage check:
- the breadth comparison is covered by Task 2 and Task 3
- the worker-count scaling on the winner breadth is covered by Task 2 and Task 3
- hot-path-only accounting is preserved in Task 2 and verified in Task 3
- Whisper recovery stays separate throughout the series
- the docs and registry are updated only after the results exist

Placeholder scan:
- no TBD / TODO / placeholder text
- no references to undefined helper names in later tasks

Type consistency:
- `BreadthTier`, `choose_best_breadth_tier()`, and `build_breadth_series_plan()` are introduced before they are used in later tasks
- the CLI options match the orchestration inputs called out in the module task

