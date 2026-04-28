# Shared Benchmark Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create one canonical benchmark manifest and wire the benchmark runner and tests to consume it so routing, Whisper admission, fallback recovery, and hot-path controls stop being hand-curated in separate cohorts.

**Architecture:** Add a small manifest loader/validator in `csf`, store the canonical cases in one committed JSON fixture, and teach `csf-fallback-crossover-benchmark` to load live-trace cases from that manifest for proof runs. Keep the existing trace-based cohort builder for legacy paths so we can migrate without breaking the current benchmarks. Unit tests will cover the manifest schema, case filtering, and the runner's manifest-backed cohort loading.

**Tech Stack:** Python 3.14, JSON, `csf-fallback-crossover-benchmark`, `csf.transcript`, pytest.

---

### Task 1: Add the shared manifest loader and fixture

**Files:**
- Create: `P:/packages/yt-is/csf/benchmark_manifest.py`
- Create: `P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json`
- Create: `P:/packages/yt-is/tests/test_benchmark_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_manifest_filters_live_trace_cases_only():
    manifest = load_benchmark_manifest(Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"))
    live_ids = [case.case_id for case in manifest.cases_for_benchmark()]
    assert "whisper-skip-music-001" in live_ids
    assert "whisper-recover-001" in live_ids
    assert "whisper-admit-live-001" not in live_ids
```

```python
def test_manifest_rejects_duplicate_case_ids(tmp_path):
    manifest_path = tmp_path / "dup.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "generated_at": "2026-04-26T00:00:00Z",
                "cases": [
                    {"case_id": "dup", "family": "routing", "source_type": "live_trace", "video_id": "dQw4w9WgXcQ", "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "A", "description": "", "duration": 0, "privacy_status": "public", "upload_status": "", "is_live_content": False, "unavailable_reason": None, "has_captions": True, "expected": {"hot_path": True, "route_to_fallback": False, "attempt_whisper": False, "skip_whisper": False, "recover_success": False, "terminal_skip": False}},
                    {"case_id": "dup", "family": "routing", "source_type": "live_trace", "video_id": "dQw4w9WgXcQ", "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "B", "description": "", "duration": 0, "privacy_status": "public", "upload_status": "", "is_live_content": False, "unavailable_reason": None, "has_captions": True, "expected": {"hot_path": True, "route_to_fallback": False, "attempt_whisper": False, "skip_whisper": False, "recover_success": False, "terminal_skip": False}},
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate case_id"):
        load_benchmark_manifest(manifest_path)
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest P:\packages\yt-is\tests\test_benchmark_manifest.py -q`
Expected: fail because the loader module and fixture do not exist yet.

- [ ] **Step 3: Implement the loader and fixture**

```python
@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    family: str
    source_type: str
    video_id: str
    source_url: str
    title: str | None
    description: str | None
    duration: int | None
    privacy_status: str | None
    upload_status: str | None
    is_live_content: bool
    unavailable_reason: str | None
    has_captions: bool
    expected: dict[str, bool]

@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    manifest_version: int
    generated_at: str
    cases: tuple[BenchmarkCase, ...]

    def cases_for_benchmark(self) -> tuple[BenchmarkCase, ...]:
        return tuple(case for case in self.cases if case.source_type == "live_trace")
```

- [ ] **Step 4: Run the test again and confirm it passes**

Run: `python -m pytest P:\packages\yt-is\tests\test_benchmark_manifest.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add P:/packages/yt-is/csf/benchmark_manifest.py P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json P:/packages/yt-is/tests/test_benchmark_manifest.py
git commit -m "feat: add shared benchmark manifest"
```

### Task 2: Teach the benchmark runner to consume the manifest

**Files:**
- Modify: `P:/packages/yt-is/bin/csf-fallback-crossover-benchmark`
- Modify: `P:/packages/yt-is/tests/test_fallback_crossover_benchmark.py`

- [ ] **Step 1: Write the failing test**

