# Shared Benchmark Manifest Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create one canonical JSON manifest at `tests/fixtures/shared_benchmark_manifest.json` that defines the benchmark and unit-test cases we keep reusing for routing, Whisper admission, fallback recovery, and hot-path controls, so we stop hand-curating separate cohorts for each experiment.

**Architecture:** The current `yt-is` benchmark work already proved that the important branches are stable enough to freeze into a shared corpus: mixed routing, no-caption fallback, Whisper admission, and terminal/no-speech skips. We will centralize those cases in one manifest, add a small reader/validator, and have both the benchmark runner and the tests consume the same source of truth. Derived benchmark cohorts under `.logs/` remain outputs, not inputs.

**Tech Stack:** Python 3.14, JSON, `csf-fallback-crossover-benchmark`, `csf-load-ladder`, `csf.transcript`, pytest.

---

## Problem

We have already tested routing splits, Whisper admission, and fallback recovery in several separate cohorts. That got us the behavioral conclusions, but it also left us with duplicated case definitions and slightly different shapes between unit tests and benchmark runs. The result is drift: a case gets added to one harness but not the others, or a benchmark set exercises a branch that the unit tests do not lock down. We need one manifest that describes the full set of cases we care about and lets every harness read the same definitions.

## Proposed Behavior

- Create one committed JSON manifest as the canonical case list.
- Include both live-trace-derived cases and synthetic edge cases in the same file.
- Partition cases into four families:
  - `routing`
  - `whisper_admission`
  - `fallback_recovery`
  - `hot_path_control`
- Store the per-case metadata needed by the current gates and benchmarks:
  - `case_id`
  - `family`
  - `source_type` (`live_trace` or `synthetic`)
  - `video_id` or synthetic fixture id
  - `source_url`
  - `title`
  - `description`
  - `duration`
  - `privacy_status`
  - `upload_status`
  - `is_live_content`
  - `unavailable_reason`
  - `has_captions`
  - explicit expected outcomes for:
    - `hot_path`
    - `route_to_fallback`
    - `attempt_whisper`
    - `skip_whisper`
    - `recover_success`
    - `terminal_skip`
- Add a small loader/validator in `csf/benchmark_manifest.py` so every consumer rejects malformed cases early.
- Have the benchmark runners build cohorts from the manifest instead of inventing their own case lists.
- Have unit tests assert the manifest schema and the expected branch decisions for the special-case items.

## Case Families

### 1. Routing
Cases that confirm the top-level split between hot NotebookLM work and fallback work.
- Caption-rich items should stay on the hot path.
- No-caption items should follow the configured routing mode.
- Live / premiere items should bypass the hot path and go to fallback/terminal handling.

### 2. Whisper Admission
Cases that validate the pre-Whisper gate.
- Terminal metadata: deleted, private, removed, unavailable.
- Live / live_stream / premiere cases.
- Obvious non-speech title or description cues:
  - `official audio`
  - `music video`
  - `instrumental`
  - `karaoke`
  - `lyrics`
  - `live performance`
- Weak non-speech cues:
  - `cover`
  - `remix`
  - `dance`
  - `performance`
  - `song`
  - These only matter when the duration is short enough to reinforce the non-speech reading.

### 3. Fallback Recovery
Cases that should actually reach Whisper and recover when speech is present.
- Ambiguous no-caption spoken-word clips.
- Short spoken clips that must not be excluded just because they are short.
- A small number of known recoverable no-caption cases so we can prove recovery still works.

### 4. Hot-Path Control
Cases that should keep the benchmark honest.
- Caption-rich controls that prove the hot path still performs normally.
- Mixed cases that reflect the real backlog shape.
- A few negative controls that should never be treated as Whisper recovery candidates.

## Manifest Shape

The manifest should be a plain JSON object with a top-level structure like:

```json
{
  "manifest_version": 1,
  "generated_at": "2026-04-26T00:00:00Z",
  "cases": [
    {
      "case_id": "routing-caption-rich-001",
      "family": "routing",
      "source_type": "live_trace",
      "video_id": "dQw4w9WgXcQ",
      "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "title": "Demo Title",
      "description": "Demo Description",
      "duration": 12,
      "privacy_status": "public",
      "upload_status": "uploaded",
      "is_live_content": false,
      "unavailable_reason": null,
      "has_captions": true,
      "expected": {
        "hot_path": true,
        "route_to_fallback": false,
        "attempt_whisper": false,
        "skip_whisper": false,
        "recover_success": false,
        "terminal_skip": false
      }
    }
  ]
}
```

Validation rules:
- `case_id` must be unique.
- `family` must be one of the four supported families.
- `source_type` must be `live_trace` or `synthetic`.
- `expected` must contain the boolean fields the harness knows how to verify.
- All cases must include the metadata fields the gate consumes, even when the value is `null`.
- Synthetic cases must populate all metadata fields explicitly.
- Live-trace cases may use `null` for fields not present in the trace, but the manifest should still carry those keys so consumers do not need special-case schema branching.

## Scope

In scope:
- One committed manifest file.
- A small loader/validator module.
- Benchmark and test consumers for the same manifest.
- Unit tests that prove the manifest schema and family filtering work.

Out of scope:
- A new benchmark engine.
- A new transcript backend.
- Reworking the routing decisions themselves.
- Reclassifying the historical conclusions we already proved about routing or Whisper.

## Success Criteria

- The same case definitions drive unit tests and benchmark runs.
- Routing, Whisper admission, fallback recovery, and hot-path control cases are all represented.
- Live-trace-derived and synthetic cases can coexist in one manifest.
- Invalid cases are rejected before they reach a benchmark run.
- Derived `.logs/` cohort files remain outputs only, not a second source of truth.
- Future benchmark additions happen by adding one manifest entry, not by hand-editing multiple harnesses.

## Risks

- The manifest can drift into a dumping ground if we do not keep the families explicit. Keep the family boundaries strict.
- Synthetic cases can overfit the current code if we only add examples that mirror today’s branches. Keep live-trace-derived cases in the manifest too.
- If the schema becomes too rich, it will be hard to maintain. Keep only the fields the current gates and benchmark runner actually consume.
- Because the current live cohorts already showed some branches are hard to reach, the manifest should include at least a few synthetic edge cases so the admission gate and terminal-skip logic are always exercised.
