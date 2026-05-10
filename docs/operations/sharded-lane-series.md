# Sharded Lane Series

Use this benchmark to test whether independent NotebookLM account lanes can exceed the current single-lane sustained hot-path ceiling.

Before trusting any throughput conclusion from this benchmark, read [Observability Contract Checklist](observability-contract-checklist.md). It is the default guardrail for metric meaning and producer/consumer schema agreement.

For the next controlled search across 2-lane and 3-lane candidates, use
[`optimal-throughput-candidate-test-plan.md`](optimal-throughput-candidate-test-plan.md).

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
- explicit `expected_email` when the lane is not already covered by the auth-family map
- Chrome browser profile root
- Chrome browser profile directory
- worker state root
- worker notebook title prefix

The CLI profile and Chrome profile directory for a lane must represent the same Google account.
The benchmark now fails closed if a lane profile has no account mapping. For a new lane, set `expected_email`
in the lane JSON or add the profile to the auth-family map before starting a run.
Each lane also gets its own `YTIS_BATCH_STATUS_DB_PATH` under the lane output root so the synthetic source seed does not race across concurrent lanes.
When the worker auth profiles are not prefix-derived, set `notebooklm_profiles` to the exact CLI profile names in worker order.
The browser-health gate normalizes Chrome subprocess `--user-data-dir` paths before matching them against lane roots, so escaped subprocess cmdlines do not trip false `unexpected_process` reports during preflight.
For the YT-IS Pro/Free lanes, keep the browser roots lane-specific and persistent:

- Pro root: `P:\\\\\\.data/yt-is/browser/notebooklm-pro`
- Free root: `P:\\\\\\.data/yt-is/browser/notebooklm-free`

For the current Pro/Free/Free2 run, the account mapping is:

- `a.hominidae@gmail.com` -> `a_hominidae_pro`
- `troup.hominidae@gmail.com` -> `troup_hominidae_free`
- `brsthomson@hotmail.com` -> `brsthomson_hotmail_free`

To add a 4th auth family, follow the extension recipe in [NotebookLM Auth Family Extension Guide](notebooklm-auth-family-extension.md). The short version is: add one new `AuthFamily` in `csf/nlm_worker_auth.py`, add one new lane entry in the lane JSON, give it its own browser root, browser profile directory, worker-state root, notebook prefix, and CDP port, then add matching tests and sync/check the new worker `01` before benchmarking.
If the new lane is not yet part of `DEFAULT_FAMILIES`, set `expected_email` in the lane JSON so preflight can verify the account without guessing. The worker env also propagates `YTIS_NLM_EXPECTED_EMAIL` as an explicit fallback for unmapped future lanes.

## Example Config

Save a config like this as `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json` after confirming which Chrome profile directory is Pro vs Free:

```json
[
  {
    "lane": "a_hominidae_pro",
    "account_class": "pro",
    "workers": 4,
    "notebooklm_profile_prefix": "ytis-pro-worker",
    "notebooklm_profiles": ["ytis-pro-worker-01", "ytis-pro-worker-02", "ytis-pro-worker-03", "ytis-pro-worker-04"],
    "browser_profile_root": "P:\\\\\\.data/yt-is/browser/notebooklm-pro",
    "browser_profile_directory": "Profile",
    "worker_state_root": "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/a_hominidae_pro/worker_states",
    "notebook_prefix": "benchmark-shard-a-hominidae-pro"
  },
  {
    "lane": "troup_hominidae_free",
    "account_class": "free",
    "workers": 4,
    "notebooklm_profile_prefix": "ytis-free1-worker",
    "notebooklm_profiles": ["ytis-free1-worker-01", "ytis-free1-worker-02", "ytis-free1-worker-03", "ytis-free1-worker-04"],
    "browser_profile_root": "P:\\\\\\.data/yt-is/browser/notebooklm-free",
    "browser_profile_directory": "Default",
    "worker_state_root": "P:\\\\\\packages/yt-is/.logs/sharded_lane_series/troup_hominidae_free/worker_states",
    "notebook_prefix": "benchmark-shard-troup-hominidae-free"
  }
]
```

## Command

Workers are lane-specific in the JSON config.
Use `python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-sequence --lane-config <lane-config> --run-root <run-root>` for the guarded sequence. It runs doctor, smoke, evidence check, then soak, writes smoke and soak outputs under `<run-root>/smoke` and `<run-root>/soak` by default, and reads the shared benchmark trace corpus from `P:\\\\\\packages/yt-is/.logs/worker_count_trials` unless you pass `--trace-root`. The same trace corpus is used for both phases.

