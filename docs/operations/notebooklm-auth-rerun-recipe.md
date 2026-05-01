# NotebookLM Auth Rerun Recipe

> Compact handoff for the next agent. Use this when you need a clean rerun with account-correct auth and minimal browser churn.

## What This Is For

- Validate that the Pro, Free, and Free2 lanes still map to the right Google accounts.
- Run a short forced-refresh smoke without turning every auth probe into browser churn.
- Then run a long soak only if the smoke is clean.

## Do Not Use

- Do not reuse `auth_smoke_v2` as throughput evidence.
- Do not set `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=1` unless the thing under test is browser churn itself.
- Do not let any command fall back to the default `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`.
- Do not treat a broken CDP lane as a reason to switch auth paths; the dedicated browser profile root is the repair target.
- Do not force-kill dedicated auth Chrome roots unless graceful close fails. Forced exits leave the profile marked crashed and can cause Chrome to restore stale tabs such as `0.0.0.2` on the next launch.
- Do not accept a valid session on the wrong Google account.

## Preflight

Run from `P:/packages/yt-is`.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-nlm-worker-auth sync

foreach ($profile in @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free1-worker-01', 'ytis-free1-worker-02', 'ytis-free1-worker-03', 'ytis-free1-worker-04',
  'ytis-free2-worker-01', 'ytis-free2-worker-02', 'ytis-free2-worker-03', 'ytis-free2-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Expected accounts:

- Pro: `a.hominidae@gmail.com`
- Free: `troup.hominidae@gmail.com`
- Free2: `brsthomson@hotmail.com`

If any profile reports the wrong account, stop and repair that family before benchmarking.

## Short Validation Smoke

Use this first. It proves repeated re-authentication without maximum browser churn.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = '5'
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_forced_smoke_v1 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_forced_smoke_v1/cohort.json `
  --limit 50 `
  --batch-size 50 `
  --reusable-pipeline-mode serial
Remove-Item Env:\YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
```

Pass criteria:

- Run completes.
- Logs show forced refresh activity.
- Any refresh command stays profile-pinned.
- No default NotebookLM Chrome profile appears.
- Post-run `python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync` still passes.

## Auth Stress Drill

Use this only when the goal is to exercise the refresh path as hard as possible.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = '1'
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v1 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v1/cohort.json `
  --limit 50 `
  --batch-size 50 `
  --reusable-pipeline-mode serial
Remove-Item Env:\YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
```

This is expected to open more browser windows and is not the default validation path.

## Long Soak

Use the long soak only after the short smoke is clean.

Recommended cadence:

- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=5` if you want repeated re-auth during the soak.
- unset the variable if you only want sustained throughput evidence.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = '5'
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run01 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run01/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Keep running new output roots until total wall time exceeds `75` minutes if you need endurance evidence.

## Post-Run Checks

After each run:

```powershell
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'nlm login --force|remote-debugging-port=9222|\.notebooklm-mcp-cli\\chrome-profile|notebooklm-pro|notebooklm-free|notebooklm-free-2' } |
  Select-Object ProcessId, Name, CommandLine
```

Record from `sharded_lane_series_summary.json`:

- `combined.hot_path_videos_per_hour`
- `combined.hot_path_success_count`
- `combined.hot_path_failure_count`
- `combined.processed_count`
- `combined.wall_elapsed_s`
- Per-lane success and failure counts
- Per-lane `cleanup_elapsed_s`
- Per-lane `add_elapsed_s`
- Per-lane `idle_elapsed_s`
- `content_fetch_status_counts_total`
- Any `source_add_failed` count
- Any content-fetch `NOT_FOUND` count

## Stop Conditions

- Any `nlm login --force` without `--profile`
- Any worker profile mapped to the wrong account
- Any appearance of the default NotebookLM Chrome profile
- `PERMISSION_DENIED` dominating source materialization
- A benchmark starting before all configured worker profiles pass `nlm login --check`
- A run root or smoke root being reused as if it were fresh evidence
