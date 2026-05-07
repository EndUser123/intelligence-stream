# Pro-Free Source Map v1 Reproduction Investigation Plan

Created: 2026-05-05

## Goal

Explain why `pro_free_source_map_v1` reached `5572.04` combined hot-path VPH while current profile/config reruns do not, and decide whether the old result is reproducible enough to become the recommended operating shape.

## Current Evidence

Known high-water mark:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/sharded_lane_series_summary.json`
- Shape: Pro + Free, 4 workers per lane, batch size 200, limit 400 per lane, serial reusable pipeline
- Combined hot-path VPH: `5572.04`
- Result mix: `796` hot-path successes, `4` failures, `800` processed

Current reproduced leader:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run01/sharded_lane_series_summary.json`
- Shape: Pro + Free, 3 workers per lane
- Combined hot-path VPH: `4123.28`
- Result mix: `795` hot-path successes, `5` failures, `800` processed

Negative controls already recorded:

- `pro_free_source_map_v7_rerun`: same cohort, current profile family, `2930.78` VPH
- `pro_free_source_map_v1_frozen_retest`: same cohort, frozen historical profile geometry, `2507.94` VPH
- `optimal_search_2lane_5w_v1`: current two-lane 5-worker shape, `3239.04` VPH
- `optimal_search_2lane_2w_v1`: floor test, `2815.36` VPH
- three-lane candidates: negative or invalid

## Phase 1 Findings

The first-round comparison did not reproduce the historical high-water mark:

- Current raw same-cohort rerun with `pro_free_lanes.json` completed at `3222.69` combined hot-path VPH with `793/7`, which is far below `5572.04`.
- The guarded same-cohort replay on the current profile family completed at `3464.4` combined hot-path VPH with `794/6`, still far below `5572.04`.
- The frozen-profile raw replay could not complete a fair comparison yet because `ytis-free-worker-01` hit NotebookLM auth expiry and force refresh failed.
- The targeted content probe suggests the gap is not caused by one permanently bad video pair:
  - `juXI9QbzzgM` stays below threshold on both Pro and Free profiles across retries.
  - `Qi07yb1S6Ps` is recoverable in isolation on both profiles, so its benchmark `command_failed` behavior looks transient or harness-sensitive.
- The historical `5572.04` run still has the best stage timing profile we have seen:
  - much lower add time
  - much lower source-ready age
  - far fewer `command_failed` events
- The current reruns are slower mainly because startup/setup and source addition are slower, not because lane count alone changed the result.
- The biggest live delta is in `startup_notebook_check_elapsed_s_total` and total `setup_elapsed_s_total` on both lanes, with add time also materially higher.
- Current run traces also show repeated `nlm_auth_recovered` events while reaping the shared default NotebookLM Chrome profile before commands; the historical run stayed on clean `nlm_auth_checked` paths. That makes auth/profile interference the leading suspect inside the startup/setup delta.
- Current runs also cleaned up more stale worker notebooks than the historical run (`4` deleted in the current batch-2 Pro trace versus `1` historically), which means the worker-state root is contributing extra startup cleanup cost in addition to auth/profile repair.
- A clean-start probe on the same Pro+Free family confirmed the startup/auth leak is real, not just a summary artifact: browser health came up `recovered_clean` only after reaping `12` default NotebookLM Chrome profile processes before the run could settle clean.
- That same clean-start probe still showed heavy startup/setup cost on the smoke path, with the Pro lane spending `57.165s` in setup plus `52.417s` in add time on batch 1 and the Free lane spending `50.605s` in setup plus `44.687s` in notebook creation on batch 2.
- The probe completed cleanly overall, but it did not recover anywhere near the historical `5572.04` throughput, which keeps the main diagnosis focused on startup/auth/profile hygiene and stale-worker cleanup rather than lane geometry.
- After adding the lane-start cleanup barrier, the rerun still reaped the same `12` default Chrome processes at preflight, but the soak completed cleanly with no lingering default-profile hygiene issue and combined hot-path VPH improved to `2078.43` on the 50-item smoke/soak probe.
- A fresh full-size clean-start probe with the family-refresh timing markers completed ok at combined hot-path VPH `1603.13`; preflight and post-run hygiene were clean, but the auth cache stayed warm so the new `nlm_family_refresh_*` markers did not fire in that run.
- A smaller direct `csf-source fetch` probe on `ytis-pro-worker-01` with `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=1` did hit the live family-refresh path and logged `nlm_family_refresh_completed.elapsed_s=10.616`, which proves the timing hook on the real fetch helper; the probe still completed the 25-item fetch in `60.621s`, so the auth cost is real but not enough by itself to explain the historical `5572.04` gap.
- The later `sweep_phase3_2lane_3w_run05` auth-check cache TTL A/B finished cleanly but negatively at `1958.94` combined hot-path VPH. Compared with the run04 comparator, run05 had higher `add_elapsed_s_total`, higher `worker_idle_wait_s_total`, and a worse `source_ready_age_s_avg`, while `session_age_s` stayed in the `0-30s` band. That pushes the remaining gap even further toward source-add/readiness/setup timing and away from auth TTL as the main limiter.

