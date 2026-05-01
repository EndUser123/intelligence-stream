# Sharded Lane Series

Use this benchmark to test whether independent NotebookLM account lanes can exceed the current single-lane sustained hot-path ceiling.

## Current Hypothesis

The best single-lane sustained hot-path result is about `3928` videos/hour on the narrow/captioned cohort with:

- `4` workers
- benchmark `--batch-size 200`
- serial reusable pipeline mode
- Whisper excluded from hot-path VPH

The sharded lane test runs matched CLI/browser account lanes concurrently and reports combined hot-path VPH from wall-clock elapsed time.

## Lane Contract

Each lane must have an isolated namespace for:

- `NOTEBOOKLM_PROFILE`
- worker `NOTEBOOKLM_PROFILE` prefix
- optional explicit worker `NOTEBOOKLM_PROFILE` list
- Chrome browser profile root
- Chrome browser profile directory
- worker state root
- worker notebook title prefix

The CLI profile and Chrome profile directory for a lane must represent the same Google account.
When the worker auth profiles are not prefix-derived, set `notebooklm_profiles` to the exact CLI profile names in worker order.
For the YT-IS Pro/Free lanes, keep the browser roots lane-specific and persistent:

- Pro root: `P:/.data/yt-is/browser/notebooklm-pro`
- Free root: `P:/.data/yt-is/browser/notebooklm-free`

For the current Pro/Free/Free2 run, the account mapping is:

- `a.hominidae@gmail.com` -> `a_hominidae_pro`
- `troup.hominidae@gmail.com` -> `troup_hominidae_free`
- `brsthomson@hotmail.com` -> `brsthomson_hotmail_free`

To add a 4th auth family, follow the extension recipe in [NotebookLM Auth Family Extension Guide](notebooklm-auth-family-extension.md). The short version is: add one new `AuthFamily` in `csf/nlm_worker_auth.py`, add one new lane entry in the lane JSON, give it its own browser root, browser profile directory, worker-state root, notebook prefix, and CDP port, then add matching tests and sync/check the new worker `01` before benchmarking.

## Example Config

Save a config like this as `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json` after confirming which Chrome profile directory is Pro vs Free:

```json
[
  {
    "lane": "a_hominidae_pro",
    "account_class": "pro",
    "workers": 4,
    "notebooklm_profile_prefix": "ytis-pro-worker",
    "notebooklm_profiles": ["ytis-pro-worker-01", "ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04"],
    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-pro",
    "browser_profile_directory": "Profile",
    "worker_state_root": "P:/packages/yt-is/.logs/sharded_lane_series/a_hominidae_pro/worker_states",
    "notebook_prefix": "benchmark-shard-a-hominidae-pro"
  },
  {
    "lane": "troup_hominidae_free",
    "account_class": "free",
    "workers": 4,
    "notebooklm_profile_prefix": "ytis-free1-worker",
    "notebooklm_profiles": ["ytis-free1-worker-01", "ytis-free1-worker-02", "ytis-free1-worker-03", "ytis-free1-worker-04"],
    "browser_profile_root": "P:/.data/yt-is/browser/notebooklm-free",
    "browser_profile_directory": "Default",
    "worker_state_root": "P:/packages/yt-is/.logs/sharded_lane_series/troup_hominidae_free/worker_states",
    "notebook_prefix": "benchmark-shard-troup-hominidae-free"
  }
]
```

## Command

Workers are lane-specific in the JSON config.

## Dedicated Browser Auth Refresh

Use a root-specific CDP port when refreshing lane auth. Plain `nlm login --profile ...` can attach to an already-running Chrome instance and capture the wrong account.

For a full repeated-refresh validation workflow, use [`notebooklm-auth-robustness-test-plan.md`](notebooklm-auth-robustness-test-plan.md).

Preferred worker-profile repair after worker `01` for each account is valid:

```powershell
python P:/packages/yt-is/bin/csf-nlm-worker-auth sync
```

This command validates that `ytis-pro-worker-01` is `a.hominidae@gmail.com`, `ytis-free1-worker-01` is `troup.hominidae@gmail.com`, and `ytis-free2-worker-01` is `brsthomson@hotmail.com`, parses the account reported by `nlm login --check`, repairs worker `01` through the dedicated Pro/Free/Free2 CDP root when needed, backs up workers `02`-`04`, copies the refreshed worker `01` credentials to sibling profiles in the same account family, then account-checks all twelve worker profiles.

