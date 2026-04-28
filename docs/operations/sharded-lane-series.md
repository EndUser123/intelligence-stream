# Sharded Lane Series

Use this benchmark to test whether two independent NotebookLM account lanes can exceed the current single-lane sustained hot-path ceiling.

## Current Hypothesis

The best single-lane sustained hot-path result is about `3928` videos/hour on the narrow/captioned cohort with:

- `4` workers
- benchmark `--batch-size 200`
- serial reusable pipeline mode
- Whisper excluded from hot-path VPH

The sharded lane test runs two matched CLI/browser account lanes concurrently and reports combined hot-path VPH from wall-clock elapsed time.

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

For the current Pro/Free run, the account mapping is:

- `a.hominidae@gmail.com` -> `a_hominidae_pro`
- `troup.hominidae@gmail.com` -> `troup_hominidae_free`

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
    "browser_profile_directory": "Profile 2",
    "worker_state_root": "P:/packages/yt-is/.logs/sharded_lane_series/a_hominidae_pro/worker_states",
    "notebook_prefix": "benchmark-shard-a-hominidae-pro"
  },
  {
    "lane": "troup_hominidae_free",
    "account_class": "free",
    "workers": 4,
    "notebooklm_profile_prefix": "ytis-free-worker",
    "notebooklm_profiles": ["ytis-free-worker-01", "ytis-free-worker-02", "ytis-free-worker-03", "ytis-free-worker-04"],
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
nlm login --profile ytis-free-worker-01 --provider openclaw --cdp-url "http://127.0.0.1:$freePort" --force
```

The expected account for `ytis-free-worker-01` is `troup.hominidae@gmail.com`. If the command reports `a.hominidae@gmail.com`, stop and relaunch the root-specific browser before continuing.

After worker `01` is correct, copy the refreshed credential into the other Free worker profiles:

```powershell
$src = 'C:\Users\brsth\.notebooklm-mcp-cli\profiles\ytis-free-worker-01'
foreach ($name in @('ytis-free-worker-02','ytis-free-worker-03','ytis-free-worker-04')) {
  $dst = Join-Path 'C:\Users\brsth\.notebooklm-mcp-cli\profiles' $name
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -LiteralPath (Join-Path $src 'cookies.json') -Destination (Join-Path $dst 'cookies.json') -Force
  Copy-Item -LiteralPath (Join-Path $src 'metadata.json') -Destination (Join-Path $dst 'metadata.json') -Force
}

foreach ($profile in @('ytis-free-worker-01','ytis-free-worker-02','ytis-free-worker-03','ytis-free-worker-04')) {
  nlm login --check --profile $profile
}
```

Before starting the benchmark, validate every worker auth profile:

```powershell
foreach ($profile in @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free-worker-01', 'ytis-free-worker-02', 'ytis-free-worker-03', 'ytis-free-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Do not start the sharded benchmark unless every profile in `notebooklm_profiles` validates and maps to the intended account. A same-account run is not evidence for Pro+Free account sharding.

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

## Success Criteria

- Combined hot-path VPH is computed from earliest lane start to latest lane finish.
- Whisper recovery remains excluded from combined hot-path VPH.
- Each lane reports its own success/fail count and hot-path VPH.
- Pro and Free lanes do not share worker state files or worker notebook title prefixes.
- If combined VPH approaches `2x` the single-lane result, the current ceiling is lane/account contention.
- If combined VPH stays near `4000`, the bottleneck is likely shared backend, IP, local machine, or network contention.