## Dedicated Browser Auth Refresh

Use a root-specific CDP port when refreshing lane auth. Plain `nlm login --profile ...` can attach to an already-running Chrome instance and capture the wrong account.

For a full repeated-refresh validation workflow, use [`notebooklm-auth-robustness-test-plan.md`](notebooklm-auth-robustness-test-plan.md).

## Recommended Run Order

Use this order for a new benchmark root:

1. `python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-sequence --lane-config <lane-config> --run-root <run-root>`
1. If you need the phases manually, run `doctor -> smoke -> evidence check -> soak` in that order and keep the smoke and soak output roots separate.
1. `csf-run-evidence-check` only passes when the smoke root has a versioned summary with `report_version=1`, `status=ok`, and no forbidden markers.
1. Long soak only after the doctor, smoke, and evidence check all pass

`csf-sharded-lane-series` always rewrites `<run-root>/sharded_lane_series_summary.json`.
New summaries are versioned with `report_version=1`.
If any lane fails or is invalidated, the summary is still durable, but the top-level
`status` is `invalidated`, `failure_count` is nonzero, and the CLI exits nonzero.
Do not treat `combined` metrics from an invalidated summary as throughput evidence;
they are diagnostic only and may include only lanes that reached `status="ok"`.

## Source-Add Failure Policy

Zero-growth NotebookLM source-add failures are not split into smaller chunks. If `nlm source add` returns nonzero and the notebook source count stays unchanged after one retry and one notebook reset, the worker logs `nlm_batch_subbatch_zero_growth_terminal`, then `nlm_batch_subbatch_add_failed` with `failure_reason="source_add_failed"`. The sharded runner and evidence checker treat this as invalid run evidence because the failure points at account/profile/service pressure, not an individual bad URL that smaller chunks are likely to isolate.

## Run-Root Cleanup Policy

- Keep a failed or partial smoke/soak root only until a newer successful root exists for the same hypothesis.
- Once a successful replacement exists, prune older partial roots so the next run starts from a small, unambiguous evidence set.
- Never reuse a benchmark root in place for a fresh run. A dirty root is invalid preflight evidence, even if it only contains partial cohort files.
- For the current source-add smoke series, `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_add_smoke_v5` is the clean successful root. The earlier `v1` through `v4` roots are disposable partial attempts and can be removed after their evidence has been captured in the docs or registry.

Preferred worker-profile repair after worker `01` for each account is valid:

```powershell
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
```

This command validates that `ytis-pro-worker-01` is `a.hominidae@gmail.com`, `ytis-free1-worker-01` is `troup.hominidae@gmail.com`, and `ytis-free2-worker-01` is `brsthomson@hotmail.com`, parses the account reported by `nlm login --check`, repairs worker `01` through the dedicated Pro/Free/Free2 CDP root when needed, backs up workers `02`-`04`, copies the refreshed worker `01` credentials to sibling profiles in the same account family, then account-checks all twelve worker profiles.

Current auth contract:

