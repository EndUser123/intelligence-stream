# NotebookLM Auth Robustness Test Plan

> For future LLM agents: follow this plan before trusting a long Pro+Free throughput soak as auth evidence. A long run is only conclusive if the logs prove multiple auth refresh paths actually executed.

## Goal

Validate that NotebookLM auth stays profile-pinned and account-correct during repeated high-throughput Pro+Free runs, including multiple re-authentication events. The current working set has three auth families and twelve worker profiles.

Status: the benchmark auth helper now checks account identity, and `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is implemented in `csf/nlm_batch.py`. Use this plan to verify those behaviors in live runs.
Standard run order: `doctor` on the lane config and run root, then a short smoke, then `csf-run-evidence-check`, then the long soak.

## NotebookLM CLI Update Note

- `nlm` / `notebooklm-mcp-cli` is now pinned in this workspace to GitHub commit `3711e782cfa63db948bd34f9ae6e97210821223c`, which installs `0.6.2`.
- The update matters to this project because it keeps the auth helper on the current launcher/runtime path and includes upstream auth robustness fixes that shipped after `0.5.30`.
- Relevant upstream changes to remember:
  - `0.5.30` fixed stale `NOTEBOOKLM_COOKIES` auth loops and removed deprecated cookie/session env vars.
- `0.5.31` separated MCP stdout and stderr so the server does not exit on startup chatter.
- `0.6.0` added label management and related CLI/MCP commands; useful, but not central to the throughput soak.
- `0.6.2` on upstream `main` adds a login timeout fix and skips `HeadlessChrome` automation browsers during login, which is directly relevant to the multi-browser environment we are testing.
- Even on `0.6.2`, benchmark validation must still watch for `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`; the local guard treats that profile as invalid during noninteractive auth checks, command execution, forced refresh, and lane validation.
- Local `yt-is` hardening was required after the 3-lane soak RCA: upstream client recovery can still lose active profile identity and use the default profile during normal `nlm` RPC recovery, so the harness must fail closed and invalidate contaminated artifacts.

## Read First

- `P:/packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:/packages/yt-is/docs/operations/notebooklm-auth-family-extension.md`
- `P:/packages/yt-is/docs/operations/hot-path-throughput-next-test-plan.md`
- `P:/packages/yt-is/docs/operations/notebooklm-auth-rerun-recipe.md`
- `P:/packages/yt-is/docs/operations/notebooklm-auth-pre-mortem.md`
- `P:/packages/yt-is/docs/operations/test-registry.md`
- `P:/packages/yt-is/csf/nlm_worker_auth.py`
- `P:/packages/yt-is/csf/nlm_batch.py`
- `P:/packages/yt-is/tests/test_nlm_worker_auth.py`
- `P:/packages/yt-is/tests/test_nlm_batch.py`

## Current Auth Contract

- `csf-nlm-worker-auth sync` parses the `Account:` line from `nlm login --check`.
- A valid session on the wrong account is auth failure.
- New lanes must either extend `DEFAULT_FAMILIES` or declare `expected_email` in the lane JSON; preflight now fails closed if a lane profile has no expected-account mapping.
- Worker `01` profiles can be repaired through the dedicated Pro/Free CDP roots before credentials are copied to sibling workers.
- `csf-nlm-worker-auth snapshot` creates a verified snapshot of all configured worker profiles, including `worker-01`; `restore` rolls profiles back from the latest verified snapshot or a specified snapshot path.
- The dedicated CDP path is the primary repair path; if it cannot recover the lane, stop and diagnose the browser profile mapping instead of switching to a fallback auth route.
- Benchmark subprocesses run with `YTIS_NLM_AUTH_NONINTERACTIVE=1`.
- Benchmark workers must use `NOTEBOOKLM_PROFILE`; unprofiled `nlm login --force` invalidates the run.
- `csf-sharded-lane-series` preflight closes any stale default `chrome-profile` tree before account checks, repairs known profiles through the dedicated account-family source profile, and rejects any lane root containing `default_profile_running` or `source_add_failed` hard-failure markers.
- Lane auth preflight propagates `expected_email` into the worker environment as `YTIS_NLM_EXPECTED_EMAIL` so future lanes can stay account-aware even before the hard-coded family map is expanded.
- `refresh_source_profile()` reuses a live family CDP browser when it is already healthy, and launches Chrome minimized/non-active by default on Windows. Set `YTIS_NLM_BROWSER_VISIBLE=1` only for manual recovery.
- `bin/csf-source` uses the same account-family refresh path for known profiles before worker launch, rather than raw profile force-login.
- `csf/nlm_batch.py` treats broad zero-growth `nlm source add` failures as a hard invalidation path after retry, notebook reset, and one split level. It logs `nlm_batch_subbatch_add_split_circuit_opened` instead of recursively splitting down to singleton retries.
- `csf/nlm_batch.py` also reaps a transient default NotebookLM `chrome-profile` once before auth or a harmless `nlm source list`/`nlm source content` command and retries. Only a persistent repeat still logs `default_profile_running`.
- `csf/nlm_auth_guard.py` now profile-pins all non-login NotebookLM CLI commands routed through the shared helper, so future `audio` / `report` / `quiz` style commands will not silently fall back to the default profile.
- If you add a 4th family, extend the lists in this file and use [NotebookLM Auth Family Extension Guide](notebooklm-auth-family-extension.md) for the exact update order.

3-lane smoke note:

- `pro_free_hotmail_smoke_v2` failed in the two free lanes because `extract_transcripts()` rejected a batch when only part of the source list matched by title and the rest had to be recovered by order.
- The batch mapper now accepts exact-length hybrid mapping when title matches and ordered fallback together cover the full batch, while still failing closed on stale or duplicate source lists.
- `pro_free_hotmail_smoke_v3` is the clean proof run: `30/30` processed, `0` failures, and no default NotebookLM Chrome profile on the post-run check.
- `pro_free_auth_forced_smoke_v3` is the clean post-hardening smoke run: `100/100` hot-path successes, `0` failures, no `default_profile_running`, no `source_add_failed`, and a fresh top-level summary written for that root. It did not contain `nlm_auth_forced_refresh_scheduled`, so keep `pro_free_auth_marker_v4` as the explicit forced-refresh marker proof.
- `pro_free_auth_forced_smoke_v7` is the clean marker-producing benchmark-shaped smoke run: `100/100` hot-path successes, `0` failures, and `nlm_auth_forced_refresh_scheduled` in the lane-root JSONL logs for both lanes.
- `pro_free_source_add_smoke_v3` is the clean post-fix source-add smoke: `40/40` hot-path successes, `0` failures, and no invalidation markers in the lane roots.
- `pro_free_auth_soak_v1_run11` completed as endurance evidence with `773/800` hot-path successes and `27` content-failure losses, but no invalidation markers.
- The durable evidence index is [here](evidence/README.md); use it instead of treating a full `.logs` tree as the current source of truth.
- `pro_free_auth_soak_v1_run08` is invalid evidence. It failed because both lanes hit broad zero-growth source-add failures and recursively split failed batches; do not use it for auth or throughput conclusions.

## Pre-Mortem

Before a long soak, read [NotebookLM Auth Pre-Mortem](notebooklm-auth-pre-mortem.md) and verify the three things that most often invalidate the evidence:

- the Pro lane points at the signed-in Pro Chrome profile
- `nlm login --check` reports the expected account for each lane
- the run root is isolated and the previous smoke or soak outputs are not being mistaken for current evidence

Expected accounts:

- Pro workers: `a.hominidae@gmail.com`
- Free workers: `troup.hominidae@gmail.com`
- Free2 workers: `brsthomson@hotmail.com`

Expected worker profiles:

- `ytis-pro-worker-01`
- `ytis-pro-worker-02`
- `ytis-pro-worker-03`
- `ytis-pro-worker-04`
- `ytis-free1-worker-01`
- `ytis-free1-worker-02`
- `ytis-free1-worker-03`
- `ytis-free1-worker-04`
- `ytis-free2-worker-01`
- `ytis-free2-worker-02`
- `ytis-free2-worker-03`
- `ytis-free2-worker-04`

## Non-Negotiable Stop Conditions

Stop immediately and mark the run invalid if any of these occur:

- A command line contains `nlm login --force` without `--profile`.
- Chrome launches or uses `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`.
- Any Pro worker reports `troup.hominidae@gmail.com`.
- Any Free worker reports `a.hominidae@gmail.com`.
- Any Free2 worker reports `a.hominidae@gmail.com` or `troup.hominidae@gmail.com`.
- `PERMISSION_DENIED` dominates source materialization.
- A benchmark starts before all configured worker profiles pass account-aware checks.
- Any run-root JSONL contains `source_add_failed` or `nlm_batch_subbatch_add_split_circuit_opened`.

## Phase 1: Static And Unit Gates

Run from `P:/packages/yt-is`.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest tests/test_nlm_worker_auth.py tests/test_sharded_lane_series.py -q
pytest tests/test_nlm_batch.py tests/test_sharded_lane_series.py -q
python -m py_compile csf/nlm_worker_auth.py csf/nlm_batch.py csf/sharded_lane_series.py tests/test_nlm_worker_auth.py tests/test_nlm_batch.py tests/test_sharded_lane_series.py bin/csf-nlm-worker-auth bin/csf-source
```

