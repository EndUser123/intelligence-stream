# YT-IS Test Registry

Last updated: 2026-04-28

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
- the hot-path throughput optimization series phases documented in [Hot-Path Throughput Optimization Series Design](../superpowers/specs/2026-04-28-hot-path-throughput-optimization-series-design.md)

## Proven And Negative Cases

| Family | Case | Status | Artifact | Evidence | Rerun Only If |
|---|---|---|---|---|---|
| Routing | Caption-rich cohort on the synthetic benchmark source | proven | [.logs/whisper_gate_validation_custom_run/benchmark_summary.json](../../.logs/whisper_gate_validation_custom_run/benchmark_summary.json) | `10/10` succeeded; route split did not hurt caption-rich throughput | routing logic, hot-path accounting, or benchmark harness changes |
| Routing | No-caption cohort, NotebookLM-first vs route-to-fallback | proven | [.logs/whisper_gate_validation_nocap/benchmark_summary.json](../../.logs/whisper_gate_validation_nocap/benchmark_summary.json) | NotebookLM-first spent about `291s`; route-to-fallback spent about `4s` | no-caption routing, fallback lane, or cohort seeding changes |
| Routing | Mixed real cohort A/B | proven | [.logs/whisper_gate_validation_mixed/benchmark_summary.json](../../.logs/whisper_gate_validation_mixed/benchmark_summary.json) | route split remained materially faster on the mixed backlog | source-shape routing or mixed-cohort selection changes |
| Fallback | One-worker no-caption soak | negative | [.logs/load_ladder_benchmark_nocap_soak_20/benchmark_summary.json](../../.logs/load_ladder_benchmark_nocap_soak_20/benchmark_summary.json) | route split short-circuited quickly, but did not recover items by itself | fallback recovery logic or admission policy changes |
| Fallback | Route-plus-fallback recovery cohort | proven | [.logs/fallback_recovery_trial_5/benchmark_summary.json](../../.logs/fallback_recovery_trial_5/benchmark_summary.json) | `3/3` recovered, but the recovery path was expensive | Whisper admission, fallback transport, or cache behavior changes |
| Whisper admission | Mixed proof cohort with skip candidates and Whisper-recovery controls | proven | [.logs/whisper_admission_proof_mix_run/benchmark_summary.json](../../.logs/whisper_admission_proof_mix_run/benchmark_summary.json) | `2` items skipped before Whisper and `3` recovered via Whisper | title cue list, admission gate, or fallback transport changes |
| Load | 2 vs 4 vs 8 workers on mixed lane | proven | [.logs/worker_count_trials/20260425_234812/sweep_summary.json](../../.logs/worker_count_trials/20260425_234812/sweep_summary.json) | `2` workers won; higher counts increased idle time and failures | worker scheduling, queue shaping, or NotebookLM concurrency changes |
| Load | Batch-size sweep 100 vs 200 vs 300 vs 400 on narrow/captioned cohort | proven | [.logs/batch_size_series_v4/batch_size_series_summary.json](../../.logs/batch_size_series_v4/batch_size_series_summary.json) | `200` won; `300` did not beat it | benchmark batch-size changes on the narrow/captioned shape |
| Load | Batch-size sweep 175 vs 200 vs 225 vs 250 on narrow/captioned cohort | proven | [.logs/throughput_explore_v1/batch_sizes_175_200_225_250/batch_size_series_summary.json](../../.logs/throughput_explore_v1/batch_sizes_175_200_225_250/batch_size_series_summary.json) | `200` remained in front; `225` and `250` fell below it | benchmark batch-size changes on the narrow/captioned shape |
| Load | Cleanup cadence 1 vs 2 on narrow/captioned cohort | negative | [.logs/cleanup_cadence_trial_v1/cleanup_2/benchmark_summary.json](../../.logs/cleanup_cadence_trial_v1/cleanup_2/benchmark_summary.json) | deferring cleanup to every 2 batches fell below the current every-batch control | cleanup cadence or reusable notebook lifecycle changes |
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort | negative | [.logs/pipeline_mode_comparison_narrow_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_narrow_v1/pipeline_mode_comparison_summary.json) | serial hot-path vph `1539.48`; double-buffered hot-path vph `1492.99`; Whisper excluded; double-buffered also had more command_failed items | keep serial as the control unless the source-add failure profile changes |
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort, `limit 250` | negative | [.logs/pipeline_mode_comparison_250_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_250_v1/pipeline_mode_comparison_summary.json) | serial hot-path vph `2270.78`; double-buffered hot-path vph `2160.38`; Whisper excluded; serial also had fewer idle waits | rerun only if the same 250-item sample family or pipeline-mode harness changes |
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort, `limit 300` | proven | [.logs/pipeline_mode_comparison_300_v2/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_300_v2/pipeline_mode_comparison_summary.json) | repeat 300-item run confirmed serial hot-path vph `2240.86` vs double-buffered `2140.72`; Whisper excluded; both runs at 300 favored serial | rerun only if the same 300-item sample family or pipeline-mode harness changes |
| Load | Serial vs double-buffered reusable path on narrow/captioned cohort, `limit 400` | negative | [.logs/pipeline_mode_comparison_400_v1/pipeline_mode_comparison_summary.json](../../.logs/pipeline_mode_comparison_400_v1/pipeline_mode_comparison_summary.json) | first 400-item run favored double-buffered (`2349.01` vs `1400.36`), but the repeat flipped to serial (`1355.25` vs `433.67`); Whisper excluded; result is unstable | rerun only if the same 400-item sample family or pipeline-mode harness changes |
| Account sharding | Free-only lane through dedicated Free Chrome root | proven | [.logs/sharded_lane_series/free_only_v1/sharded_lane_series_summary.json](../../.logs/sharded_lane_series/free_only_v1/sharded_lane_series_summary.json) | `troup.hominidae@gmail.com` validated through `P:/.data/yt-is/browser/notebooklm-free`; hot-path vph `2841.46`; `348` successes, `52` failures, `400` processed; Whisper excluded | Free account auth, lane config, browser root isolation, or sharded lane runner changes |
| Load | NotebookLM subbatch sweep 50 vs 75 vs 100 | pending | [.logs/nlm_subbatch_sweep_20260427_184743.err.txt](../../.logs/nlm_subbatch_sweep_20260427_184743.err.txt) | both attempts halted on subbatch 2 with `NotebookSourceMaterializationTimeout` after the first `50`-source add | NotebookLM materialization timing or subbatch handling changes |
| Load | Fullness / reuse / stagger / rotation ladder | negative | [.logs/load_ladder_benchmark/benchmark_summary.json](../../.logs/load_ladder_benchmark/benchmark_summary.json) | those knobs did not materially change the no-caption cohort | notebook state management or access staggering changes |
| Whispers | Unit tests for obvious non-speech, live/terminal, and short speech-like clips | proven | [tests/test_transcript.py](../../tests/test_transcript.py) | the admission gate logic is covered by tests | title cue list, metadata fields, or admission gate code changes |

## Pending Or Partial Cases

| Family | Case | Status | Current Gap | Next Time To Run |
|---|---|---|---|---|
| Manifest-driven coverage | Shared benchmark manifest replacing ad hoc cohort lists | proven | [.logs/shared_benchmark_manifest_proof/benchmark_summary.json](../../.logs/shared_benchmark_manifest_proof/benchmark_summary.json) | manifest-backed proof hit the live skip and Whisper recovery branches without reintroducing ad hoc cohorts | manifest loader, runner, or case family selection changes |

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
- [Hot-Path Throughput Optimization Series Design](../superpowers/specs/2026-04-28-hot-path-throughput-optimization-series-design.md)
