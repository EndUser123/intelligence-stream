# Optimal Throughput Candidate Test Plan

Created: 2026-05-04

## Purpose

Find the highest sustained NotebookLM hot-path throughput method that still satisfies the current operational safety contract.

Do not describe any method as "optimal" until it has won a controlled sweep and repeated full soaks. Until then, use "current best proven" for existing evidence and "candidate" for new shapes.

## Current Evidence

Current best proven sustained throughput:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/sharded_lane_series_summary.json`
- Shape: Pro + Free, 4 workers per lane, batch size 200, no lane startup stagger
- Combined hot-path VPH: `5572.04`
- Result mix: `796` hot-path successes, `4` failures, `800` processed
- Contract: Whisper recovery excluded from hot-path throughput

Current best guarded operational run:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run19/sharded_lane_series_summary.json`
- Shape: guarded sequence, Pro + Free, 4 workers per lane, batch size 200
- Combined hot-path VPH: `3110.09`
- Result mix: `796` hot-path successes, `4` failures, `800` processed
- Post-run hygiene: `status="clean"`, `detected_count=0`, `reaped_count=0`

Interpretation:

- `pro_free_source_map_v1` is the current best proven throughput result.
- `pro_free_auth_soak_v1_run19` proves the newer guarded sequence and hygiene contract, but it does not beat the best throughput result.
- The search space has not been tested thoroughly enough to claim a true optimum.

## Non-Negotiable Contracts

Use these rules for every candidate:

- Use combined hot-path VPH as the main throughput metric.
- Exclude Whisper recovery from sustained hot-path throughput.
- Use completed-worker totals and stage timings, not backlog-derived scan rates.
- Use fresh, empty run roots for every run.
- Keep dedicated browser roots per lane.
- Run doctor before smoke.
- Run the browser health gate after doctor and before smoke; record `browser_health.json` and treat persistent shared default-profile churn as a stop signal.
- Run smoke before soak.
- Run evidence check before any long soak.
- Require the final top-level summary.
- Require `post_run_hygiene.status == "clean"`.
- Treat invalidated runs as diagnostics only, never as throughput winners.
- Do not reuse dirty run roots.

## Known Lane Configs

Two-lane baseline:

- Config: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json`
- Lanes:
- `a_hominidae_pro` through `P:\\.data/yt-is/browser/notebooklm-pro`
- `troup_hominidae_free` through `P:\\.data/yt-is/browser/notebooklm-free`

Three-lane candidate:

- Config: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_hotmail_lanes.json`
- Lanes:
- `a_hominidae_pro` through `P:\\.data/yt-is/browser/notebooklm-pro`
- `troup_hominidae_free` through `P:\\.data/yt-is/browser/notebooklm-free`
- `brsthomson_hotmail_free` through `P:\\.data/yt-is/browser/notebooklm-free-2`

If testing lower worker counts, create a temporary lane config under `P:\\packages/yt-is/.logs/sharded_lane_series/` and reduce both `workers` and `notebooklm_profiles` consistently. Example: a 3-worker lane must list exactly `worker-01` through `worker-03`.

## Preflight

Run these before the candidate sweep:

```powershell
cd P:\\packages/yt-is
python -m pytest tests/test_nlm_worker_auth.py tests/test_nlm_batch.py tests/test_sharded_lane_series.py tests/test_sharded_lane_sequence.py tests/test_run_failure_analyzer.py -q
python -m py_compile csf/nlm_worker_auth.py csf/nlm_batch.py csf/sharded_lane_series.py csf/sharded_lane_sequence.py csf/run_failure_analyzer.py bin/csf-nlm-worker-auth bin/csf-source bin/csf-sharded-lane-sequence bin/csf-run-evidence-check bin/csf-run-failure-analyzer
python P:\\packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Then run doctor for the next candidate root:

```powershell
python P:\\packages/yt-is/bin/csf-nlm-worker-auth doctor --lane-config <lane-config> --run-root <fresh-run-root>
```

Stop if any auth profile opens the shared default NotebookLM Chrome profile:

- Forbidden profile root: `C:/Users/brsth/.notebooklm-mcp-cli/chrome-profile`
- The guarded sequence should reap transient leaks, but a persistent leak after cleanup is a failed candidate.

## Candidate Matrix

Run the matrix in this order. Do not expand the matrix until these candidates are classified.

### Candidate A: Current Guarded Baseline

Purpose: establish the current guarded 2-lane baseline immediately before testing 3 lanes.

- Config: `pro_free_lanes.json`
- Lanes: 2
- Workers per lane: 4
- Batch size: 200
- Soak limit: 400 per lane
- Fresh root example: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_4w_v1`

Command:

```powershell
python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_4w_v1
```

### Candidate B: Three Lanes, Lower Per-Lane Pressure

