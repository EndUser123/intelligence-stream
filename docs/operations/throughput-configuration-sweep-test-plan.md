# Throughput Configuration Sweep Test Plan

Created: 2026-05-05

## Purpose

Find the current highest sustained NotebookLM hot-path throughput configuration for `yt-is` without confusing historical one-off results with reproducible current behavior.

This plan covers the variables worth testing, the order to test them, the controls that must remain fixed, and the promotion rules for calling a configuration better.

Do not call any configuration "optimal" until it has won a controlled sweep and passed repeated fresh-root soaks.

## Execution Requirements

These requirements apply to every phase and every run:

- Use a fresh run root for every candidate run.
- Record the exact lane config file used.
- Record the exact cohort file used.
- Record the exact command used to launch the run.
- Record the top-level summary path.
- Record whether the run is `valid`, `invalid`, `negative`, `promising`, or `promoted`.
- Record the account family used by each lane.
- Record the browser root used by each lane.
- Record whether the run used the current supported profile family or a frozen historical one.
- Record whether the run used the raw series runner or the guarded sequence runner.
- Keep `serial` mode and batch size `200` unless the phase explicitly changes them.
- Do not compare a phase result against an unverified run.
- Do not advance to the next phase until the current phase has at least one clean summary artifact.

If any of these are missing, the run is incomplete and cannot be used as sweep evidence.

## Current Baseline Evidence

Use these runs as the starting evidence for the sweep:

| Run | Shape | Status | Combined hot-path VPH | Success/fail | Notes |
|---|---|---:|---:|---:|---|
| `pro_free_source_map_v1` | Pro + Free, 4 workers per lane, historical profile geometry | historical best | `5572.04` | `796/4` | Best recorded sustained result, but not reproduced by current retests. |
| `optimal_search_2lane_4w_v2` | Current guarded Pro + Free, 4 workers per lane | valid | `3573.61` | `793/7` | Current supported guarded shape. |
| `optimal_search_2lane_4w_v3` | Current guarded Pro + Free, 4 workers per lane | valid | `3227.63` | `793/7` | Repeat of supported guarded shape; shows variance. |
| `sweep_phase1_2lane_4w_run02` | Current guarded Pro + Free, 4 workers per lane | valid | `1541.10` | `798/2` | Fresh control sample; valid but much slower than the earlier guarded repeats. |
| `sweep_phase1_2lane_4w_run03` | Current guarded Pro + Free, 4 workers per lane | valid | `3078.63` | `795/5` | Fresh control sample; recovered toward the earlier guarded repeats but still below them. |
| `pro_free_source_map_v7_rerun` | Current Pro + Free lane config, raw series | valid | `2930.78` | `795/5` | Same `pro_free_source_map_v1` cohort, current profile family. |
| `pro_free_source_map_v1_frozen_retest` | Frozen historical Pro + Free profile geometry, raw series | valid | `2507.94` | `793/7` | Same cohort; historical profile geometry alone did not recover old throughput. |
| `optimal_search_3lane_3w_v2` | Three lanes, 3 workers per lane | invalid | `2198.93` | `789/11` | Invalidated; diagnostic only. |
| `optimal_search_3lane_4w_v3` | Three lanes, 4 workers per lane | valid | `1466.83` | `1095/55` | Valid but far below current 2-lane control. |
| `optimal_search_3lane_4w_v4` | Three lanes, 4 workers per lane | valid | `2290.88` | `1039/11` | Valid but still below current 2-lane control. |

Interpretation:

- The 5572 VPH run is still the historical high-water mark.
- The current reproducible 2-lane baseline is materially lower, roughly 3200-3600 VPH from recent guarded repeats.
- The fresh control sample shows the current guarded baseline can fall much lower, so variance must be handled before comparing new shapes.
- The next fresh control sample recovered toward the earlier guarded repeats, which confirms the baseline is volatile rather than consistently degraded.
- The frozen historical profile retest was slower than the current supported config.
- Recent 3-lane tests do not beat the current 2-lane control.
- The next useful work is a disciplined sweep, not more ad hoc reruns of the old best.

## Fixed Controls

Hold these constant unless the phase explicitly names the variable:

