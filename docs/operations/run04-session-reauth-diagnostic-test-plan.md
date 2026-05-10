# Run04 Session Reauth Diagnostic Test Plan

Goal: run one clean, unmitigated `3+3` guarded benchmark to determine whether run02/run03 throughput loss is explained by measured session-age/reauth timing.

Audience: a follow-on LLM with limited context. Follow the steps in order. Do not infer causality beyond the decision table.

## Current Evidence

Proven from existing JSONL logs:

- `sweep_phase3_2lane_3w_run01`: `4123.28` VPH, `0` `nlm_login_started`, `0` `nlm_auth_forced_refresh_scheduled`.
- `sweep_phase3_2lane_3w_run02`: `2953.82` VPH, `151` total `nlm_login_started`, `0` `nlm_auth_forced_refresh_scheduled`.
- `sweep_phase3_2lane_3w_run03`: `2384.21` VPH, `181` total `nlm_login_started`, `0` `nlm_auth_forced_refresh_scheduled`.
- `pro_free_source_map_v1`: `5572.04` VPH, `0` `nlm_login_started`, `0` `nlm_auth_forced_refresh_scheduled`, but it used `4+4`, not `3+3`.

Not proven yet:

- NotebookLM session TTL is not proven.
- The auth-check cache TTL is now the leading hypothesis because the measured `session_age_s` band in run04 matches the default 30s cache TTL in `csf/nlm_auth_guard.py`.
- Warm-auth is not proven. It is a candidate mitigation for a later A/B test.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` parent-shell contamination is not proven for run02/run03. The scheduled force-refresh path was not observed.

## Run04 Outcome

Run04 completed cleanly, and it moved the diagnosis away from NotebookLM TTL toward the local auth-check cache TTL.

- `status="ok"`
- `hygiene="clean"`
- combined hot-path VPH: `2398.89`
- success/fail/processed: `791/9/800`
- wall time: `1187.050s`
- add time: `1245.643s`
- idle wait: `721.360s`
- `nlm_auth_forced_refresh_scheduled=0` in both lanes
- `nlm_login_started=106` on Pro, `105` on Free
- `session_age_event_count=531` on Pro, `519` on Free
- `session_age_s` ranged from `0.032` to `29.630` on Pro and `0.005` to `29.979` on Free, with medians of about `15s`

Interpretation:

- The run matched the decision-table row "High logins, forced scheduled `0`, session-age events present, and login/session ages cluster tightly".
- The `session_age_s` band is capped near the default 30s auth-check cache TTL, so the next useful investigation is whether raising `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS` improves throughput.
- Warm-auth is now a later follow-up, not the next diagnostic step.

## Hard Rules

- Do not run warm-auth in run04.
- Do not use `--require-forced-refresh-marker`; run04 is not a forced-refresh test.
- Do not change lane width, batch size, source-add code, auth code, or cleanup code before run04.
- Do not reuse an existing run root.
- Do not call TTL confirmed unless `session_age_s` or equivalent timing evidence supports it.
- If any command fails, stop and report the failed command plus the first error.

## Files To Read First

- `P:\\\\\\packages/yt-is/docs/operations/run04-session-reauth-diagnostic-test-plan.md`
- `P:\\\\\\packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`
- `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run03/DIAGNOSTIC.md`

Treat `DIAGNOSTIC.md` as historical analysis with known overclaims, not as authority.

## Task 1: Preflight

Run this from PowerShell:

```powershell
cd P:\\\\\\packages/yt-is

$RunId = "sweep_phase3_2lane_3w_run04"
$RunRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/$RunId"
$LaunchRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/$RunId"

if (Test-Path $RunRoot) {
    throw "Run root already exists: $RunRoot. Pick the next unused suffix and update this plan before running."
}

$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = $null