## Working Hypotheses

1. The `5572.04` run benefited from a transient NotebookLM/backend/local-machine condition that has not recurred.
2. The `5572.04` run used a different effective benchmark contract than current guarded runs, even if the headline shape looks the same.
3. The gap is mostly stage-specific: add time, cleanup time, idle wait, or content fetch behavior changed between runs.
4. The old profile state helped, but the frozen-profile retest shows profile geometry alone is not sufficient.
5. The old run had a favorable cohort/order/source-add outcome that current source-map reruns have not reproduced.
6. The remaining gap is likely in source-add/readiness timing or backend variance, not in lane count by itself.
7. The next useful investigation is still the startup/setup path, especially notebook check, notebook create, worker cleanup timing, stale worker-notebook accumulation, and the shared default-profile/auth recovery path, because those are the largest current-vs-historical deltas on both lanes and the clean-start probe showed they are active in the live environment.

## Phase 1: Artifact Forensics Before More Soaks

Purpose: prove exactly what changed before spending another full benchmark cycle.

Compare these artifacts:

- `pro_free_source_map_v1`
- `pro_free_source_map_v7_rerun`
- `pro_free_source_map_v1_frozen_retest`
- `sweep_phase3_2lane_3w_run01`
- `optimal_search_2lane_5w_v1`

Record, per run:

- runner command and executable path
- lane config path, lane count, workers, profile names, browser roots, worker state roots
- cohort path, per-lane cohort file hashes, and first/last 10 video IDs per lane
- combined wall time, lane wall time, add time, cleanup time, idle wait, source-ready age, ytdlp time
- content-fetch status counts
- fail types from worker outputs
- post-run hygiene if present

Suggested command skeleton:

```powershell
cd P:\\packages/yt-is
$runs = @(
  "pro_free_source_map_v1",
  "pro_free_source_map_v7_rerun",
  "pro_free_source_map_v1_frozen_retest",
  "sweep_phase3_2lane_3w_run01",
  "optimal_search_2lane_5w_v1"
)
foreach ($run in $runs) {
  $summary = "P:\\packages/yt-is/.logs/sharded_lane_series/$run/sharded_lane_series_summary.json"
  if (Test-Path $summary) {
    $j = Get-Content -Raw $summary | ConvertFrom-Json
    [pscustomobject]@{
      run = $run
      status = $j.status
      vph = $j.combined.hot_path_videos_per_hour
      wall = $j.combined.wall_elapsed_s
      success = $j.combined.hot_path_success_count_total
      fail = $j.combined.fail_count_total
      processed = $j.combined.processed_count_total
      hygiene = $j.post_run_hygiene.status
      pro_vph = $j.runs[0].hot_path_videos_per_hour
      free_vph = $j.runs[1].hot_path_videos_per_hour
      pro_add = $j.runs[0].add_elapsed_s_total
      free_add = $j.runs[1].add_elapsed_s_total
      pro_cleanup = $j.runs[0].cleanup_elapsed_s_total
      free_cleanup = $j.runs[1].cleanup_elapsed_s_total
      pro_idle = $j.runs[0].worker_idle_wait_s_total
      free_idle = $j.runs[1].worker_idle_wait_s_total
    }
  }
}
```

Exit criteria:

- If one stage explains most of the gap, investigate that stage first.
- If auth recovery dominates startup/setup, isolate the default-profile leak path before running any broader throughput sweep.
- If cleanup dominates startup/setup, inspect worker-state retention and stale notebook accumulation before widening the matrix.
- If no stage explains the gap, run the paired reproduction in Phase 2.

## Phase 2: Paired Same-Window Reproduction

Purpose: remove time-of-day/backend variance from the comparison.

Run these back to back in the same session, with fresh roots:

1. Raw series, current `pro_free_lanes.json`, same `pro_free_source_map_v1` cohort.
2. Raw series, `pro_free_source_map_v1_frozen_lanes.json`, same cohort.
3. Guarded sequence, current `pro_free_lanes.json`, fresh cohort from the sequence.

Commands:

```powershell
cd P:\\packages/yt-is
python P:\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\packages/yt-is/.logs/sharded_lane_series/repro_v1_raw_current_run01 `
  --cohort-json P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial

python P:\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1_frozen_lanes.json `
  --output-root P:\\packages/yt-is/.logs/sharded_lane_series/repro_v1_raw_frozen_run01 `
  --cohort-json P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial

python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/repro_v1_guarded_current_run01
```

Exit criteria:

- If raw current approaches `5572`, the guarded sequence is adding enough overhead or behavioral difference to matter.
- If raw frozen approaches `5572`, the old profile state/config still matters and needs a safer reconstruction.
- If neither raw run approaches `5572`, treat `pro_free_source_map_v1` as a non-reproduced high-water mark and investigate environmental variance.

## Phase 3: Stage-Specific Diagnosis

Purpose: identify the actual bottleneck if Phase 2 stays slow.

Run these only after Phase 1/2 identify the slow stage:

- Startup/setup probe if `startup_notebook_check_elapsed_s_total`, `startup_prepare_total_elapsed_s_total`, or `setup_elapsed_s_total` dominates.
- Add-path probe if `add_elapsed_s_total` or `source_add_failed` dominates after startup is normalized.
- Cleanup probe if cleanup time dominates after startup and add are normal.
- Single-lane Pro and Free calibration if both lanes slow down together.
- Content-fetch probe if `command_failed` or `nlm_content_below_threshold` changes materially.

Current stage signal:

- `repro_v1_startup_probe_run01` showed the startup/setup path is expensive even on a clean start: combined hot-path VPH `922.3`, `worker_idle_wait_s_total=241.507`, `cleanup_elapsed_s_total=46.614`, and `source_ready_age_s_avg=11.421`.
- `repro_v2_startup_probe_run01` improved the same clean-start family to combined hot-path VPH `1183.53`, but still only on a 50-item probe with `worker_idle_wait_s_total=213.679` and `source_ready_age_s_avg=11.175`.
- `repro_v3_startup_probe_run01` scaled back to combined hot-path VPH `1603.13` on a full-size clean-start probe, with `add_elapsed_s_total=737.121`, `cleanup_elapsed_s_total=101.102`, `worker_idle_wait_s_total=339.463`, and `source_ready_age_s_avg=62.639`.
- The later `sweep_phase3_2lane_3w_run05` auth-check cache TTL A/B also stayed slow at `1958.94` combined hot-path VPH and increased `add_elapsed_s_total`, `worker_idle_wait_s_total`, and `source_ready_age_s_avg` versus the `run04` comparator.
- The single-lane calibration runs split the cohort: Pro-only completed at `1980.19` combined hot-path VPH with `505.369` add time, `102.566` cleanup time, `243.778` worker idle wait, and `27.671` source-ready age average; Free-only completed at `3361.75` combined hot-path VPH with `581.27` add time, `116.913` cleanup time, `0.0` worker idle wait, and `22.865` source-ready age average. That makes Pro the slower lane on this branch and points the next investigation at Pro startup/setup/auth cleanup behavior rather than a symmetric lane-width issue.
- The per-worker traces in `repro_v1_pro_only_4w_run01` and `repro_v1_free_only_4w_run01` sharpened that further: Pro showed nonzero lane-wide idle wait and one worker with a much larger `extract_elapsed_s_total` (`124.53` versus Free's `62.649` on the comparable worker), while Free stayed at `0.0` idle wait.
- The same-window follow-up pair `sweep_p2_pro_only_startup_extract_run01` and `sweep_p2_free_only_startup_extract_run01` kept the same pattern but removed the idle-wait delta: Pro improved to combined hot-path VPH `2396.42` and Free to `2721.6`, both clean with `398/2/400`, and the Pro workers still carried the higher extract totals. That moves the remaining gap toward the Pro startup/setup/extract path rather than auth TTL or lane count.
- Taken together, these probes keep the highest-ROI follow-on in the startup/setup and source-readiness path, with extract now the sharper Pro-side substage to isolate.

Commands:

```powershell
cd P:\\packages/yt-is
python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_only_lanes.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/repro_v1_pro_only_4w_run01

python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/free_only_lanes.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/repro_v1_free_only_4w_run01
```

Exit criteria:

- If single-lane rates are also low, the issue is likely account/backend/local condition, not lane contention.
- If single-lane rates are high but combined rates are low, focus on concurrent startup, browser contention, and NotebookLM account-lane contention.
- If Pro-only is materially slower than Free-only, isolate the Pro startup/setup/auth cleanup path before widening the matrix again.
- If Pro-only is materially slower than Free-only and the worker logs show extract skew, isolate the Pro startup/setup/extract path before widening the matrix again.

## Phase 4: Decide Operational Setting

Promotion rules:

- Do not promote `4+4` unless at least two fresh same-window runs beat `3+3` by a meaningful margin and pass hygiene.
- Keep `3+3` as the operational max-VPH setting if `4+4` remains below `4123.28` or only wins once.
- Treat `5572.04` as historical high-water evidence, not the operating setting, until reproduced.

Current bake-off note:

- The latest same-window `3+3` control completed cleanly at `1900.79` hot-path VPH.
- The paired `4+4` candidate invalidated on `default_profile_running profile=ytis-free1-worker-03` in the Free lane, so it did not produce promotable same-window evidence.

Document:

- Update `P:\\packages/yt-is/docs/operations/test-registry.md`.
- Update `P:\\packages/yt-is/docs/operations/sharded-lane-series.md`.
- If a new run becomes the leader, update `P:\\packages/yt-is/docs/operations/optimal-throughput-candidate-test-plan.md`.

## Stop Conditions

Stop and diagnose before more full runs if:

- browser health reports persistent shared default-profile leakage
- any run opens an account chooser
- smoke fails or evidence check fails
- a run root is reused or dirty
- cohort files differ when the comparison claims same-cohort behavior
- a candidate looks faster only because it processed fewer items