```python
def test_load_or_build_cohort_manifest_shape_uses_live_trace_cases(tmp_path):
    mod = _load_benchmark_module()
    cohort_path = tmp_path / "manifest-cohort.json"
    cohort = mod._load_or_build_cohort(
        cohort_path,
        tmp_path / "trace-root",
        "manifest",
        manifest_json=Path("P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
    )
    assert cohort["cohort_shape"] == "manifest"
    assert all(item["source_type"] == "live_trace" for item in cohort["items"])
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest P:\packages\yt-is\tests\test_fallback_crossover_benchmark.py -k manifest -q`
Expected: fail because the manifest shape and manifest loader wiring do not exist yet.

- [ ] **Step 3: Implement manifest-backed cohort loading**

```python
parser.add_argument(
    "--cohort-shape",
    choices=("trace", "captioned", "mixed", "manifest"),
    default=DEFAULT_COHORT_SHAPE,
)
parser.add_argument(
    "--manifest-json",
    type=Path,
    default=REPO_ROOT / "tests" / "fixtures" / "shared_benchmark_manifest.json",
)
parser.add_argument(
    "--manifest-families",
    default="routing,whisper_admission,fallback_recovery,hot_path_control",
)
```

```python
if cohort_shape == "manifest":
    manifest = load_benchmark_manifest(manifest_json)
    items = [case.to_batch_item() for case in manifest.cases_for_benchmark() if case.family in allowed_families]
```

Keep the trace/captioned/mixed code paths unchanged.

- [ ] **Step 4: Run the test again and confirm it passes**

Run: `python -m pytest P:\packages\yt-is\tests\test_fallback_crossover_benchmark.py -k manifest -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add P:/packages/yt-is/bin/csf-fallback-crossover-benchmark P:/packages/yt-is/tests/test_fallback_crossover_benchmark.py
git commit -m "feat: load benchmark cohorts from shared manifest"
```

### Task 3: Document the canonical test inventory

**Files:**
- Modify: `P:/packages/yt-is/docs/operations/test-registry.md`
- Modify: `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`

- [ ] **Step 1: Add the manifest registry row**

```markdown
| Manifest | Shared benchmark manifest | pending | [tests/fixtures/shared_benchmark_manifest.json](../../tests/fixtures/shared_benchmark_manifest.json) | replaces ad hoc cohort lists once the runner consumes it | manifest loader or benchmark runner changes |
```

- [ ] **Step 2: Point the run sheet at the registry**

```markdown
Canonical test registry:
- [Test Registry](test-registry.md)
```

- [ ] **Step 3: Commit**

```bash
git add P:/packages/yt-is/docs/operations/test-registry.md P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md
git commit -m "docs: record manifest test inventory"
```

### Task 4: Validate with a manifest-backed proof run

**Files:**
- Use: `P:/packages/yt-is/.logs/whisper_admission_proof_mix_run/benchmark_summary.json`
- Use: `P:/packages/yt-is/.logs/whisper_admission_proof_run/benchmark_summary.json`

- [ ] **Step 1: Run the manifest-backed proof cohort**

Run:
`python P:/packages/yt-is/bin/csf-fallback-crossover-benchmark --cohort-shape manifest --manifest-json P:/packages/yt-is/tests/fixtures/shared_benchmark_manifest.json --manifest-families whisper_admission,fallback_recovery --workers 1 --batch-size 5 --limit 5 --policy notebooklm_route_plus_fallback_30s --sample-label manifest_proof`

Expected:
- skip cases trigger `transcript_whisper_admission_skipped`
- recovery cases still reach Whisper

- [ ] **Step 2: Confirm the output matches the registry**

Check:
- `transcript_whisper_admission_skipped`
- `source = whisper`
- `hot_path_success_count`
- `transcript_fallback_success_count`

- [ ] **Step 3: Update the registry if the proof changes the status of any case**

Expected:
- proven cases remain proven
- only the manifest row stays pending until the loader is fully wired everywhere

