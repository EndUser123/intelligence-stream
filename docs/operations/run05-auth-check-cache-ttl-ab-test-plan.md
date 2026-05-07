# Run05 Auth-Check Cache TTL A/B Test Plan

Goal: run one clean, unmitigated `3+3` guarded benchmark with a longer auth-check cache TTL to see whether the 30-second cache cadence is the throughput limiter.

Audience: a follow-on LLM with limited context. Follow the steps in order. Do not infer causality beyond the decision table.

## Current Evidence

Proven from existing logs and code:

- `sweep_phase3_2lane_3w_run04` completed cleanly at `2398.89` combined VPH.
- Run04 had `106` Pro logins, `105` Free logins, `nlm_auth_forced_refresh_scheduled=0` in both lanes, and `session_age_s` values in the `0-30s` band.
- `csf/nlm_auth_guard.py` uses `auth_check_cache_ttl_seconds(default=30.0)`.
- The auth-check cache TTL is configurable with `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS`.

Not proven yet:

- Raising the auth-check cache TTL improves throughput.
- Warm-auth is the next best step.
- NotebookLM session TTL is the main limiter.

## Hard Rules

- Do not change lane width, batch size, source-add code, or warm-auth behavior.
- Do not rerun `sweep_phase3_2lane_3w_run04`.
- Do not use `--require-forced-refresh-marker`; this is not a forced-refresh test.
- Do not call the run successful unless the summary and hygiene are clean.
- If any command fails, stop and report the failed command plus the first error.

## Files To Read First

- `P:\\packages/yt-is/docs/operations/run05-auth-check-cache-ttl-ab-test-plan.md`
- `P:\\packages/yt-is/docs/operations/run04-session-reauth-diagnostic-test-plan.md`
- `P:\\packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:\\packages/yt-is/docs/operations/test-registry.md`

Treat run04 as the baseline comparator.

## Task 1: Preflight

Run this from PowerShell:

```powershell
cd P:\\packages/yt-is

$RunId = "sweep_phase3_2lane_3w_run05"
$RunRoot = "P:\\packages/yt-is/.logs/sharded_lane_series/$RunId"
$LaunchRoot = "P:\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/$RunId"

if (Test-Path $RunRoot) {
    throw "Run root already exists: $RunRoot. Pick the next unused suffix and update this plan before running."
}

$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = $null
$env:YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS = "120"

New-Item -ItemType Directory -Force -Path $LaunchRoot | Out-Null
[ordered]@{
    captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    cwd = (Get-Location).Path
    run_id = $RunId
    run_root = $RunRoot
    YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = $env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
    YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS = $env:YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS
    YTIS_NLM_AUTH_NONINTERACTIVE = $env:YTIS_NLM_AUTH_NONINTERACTIVE
    NOTEBOOKLM_PROFILE = $env:NOTEBOOKLM_PROFILE
    YTIS_NLM_BROWSER_PROFILE_ROOT = $env:YTIS_NLM_BROWSER_PROFILE_ROOT
    python = (Get-Command python).Source
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 "$LaunchRoot/env_snapshot.json"

Get-Content "$LaunchRoot/env_snapshot.json"
```

Expected:

- `Test-Path` does not throw.
- `env_snapshot.json` exists under `P:\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/sweep_phase3_2lane_3w_run05/`.
- `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS` is `120` in the snapshot.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is `null` or empty in the snapshot.

## Task 2: Verify Code Before Launch

Run:

```powershell
python -m pytest tests/test_nlm_auth_guard.py tests/test_nlm_batch.py tests/test_sharded_lane_series.py tests/test_sharded_lane_sequence.py tests/test_sharded_lane_summary.py -q
python -m py_compile csf/nlm_auth_guard.py csf/nlm_batch.py csf/sharded_lane_series.py csf/sharded_lane_sequence.py csf/sharded_lane_summary.py bin/csf-source bin/csf-sharded-lane-sequence bin/csf-sharded-lane-summary
python P:\\packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Expected:

- Pytest passes.
- `py_compile` exits `0`.
- Worker auth sync exits `0` and does not open the shared default NotebookLM Chrome profile.

If this fails, do not run the benchmark. Report the failing command.

## Task 3: Launch Run05

Run:

```powershell
cd P:\\packages/yt-is

$RunId = "sweep_phase3_2lane_3w_run05"
$RunRoot = "P:\\packages/yt-is/.logs/sharded_lane_series/$RunId"
$LaunchRoot = "P:\\packages/yt-is/.logs/sharded_lane_series_launcher_logs/$RunId"

python P:\\packages/yt-is/bin/csf-sharded-lane-sequence `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/tmp_pro_free_3w.json `
  --run-root $RunRoot `
  2>&1 | Tee-Object -FilePath "$LaunchRoot/sequence.output.txt"

if ($LASTEXITCODE -ne 0) {
    throw "csf-sharded-lane-sequence failed with exit code $LASTEXITCODE"
}
```

Expected:

- Smoke completes first.
- Soak completes after smoke.
- Top-level summary is written to `P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run05/sharded_lane_series_summary.json`.

## Task 4: Monitor Progress Without Guessing

In a second PowerShell terminal, run:

```powershell
$RunRoot = "P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run05"
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
$RunRoot = "P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run05"

python P:\\packages/yt-is/bin/csf-sharded-lane-summary --run-root $RunRoot
python P:\\packages/yt-is/bin/csf-run-evidence-check --run-root "$RunRoot/smoke"
python P:\\packages/yt-is/bin/csf-run-evidence-check --run-root "$RunRoot/soak"
python P:\\packages/yt-is/bin/csf-run-failure-analyzer --run-root $RunRoot
```

Expected:

- `csf-sharded-lane-summary` prints candidate, status, hygiene, VPH, wall time, add time, idle wait, success/fail/processed, and lane count.
- Evidence check exits `0` for smoke and soak unless the run is invalid.
- Failure analyzer output is saved or pasted into the handoff.

## Task 6: Count Auth And Session-Age Events

Run this exact script:

```powershell
@'\nimport json\nimport pathlib\nimport statistics\n\nrun_root = pathlib.Path(\"P:\\packages/yt-is/.logs/sharded_lane_series/sweep_phase3_2lane_3w_run05\")\nfor lane_dir in sorted((run_root / \"soak\").iterdir()):\n    if not lane_dir.is_dir() or lane_dir.name.startswith(\"cohort.\"):\n        continue\n    files = list(lane_dir.glob(\"**/term_*.jsonl\"))\n    if not files:\n        continue\n    counts = {}\n    session_ages = []\n    for fp in files:\n        with fp.open(\"r\", encoding=\"utf-8\", errors=\"replace\") as f:\n            for line in f:\n                if not line.strip():\n                    continue\n                try:\n                    event = json.loads(line)\n                except json.JSONDecodeError:\n                    continue\n                action = event.get(\"action\", \"\")\n                data = event.get(\"data\") or {}\n                counts[action] = counts.get(action, 0) + 1\n                if action == \"nlm_auth_checked\" and data.get(\"session_age_s\") is not None:\n                    session_ages.append(float(data[\"session_age_s\"]))\n    print()\n    print(f\"lane={lane_dir.name}\")\n    print(f\"term_jsonl_files={len(files)}\")\n    print(f\"nlm_login_started={counts.get('nlm_login_started', 0)}\")\n    print(f\"nlm_family_refresh_started={counts.get('nlm_family_refresh_started', 0)}\")\n    print(f\"nlm_auth_forced_refresh_scheduled={counts.get('nlm_auth_forced_refresh_scheduled', 0)}\")\n    print(f\"nlm_auth_checked={counts.get('nlm_auth_checked', 0)}\")\n    print(f\"session_age_event_count={len(session_ages)}\")\n    if session_ages:\n        print(f\"session_age_min={min(session_ages):.3f}\")\n        print(f\"session_age_median={statistics.median(session_ages):.3f}\")\n        print(f\"session_age_max={max(session_ages):.3f}\")\n'@ | python -\n```\n\nExpected:\n\n- `nlm_auth_forced_refresh_scheduled` should be `0`.\n- `session_age_event_count` should be greater than `0`.\n- If login counts are high but `session_age_event_count=0`, stop and inspect the auth path or log parsing.\n\n## Task 7: Interpret Run05\n\nUse this decision table exactly:\n\n| Observation | Conclusion |\n|---|---|\n| `status!=\"ok\"` or hygiene not clean | Invalid run. Do not use VPH for config selection. Investigate the first invalidation. |\n| `nlm_auth_forced_refresh_scheduled>0` | Forced-refresh contamination occurred. Do not use this as a cache-TTL result. |\n| Cache TTL 120 lowers logins materially and raises VPH vs run04 | Auth-check cache TTL is the limiting knob. Consider a higher sustained TTL or a smaller follow-up A/B. |\n| Cache TTL 120 lowers logins but VPH stays flat | The auth cache is part of the problem, but something else is still capping throughput. Investigate source-add/readiness/setup stages. |\n| Cache TTL 120 does not change logins or VPH | Auth-check cache TTL is not the main limiter. Investigate another stage or revert to the prior diagnosis. |\n\nDefinitions:\n\n- \"Materially\" means at least a 20% reduction in `nlm_login_started` count versus run04.\n- \"Flat\" means combined hot-path VPH is within 10% of `2398.89`.\n\n## Task 8: Update Docs After Run05\n\nUpdate these files only after the run finishes:\n\n- `P:\\packages/yt-is/docs/operations/test-registry.md`\n- `P:\\packages/yt-is/docs/operations/sharded-lane-series.md`\n- `P:\\packages/yt-is/docs/operations/optimal-throughput-candidate-test-plan.md`\n\nRequired wording discipline:\n\n- Say \"auth-check cache TTL raised throughput\" only if run05 beats run04 on both VPH and login count.\n- Do not say \"NotebookLM TTL\" unless a separate run tests that directly.\n- Do not say \"warm-auth is the fix\" until an A/B run beats the cache-TTL baseline.\n\n## Final Handoff Format\n\nReport exactly these fields:\n\n```text\nrun_id:\nrun_root:\nstatus:\nhygiene:\ncombined_vph:\nsuccess/fail/processed:\nwall_s:\nadd_s:\nidle_wait_s:\nforced_refresh_scheduled_by_lane:\nlogin_started_by_lane:\nsession_age_event_count_by_lane:\nsession_age_range_by_lane:\ndecision_table_row_used:\nconclusion:\nnext_action:\n```\n*** End Patch\n'@ | apply_patch","workdir":"P:\\\packages\\yt-is","timeout_ms":30000}}]}