Expected:

- The focused auth/batch/lane tests pass.
- `py_compile` exits `0`

If this fails, fix the account-aware auth helper before continuing.

## Phase 2: Live Worker Sync Drill

Run the live sync without creating another backup:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:/packages/yt-is/bin/csf-nlm-worker-auth snapshot
```

Then verify each profile explicitly:

```powershell
$profiles = @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free1-worker-01', 'ytis-free1-worker-02', 'ytis-free1-worker-03', 'ytis-free1-worker-04',
  'ytis-free2-worker-01', 'ytis-free2-worker-02', 'ytis-free2-worker-03', 'ytis-free2-worker-04'
)
foreach ($profile in $profiles) {
  nlm login --check --profile $profile
}
```

Expected:

- Pro profiles report `Account: a.hominidae@gmail.com`.
- Free profiles report `Account: troup.hominidae@gmail.com`.
- Free2 profiles report `Account: brsthomson@hotmail.com`.
- No profile reports the other account family.

If `sync` fails because Google needs passkey/browser approval, refresh only the affected worker `01` through the manual CDP flow in `sharded-lane-series.md`, then rerun this phase.

If a `worker-01` profile is corrupt, restore before opening another manual login:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
python P:/packages/yt-is/bin/csf-nlm-worker-auth restore
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Use `--snapshot <path>` only when you intentionally want a specific older verified snapshot. The restore command validates the snapshot manifest against the configured expected accounts before copying credentials.

## Phase 3: Verify The Deterministic In-Run Auth Stress Hook

Purpose: make the long soak prove multiple refreshes. Do not skip this phase unless auth refresh events are already known to occur naturally during the soak.

Implementation target:

- Modify: `P:/packages/yt-is/csf/nlm_batch.py`
- Test: `P:/packages/yt-is/tests/test_nlm_batch.py`

Required behavior:

- Env var `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` is available.
- Default is disabled.
- When set to a positive integer `N`, every Nth `_ensure_nlm_auth()` check should force the refresh branch even if `nlm login --check` succeeds.
- The forced path must still use `NOTEBOOKLM_PROFILE`.
- In `YTIS_NLM_AUTH_NONINTERACTIVE=1`, missing `NOTEBOOKLM_PROFILE` must fail closed.
- Log a distinct event such as `nlm_auth_forced_refresh_scheduled` with `notebooklm_profile` and `check_count`.

Minimum tests:

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest tests/test_nlm_batch.py -q -k "auth_context or auth_refresh"
```

