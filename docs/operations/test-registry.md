# YT-IS Test Registry

Last updated: 2026-04-26

## Purpose

This registry records the benchmark and unit-test cases we have already used to establish behavior in `yt-is`. Its purpose is to prevent accidental reruns of the same evidence when the code under test has not changed in a meaningful way.

Use this registry before starting a new benchmark or adding a new cohort:
- If a case is marked `proven`, do not rerun it unless the code path it exercises has changed.
- If a case is marked `negative`, do not rerun it unless the touched code affects the underlying assumption.
- If a case is marked `pending`, it is still a valid candidate for future validation.

## Canonical Rule

Treat the following as stable unless the relevant code changes:
- routing split conclusions
- worker-count conclusions
- notebook fullness / reuse / stagger conclusions
- fallback recovery conclusions
- Whisper admission unit-test conclusions

Treat the following as still needing live proof:
- any benchmark branch that has only been unit-tested
- any gate branch we have not yet seen in a live benchmark trace
- any new manifest case that has not yet been assigned a status

## Proven And Negative Cases

| Family | Case | Status | Artifact | Evidence | Rerun Only If |
|---|---|---|---|---|---|
| Routing | Caption-rich cohort on the synthetic benchmark source | proven | [.logs/whisper_gate_validation_custom_run/benchmark_summary.json](../../.logs/whisper_gate_validation_custom_run/benchmark_summary.json) | `10/10` succeeded; route split did not hurt caption-rich throughput | routing logic, hot-path accounting, or benchmark harness changes |
| Routing | No-caption cohort, NotebookLM-first vs route-to-fallback | proven | [.logs/whisper_gate_validation_nocap/benchmark_summary.json](../../.logs/whisper_gate_validation_nocap/benchmark_summary.json) | NotebookLM-first spent about `291s`; route-to-fallback spent about `4s` | no-caption routing, fallback lane, or cohort seeding changes |
| Routing | Mixed real cohort A/B | proven | [.logs/whisper_gate_validation_mixed/benchmark_summary.json](../../.logs/whisper_gate_validation_mixed/benchmark_summary.json) | route split remained materially faster on the mixed backlog | source-shape routing or mixed-cohort selection changes |
| Fallback | One-worker no-caption soak | negative | [.logs/load_ladder_benchmark_nocap_soak_20/benchmark_summary.json](../../.logs/load_ladder_benchmark_nocap_soak_20/benchmark_summary.json) | route split short-circuited quickly, but did not recover items by itself | fallback recovery logic or admission policy changes |
| Fallback | Route-plus-fallback recovery cohort | proven | [.logs/fallback_recovery_trial_5/benchmark_summary.json](../../.logs/fallback_recovery_trial_5/benchmark_summary.json) | `3/3` recovered, but the recovery path was expensive | Whisper admission, fallback transport, or cache behavior changes |
| Load | 2 vs 4 vs 8 workers on mixed lane | proven | [.logs/worker_count_trials/20260425_234812/sweep_summary.json](../../.logs/worker_count_trials/20260425_234812/sweep_summary.json) | `2` workers won; higher counts increased idle time and failures | worker scheduling, queue shaping, or NotebookLM concurrency changes |
| Load | Fullness / reuse / stagger / rotation ladder | negative | [.logs/load_ladder_benchmark/benchmark_summary.json](../../.logs/load_ladder_benchmark/benchmark_summary.json) | those knobs did not materially change the no-caption cohort | notebook state management or access staggering changes |
| Whispers | Unit tests for obvious non-speech, live/terminal, and short speech-like clips | proven | [tests/test_transcript.py](../../tests/test_transcript.py) | the admission gate logic is covered by tests | title cue list, metadata fields, or admission gate code changes |

## Pending Or Partial Cases

| Family | Case | Status | Current Gap | Next Time To Run |
|---|---|---|---|---|
| Whisper admission | Live benchmark branch that actually emits `transcript_whisper_admission_skipped` | pending | the unit tests prove the logic, but the live cohorts so far did not drive that branch | when we have a cohort that reaches Whisper and includes skip candidates |
| Fallback recovery | Curated no-caption cohort that exercises Whisper recovery in a live benchmark with skip candidates mixed in | pending | we know recovery works, but we still need a cleaner mixed live proof of the admission gate | when the manifest includes a proper mixed no-caption sample |
| Manifest-driven coverage | Shared benchmark manifest replacing ad hoc cohort lists | pending | the registry exists conceptually, but the manifest loader and consumers are not yet implemented | before any new large benchmark series |

## How To Extend

When a new benchmark is run:
- add the case or cohort here
- include the run artifact path
- mark the status as `proven`, `negative`, or `pending`
- add a rerun guard that names the code path that would justify trying it again

When a benchmark result is only meaningful because the sample changed, record that explicitly. That keeps future runs from repeating the same test under a different name.

## Related Docs

- [Worker Count Trial Run Sheet](worker-count-trial-run-sheet.md)
- [Whisper Admission Policy Design](../superpowers/specs/2026-04-26-whisper-admission-policy-design.md)
- [Source-Shape Routing Ladder Design](../superpowers/specs/2026-04-26-source-shape-routing-ladder-design.md)
- [Shared Benchmark Manifest Design](../superpowers/specs/2026-04-26-shared-benchmark-manifest-design.md)