New-Item -ItemType Directory -Force -Path $LaunchRoot | Out-Null
[ordered]@{
    captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    cwd = (Get-Location).Path
    run_id = $RunId
    run_root = $RunRoot
    YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = $env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
    YTIS_NLM_AUTH_NONINTERACTIVE = $env:YTIS_NLM_AUTH_NONINTERACTIVE
    NOTEBOOKLM_PROFILE = $env:NOTEBOOKLM_PROFILE
    YTIS_NLM_BROWSER_PROFILE_ROOT = $env:YTIS_NLM_BROWSER_PROFILE_ROOT
    python = (Get-Command python).Source
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 "$LaunchRoot/env_snapshot.json"

Get-Content "$LaunchRoot/env_snapshot.json"
```

Expected:

- `Test-Path` does not throw.
- `env_snapshot.json` exists under `P:\\\\\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/sweep_phase3_2lane_3w_run04/`.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is `null` or empty in the snapshot.

## Task 2: Verify Code Before Launch

Run:

```powershell
python -m pytest tests/test_nlm_auth_guard.py tests/test_nlm_batch.py tests/test_csf_source_fetch_timing.py tests/test_sharded_lane_series.py tests/test_sharded_lane_sequence.py tests/test_sharded_lane_summary.py tests/test_fallback_crossover_benchmark.py -q
python -m py_compile csf/nlm_auth_guard.py csf/nlm_batch.py csf/sharded_lane_series.py csf/sharded_lane_sequence.py csf/sharded_lane_summary.py bin/csf-source bin/csf-sharded-lane-sequence bin/csf-sharded-lane-summary bin/csf-fallback-crossover-benchmark
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Expected:

- Pytest passes.
- `py_compile` exits `0`.
- Worker auth sync exits `0` and does not open the shared default NotebookLM Chrome profile.

If this fails, do not run the benchmark. Report the failing command.

## Task 3: Launch Run04

Run:

```powershell
cd P:\\\\\\packages/yt-is

$RunId = "sweep_phase3_2lane_3w_run04"
$RunRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/$RunId"
$LaunchRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/$RunId"

python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/tmp_pro_free_3w.json `
  --run-root $RunRoot `
  2>&1 | Tee-Object -FilePath "$LaunchRoot/sequence.output.txt"

if ($LASTEXITCODE -ne 0) {
    throw "csf-sharded-lane-sequence failed with exit code $LASTEXITCODE"
}
```

Expected:

- Smoke completes first.
- Soak completes after smoke.
- Top-level summary is written to `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run04/sharded_lane_series_summary.json`.

## Task 4: Monitor Progress Without Guessing

In a second PowerShell terminal, run:

```powershell
$RunRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run04"
Get-ChildItem -Path $RunRoot -Recurse -Filter benchmark_progress.jsonl -ErrorAction SilentlyContinue | Select-Object FullName,Length,LastWriteTime
Get-ChildItem -Path $RunRoot -Recurse -Filter sharded_lane_series_summary.json -ErrorAction SilentlyContinue | Select-Object FullName,Length,LastWriteTime
```

If a `benchmark_progress.jsonl` exists, tail it:

```powershell
Get-Content "<paste benchmark_progress.jsonl path here>" -Wait
```

Do not report final VPH until the top-level `sharded_lane_series_summary.json` exists.

## Task 5: Post-Run Summary

Run:

```powershell
$RunRoot = "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run04"

python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-summary --run-root $RunRoot
python P:\\\\\\packages/yt-is/bin/csf-run-evidence-check --run-root "$RunRoot/smoke"
python P:\\\\\\packages/yt-is/bin/csf-run-evidence-check --run-root "$RunRoot/soak"
python P:\\\\\\packages/yt-is/bin/csf-run-failure-analyzer --run-root $RunRoot
```

Expected:

- `csf-sharded-lane-summary` prints candidate, status, hygiene, VPH, wall time, add time, idle wait, success/fail/processed, and lane count.
- Evidence check exits `0` for smoke and soak unless the run is invalid.
- Failure analyzer output is saved or pasted into the handoff.

## Task 6: Count Auth And Session-Age Events

Run this exact script:

```powershell
@'
import json
import pathlib

run_root = pathlib.Path("P:\\\\\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run04")
for lane_dir in sorted((run_root / "soak").iterdir()):
    if not lane_dir.is_dir() or lane_dir.name.startswith("cohort."):
        continue
    files = list(lane_dir.glob("**/term_*.jsonl"))
    if not files:
        continue
    counts = {}
    session_ages = []
    first_login = None
    last_login = None
    first_wait = None
    last_wait = None
    for fp in files:
        with fp.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = event.get("action", "")
                counts[action] = counts.get(action, 0) + 1
                ts = event.get("timestamp") or event.get("ts")
                if action == "nlm_login_started" and ts:
                    first_login = min(first_login, ts) if first_login else ts
                    last_login = max(last_login, ts) if last_login else ts
                if action == "nlm_batch_source_materialization_wait_started" and ts:
                    first_wait = min(first_wait, ts) if first_wait else ts
                    last_wait = max(last_wait, ts) if last_wait else ts
                if event.get("session_age_s") is not None:
                    session_ages.append(float(event["session_age_s"]))
    print()
    print(f"lane={lane_dir.name}")
    print(f"term_jsonl_files={len(files)}")
    print(f"nlm_login_started={counts.get('nlm_login_started', 0)}")
    print(f"nlm_family_refresh_started={counts.get('nlm_family_refresh_started', 0)}")
    print(f"nlm_auth_forced_refresh_scheduled={counts.get('nlm_auth_forced_refresh_scheduled', 0)}")
    print(f"nlm_auth_checked={counts.get('nlm_auth_checked', 0)}")
    print(f"materialization_wait_started={counts.get('nlm_batch_source_materialization_wait_started', 0)}")
    print(f"first_login={first_login}")
    print(f"last_login={last_login}")
    print(f"first_wait={first_wait}")
    print(f"last_wait={last_wait}")
    print(f"session_age_event_count={len(session_ages)}")
    if session_ages:
        print(f"session_age_min={min(session_ages):.3f}")
        print(f"session_age_max={max(session_ages):.3f}")
        print(f"session_age_values_top10={sorted(session_ages)[-10:]}")
'@ | python -
```

Expected:

- `nlm_auth_forced_refresh_scheduled` should be `0`. If it is nonzero, this run is contaminated by forced-refresh behavior.
- `session_age_event_count` should be greater than `0` if the new instrumentation is active in the relevant path.
- If login counts are high but `session_age_event_count=0`, report instrumentation gap instead of claiming TTL.

## Task 7: Interpret Run04

Use this decision table exactly:

| Observation | Conclusion |
|---|---|
| `status!="ok"` or hygiene not clean | Invalid run. Do not use VPH for config selection. Investigate the first invalidation. |
| `nlm_auth_forced_refresh_scheduled>0` | Forced-refresh contamination occurred. Do not use this as TTL evidence. |
| High logins, forced scheduled `0`, session-age events present, and login/session ages cluster tightly | TTL/session-age hypothesis supported. Next run should be a warm-auth A/B mitigation test. |
| High logins, forced scheduled `0`, but session ages are scattered or absent | TTL not proven. Investigate the reauth trigger or instrumentation path before mitigation. |
| Low logins and VPH returns near run01 or better | Prior low runs were timing/session-state sensitive. Repeat same config before promotion. |
| Low logins but VPH stays low | Auth is not the main throughput limiter. Investigate source-add/readiness/setup stage budgets. |

Definitions:

- "High logins" means more than `50` `nlm_login_started` events per lane in soak.
- "Low logins" means `0` to `10` `nlm_login_started` events per lane in soak.
- "Near run01" means combined hot-path VPH within `10%` of `4123.28`, or at least `3710.95`.

## Task 8: Update Docs After Run04

Update these files only after the run finishes:

- `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`
- `P:\\\\\\packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:\\\\\\packages/yt-is/docs/operations/optimal-throughput-candidate-test-plan.md`

Required wording discipline:

- Say "forced-refresh scheduled path was not observed" if `nlm_auth_forced_refresh_scheduled=0`.
- Say "TTL/session-age hypothesis is supported" only if session-age evidence supports it.
- Do not say "root cause identified" unless the evidence directly matches the decision table.
- Do not say "warm-auth is the fix" until a separate warm-auth A/B run beats the unmitigated run.

## Final Handoff Format

Report exactly these fields:

```text
run_id:
run_root:
status:
hygiene:
combined_vph:
success/fail/processed:
wall_s:
add_s:
idle_wait_s:
forced_refresh_scheduled_by_lane:
login_started_by_lane:
session_age_event_count_by_lane:
session_age_range_by_lane:
decision_table_row_used:
conclusion:
next_action:
```