Add or update tests so they prove:

- forced refresh uses `nlm login --force --profile <profile>`
- forced refresh never uses unprofiled `nlm login --force`
- noninteractive mode without `NOTEBOOKLM_PROFILE` fails closed
- forced refresh logs the profile and check count

Expected before continuing:

- Focused tests pass.
- `python -m py_compile csf/nlm_batch.py tests/test_nlm_batch.py` exits `0`.

## Phase 4: Short Validation Smoke

Run a small benchmark with forced refresh enabled before the long soak, but do not use the most aggressive cadence unless you are specifically debugging auth churn.

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

Use `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS='1'` only for a dedicated auth-stress drill where browser churn is the thing under test.

During the run, watch for invalid auth launches:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'nlm login --force|remote-debugging-port=9222|\.notebooklm-mcp-cli\\chrome-profile|notebooklm-pro|notebooklm-free' } |
  Select-Object ProcessId, Name, CommandLine
```

Pass criteria:

- The run completes.
- Logs show `nlm_auth_forced_refresh_scheduled` or equivalent.
- Any `nlm login --force` command is profile-pinned.
- No default NotebookLM Chrome profile appears.
- If `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=1` is used, the run must either emit `nlm_auth_forced_refresh_scheduled` with no default `chrome-profile` process or fail closed before any default-profile Chrome mutation becomes evidence.
- Post-run `python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync` still passes.

Observed stress run:

- Benchmark-shaped forced-refresh stress roots produced too much browser churn and were pruned from the workspace after the direct marker proof landed.
- Use `pro_free_auth_marker_v4` as the canonical forced-refresh proof.
- `csf/sharded_lane_series.py` now deletes stale top-level summary files before a rerun and writes the final summary atomically, so an interrupted previous run cannot masquerade as current evidence.
- `csf/nlm_batch.py` now self-heals when cleanup commands observe a transient default `chrome-profile`, so a shared-profile intrusion during notebook cleanup no longer invalidates an otherwise successful benchmark batch.

Marker and guard drills:

- `pro_free_auth_marker_v4` wrote `P:\packages\yt-is\.logs\sharded_lane_series\pro_free_auth_marker_v4\logs\term_ad61538d.jsonl`.
- That JSONL contains `nlm_auth_forced_refresh_scheduled`, `nlm_login_started`, `nlm_login_completed`, and `nlm_auth_refreshed` for `ytis-pro-worker-01`.
- The guard drill returned `check_exit=1` and `remaining_default_profile_processes=0` after starting a shared default-profile Chrome tree.

## Phase 5: Long Max-Throughput Soak

Run repeated Pro+Free no-stagger max-throughput loops for at least `75` minutes. Prefer repeated standard runs over one giant limit because artifacts stay easier to compare.

Do not enter this phase immediately after a source-add circuit-breaker patch. First run the source-add circuit smoke below.

### Source-Add Circuit Smoke

This is the next benchmark-shaped run after `pro_free_auth_soak_v1_run08` was invalidated. It keeps forced refresh cadence at `5`, but lowers the source-add window so failures are easier to classify before investing in another soak.

```powershell
$env:PYTHONPATH = 'P:\packages\yt-is'
$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = '5'
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:/packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:/packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:/packages/yt-is/.logs/sharded_lane_series/pro_free_source_add_smoke_v1 `
  --cohort-json P:/packages/yt-is/.logs/sharded_lane_series/pro_free_source_add_smoke_v1/cohort.json `
  --limit 20 `
  --batch-size 10 `
  --reusable-pipeline-mode serial