- Primary metric: `combined.hot_path_videos_per_hour`.
- Scope: NotebookLM hot path only; Whisper recovery remains excluded.
- Sample: start with `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/cohort.json`.
- Limit: `400` videos per 2-lane run unless the phase says otherwise.
- Batch size: `200`.
- Pipeline mode: `serial`.
- Run root: fresh, empty root for every run.
- Browser roots: dedicated lane roots only; never shared default NotebookLM Chrome profile.
- Auth: expected account must match every worker profile before a soak.
- Hygiene: doctor, smoke, evidence check, soak, then post-run hygiene check for guarded candidates.
- Classification: invalidated runs are diagnostics only and cannot win.

If any control is intentionally changed, record it as the tested variable in the run name and registry entry.

## How To Execute The Plan

Use the plan in discrete phases. Each phase has:

- a goal
- a required input set
- one or more candidate runs
- an exit rule
- a decision on whether to continue or stop

The phases are ordered so that expensive or ambiguous variables are tested only after the cheaper, higher-signal variables are narrowed down.

Execution rule:

- Run the earliest incomplete phase first.
- Do not skip a phase unless the phase is explicitly marked optional.
- Do not widen the matrix until the current phase has produced a clear winner or a clear non-winner.

Recommended evidence handling:

- Store raw outputs under the run root.
- Add the summary path to `test-registry.md` after each run.
- Update the related operations doc only after the run is classified.
- Keep one short note with the reason for promotion or rejection.

## Metrics To Capture

Record these from every top-level `sharded_lane_series_summary.json`:

- `combined.hot_path_videos_per_hour`
- `combined.wall_elapsed_s`
- `combined.hot_path_success_count_total`
- `combined.fail_count_total`
- `combined.processed_count_total`
- per-lane hot-path VPH
- per-lane success/fail counts
- `add_elapsed_s_total`
- `worker_idle_wait_s_total`
- `source_ready_age_s_total`
- `source_ready_age_s_avg` if present
- `youtube_ytdlp_elapsed_s_total`
- `youtube_ytdlp_elapsed_s_count_total`
- `cleanup_elapsed_s_total`
- `content_fetch_status_counts_total`
- source-add failure counts from lane logs
- auth refresh counts and any forced-refresh markers
- default-profile detection/reap count
- invalidation reason, if any

The analysis should separate:

- source-add time
- materialization/source-ready wait
- content-fetch/probe churn
- worker idle wait
- auth/profile churn
- cleanup overhead
- actual hot-path success rate

## Variables Worth Testing

### Lane Count And Account Mix

Test whether throughput is limited by per-account concurrency, browser/process contention, or NotebookLM service behavior.

Options:

- Pro only.
- Free only.
- Pro + Free.
- Pro + Free + Free2/Hotmail.
- Free + Free2 without Pro, if Pro-specific behavior looks like the bottleneck.

Decision use:

- Single-lane runs define each account family ceiling.
- Two-lane runs test account sharding with current operational complexity.
- Three-lane runs are only worth continuing if they beat a repeated 2-lane median or clearly reduce per-account failure churn.

### Worker Count

Test both symmetric and asymmetric worker allocations.

Symmetric options:

- `1 + 1`
- `2 + 2`
- `3 + 3`
- `4 + 4`
- `5 + 5` only if lower counts show no clear saturation.

Asymmetric options:

- `3 + 2`
- `2 + 3`
- `4 + 2`
- `2 + 4`
- `4 + 3`
- `3 + 4`

Three-lane options:

- `2 + 2 + 2`
- `3 + 3 + 3`
- `4 + 4 + 4` only as a bounded repeat because recent evidence is weak.
- `3 + 2 + 2`, `2 + 3 + 2`, `2 + 2 + 3` if one account family is weaker.

Decision use:

- Prefer fewer workers if VPH is within 5 percent of the higher-worker result and failures or probe churn are lower.
- Stop increasing workers once `worker_idle_wait_s_total`, `command_failed`, or source-add failures rise faster than VPH.

### Profile Family And Browser Geometry

The frozen retest shows profile geometry alone did not recover historical throughput, but it is still a valid variable because auth/profile state can affect NotebookLM behavior.

Options:

- Current supported Pro profile: `Profile`.
- Historical Pro profile: `Profile 2`.
- Current supported Free family: `ytis-free1-worker-*`.
- Historical Free family: `ytis-free-worker-*`.
- Free2/Hotmail family from `pro_free_hotmail_lanes.json`.
- Fresh browser root with newly authenticated worker profiles.
- Warm persistent browser root with existing state.

Prerequisite:

- Do not rely on manual cookie copying for repeated tests. If historical profiles remain in the matrix, make the auth tooling treat `ytis-free-worker-*` as a first-class expected family or explicitly mark those runs as manually prepared diagnostics.

Decision use:

- Compare profile geometry only after the current 2-lane baseline variance is known.
- A profile family does not win unless it beats the current supported family across repeated fresh-root soaks.

### Lane Startup Order, Stagger, And Prewarm

Prior 60s/120s stagger evidence was weak or negative, but smaller startup offsets may still reduce auth/browser contention.

Options:

- No stagger.
- Pro first, Free after 15s.
- Pro first, Free after 30s.
- Free first, Pro after 15s.
- Free first, Pro after 30s.
- Three-lane stagger: 0/15/30.
- Browser prewarm before source add.

Decision use:

- Do not retest 60s or 120s unless startup logic changed; historical evidence says large stagger hurts or invalidates.
- Keep a stagger only if it improves median VPH and reduces failure churn.

### Batch Size

Existing evidence favors `200`, so this is a late-phase variable.

Options:

- `150`
- `175`
- `200`
- `225`
- `250`

Decision use:

- Test only after lane and worker count are stable.
- Do not retest `300` or `400` until the current bottleneck is no longer source-add/materialization churn.

### NotebookLM Source Add Sizing

The old subbatch sweep stalled after the second add, so this requires instrumentation before a full soak.

Options:

- Source-add subbatch `25`.
- Source-add subbatch `50`.
- Source-add subbatch `75`.
- Source-add subbatch `100`.

Prerequisite:

- Add or confirm per-subbatch timing and materialization timeout reporting.
- Run a smoke-sized proof before any full soak.

Decision use:

- Promote smaller subbatches only if they reduce materialization time or command failures enough to offset extra add overhead.

### Reusable Pipeline Mode

Serial is the control because previous double-buffered tests were negative or unstable.

Options:

- `serial`.
- `double-buffered`.

Decision use:

- Retest double-buffered only after source-add failures and materialization churn are materially lower.
- Do not promote double-buffered from a single lucky 400-item run.

### Notebook State And Cleanup

Existing evidence says deferred cleanup and bulk cleanup are negative, but state lifecycle can still interact with worker count and source-add pressure.

Options:

- Fresh worker notebook per batch.
- Reusable worker notebooks.
- Cleanup every batch.
- Cleanup every 2 batches only if cleanup code changed.
- New worker-state root.
- Warm worker-state root.
- Notebook reset fallback enabled.
- Notebook reset fallback disabled.

Decision use:

- Treat stale source maps, zero-growth adds, and cleanup loops as invalidation risks.
- Prefer the simpler state model when throughput is within 5 percent.

### Auth And Session Refresh

Auth experiments are safety tests, not throughput optimizations, unless they remove a demonstrated bottleneck.

Options:

- Normal auth, no forced refresh.
- Auth sync immediately before run.
- Forced refresh cadence `5`.
- Forced refresh cadence `10`.
- Historical profile family with first-class auth mapping.

Decision use:

- Forced refresh can prove robustness, but it should not be used in the throughput winner unless normal auth is unstable.
- Any run with account mismatch or default-profile leakage is invalid.

### Cohort And Source Shape

Optimize first on the fixed historical cohort, then verify the winner on representative cohorts.

Options:

- `pro_free_source_map_v1/cohort.json`.
- Narrow caption-rich cohort.
- Mixed real cohort.
- No-caption cohort.
- High-risk residual content extraction IDs.

Decision use:

- Do not mix cohort changes into the configuration sweep.
- After selecting a winner on the fixed cohort, run a transfer check on mixed and caption-rich cohorts.

### Local Environment And Time Window

NotebookLM behavior may vary by local process state and backend service conditions.

Options:

- Fresh Chrome/process environment.
- Warm Chrome/process environment.
- Back-to-back repeats in the same window.
- Repeats in separate time windows.
- Network/browser process scan before and after run.

Decision use:

- If repeated control variance is greater than the expected improvement from a candidate, stop expanding the matrix and characterize variance first.

### Retry, Probe, And Fetch Policy

These are semantic variables and should be tested after structural variables.

Options:

- Current content-fetch retry policy.
- Increased content-fetch retry attempts.
- Increased probe delay.
- Reduced probe delay.
- ytdlp probe enabled.
- ytdlp probe disabled for known NotebookLM-ready cases.

Decision use:

- Do not improve VPH by silently lowering success quality.
- Any retry/probe change must preserve hot-path success classification semantics.

## Phase Plan

### Phase 0: Baseline And Tooling Gate

Goal: make the sweep measurable before adding more variables.

Actions:

- Add the latest `v7_rerun`, `v1_frozen_retest`, and `optimal_search_*` outcomes to `test-registry.md`.
- Create a small comparison script or documented command that extracts the required metrics from a list of summary files.
- Confirm the current lane configs and expected-account mappings.
- Confirm no benchmark will use the shared default NotebookLM Chrome profile.

Exit criteria:

- A current evidence table can be regenerated from summary files.
- The current supported 2-lane config is runnable through the guarded sequence.
- The baseline command set can be executed without manual path guessing.
- The lane/account mapping is explicit enough that a rerun would not rely on memory.

### Phase 1: Reproducibility Baseline

Goal: quantify current baseline variance before testing new shapes.

Run:

- Current supported Pro + Free, `4 + 4`, no stagger, three fresh roots.
- Same cohort, batch size `200`, serial mode.

Decision:

- Use median VPH as the current baseline.
- If max/min spread is greater than 20 percent, run two more controls in a separate time window before expanding the matrix.
- If variance remains high, optimize for median and failure rate, not single-run peak.
- Do not move to Phase 2 until at least three baseline runs are classified.
- If one control run is invalidated, replace it with another control run before comparing candidates.

### Phase 2: Single-Lane Capacity

Goal: learn each account family's independent ceiling.

Run:

- Pro-only: 2, 3, and 4 workers.
- Free-only: 2, 3, and 4 workers.
- Free2-only: 2 and 3 workers, if auth is stable.

Decision:

- Identify each lane's best worker count by median VPH and failure profile.
- Use this to prune asymmetric 2-lane and 3-lane candidates.
- Record the best Pro-only and Free-only shapes separately before combining them.
- If one account family fails at a lower worker count, treat that as a hard ceiling for the next phases.

Observed current results:

| Lane | Workers | Status | VPH | Success/fail | Interpretation |
|---|---:|---|---:|---:|---|
| Pro-only | 2 | valid | `1182.08` | `396/4` | Too low; not a useful ceiling point. |
| Pro-only | 3 | valid | `2340.80` | `398/2` | Much better than 2 workers. |
| Pro-only | 4 | valid | `2346.99` | `397/3` | Slightly ahead of 3 workers, but not by much. |
| Free-only | 2 | valid | `1010.17` | `396/4` | Too low; not a useful ceiling point. |
| Free-only | 3 | valid | `2588.82` | `398/2` | Best observed Free-only point so far. |
| Free-only | 4 | valid | `2179.20` | `398/2` | Below 3 workers and higher idle cost. |

Current working conclusion:

- Pro currently prefers `4` workers by a small margin.
- Free currently prefers `3` workers.
- The single-lane results are useful for pruning, but they are not enough to beat the current guarded 2-lane baseline by themselves.

### Phase 3: Two-Lane Worker Matrix

Goal: find the best current 2-lane allocation.

Run first:

- `2 + 2`
- `3 + 3`
- `4 + 4`

Then run asymmetric candidates based on Phase 2:

- `3 + 2`
- `2 + 3`
- `4 + 2`
- `2 + 4`

Optional:

- `5 + 5`, `5 + 3`, or `3 + 5` only if Phase 2 shows neither account is saturated at 4 workers.

Decision:

- Promote the top two valid 2-lane shapes to repeated soaks.
- Do not test 3 lanes until the 2-lane median is known.
- Do not keep testing worker counts above the observed ceiling unless the failure mode changes.
- If the asymmetric matrix beats all symmetric runs, carry only the best asymmetric shapes forward.

Observed current results:

| Shape | Status | VPH | Success/fail | Interpretation |
|---|---|---:|---:|---|
| `3+3` | valid | `4123.28` | `795/5` | Best 2-lane point so far; clearly ahead of the current 4+4 control. |
| `4+3` | valid | `3120.55` | `795/5` | Worse than `3+3`; the asymmetry does not help in this direction. |
| `3+4` | valid | `3396.86` | `793/7` | Better than `4+3`, but still below `3+3`. |
| `4+4` control | valid | `3153.13` median | `793/7`, `793/7`, `798/2`, `795/5` | Current baseline remains volatile, but the median is below the `3+3` result. |

Current working conclusion:

- `3+3` is the current best 2-lane shape.
- The observed Pro `4` / Free `3` asymmetry did not beat `3+3`.
- The observed Pro `3` / Free `4` asymmetry also did not beat `3+3`.
- The next useful 2-lane check is `2+2` only if you want to confirm whether the lane floor is lower than expected, otherwise move to the 3-lane sweep using `3+3` as the 2-lane reference.

### Phase 4: Profile Family Comparison

Goal: isolate whether browser profile geometry still matters under current conditions.

Run:

- Best current 2-lane worker allocation on current supported profiles.
- Same allocation on frozen historical profile geometry.
- Same allocation on fresh newly authenticated profile roots, if practical.

Decision:

- Keep only profile families that beat the supported profile median and pass auth/profile hygiene.
- If historical profiles require manual auth/cookie copying, classify them as diagnostics until tooling supports them.
- Do not compare profile families while worker count is still changing.
- If profile geometry does not move the median by at least 5 percent, keep the simpler supported family.

### Phase 5: Startup And Stagger Sweep

Goal: see whether smaller startup offsets reduce contention without wasting wall time.

Run on the best 2-lane allocation:

- no stagger
- Pro first, Free +15s
- Pro first, Free +30s
- Free first, Pro +15s
- Free first, Pro +30s

Decision:

- Keep stagger only if it beats no-stagger median by at least 10 percent and reduces failure/probe churn.
- Otherwise keep no-stagger.
- If stagger changes only the failure pattern but not the median, keep it out of the winner path.
- Do not use large staggers as a proxy for fixing auth instability.

### Phase 6: Three-Lane Lower-Pressure Sweep

Goal: test whether three accounts help only when per-account pressure is lower.

Run:

- `2 + 2 + 2`
- `3 + 3 + 3`
- best asymmetric three-lane allocation derived from Phase 2

Only repeat `4 + 4 + 4` if code or auth/profile handling changed since the recent negative runs.

Decision:

- Stop three-lane testing if it does not beat the 2-lane median by at least 15 percent.
- Stop three-lane testing if it increases invalidations, default-profile leakage, or source-add churn.
- If 3-lane candidates are close to the 2-lane median but more fragile, retain 2 lanes.
- Do not advance to phase 7 unless the best 3-lane result is clearly better or clearly worse than the 2-lane winner.

### Phase 7: Notebook State And Cleanup Sweep

Goal: test state lifecycle only after the lane/worker shape is known.

Run on the current winner:

- fresh state root
- warm state root
- reusable notebooks
- fresh notebooks
- notebook reset fallback enabled/disabled if the code path is still active

Decision:

- Favor the simpler state lifecycle unless the more complex one improves median VPH by at least 10 percent and lowers failure churn.
- Treat cleanup changes as a secondary variable unless the run log shows cleanup dominates wall time.
- If a cleanup change increases root complexity, require a larger throughput gain before keeping it.

### Phase 8: Batch And Source-Add Sizing Sweep

Goal: tune queue shape after structural bottlenecks are addressed.

Run:

- batch `175`, `200`, `225`
- subbatch `25`, `50`, `75` after smoke proof

Decision:

- Keep `200` unless another size wins repeated soaks.
- Do not promote a subbatch value that increases materialization timeouts.
- If a lower batch size reduces failures but not enough to change the median meaningfully, keep `200`.
- Only test subbatch changes after the runner can report materialization timing clearly.

### Phase 9: Retry And Probe Policy Sweep

Goal: reduce transient command/probe failures without changing success semantics.

Run only after the structural winner is stable:

- current retry policy
- higher content-fetch retry count
- longer probe delay
- lower probe delay

Decision:

- Promote only if success quality is unchanged and median VPH improves.
- Reject any setting that hides failures by reducing validation.
- Do not treat less logging as a win.
- If retry changes are needed only to survive a bad profile state, fix the profile state first.

### Phase 10: Confirmation Soaks

Goal: lock the winner.

For each top candidate:

- Run 3 fresh-root full soaks.
- Use the same cohort and lane config.
- Record median, min, max VPH.
- Record success/fail totals.
- Record post-run hygiene.
- Record invalidations and failure classes.

Promotion rule:

- Winner must beat the current baseline median by at least 10 percent.
- If two candidates are within 5 percent, choose the one with fewer workers, fewer lanes, fewer failures, and simpler auth/profile handling.
- Require 3 fresh-root repeats for any candidate that is still under consideration.
- Do not promote a candidate unless its spread is acceptable as well as its median.

## Phase Checklist

Use this checklist before every candidate run:

1. Confirm the phase and candidate name.
2. Confirm the lane config file path.
3. Confirm the cohort file path.
4. Confirm the run root is new and empty.
5. Confirm the browser roots are lane-specific.
6. Confirm the expected account mapping.
7. Confirm the runner mode.
8. Confirm the batch size and limit.
9. Confirm the summary destination.
10. Start the run.
11. Verify the summary file exists.
12. Classify the run.
13. Update `test-registry.md`.
14. Decide whether the candidate advances.

## Required Outputs

Every completed run should leave behind:

- `sharded_lane_series_summary.json`
- launcher stdout and stderr
- any smoke and soak sub-summary files
- lane-level logs
- a short classification note
- a registry update

For invalid runs, keep the same outputs and mark the invalidation reason clearly.

## Stop Conditions

Stop the sweep and reassess if any of these happen:

- repeated invalidations from the same auth or profile failure
- default NotebookLM profile leakage after cleanup
- missing or inconsistent top-level summary files
- a candidate appears better only because the sample changed
- the control spread is too large to interpret candidate wins
- a later phase depends on a prerequisite that has not yet been measured
- the benchmark runner changes in a way that makes old results non-comparable

If a stop condition triggers, do not widen the matrix. Fix the prerequisite or rebaseline first.

## Run Naming

Use names that encode the tested variable:

- `sweep_p1_2lane_4w_control_run01`
- `sweep_p2_pro_only_3w_run01`
- `sweep_p3_2lane_pro3_free2_run01`
- `sweep_p4_2lane_frozen_profiles_run01`
- `sweep_p5_2lane_pro_first_15s_run01`
- `sweep_p6_3lane_2w_run01`
- `sweep_p8_batch225_run01`

Avoid names like `rerun`, `test`, or `new_best` unless they are aliases in a separate note.

## Command Pattern

Prefer the guarded sequence for candidate classification:

```powershell
cd P:\\\\\\packages/yt-is
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config <lane-config-json> `
  --run-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/<fresh-run-root>
```

Use the raw series runner only for exact historical reconstruction or when intentionally bypassing sequence gates for diagnostics:

```powershell
cd P:\\\\\\packages/yt-is
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config <lane-config-json> `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/<fresh-run-root> `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

## Classification Rules

Classify each run as one of:

- `valid`: completed with top-level summary and no invalidation.
- `invalid`: auth/profile/default-profile/runner failure makes throughput unusable.
- `negative`: valid, but below baseline or worse on failure/complexity.
- `promising`: valid and beats baseline once, but needs repeats.
- `promoted`: valid repeated soaks beat baseline median and pass hygiene.

Hard invalidators:

- wrong account in any worker profile
- shared default NotebookLM Chrome profile used for lane work
- missing top-level summary
- dirty run root reuse
- missing post-run hygiene for guarded candidate
- interrupted run
- auth fallback not pinned to worker profile
- unexplained notebook-state loop

## Immediate Next Step

Start with Phase 1:

- Run one more current supported 2-lane `4 + 4` guarded control if only two clean repeats are available.
- Compute median and spread across `optimal_search_2lane_4w_v2`, `optimal_search_2lane_4w_v3`, and the new control.
- If the spread is acceptable, move to Phase 2 single-lane capacity.
- If the spread remains large, run two more 2-lane controls in a separate time window before testing new variables.

This avoids optimizing against a moving baseline.