- `csf-nlm-worker-auth sync` checks each worker `01` source profile with `nlm login --check`, parses the reported `Account:`, and treats a valid session on the wrong account as a failed auth state.
- When a source worker profile is expired or mapped to the wrong account, `csf-nlm-worker-auth sync` uses the configured root-specific CDP refresh path by default and fails closed if that path cannot recover the account mapping.
- `csf-nlm-worker-auth doctor` is the fast preflight gate for a benchmark root: it validates the lane config, confirms the run root is empty, and refuses to start a run on dirty evidence.
- `csf-nlm-worker-auth doctor` also fails closed when a lane profile has no expected-account mapping.
- `csf-sharded-lane-sequence` runs an objective pre-run browser health gate after doctor and before smoke. It writes `<run-root>/browser_health.json`, reaps transient shared default NotebookLM Chrome profile leaks once, and records the result in the top-level summary as `pre_run_browser_health`.
- Each lane also reaps a transient shared default NotebookLM Chrome profile again at lane start, right before the benchmark command launches, so a leak that appears after preflight cannot poison the lane.
- `csf-sharded-lane-series` pins `INTELLIGENCE_STREAM_LOG_DIR` into each lane output root so auth markers and lane events stay inside the benchmark evidence tree.
- The guarded sequence records `post_run_hygiene` in the top-level summary and reaps any lingering default NotebookLM `chrome-profile` after soak; a transient shared-profile intrusion is cleaned up, but a persistent one remains a failure signal.
- `csf/nlm_batch.py` self-heals cleanup commands if a transient default `chrome-profile` appears after the batch work is already complete, so a stale shared-profile intrusion does not invalidate an otherwise successful run.
- The live `csf-source` fetch helper now emits `nlm_auth_forced_refresh_scheduled` plus `nlm_family_refresh_started` / `nlm_family_refresh_completed` when `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` forces a family refresh, and a 25-item direct probe on `ytis-pro-worker-01` measured `nlm_family_refresh_completed.elapsed_s=10.616`.
- The second free account lane is defined in [`P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_hotmail_lanes.json`](../../.logs/sharded_lane_series/pro_free_hotmail_lanes.json) and uses `ytis-free2-worker-01` through `ytis-free2-worker-04` on `brsthomson@hotmail.com`.
- The canonical evidence index is [Evidence Index](evidence/README.md); treat full run roots as runtime output, not the source of truth.
- The auth family map in `csf/nlm_worker_auth.py` is still the primary source of truth. If you add a 4th family, update that file first, then mirror the new lane into the lane JSON, tests, and this doc. If the lane exists before the code map is extended, set `expected_email` in the lane JSON so doctor/preflight can still validate it.
- `csf-sharded-lane-series` preflights every lane profile before launching Pro/Free lanes and gives `nlm login --force --profile <profile>` one bounded recovery attempt for expired profiles.
- Benchmark subprocesses run with `YTIS_NLM_AUTH_NONINTERACTIVE=1`, so `csf-source` uses `nlm login --force` instead of plain interactive `nlm login` if auth expires mid-run.
- `YTIS_NLM_EXPECTED_EMAIL` can be used as an explicit fallback for future lane profiles that are not yet part of the hard-coded auth-family map, but the preferred contract is still to set `expected_email` in the lane JSON or extend `DEFAULT_FAMILIES`.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is a stress knob, not a default throughput setting. Use `1` only when the goal is to force browser churn on every auth probe. For routine validation or soak runs, prefer a higher cadence such as `5`, or leave the knob unset entirely if auth churn is not the thing being tested.
- If automatic CDP renewal fails for `ytis-free1-worker-01` or `ytis-pro-worker-01`, refresh only that worker `01` profile through the manual dedicated CDP root below, then rerun `python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync`.
- For a failure-mode map before long auth-heavy runs, see [NotebookLM Auth Pre-Mortem](notebooklm-auth-pre-mortem.md).
- Zero-growth `source_add_failed` now has a bounded notebook-reset fallback in `csf/nlm_batch.py`; the `pro_free_source_map_v6` rerun showed that it reduces the failure class but still does not beat the current best sustained `pro_free_source_map_v1` result.
- Cleanup-cost optimization was attempted with a bulk source-delete path, but `pro_free_cleanup_opt_v2` was negative and the prior chunked cleanup path was restored.

Pro lane refresh:

```powershell
$proRoot = 'P:\\\\\\.data\yt-is\browser\notebooklm-pro'
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
$freeRoot = 'P:\\\\\\.data\yt-is\browser\notebooklm-free'
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
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_v1 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

## Free-Only Validation

Use this run to validate that the Free route works by itself after auth refresh:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/free_only_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/free_only_v1 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/free_only_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Latest validation result:

- artifact: `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/free_only_v1/sharded_lane_series_summary.json`
- account route: `troup.hominidae@gmail.com` through `P:\\\\\\.data/yt-is/browser/notebooklm-free`
- hot-path VPH: `2841.46`
- hot-path successes: `348`
- failures: `52`
- processed: `400`
- wall elapsed: `440.9s`
- Whisper recovery VPH: `0.0`, excluded by metric contract

## Combined Validation

Use the same command after both lanes validate to measure the combined ceiling with the dedicated Pro and Free browser roots:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_v2 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_v2/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Latest combined validation result:

- artifact: `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v3/sharded_lane_series_summary.json`
- Pro route: `a.hominidae@gmail.com` through `P:\\\\\\.data/yt-is/browser/notebooklm-pro`
- Free route: `troup.hominidae@gmail.com` through `P:\\\\\\.data/yt-is/browser/notebooklm-free`
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

- Worker auth repair is scripted. Prefer `python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync`; it now account-checks `nlm login --check` output and uses root-specific CDP refresh for worker `01` recovery before copying credentials to sibling workers.
- Latest best sustained Pro+Free control is `pro_free_source_map_v1` at `5572.04` combined hot-path VPH.
- The earlier `pro_free_post_retry_v2` control at `4407.40` is now superseded by the source-map run.
- The later `pro_free_post_retry_v3` recheck is negative at `1982.17` combined hot-path VPH and should not replace the best method.
- Source ID mapping hardening in `P:\\\\\\packages/yt-is/csf/nlm_batch.py` is now implemented and covered by focused tests.
- `nlm source add --wait` stdout should still be treated as the canonical add-order mapping for submitted video IDs.
- The duplicate-source-ID guard before content fetch is now validated live by `pro_free_source_map_v1`; keep it in place because duplicate source IDs mapped to multiple video IDs showed up in worker stdout from the bad `v3` run.
- The follow-up `pro_free_source_map_v2` rerun regressed to `2917.93` combined hot-path VPH with `397/403`; Pro `source_add_failed` dominated while Free only showed one `command_failed`.
- The residual two-video probe is complete. `juXI9QbzzgM` stayed below threshold and `u2hmsms-alg` was `ready` in isolation.
- A fresh targeted probe of representative benchmark `command_failed` cases (`j6lOJPRvuzc`, `MXAvtEHyl0A`, and `u2hmsms-alg`) returned `ready` in fresh notebooks, so the benchmark fetch failures look transient or harness-sensitive rather than content-specific.
- A fresh isolated 50-source add on Pro succeeded, and a repeated reusable Pro run succeeded twice, so the remaining add failures are not deterministic; they now look like transient NotebookLM add flakiness under the benchmark run shape.
- The add-path fix now treats a nonzero `nlm source add` return as recovered success when the notebook source count reaches the full batch size.
- The fresh `pro_free_source_map_v3` rerun improved to `3850.52` combined hot-path VPH, but the remaining gap is now concentrated in transient add failures rather than content fetch.
- The attempted `pro_free_source_map_v4` rerun is invalid and has no summary. It exposed an unprofiled auth-refresh path that opened the default NotebookLM Chrome account chooser.
- `csf/nlm_batch.py` now routes mapped worker-family auth refresh through the family source profile path, emits dedicated `nlm_family_refresh_started` / `nlm_family_refresh_completed` timing markers for that branch, keeps the unknown-profile `nlm login --check`/`--force` fallback pinned to `NOTEBOOKLM_PROFILE`, and still fails closed if a noninteractive profile is missing.
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
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
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
- artifact: `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_staggered_60s_v3/sharded_lane_series_summary.json`
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

## 3+3 Three-Sample Status (2026-05-05)

### Instrumented Diagnosis

**Evidence status**: the scheduled force-refresh path was not observed in run02/run03. The
`nlm_login_started` and `nlm_family_refresh_started` events are present in run02/run03, but
`nlm_auth_forced_refresh_scheduled=0` in all three `3+3` samples. Run04 then showed
`session_age_s` values capped near `30s`, which matches the default auth-check cache TTL in
`csf/nlm_auth_guard.py` and shifts the leading hypothesis away from NotebookLM TTL and toward the
local auth-check cache cadence.

| Run | Comb VPH | Pro idle (s) | Pro add (s) | Pro logins | Auth checked | Source fetch mean/max |
|---|---|---|---|---|---|---|
| `sweep_phase3_2lane_3w_run01` | 4123.28 | 0.5 | 590.0 | 0 | 564 | — |
| `sweep_phase3_2lane_3w_run02` | 2953.82 | 242.4 | 689.7 | 70 (Pro), 81 (Free) | 498 | — |
| `sweep_phase3_2lane_3w_run03` | 2384.21 | 294.0 | 597.9 | 87 (Pro), 94 (Free) | 467 | 14.8s / 229.8s |
| `sweep_phase3_2lane_3w_run04` | 2398.89 | 500.0 | 555.4 | 106 (Pro), 105 (Free) | 531 | — |
| `pro_free_source_map_v1` | 5572.04 | 37.9 | 396.0 | 0 | 1121 | 5.9s / 16.0s |

**Leading hypothesis**: the local auth-check cache TTL drives the lower run02/run03/run04 VPH.
NotebookLM TTL is still possible, but run04's `session_age_s` band (roughly `0s` to `30s`) is a
better match for the default `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS=30` knob than for a NotebookLM
session expiry window.

Each login can block the source materialization polling loop when it overlaps active polling, adding
idle time: 242s (run02 Pro), 294s (run03 Pro), 432s (run03 Free), 500s (run04 Pro), and 221s
(run04 Free). Login count alone is not sufficient because run02 Free had many logins but no measured
idle wait.

Source fetch slowdown (run03: mean 14.8s, max 229.8s vs v1: mean 5.9s, max 16.0s) is correlated with
the slower run, but causal direction remains part of the diagnostic.

**Run05 result**: the cache-TTL A/B finished cleanly but negatively. It did not improve throughput,
so the next step is not another TTL-only rerun.
Warm-auth is still not justified by this evidence; move to source-add/readiness/setup cost or another
non-TTL limiter instead.

### Three-Sample Interpretation

| Run | VPH | Success/Fail | Hygiene | Auth state |
|---|---|---|---|---|
| `sweep_phase3_2lane_3w_run01` | 4123.28 | 795/5 | clean | 0 logins observed |
| `sweep_phase3_2lane_3w_run02` | 2953.82 | 792/8 | clean | many family-refresh logins; forced-refresh scheduled path not observed |
| `sweep_phase3_2lane_3w_run03` | 2384.21 | 797/3 | clean | many family-refresh logins; forced-refresh scheduled path not observed |
| `sweep_phase3_2lane_3w_run04` | 2398.89 | 791/9 | clean | many family-refresh logins; `session_age_s` observed in the `0-30s` band, matching default auth-check cache TTL |

**Current interpretation**: `run01` is still the best observed clean `3+3` sample, but the sustained
`3+3` ceiling is not locked. Run04 repeated the low window at `2398.89` VPH and showed the auth
cache age band capped near `30s`, so the next question is whether extending
`YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS` raises throughput.

Run05 answered that question negatively. With `YTIS_NLM_AUTH_CHECK_CACHE_TTL_SECONDS=120`,
`sweep_phase3_2lane_3w_run05` completed cleanly at `1958.94`, but it did not improve throughput;
it increased login churn to `132` Pro and `128` Free logins, kept `nlm_auth_forced_refresh_scheduled=0`,
and still recorded `session_age_s` in the `0-30s` band. That makes the cache-TTL hypothesis a
negative result for this cohort.

The fresh guarded rerun `sweep_phase3_2lane_3w_run06` also completed cleanly at `2284.56`
after the browser-health hardening and Pro root auth repair, but it still stayed well below
`sweep_phase3_2lane_3w_run01` and did not recover the historical `3+3` ceiling.

A fresh Pro-only rerun after the lane-config repair also stayed weak at `1105.3`, and the
matching Free-only rerun came in at `929.05` on a smaller 200-item processed set. That keeps
the remaining work in the startup/setup and source-readiness branch, but it is noisy enough
that lane-width or auth TTL repeats are no longer the next best move.

The next guarded repeat, `sweep_phase3_2lane_3w_run07`, also completed cleanly but only reached
`1974.57` VPH. Its `37` `command_failed` events were all `NOT_FOUND`, and the new source-list
probe marked them `source_validated=true` while also capturing the matched source-row metadata,
so the remaining gap still points to source-add/readiness/setup cost or source-id remap/staleness
rather than a missing retry marker. The next probe should target notebook-age / rotation cadence.
The follow-up single-source 300-second probe stayed ready at both the immediate and delayed
fetches, so notebook age alone is not sufficient to reproduce the `NOT_FOUND` behavior.
The next guarded repeat, `sweep_phase3_2lane_3w_run08`, regressed to `1779.65` VPH with `45`
`command_failed` events on each lane. The live `NOT_FOUND` rows still showed
`source_validated_after_not_found=true`, and the failed items clustered at source ages above
roughly `240s` while a fresh recreated notebook batch stayed healthy. That keeps the next useful
branch on notebook rotation/cadence or another age-capped diagnostic, not broader retry markers.
The follow-up age-capped guarded sequence, `sweep_phase3_2lane_3w_agecap_200_run02`, improved the
same 3+3 shape to `3084.08` combined hot-path VPH with `398/2/400` and clean post-run hygiene.
The age cap held both lanes under the earlier cliff, with Pro `source_ready_age_s_max=211.292`
and Free `source_ready_age_s_max=160.966`, and the residual failures shifted to
`nlm_content_below_threshold` rather than `NOT_FOUND`. That makes the next branch a tighter age-cap
or sparse-content follow-up, not broader retry markers.

## Success Criteria

- Combined hot-path VPH is computed from earliest lane start to latest lane finish.
- Whisper recovery remains excluded from combined hot-path VPH.
- Each lane reports its own success/fail count and hot-path VPH.
- Pro and Free lanes do not share worker state files or worker notebook title prefixes.
- If combined VPH approaches `2x` the single-lane result, the current ceiling is lane/account contention.
- If combined VPH stays near `4000`, the bottleneck is likely shared backend, IP, local machine, or network contention.