Remove-Item Env:\YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
```

Pass criteria:

- No `default_profile_running`.
- No `source_add_failed`.
- No `nlm_batch_subbatch_add_split_circuit_opened`.
- Post-run profile sync still reports the expected account for each configured profile.

Only proceed to the long soak after this smoke is clean.

If you need forced refreshes during the soak, start with `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS='5'` instead of `1`. That still proves repeated re-authentication without turning the run into a browser-launch stress test.

Use fresh output roots:

- `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run01`
- `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run02`
- `P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run03`
- Continue until elapsed wall time is greater than `75` minutes.

Command template:

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

Leave the cadence at `5` unless a specific auth bug requires more aggressive refresh churn. If you are only checking sustained throughput, do not set `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS` at all.

Observed soak evidence:

- `run01` wall elapsed `2035.2s`
- `run02` wall elapsed `1714.443s`
- `run03` wall elapsed `1238.153s`
- Combined wall elapsed across `run01` through `run03`: `4987.792s` (`83.13` minutes)
- `run03` completed with combined hot-path vph `1157.21`, `398` hot-path successes, `2` failures, and `400` processed
- The only `run03` failures were `nlm_content_below_threshold`; no default NotebookLM Chrome profile remained after cleanup
- I did not find a natural `nlm_auth_forced_refresh_scheduled` event in the soak roots, so treat the soak as endurance evidence only and keep `pro_free_auth_marker_v4` as the explicit forced-refresh marker proof

Run the process guard every 5 minutes in another terminal:

```powershell
Get-Date
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'csf-sharded-lane-series|csf-source fetch|nlm login --force|remote-debugging-port=9222|\.notebooklm-mcp-cli\\chrome-profile|notebooklm-pro|notebooklm-free' } |
  Select-Object ProcessId, Name, CommandLine