Current auth contract:

- `csf-nlm-worker-auth sync` checks each worker `01` source profile with `nlm login --check`, parses the reported `Account:`, and treats a valid session on the wrong account as a failed auth state.
- When a source worker profile is expired or mapped to the wrong account, `csf-nlm-worker-auth sync` uses the configured root-specific CDP refresh path by default and fails closed if that path cannot recover the account mapping.
- The second free account lane is defined in [`P:/packages/yt-is/.logs/sharded_lane_series/pro_free_hotmail_lanes.json`](../../.logs/sharded_lane_series/pro_free_hotmail_lanes.json) and uses `ytis-free2-worker-01` through `ytis-free2-worker-04` on `brsthomson@hotmail.com`.
- The auth family map in `csf/nlm_worker_auth.py` is still hard-coded. If you add a 4th family, update that file first, then mirror the new lane into the lane JSON, tests, and this doc.
- `csf-sharded-lane-series` preflights every lane profile before launching Pro/Free lanes and gives `nlm login --force --profile <profile>` one bounded recovery attempt for expired profiles.
- Benchmark subprocesses run with `YTIS_NLM_AUTH_NONINTERACTIVE=1`, so `csf-source` uses `nlm login --force` instead of plain interactive `nlm login` if auth expires mid-run.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is a stress knob, not a default throughput setting. Use `1` only when the goal is to force browser churn on every auth probe. For routine validation or soak runs, prefer a higher cadence such as `5`, or leave the knob unset entirely if auth churn is not the thing being tested.
- If automatic CDP renewal fails for `ytis-free1-worker-01` or `ytis-pro-worker-01`, refresh only that worker `01` profile through the manual dedicated CDP root below, then rerun `python P:/packages/yt-is/bin/csf-nlm-worker-auth sync`.
- For a failure-mode map before long auth-heavy runs, see [NotebookLM Auth Pre-Mortem](notebooklm-auth-pre-mortem.md).
- Zero-growth `source_add_failed` now has a bounded notebook-reset fallback in `csf/nlm_batch.py`; the `pro_free_source_map_v6` rerun showed that it reduces the failure class but still does not beat the current best sustained `pro_free_source_map_v1` result.
- Cleanup-cost optimization was attempted with a bulk source-delete path, but `pro_free_cleanup_opt_v2` was negative and the prior chunked cleanup path was restored.

Pro lane refresh:

```powershell
$proRoot = 'P:\.data\yt-is\browser\notebooklm-pro'
$proPort = 18870
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'

Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
  Where-Object { $_.CommandLine -like "*$proRoot*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Process -FilePath $chrome -ArgumentList @(
  "--user-data-dir=$proRoot",
  "--profile-directory=Profile",
  "--remote-debugging-port=$proPort",
  "--remote-allow-origins=*",
  '--no-first-run',
  '--no-default-browser-check',
  'https://notebooklm.google.com/'
)

Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$proPort/json/version"
nlm login --profile ytis-pro-worker-01 --provider openclaw --cdp-url "http://127.0.0.1:$proPort" --force
```

The expected account for `ytis-pro-worker-01` is `a.hominidae@gmail.com`.

After worker `01` is correct, copy the refreshed credential into the other Pro worker profiles:

```powershell
$src = 'C:\Users\brsth\.notebooklm-mcp-cli\profiles\ytis-pro-worker-01'
foreach ($name in @('ytis-pro-worker-02','ytis-pro-worker-03','ytis-pro-worker-04')) {
  $dst = Join-Path 'C:\Users\brsth\.notebooklm-mcp-cli\profiles' $name
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -LiteralPath (Join-Path $src 'cookies.json') -Destination (Join-Path $dst 'cookies.json') -Force
  Copy-Item -LiteralPath (Join-Path $src 'metadata.json') -Destination (Join-Path $dst 'metadata.json') -Force
}
```

Free lane refresh:

```powershell
$freeRoot = 'P:\.data\yt-is\browser\notebooklm-free'
$freePort = 18871
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'

Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
  Where-Object { $_.CommandLine -like "*$freeRoot*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Process -FilePath $chrome -ArgumentList @(
  "--user-data-dir=$freeRoot",
  "--profile-directory=Default",
  "--remote-debugging-port=$freePort",
  "--remote-allow-origins=*",
  '--no-first-run',
  '--no-default-browser-check',
  'https://notebooklm.google.com/'
)

Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$freePort/json/version"
nlm login --profile ytis-free1-worker-01 --provider openclaw --cdp-url "http://127.0.0.1:$freePort" --force
```

The expected account for `ytis-free1-worker-01` is `troup.hominidae@gmail.com`. If the command reports `a.hominidae@gmail.com`, stop and relaunch the root-specific browser before continuing.

After worker `01` is correct, copy the refreshed credential into the other Free worker profiles:

```powershell
$src = 'C:\Users\brsth\.notebooklm-mcp-cli\profiles\ytis-free1-worker-01'
foreach ($name in @('ytis-free1-worker-02','ytis-free1-worker-03','ytis-free1-worker-04')) {
  $dst = Join-Path 'C:\Users\brsth\.notebooklm-mcp-cli\profiles' $name
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -LiteralPath (Join-Path $src 'cookies.json') -Destination (Join-Path $dst 'cookies.json') -Force
  Copy-Item -LiteralPath (Join-Path $src 'metadata.json') -Destination (Join-Path $dst 'metadata.json') -Force
}

foreach ($profile in @('ytis-free1-worker-01','ytis-free1-worker-02','ytis-free1-worker-03','ytis-free1-worker-04')) {
  nlm login --check --profile $profile
}
```

Before starting the benchmark, validate every worker auth profile:

```powershell
foreach ($profile in @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free1-worker-01', 'ytis-free1-worker-02', 'ytis-free1-worker-03', 'ytis-free1-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Do not start the sharded benchmark unless every profile in `notebooklm_profiles` validates and maps to the intended account. A same-account run is not evidence for Pro+Free account sharding.

Important: `nlm login --check --profile ...` only proves the stored credentials are valid. The batch worker must also pin every NotebookLM command with `--profile <worker-profile>` before it runs `nlm notebook ...` or `nlm source ...`. Do not use `nlm login switch` in the concurrent worker path; it mutates process-global CLI state and can make one worker add sources under one account, then poll `source list` under another account, producing `PERMISSION_DENIED`.

```powershell
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_v1 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

## Free-Only Validation

Use this run to validate that the Free route works by itself after auth refresh:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/free_only_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/free_only_v1 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/free_only_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Latest validation result:

- artifact: `P:/packages/yt-is/.logs/sharded_lane_series/free_only_v1/sharded_lane_series_summary.json`
- account route: `troup.hominidae@gmail.com` through `P:/.data/yt-is/browser/notebooklm-free`
- hot-path VPH: `2841.46`
- hot-path successes: `348`
- failures: `52`
- processed: `400`
- wall elapsed: `440.9s`
- Whisper recovery VPH: `0.0`, excluded by metric contract

## Combined Validation

Use the same command after both lanes validate to measure the combined ceiling with the dedicated Pro and Free browser roots:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_v2 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_v2/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Latest combined validation result:

- artifact: `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v3/sharded_lane_series_summary.json`
- Pro route: `a.hominidae@gmail.com` through `P:/.data/yt-is/browser/notebooklm-pro`
- Free route: `troup.hominidae@gmail.com` through `P:/.data/yt-is/browser/notebooklm-free`
- combined hot-path VPH: `3850.52`
- combined hot-path successes: `614`
- combined failures: `186`
- combined processed: `800`
- combined wall elapsed: `574.052s`
- Pro lane hot-path VPH: `1795.93`
- Pro lane success/failure: `286/114`
- Pro lane `content_fetch_status_counts_total`: `{"ready":286,"command_failed":14}`
- Free lane hot-path VPH: `2184.75`
- Free lane success/failure: `328/72`
- Free lane `content_fetch_status_counts_total`: `{"ready":328,"command_failed":22}`
- Whisper recovery VPH: `0.0`, excluded by metric contract

This rerun is an improvement over `pro_free_source_map_v2` and validates the add-path salvage fix, but it is still below the `pro_free_source_map_v1` best. The live trace now shows the real issue: `nlm source add` returned nonzero even when the notebook source count increased to the full batch size, and that was recovered instead of being counted as a hard failure.