Purpose: test whether a third account improves throughput without increasing per-account pressure.

- Config: create `tmp_optimal_search_3lane_3w.json` from `pro_free_hotmail_lanes.json`
- Lanes: 3
- Workers per lane: 3
- Batch size: 200
- Soak limit: 400 per lane
- Fresh root example: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_3w_v1`

Command:

```powershell
python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/tmp_optimal_search_3lane_3w.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_3w_v1
```

### Candidate C: Three Lanes, Current Per-Lane Pressure

Purpose: test the raw upside of adding a second Free account at the current 4-worker lane shape.

- Config: `pro_free_hotmail_lanes.json`
- Lanes: 3
- Workers per lane: 4
- Batch size: 200
- Soak limit: 400 per lane
- Fresh root example: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_4w_v1`

Command:

```powershell
python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_hotmail_lanes.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_4w_v1
```

### Candidate D: Two Lanes, Higher Per-Lane Pressure

Purpose: test whether worker count beats lane count before adding account complexity.

- Config: create `tmp_optimal_search_2lane_5w.json` from `pro_free_lanes.json`
- Lanes: 2
- Workers per lane: 5
- Batch size: 200
- Soak limit: 400 per lane
- Fresh root example: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_5w_v1`

Only run this if Candidate B or C does not clearly beat Candidate A.

Command:

```powershell
python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/tmp_optimal_search_2lane_5w.json `
  --run-root P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_5w_v1
```

Observed result:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_5w_v1/sharded_lane_series_summary.json`
- Status: `ok`
- Combined hot-path VPH: `3239.04`
- Result mix: `792` hot-path successes, `8` failures, `800` processed
- Interpretation: completed cleanly, but it did not beat the current `3+3` 2-lane leader, so it is a negative branch.

Floor test result:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_2lane_2w_v1/sharded_lane_series_summary.json`
- Status: `ok`
- Combined hot-path VPH: `2815.36`
- Result mix: `793` hot-path successes, `7` failures, `800` processed
- Post-run hygiene: `clean`
- Interpretation: this is the actual 2+2 floor test. It completed cleanly, but it was below the `3+3` leader and below the `2+5` branch.

### Candidate E: Three Lanes, Low Pressure Fallback

Purpose: isolate whether 3 lanes are useful only at lower per-account pressure.