```

After each run:

```powershell
python P:/packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Pass criteria:

- Total soak duration is greater than `75` minutes.
- At least two auth refresh events are observed across artifacts or process logs.
- Every observed refresh is profile-pinned.
- All post-run sync checks report the expected accounts.
- No unprofiled auth browser or default NotebookLM Chrome profile appears.
- Benchmark summaries are structurally valid and use the standard metric contract.

If no refresh events occur naturally during the long soak, the soak is endurance evidence only. Do not claim it proves re-auth correctness; rerun with the forced-refresh hook from Phase 3.

## Phase 6: Evidence Extraction

Extract summary data for each run:

```powershell
@'
import json
from pathlib import Path

roots = sorted(Path("P:/packages/yt-is/.logs/sharded_lane_series").glob("pro_free_auth_soak_v1_run*/sharded_lane_series_summary.json"))
for path in roots:
    summary = json.loads(path.read_text())
    print(json.dumps({
        "artifact": str(path),
        "combined_hot_path_vph": summary["combined"]["hot_path_videos_per_hour"],
        "success": summary["combined"]["hot_path_success_count_total"],
        "failure": summary["combined"]["fail_count_total"],
        "processed": summary["combined"]["processed_count_total"],
        "wall_elapsed_s": summary["combined"]["wall_elapsed_s"],
        "lanes": {
            lane["lane"]: {
                "hot_path_vph": lane["hot_path_videos_per_hour"],
                "success": lane["hot_path_success_count_total"],
                "failure": lane["fail_count_total"],
                "content_fetch_status_counts_total": lane.get("content_fetch_status_counts_total"),
            }
            for lane in summary["runs"]
        },
    }, indent=2))
'@ | python -
```

Search for auth events:

```powershell
rg -n "nlm_auth|nlm_login|login --force|Account:|PERMISSION_DENIED|chrome-profile" P:/packages/yt-is/.logs/sharded_lane_series/pro_free_auth_soak_v1_run*
```

## Documentation Requirements

After the test:

- Add a row to `P:/packages/yt-is/docs/operations/test-registry.md`.
- Update `P:/packages/yt-is/docs/operations/sharded-lane-series.md` if the auth contract changes.
- If the run is invalid, say exactly which stop condition triggered.
- If no refresh event occurred, mark the result as endurance-only, not re-auth proof.

## Adding Another Family

If a 4th lane is added later, update these places together:

- `csf/nlm_worker_auth.py` `DEFAULT_FAMILIES`
- the lane JSON used by the sharded benchmark
- `docs/operations/sharded-lane-series.md`
- this file's expected accounts and profile lists
- `tests/test_nlm_worker_auth.py`
- `tests/test_sharded_lane_series.py`

The exact update order is documented in [NotebookLM Auth Family Extension Guide](notebooklm-auth-family-extension.md).

## Final Decision Rules

Mark the auth robustness test `proven` only if:

- the unit/process gates pass
- the live sync drill passes
- the short forced-refresh smoke passes
- the long soak exceeds `75` minutes
- at least two refresh events are observed
- every post-run account check is correct

Mark it `negative` if:

- any refresh maps a worker profile to the wrong account
- any unprofiled auth command appears
- the default NotebookLM Chrome profile appears
- post-run account checks fail

Mark it `partial` if:

- the long soak completes but no refresh events occur
- only one refresh event occurs
- throughput artifacts are valid but process-monitor evidence is missing