The attempted `pro_free_source_map_v4` follow-up is invalid. It was interrupted and stopped before a `sharded_lane_series_summary.json` was produced after `csf/nlm_batch.py` launched unprofiled `nlm login --force`, opening the default `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile` account chooser. Do not use `pro_free_source_map_v4` as throughput evidence.

## Next LLM Bootstrap

Current state as of 2026-04-30:

- Worker auth repair is scripted. Prefer `python P:/packages/yt-is/bin/csf-nlm-worker-auth sync`; it now account-checks `nlm login --check` output and uses root-specific CDP refresh for worker `01` recovery before copying credentials to sibling workers.
- Latest best sustained Pro+Free control is `pro_free_source_map_v1` at `5572.04` combined hot-path VPH.
- The earlier `pro_free_post_retry_v2` control at `4407.40` is now superseded by the source-map run.
- The later `pro_free_post_retry_v3` recheck is negative at `1982.17` combined hot-path VPH and should not replace the best method.
- Source ID mapping hardening in `P:/packages/yt-is/csf/nlm_batch.py` is now implemented and covered by focused tests.
- `nlm source add --wait` stdout should still be treated as the canonical add-order mapping for submitted video IDs.
- The duplicate-source-ID guard before content fetch is now validated live by `pro_free_source_map_v1`; keep it in place because duplicate source IDs mapped to multiple video IDs showed up in worker stdout from the bad `v3` run.
- The follow-up `pro_free_source_map_v2` rerun regressed to `2917.93` combined hot-path VPH with `397/403`; Pro `source_add_failed` dominated while Free only showed one `command_failed`.
- The residual two-video probe is complete. `juXI9QbzzgM` stayed below threshold and `u2hmsms-alg` was `ready` in isolation.
- A fresh targeted probe of representative benchmark `command_failed` cases (`j6lOJPRvuzc`, `MXAvtEHyl0A`, and `u2hmsms-alg`) returned `ready` in fresh notebooks, so the benchmark fetch failures look transient or harness-sensitive rather than content-specific.
- A fresh isolated 50-source add on Pro succeeded, and a repeated reusable Pro run succeeded twice, so the remaining add failures are not deterministic; they now look like transient NotebookLM add flakiness under the benchmark run shape.
- The add-path fix now treats a nonzero `nlm source add` return as recovered success when the notebook source count reaches the full batch size.
- The fresh `pro_free_source_map_v3` rerun improved to `3850.52` combined hot-path VPH, but the remaining gap is now concentrated in transient add failures rather than content fetch.
- The attempted `pro_free_source_map_v4` rerun is invalid and has no summary. It exposed an unprofiled auth-refresh path that opened the default NotebookLM Chrome account chooser.
- `csf/nlm_batch.py` now pins `nlm login --check` and `nlm login --force` to `NOTEBOOKLM_PROFILE` when present, and `YTIS_NLM_AUTH_NONINTERACTIVE=1` fails closed if a profile is missing.
- The fresh `pro_free_source_map_v5` rerun completed cleanly with no unprofiled auth browser activity, but it still falls below the `pro_free_source_map_v1` best:
  - combined hot-path VPH: `3930.79`
  - hot-path successes/failures: `638/162`
  - processed: `800`
  - wall elapsed: `584.31s`
  - Pro lane: `2406.66` hot-path VPH, `390/10`, `content_fetch_status_counts_total={"ready":390,"nlm_content_below_threshold":1,"command_failed":9}`
  - Free lane: `2001.72` hot-path VPH, `248/152`, `content_fetch_status_counts_total={"ready":248,"nlm_content_below_threshold":1,"command_failed":1}`
  - Free lane stdout still showed `source_add_failed` in batch 01 and batch 02
- Next action: no further documented throughput phase remains without a new hypothesis.
- Cleanup-cost optimization was tried next and stayed negative at `pro_free_cleanup_opt_v2`; the bulk delete path was rolled back and no further documented throughput phase remains without a new hypothesis.