- Config: create `tmp_optimal_search_3lane_2w.json` from `pro_free_hotmail_lanes.json`
- Lanes: 3
- Workers per lane: 2
- Batch size: 200
- Soak limit: 400 per lane
- Fresh root example: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_2w_v1`

Only run this if Candidate B or C has promising throughput but shows profile pressure, source-add churn, or NotebookLM recovery storms.

Observed result:

- Artifact: `P:\\packages/yt-is/.logs/sharded_lane_series/optimal_search_3lane_2w_v1/sharded_lane_series_summary.json`
- Status: `ok`
- Combined hot-path VPH: `2665.18`
- Result mix: `1143` hot-path successes, `7` failures, `1150` processed
- Post-run hygiene: `clean`
- Interpretation: the low-pressure 3-lane fallback completed cleanly, but it was below both the `3+3` 2-lane leader and the `2+5` branch.

## Promotion Rules

A candidate is eligible for promotion only if all are true:

- Top-level summary exists.
- `status == "ok"`.
- `failure_count == 0` at the lane summary level.
- `combined.hot_path_success_count_total >= 0.98 * combined.processed_count_total`.
- `post_run_hygiene.status == "clean"`.
- No persistent default NotebookLM Chrome profile remains after the run.
- The run has no unexplained missing terminal marker, notebook-state loop, or auth fallback.

Ranking metric:

- Primary: `combined.hot_path_videos_per_hour`
- Tie-breaker 1: lower `combined.fail_count_total`
- Tie-breaker 2: lower `worker_idle_wait_s_total`
- Tie-breaker 3: lower `source_ready_age_s_avg`
- Tie-breaker 4: lower default-profile reap count

Do not promote a candidate from one lucky run. Promote the top two candidates to repeated full soaks.

## Repeated Soak Lock-In

For each promoted candidate:

- Run 3 fresh-root full soaks.
- Use the same lane config and same sample family.
- Record median combined hot-path VPH.
- Record min and max combined hot-path VPH.
- Record total success/failure counts.
- Record post-run hygiene for each run.

Winner:

- Highest median combined hot-path VPH among candidates that pass every safety gate.
- If the highest median has much higher variance or repeated recovery storms, choose the lower-variance candidate unless the VPH gap is large enough to matter operationally.

## Current Lock-In

Based on the valid repeated runs available now:

- `3+3` is the current best observed 2-lane shape at `4123.28`.
- `3+3` is not fully locked in yet because it still needs repeated fresh-root full soaks.
- The fresh repeat `sweep_phase3_2lane_3w_run02` came in at `2953.82`, so the current `3+3` window is still noisy and needs more than one repeat before being called stable.
- `2lane_4w` is the current repeated 4-worker control, not the overall winner.
- The valid `2lane_4w` control runs are `4213.19`, `3573.61`, and `3227.63`, for a median of `3573.61`.
- All three `2lane_4w` control runs were `status="ok"` with `post_run_hygiene.status="clean"`.
- `3lane_4w` is not the winner despite a single high spike.
- Its valid runs are `4481.37`, `2290.88`, and `1466.83`, for a median of `2290.88`.
- One `3lane_4w` repeat invalidated, so treat that shape as a diagnostic branch rather than the promoted winner.
- `optimal_search_3lane_4w_v5` completed cleanly at `1500.58`, but it is still negative throughput evidence and does not change the lock-in path.
- The run04 repeat came in at `2398.89`, which confirms the low `3+3` window was not just a one-off; the sustained `3+3` ceiling is still not locked.
- `sweep_phase3_2lane_3w_run05` completed cleanly at `1958.94`, but it did not improve throughput or reduce login churn. It raised the Pro/Free login counts to `132` / `128` and still kept `session_age_s` in the `0-30s` band, so the `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS=120` A/B is now negative evidence.
- Operationally, keep `3+3` as a diagnostic branch and use `2lane_4w` as the current repeated control until the reauth question is resolved.
- Operationally after run05, pivot away from auth-check cache TTL and inspect source-add/readiness/setup cost or another non-TTL limiter.

## Next Agent Run Packet

Purpose: record the bounded `run05` A/B result and point the next LLM at the now-unblocked follow-on question.
Run05 completed and was negative, so do not rerun this packet as-is. The next follow-on should pivot to source-add/readiness/setup cost or another non-TTL limiter.

Historical reference only: [Run05 Auth-Check Cache TTL A/B Test Plan](run05-auth-check-cache-ttl-ab-test-plan.md).

Historical run target:

- Candidate: `Pro+Free`, 2 lanes, 3 workers per lane, batch size 200.
- Lane config: `P:\\packages/yt-is/.logs/sharded_lane_series/tmp_pro_free_3w.json`.
- Fresh run root: `P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run05`.
- Success metric: combined hot-path VPH from `combined.hot_path_videos_per_hour`.
- Safety gates: top-level `status="ok"`, `post_run_hygiene.status="clean"`, no soak-time `default_profile_running`, and no dirty root reuse.

Do not change lane width, batch size, or source-add code. The only intended change is `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS`.

## Post-Run Analysis

After every candidate run:

```powershell
python P:\\packages/yt-is/bin/csf-sharded-lane-summary --run-root <run-root>
python P:\\packages/yt-is/bin/csf-run-evidence-check --run-root <run-root>/smoke
python P:\\packages/yt-is/bin/csf-run-failure-analyzer --run-root <run-root>
```

Also inspect:

- `<run-root>/sharded_lane_series_summary.json`
- `<run-root>/soak/sharded_lane_series_summary.json`
- `<run-root>/smoke/sharded_lane_series_summary.json`
- `<run-root>/sharded_lane_series_summary.json` `post_run_hygiene`

If a run is invalidated, diagnose it before continuing the matrix. Do not stack multiple failed candidates without explaining the first failure.

## Stop Rules

Stop the sweep and investigate before continuing if any of these happen:

- A shared default NotebookLM Chrome profile remains after post-run cleanup.
- A lane uses the wrong browser root or account family.
- Any candidate opens an account chooser unexpectedly.
- A run root is dirty before launch.
- Smoke fails.
- Evidence check fails.
- A 3-lane candidate causes repeated source-add zero-growth, content `NOT_FOUND` storms, or default-profile reaps beyond isolated transients.
- The same worker profile fails auth twice in a row.

## Expected Outcomes

Possible outcomes:

- 3 lanes beat 2 lanes cleanly: promote the best 3-lane shape to repeated full soaks.
- 3 lanes improve throughput but increase recovery churn: test 3 lanes at lower worker count before deciding.
- 3 lanes regress: keep 2-lane guarded sequence as the operational method and investigate why `pro_free_source_map_v1` was much faster than newer guarded runs.
- 2 lanes with 5 workers beats 3 lanes: prefer fewer account lanes until the third account has a clear benefit.

Final deliverable from the next LLM:

- Update `docs/operations/test-registry.md` with each candidate result.
- Update `docs/operations/sharded-lane-series.md` with the new current best proven method.
- Leave full run roots local under `.logs/sharded_lane_series`.
- Keep only small durable proof artifacts under version control according to the evidence policy.