Required verification before another full benchmark:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-nlm-worker-auth sync
pytest tests/test_nlm_batch.py tests/test_nlm_scraper.py tests/test_nlm_config.py tests/test_sharded_lane_series.py tests/test_nlm_worker_auth.py tests/test_worker_count_sweep.py tests/test_fallback_crossover_benchmark.py -q
python -m py_compile csf/nlm_batch.py csf/nlm_scraper.py csf/nlm_config.py csf/nlm_worker_auth.py tests/test_nlm_batch.py tests/test_nlm_scraper.py tests/test_nlm_config.py tests/test_nlm_worker_auth.py bin/csf-source bin/csf-nlm-worker-auth
```

The residual two-video probe has been run. The four remaining failures in `pro_free_source_map_v1` collapsed to two video IDs:

- `juXI9QbzzgM`: below-threshold NotebookLM source content in both lanes.
- `u2hmsms-alg`: `command_failed` in both lanes.

Use `nlm_content_below_threshold`, not `too_short`, when reporting NotebookLM source-content below the configured ready threshold. Capture `nlm_content_chars`, `usable_text_chars`, raw `nlm source content` stdout/stderr, and delayed retry behavior if that probe harness is rerun in the future.

Later no-stagger control recheck:

- The follow-up `pro_free_post_retry_v3` rerun regressed to combined hot-path VPH `1982.17` with `639` hot-path successes and `161` failures.
- Pro lane hot-path VPH dropped to `1202.13` and Free lane hot-path VPH dropped to `1036.97`.
- This was a clear negative control rerun, so `pro_free_post_retry_v2` remains the best sustained result.
- The fresh `pro_free_source_map_v1` rerun improved to combined hot-path VPH `5572.04` with `796` hot-path successes and `4` failures; it is the current best sustained result.

Observed caveat from the later `pro_free_v3` rerun:
- Pro batch 1 lost `worker-04` to a `120s` `nlm login --force` timeout during startup.
- The Pro-only rerun `pro_only_v6` still held at `398/2`, so the remaining issue is concurrent startup contention, not the active-profile fix.
- If that pattern reappears, stagger lane startup or prewarm the Pro lane before starting the Free lane.

Follow-up stagger test:
- `pro_free_staggered_v1` used a `120s` Free lane startup delay.
- Result was worse overall: combined hot-path VPH `1453.26`, Pro `2406.57`, Free `645.16`.
- The delay moved the failure pattern rather than fixing it, so `120s` is not the right stagger for this setup.

Follow-up stagger test:
- `pro_free_staggered_60s_v1` used a `60s` Free lane startup delay.
- Result was worse overall: combined hot-path VPH `1102.32`, Pro `368.99`, Free `1744.95`.
- Pro worker `04` never materialized sources and timed out after `600s` with `PERMISSION_DENIED` during the source-materialization wait; Free worker `03` hit `source_add_failed`.
- This result is invalid as throughput evidence because the Pro worker used the wrong account profile while polling source materialization.

Corrected follow-up stagger test:
- `pro_free_staggered_60s_v3` used the same `60s` Free lane startup delay after profile pinning and the cleanup-race guard.
- artifact: `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_staggered_60s_v3/sharded_lane_series_summary.json`
- combined hot-path VPH: `3626.67`
- Pro lane hot-path VPH: `3287.39`
- Free lane hot-path VPH: `1789.83`
- combined hot-path successes: `726`
- combined failures: `74`
- combined processed: `800`
- combined wall elapsed: `720.661s`
- Whisper recovery VPH: `0.0`, excluded by metric contract
- The prior `PERMISSION_DENIED` profile race did not recur.
- The cleanup-race `NOT_FOUND` materialization timeout from the invalid `pro_free_staggered_60s_v2` run did not recur.
- Remaining failures include counted Free `source_add_failed` and content-fetch `NOT_FOUND` cases, which are included in the `74` failures.

## Success Criteria

- Combined hot-path VPH is computed from earliest lane start to latest lane finish.
- Whisper recovery remains excluded from combined hot-path VPH.
- Each lane reports its own success/fail count and hot-path VPH.
- Pro and Free lanes do not share worker state files or worker notebook title prefixes.
- If combined VPH approaches `2x` the single-lane result, the current ceiling is lane/account contention.
- If combined VPH stays near `4000`, the bottleneck is likely shared backend, IP, local machine, or network contention.
