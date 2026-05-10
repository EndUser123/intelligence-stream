# Hot-Path Throughput Next Test Plan

> For future LLM agents: follow this plan in order. Do not rerun old benchmark shapes unless the named code path has changed. Whisper fallback is allowed for recovery, but Whisper time and recovery counts are never included in sustained hot-path videos/hour.

> Default observability guardrail: read [Observability Contract Checklist](observability-contract-checklist.md) before trusting any metric, reducer output, or interpretation in this plan.

## Goal

Find whether `yt-is` can exceed the current best proven sustained hot-path throughput:

- Latest best artifact: `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v1/sharded_lane_series_summary.json`
- Latest best combined hot-path VPH: `5572.04`
- Prior control artifact: `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_v2/sharded_lane_series_summary.json`
- Prior control combined hot-path VPH: `4148.71`
- Current best shape: Pro+Free lanes, no startup stagger, `4` workers per lane, `--limit 400` per lane, `--batch-size 200`, serial reusable pipeline
- Metric contract: use `combined.hot_path_videos_per_hour` from `sharded_lane_series_summary.json`; do not include Whisper fallback throughput
- Extraction-status contract: do not use `too_short` as a NotebookLM metric. Use `nlm_content_below_threshold` for below-threshold NotebookLM source content, and record `nlm_content_chars` plus `usable_text_chars` when diagnosing sparse source content.

## Read First

Before running anything, read:

- `P:\\\\\\packages/yt-is/docs/operations/observability-contract-checklist.md`
- `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`
- `P:\\\\\\packages/yt-is/docs/operations/sharded-lane-series.md`
- `P:\\\\\\packages/yt-is/docs/operations/notebooklm-auth-family-extension.md`
- `P:\\\\\\packages/yt-is/docs/superpowers/specs/2026-04-28-hot-path-throughput-optimization-series-design.md`

These files record what has already been proven, what was negative, and how the dedicated Pro and Free browser roots must be authenticated.

## Current Session State: 2026-04-30

What has been actioned:

- Worker-profile auth repair was implemented through `python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync`.
- The sync command validates `ytis-pro-worker-01` as `a.hominidae@gmail.com`, `ytis-free1-worker-01` as `troup.hominidae@gmail.com`, and `ytis-free2-worker-01` as `brsthomson@hotmail.com`, parses `nlm login --check` account output, repairs worker `01` through the dedicated Pro/Free/Free2 CDP root when needed, backs up sibling worker profiles, copies account-family credentials to workers `02`-`04`, and account-checks all twelve worker profiles.
- Bounded whole-batch source-add retry was implemented and covered by focused tests.
- The zero-growth add failure path now has its own bounded retry and regression coverage. The live `pro_free_source_map_v5` rerun showed that the fallback was still needed for remaining Free lane zero-growth `source_add_failed` cases, and the notebook-reset fallback has now been implemented and rerun as `pro_free_source_map_v6`.
- The Pro+Free no-stagger control was rerun twice after the auth/retry work:
  - `pro_free_post_retry_v2`: proven new best, `4407.40` combined hot-path VPH, `688/112`, `800` processed, wall `561.964s`.
  - `pro_free_post_retry_v3`: negative recheck, `1982.17` combined hot-path VPH, `639/161`, `800` processed, wall `1160.544s`.
- The fresh Pro+Free no-stagger source-map rerun was executed as `pro_free_source_map_v2` and regressed:
  - `pro_free_source_map_v2`: `2917.93` combined hot-path VPH, `397/403`, `800` processed, wall `489.8s`.
  - Per-lane: Pro `721.48` with `98/302` and `content_fetch_status_counts_total={"ready":98,"command_failed":2}`; Free `3035.89` with `299/101` and `content_fetch_status_counts_total={"ready":299,"command_failed":1}`.
- The add-path fix was then validated in a live rerun:
  - `pro_free_source_map_v3`: `3850.52` combined hot-path VPH, `614/186`, `800` processed, wall `574.052s`.
  - Per-lane: Pro `1795.93` with `286/114` and `content_fetch_status_counts_total={"ready":286,"command_failed":14}`; Free `2184.75` with `328/72` and `content_fetch_status_counts_total={"ready":328,"command_failed":22}`.
- A follow-up `pro_free_source_map_v4` attempt was stopped and is invalid. It launched an unprofiled `nlm login --force`, opening the default NotebookLM Chrome profile account chooser. Root cause: `csf/nlm_batch.py` still used unprofiled auth refresh commands while benchmark workers were otherwise profile-pinned. The auth helper now uses `NOTEBOOKLM_PROFILE` for `nlm login --check/--force`, and noninteractive mode fails closed if no profile is set.
- Cleanup-cost optimization was then tried through a bulk `source delete` cleanup path and a bounded settle wait. The live `pro_free_cleanup_opt_v2` rerun remained negative and the cleanup path was restored to the prior stable chunked delete behavior.
- The live `auth_smoke_v2` run was interrupted before it could finish, and it used `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS='1'`, which is now treated as a stress-only setting. For any rerun whose goal is validation rather than browser churn, use `5` or leave the knob unset.
- The NotebookLM sparse-content metric was renamed:
  - Old status: `too_short`
  - New status: `nlm_content_below_threshold`
  - New diagnostic fields: `extraction_outcome`, `nlm_content_chars`, `usable_text_chars`
  - Legacy `too_short` remains only as a retry/defer compatibility input for old traces.
- A targeted isolated probe of representative benchmark `command_failed` videos (`j6lOJPRvuzc`, `MXAvtEHyl0A`, and `u2hmsms-alg`) came back `ready` in fresh notebooks, so the benchmark failures look transient or harness-sensitive rather than content-specific. Artifacts: `P:\\\\\\packages/yt-is/.logs/nlm_content_probe/residual_pro_v1/20260430T002429Z/probe_summary.json` and `P:\\\\\\packages/yt-is/.logs/nlm_content_probe/residual_free_v1/20260430T002429Z/probe_summary.json`.
- Phase 2 JSON corpus scan did not find literal `NOT_FOUND`, `source_add_failed`, or `source_id` strings in `pro_free_staggered_60s_v3/**/*.json`.
- Worker `stdout.txt` artifacts did show duplicate failed source IDs mapped to multiple video IDs. The bad `pro_free_post_retry_v3` run had `48` duplicate failed source IDs across `111` failed fetch lines.

Current interpretation:

- `pro_free_source_map_v1` remains the best sustained NotebookLM hot-path result.
- `pro_free_source_map_v2` is a negative rerun: the Pro lane `source_add_failed` pattern dominated and the combined VPH fell well below the current best.
- `pro_free_source_map_v3` validates the add-path salvage fix and improves throughput materially, but it still does not beat `pro_free_source_map_v1`.
- `pro_free_source_map_v6` is a negative recheck after the notebook-reset fallback: `1837.24` combined hot-path VPH, `299/501`, `800` processed, wall `585.88s`. Pro `616.2` with `100/300` and `content_fetch_status_counts_total={"ready":100}`; Free `1394.33` with `199/201` and `content_fetch_status_counts_total={"ready":199,"command_failed":1}`. The fallback did not recover enough throughput to beat the current best.
- `pro_free_cleanup_opt_v2` is a negative cleanup-cost rerun: `1807.26` combined hot-path VPH, `349/451`, `800` processed, wall `695.2s`. Pro `2904.14` with `249/151`, `cleanup_elapsed_s_total=78.039`; Free `518.55` with `100/300`, `cleanup_elapsed_s_total=175.345`. Bulk cleanup did not improve throughput enough, so the code path was rolled back.
- The `v3` regression was mostly wall-time and lifecycle variance, not just lower success count; the remaining `command_failed` cases now look transient rather than content-specific.
- A fresh isolated 50-source add on Pro succeeded, and a repeated reusable Pro run succeeded twice as well, so the remaining open issue is not a deterministic add/path break. The current evidence points to transient NotebookLM add flakiness that only shows up under the benchmark run shape.
- Source ID mapping was the prior highest-value correctness change. `nlm source add --wait` stdout is now the canonical add-order mapping path and has been validated live.
- The remaining true add failures on Pro were handled with a bounded zero-growth add retry, not a content-classification change.
- Do not treat `pro_free_source_map_v4` as benchmark evidence; rerun under a new output root after confirming no unprofiled `nlm login --force` appears in the process table.
- The remaining failure analysis must distinguish "NotebookLM returned less than the configured content threshold" from "the video is too short to matter." Short videos remain valid content candidates.
- The new `idle_wait_validation_run01` validation stayed clean (`200/0/200`) but still showed large idle cost: combined hot-path VPH `1038.73`, Pro `worker_idle_wait_s_total=478.6s`, Free `worker_idle_wait_s_total=542.0s`.
- Corrected auth accounting on that run shows family-refresh plus failed-login time of about `234.0s` on Pro and `247.6s` on Free, which is roughly `35%` of lane wall time and about half of the lane idle wait. Auth is a real contributor, but it does not explain all idle cost.
- The fresh `idle_wait_validation_run03` rerun with corrected auth-age logging improved to `1602.89` combined hot-path VPH and stayed clean (`200/0/200`). Auth refreshes were dominated by `cache_expired` rather than `cache_miss` (`312` vs `30` auth-refresh events in the worker logs), and the logged refresh age now looks sane: Pro `auth_cache_session_age_s` centered around `33.983s` with a `30.004s` minimum and `74.568s` maximum; Free showed the same pattern. This keeps auth cadence in play as a possible lever, but the reducer still labels extract as the lane bottleneck.
- The auth TTL 120 A/B `idle_wait_validation_auth_ttl120_run02` was also a negative branch: combined hot-path VPH fell to `988.48`, idle wait rose to `1126.331s`, and `source_ready_age_s_avg` rose to `124.626s` even though Pro `cache_expired` auth refreshes dropped from `312` to `231`. That makes TTL 120 a dead branch for this cohort.
- The tighter `YTIS_NLM_SOURCE_AGE_CLIFF_S=150` follow-up `idle_wait_validation_run04` was a hard negative: combined hot-path VPH fell to `257.95`, success/fail dropped to `180/20`, and worker idle wait exploded to `3402.45s`. The worker logs show `cache_expired` auth refreshes ballooned to `1206` versus `30` `cache_miss` events, so this threshold is too aggressive for the current cohort and should not be used as the default branch.
- Age-guard observability is live in the same run: `nlm_batch_subbatch_age_guard_rotation_requested` and `nlm_batch_notebook_recycled` appeared in the worker logs, so the guard path is now observable and functioning.
- The `clean_3plus3_pressure_run02` rerun with `--batch-size 100` completed only as a partial benchmark and did not become a new ceiling:
  - Combined hot-path VPH `814.6`, `700/100`, `700` processed, `status=partial`
  - Pro `248/102`, `worker_idle_wait_s_total=1820.633`, `source_ready_age_s_max=697.106s`
  - Free `349/1`, `worker_idle_wait_s_total=1776.019`, `source_ready_age_s_max=501.54s`
  - The final Pro failure was `NOT_FOUND` after retry, with `source_id_validated_after_not_found=null`; the Free lane was the slower lane overall and dominated the combined result
- The isolated Free-only 3-worker rerun completed as a negative branch at `free_only_retest_current_profile_run01`:
  - Combined hot-path VPH `1608.92`, `395/5`, `400` processed
  - `worker_idle_wait_s_total=30.591`, `source_ready_age_s_max=221.47s`
  - This is still well below the historical Free-only 3-worker leader, so it does not justify more geometry tuning on the Free lane
- The current Free regression is concentrated in startup and add cost, not browser-root cleanup or notebook creation:
  - `startup_notebook_create_elapsed_s_total=0.0` in both batches, so notebook creation is not the limiter
  - lane stderr does not show `closed default NotebookLM chrome-profile`, so browser-root cleanup is not the dominant signal here
  - current batch logs show `nlm_worker_notebook_cleanup_started` followed by deletion of `3-4` stale worker notebooks before work begins, and `startup_notebook_check_elapsed_s_total` / `startup_prepare_cleanup_elapsed_s_total` are both higher than the historical Free leader
- Next investigation should focus on worker-state hygiene and the notebook-check/add path: compare the shared `worker_states` lifecycle against the historical Free leader, then decide whether a fresh worker-state root or stricter preflight pruning is warranted before any more lane geometry work.
- The fresh worker-state-root control `free_only_fresh_state_control_run01` completed at `2825.29` combined hot-path VPH with `400/0/400` across two 200-item batches. Batch 1 was `2236.72` VPH and batch 2 was `3782.61` VPH. Both batches used clean per-run worker states and did not show stale-worker cleanup. That is now the strongest evidence that worker-state hygiene is the lever, not lane geometry.
- Throughput accounting is now split from cleanup accounting: benchmark summaries publish `throughput_wall_elapsed_s` separately from full `wall_elapsed_s`, and the combined VPH uses the cleanup-free span. Treat any cleanup timing as hygiene cost, not sustained throughput.
- The sharded lane runner now defaults to a fresh per-run worker-state root under `<run-root>/<lane>/worker_states`, with `--preserve-worker-state-root` retained only for explicit reuse experiments. The next best investigation is to rerun the canonical lane-series comparison on the same cohort with that default in place. Do not spend more time on the stale Free-only rerun shape unless the worker-state path changes again.

## Non-Negotiable Controls

- Run from `P:\\\\\\packages/yt-is`.
- Keep the control comparison against `pro_free_v2`, not against the slower `pro_free_staggered_60s_v3`.
- Keep no-stagger Pro+Free as the default benchmark shape unless this plan explicitly says to test a stagger variant.
- Keep `--batch-size 200`; it has already beaten nearby and larger batch sizes for this workload.
- Keep `--reusable-pipeline-mode serial`; double-buffered runs have not established a stable win.
- Keep profile-pinned NotebookLM commands. Do not use `nlm login switch` in concurrent worker code.
- For any new root, run `doctor` first, then the smoke, then `csf-run-evidence-check`, then the long soak.
- Keep dedicated Chrome roots:
  - Pro: `P:\\\\\\.data/yt-is/browser/notebooklm-pro`
  - Free: `P:\\\\\\.data/yt-is/browser/notebooklm-free`
- Keep account mapping:
  - Pro: `a.hominidae@gmail.com`
  - Free: `troup.hominidae@gmail.com`
- Keep source cleanup recheck protection before deleting stale worker notebooks.
- Do not count Whisper fallback in VPH. If a summary includes fallback fields, report them separately.

## Preflight

- [ ] Confirm no old benchmark process is running.

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'csf-sharded-lane-series|csf-source|nlm_batch' } |
  Select-Object ProcessId, CommandLine
```

Expected: no active benchmark processes. If a benchmark process is active, stop and decide whether it is the intended run before starting a new one.

- [ ] Confirm no unprofiled NotebookLM auth browser is running.

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'nlm login --force|remote-debugging-port=9222|\.notebooklm-mcp-cli\\chrome-profile' } |
  Select-Object ProcessId, Name, CommandLine
```

Expected: no default NotebookLM auth browser and no unprofiled `nlm login --force`. A transient `nlm login --force --profile <worker-profile>` is acceptable only when tied to one of the named worker profiles. If `nlm login --force` appears without `--profile`, stop the run and mark it invalid.

- [ ] Validate all NotebookLM worker profiles.

```powershell
foreach ($profile in @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free1-worker-01', 'ytis-free1-worker-02', 'ytis-free1-worker-03', 'ytis-free1-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Expected: every profile is authenticated. If any profile fails, use the dedicated browser auth refresh commands in `sharded-lane-series.md`. Do not use a shared/default Chrome profile.

The sharded runner now also performs this as a mandatory preflight. If a profile is expired, it runs one bounded `nlm login --force --profile <profile>` recovery before launching any lane. During benchmark subprocesses, `csf-source` runs with `YTIS_NLM_AUTH_NONINTERACTIVE=1`; expired auth uses `nlm login --force` instead of plain interactive `nlm login`.

- [ ] Run the existing focused regression tests before changing code.

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
pytest tests/test_nlm_batch.py -q
python -m py_compile csf/nlm_batch.py tests/test_nlm_batch.py bin/csf-source
```

Expected: tests pass and compile succeeds. If this fails before new edits, stop and inspect the current worktree before modifying behavior.

## Verified Test Suite

Use this suite before and after the next code change:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
pytest tests/test_nlm_batch.py tests/test_nlm_config.py tests/test_sharded_lane_series.py tests/test_nlm_worker_auth.py -q
python -m py_compile csf/nlm_batch.py csf/nlm_config.py csf/nlm_worker_auth.py tests/test_nlm_batch.py tests/test_nlm_config.py tests/test_nlm_worker_auth.py bin/csf-source bin/csf-nlm-worker-auth
```

Last verified results:

- `pytest tests/test_nlm_batch.py -q`: `68 passed` after `nlm_content_below_threshold` metric update.
- `pytest tests/test_nlm_scraper.py -q`: `59 passed` after staging scraper readiness-probe metric update.
- `pytest tests/test_worker_count_sweep.py tests/test_fallback_crossover_benchmark.py -q`: `10 passed` after reporting fixtures were updated to the new status.
- `python bin/csf-nlm-worker-auth sync`: uses account-aware `nlm login --check` parsing, repairs expired or wrong-account worker `01` profiles through the dedicated CDP root by default, then copies only after the renewed source profile matches the expected account.
- `pytest tests/test_nlm_batch.py tests/test_nlm_config.py tests/test_sharded_lane_series.py tests/test_nlm_worker_auth.py -q`: `79 passed`.
- `pytest tests/test_nlm_batch.py -q -k 'records_source_ids_from_stdout_in_order or rejects_duplicate_source_ids_before_fetch'`: `2 passed`.
- `pytest tests/test_nlm_worker_auth.py -q -k "real_nlm_process or worker_auth_cli_sync"`: `2 passed`; these are process-boundary tests that run a real temporary `nlm` executable and verify `check -> force -> check -> copy`, including the `bin/csf-nlm-worker-auth sync` wrapper, without mocking `subprocess.run`.
- `pytest tests/test_nlm_batch.py tests/test_nlm_config.py tests/test_sharded_lane_series.py tests/test_nlm_worker_auth.py tests/test_csf_source_fetch_timing.py -q -k "not cmd_check_all_emits_elapsed_scan_status_heartbeat and not logs_fetch_start_and_first_download_started_industrial and not limit_caps_selected_pending_items and not logs_worker_prewarm_summary_before_dispatch"`: `104 passed, 4 deselected`.
- `python -m py_compile ...`: passed for the touched `nlm_batch`, config, auth helper, tests, and CLI wrappers.

## Auth Renewal Proof Gate

Run this before the next full benchmark whenever any worker profile has expired:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
foreach ($profile in @(
  'ytis-pro-worker-01', 'ytis-pro-worker-02', 'ytis-pro-worker-03', 'ytis-pro-worker-04',
  'ytis-free1-worker-01', 'ytis-free1-worker-02', 'ytis-free1-worker-03', 'ytis-free1-worker-04'
)) {
  nlm login --check --profile $profile
}
```

Expected:

- If a worker `01` source profile is expired or mapped to the wrong account but recoverable, `csf-nlm-worker-auth sync` should refresh it through the configured dedicated CDP root, pass the follow-up account check, then copy refreshed credentials to sibling workers.
- If Google requires passkey/browser approval or the dedicated CDP root is itself on the wrong account, the command must fail before copying sibling credentials. Refresh only the affected worker `01` through the manual CDP flow in `sharded-lane-series.md`, then rerun this gate.
- Do not start `pro_free_source_map_v1` until all twelve `nlm login --check --profile ...` commands pass.

## Metrics To Record

For every full benchmark, record these values from `sharded_lane_series_summary.json`:

- `combined.hot_path_videos_per_hour`
- `combined.hot_path_success_count`
- `combined.hot_path_failure_count`
- `combined.processed_count`
- `combined.wall_elapsed_s`
- Per-lane hot-path VPH
- Per-lane success and failure counts
- Per-lane `cleanup_elapsed_s`
- Per-lane `add_elapsed_s`
- Per-lane `idle_elapsed_s`
- `content_fetch_status_counts_total`
- Any `source_add_failed` count
- Any content-fetch `NOT_FOUND` count
- Whisper fallback counts, reported separately and excluded from hot-path VPH

Use completed-worker totals and stage timings as throughput truth. Do not use backlog scan rate, queued item count, or fallback recovery count as sustained hot-path VPH.

## Phase 1: Fix Bounded Source-Add Retry

Purpose: recover transient whole-batch `source_add_failed` events without hiding permanent failures or creating duplicate add loops.

Known evidence: `pro_free_staggered_60s_v3` still had a counted Free lane `source_add_failed` where a 50-video subbatch failed quickly with zero added sources. That is a correctness and throughput opportunity.

- [ ] Inspect the current source-add path in `P:\\\\\\packages/yt-is/csf/nlm_batch.py`.
- [ ] Add or update focused tests in `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py` for:
  - transient source-add command failure retries once and then succeeds
  - permanent source-add command failure stops after the configured retry limit
  - retry logs include attempt count and worker profile
  - retry path still passes `--profile <worker-profile>` to every `nlm source` command
  - retry does not call `nlm login switch`
- [ ] Implement bounded retry only around the source-add command failure class.
- [ ] Do not retry content-fetch failures in this phase.
- [ ] Do not retry Whisper fallback in this phase.

Run:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
pytest tests/test_nlm_batch.py -q
python -m py_compile csf/nlm_batch.py tests/test_nlm_batch.py bin/csf-source
```

Pass criteria:

- Focused tests pass.
- Permanent failure still exits quickly.
- Command construction remains profile-pinned.
- Logs make retries auditable.

Stop criteria:

- If retry requires broad pipeline restructuring, stop and document the blocker before running a full benchmark.
- If retry causes duplicate source IDs or duplicate transcripts in a smoke run, revert only the new retry change and investigate before continuing.

## Phase 2: Diagnose Content-Fetch NOT_FOUND

Purpose: reduce counted `command_failed`/`NOT_FOUND` failures after source materialization.

Known evidence: `pro_free_staggered_60s_v3` still had content-fetch `NOT_FOUND` cases after the profile race and cleanup-race materialization timeout were fixed. Repeated source IDs appeared across multiple video IDs, so source-to-video mapping must be verified before assuming NotebookLM backend loss.

- [ ] Use the existing v3 artifacts as the failure corpus.

```powershell
Select-String -Path '.logs/sharded_lane_series/pro_free_staggered_60s_v3/**/*.json' -Pattern 'NOT_FOUND','source_add_failed','source_id' -List
```

- [ ] Add diagnostics or tests that prove whether one materialized source ID maps to exactly one input video ID inside a worker batch.
- [ ] Check whether source list parsing can reuse a stale source row, duplicate source ID, or wrong title/url match.
- [ ] If mapping is ambiguous, fix the mapping logic so the worker relists and remaps before `nlm source content`.
- [ ] If NotebookLM legitimately returns `NOT_FOUND` for a previously listed source, classify it distinctly from auth failure and source-add failure.

Run:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
pytest tests/test_nlm_batch.py -q
python -m py_compile csf/nlm_batch.py tests/test_nlm_batch.py bin/csf-source
```

Pass criteria:

- Tests cover the observed duplicate or stale mapping failure mode.
- Content-fetch failures have stage-specific status, not generic conflation with auth or source-add failures.
- The fix does not add broad sleeps to the hot path.

Stop criteria:

- If the root cause is not reproducible from logs or unit seams, document the uncertainty and proceed to Phase 3 only if Phase 1 is already passing.

Phase 2 evidence update:

- The summary JSON files did not contain the target strings, but worker stdout did.
- Use stdout as the failure corpus for source ID mapping diagnosis:

```powershell
rg -n "Fetch failed for|Source ID:|source_id_title_match_count|source_id_order_fallback_count" `
  P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_post_retry_v3 `
  -g "stdout.txt" -g "*.jsonl"
```

- Reproduce the duplicate-mapping risk in a unit test by creating a source list where one source entry exact-matches a video ID and the remaining entries rely on order fallback. The final mapping must be one-to-one and must not assign one source ID to multiple video IDs.
- Preferred implementation: parse `Source ID:` lines from the successful `nlm source add --wait` stdout in add order and persist that as the canonical mapping for the just-added video IDs. Keep `source list` as a materialization/count check, not the primary correlation source.
- Add a defensive duplicate-source-ID guard before `nlm source content` fetches. If duplicates are detected, log the duplicated source IDs and affected video IDs, classify the batch as a mapping failure, and do not waste hot-path time retrying duplicated content fetches.

## Phase 3: Run Fresh No-Stagger Control

Purpose: prove whether the fixes beat the current best under the same benchmark shape.

Use a new output root. Do not overwrite prior evidence.

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_post_retry_v1 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_post_retry_v1/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Extract summary:

```powershell
@'
import json
from pathlib import Path

path = Path("P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_post_retry_v1/sharded_lane_series_summary.json")
summary = json.loads(path.read_text())
print(json.dumps({
    "artifact": str(path),
    "combined_hot_path_vph": summary["combined"]["hot_path_videos_per_hour"],
    "success": summary["combined"]["hot_path_success_count"],
    "failure": summary["combined"]["hot_path_failure_count"],
    "processed": summary["combined"]["processed_count"],
    "wall_elapsed_s": summary["combined"]["wall_elapsed_s"],
    "lanes": {
        lane["lane"]: {
            "hot_path_vph": lane["hot_path_videos_per_hour"],
            "success": lane["hot_path_success_count"],
            "failure": lane["hot_path_failure_count"],
            "cleanup_elapsed_s": lane.get("cleanup_elapsed_s"),
            "add_elapsed_s": lane.get("add_elapsed_s"),
            "idle_elapsed_s": lane.get("idle_elapsed_s"),
            "content_fetch_status_counts_total": lane.get("content_fetch_status_counts_total"),
        }
        for lane in summary["lanes"]
    },
}, indent=2))
'@ | python -
```

Decision:

- If VPH is greater than `4148.71` and there are no harness/auth failures, this is the new best known method.
- If VPH is at least `4356`, treat it as a strong win because it is more than `5%` above control.
- If VPH is lower than or equal to `4148.71`, keep `pro_free_v2` as the max known sustained result and document the negative result.
- If the run has `PERMISSION_DENIED`, same-account behavior, or materialization-timeout cleanup race, mark it invalid, fix the harness, and rerun once.

Fresh rerun outcome:

- The fresh no-stagger rerun after worker auth sync completed cleanly and beat the prior control:
  - combined hot-path VPH: `4407.40`
  - hot-path success/failure: `688/112`
  - relative to `pro_free_v2`, this is a strong win and the new best sustained result
- Pro lane hot-path VPH: `2518.32`
- Free lane hot-path VPH: `1984.00`
- The lane stdout summaries showed no `PERMISSION_DENIED` or cleanup-race invalidation.

Later control recheck:

- A subsequent fresh no-stagger control rerun under `pro_free_post_retry_v3` regressed sharply:
  - combined hot-path VPH: `1982.17`
  - hot-path success/failure: `639/161`
  - Pro lane hot-path VPH: `1202.13`
  - Free lane hot-path VPH: `1036.97`
- Treat that as a negative control recheck, not the new best method.

## Phase 4: Cleanup Cost Optimization

Purpose: reduce measured hot-path wall time only after source-add and content-fetch correctness are stable.

Candidate approaches:

- Avoid full notebook delete/recreate when source delete/reset is sufficient and faster.
- Move nonessential stale-notebook inventory outside the measured hot path, but only if active worker notebooks remain protected.
- Keep notebook reuse/audit behavior deterministic; do not reintroduce accidental deletion of active worker notebooks.

Test shape:

- Use the same Pro+Free no-stagger control.
- Use the same `--limit 400`, `--batch-size 200`, and serial pipeline.
- Use a new output root such as `P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_cleanup_opt_v1`.
- Compare against both `pro_free_v2` and the Phase 3 post-retry result.

Pass criteria:

- Combined hot-path VPH increases.
- `cleanup_elapsed_s` decreases materially.
- Failure count does not increase.
- No active worker notebook is deleted.

Stop criteria:

- If cleanup optimization lowers cleanup time but increases failures enough to reduce VPH, record it as negative and keep the old cleanup path.

Outcome:

- `pro_free_cleanup_opt_v2` was a negative cleanup-cost rerun:
  - combined hot-path VPH: `1807.26`
  - hot-path success/failure: `349/451`
  - processed: `800`
  - wall elapsed: `695.2s`
- Pro lane hot-path VPH: `2904.14`
- Free lane hot-path VPH: `518.55`
- The bulk source-delete cleanup path did not improve throughput enough and the prior chunked cleanup path was restored.

## Phase 5: Focused Sparse-Content And Command-Failed Probe

Purpose: explain the four residual failures from `pro_free_source_map_v1` before spending more time on broad worker/load sweeps.

Known evidence from the latest best run:

- `juXI9QbzzgM` failed in both Pro and Free lanes as below-threshold NotebookLM content.
- `u2hmsms-alg` failed in both Pro and Free lanes as `command_failed`.
- The same two video IDs failed across lanes with different NotebookLM source IDs, so the next hypothesis should be content/path behavior, not lane auth or source-ID mapping.

Live probe result:

- `juXI9QbzzgM` is stable below-threshold NotebookLM content on both `ytis-pro-worker-01` and `ytis-free1-worker-01`.
- `u2hmsms-alg` recovered immediately on both `ytis-pro-worker-01` and `ytis-free1-worker-01` in isolated probe runs, so the benchmark `command_failed` result looks transient or harness-sensitive rather than content-intrinsic.
- The probe harness is now available as [`bin/csf-nlm-content-probe`](../../bin/csf-nlm-content-probe) and writes JSON artifacts under `.logs/nlm_content_probe/`.

Required setup:

- Use the new status name `nlm_content_below_threshold`.
- Capture `nlm_content_chars`, `usable_text_chars`, raw `nlm source content` return code, stdout, and stderr.
- Do not classify short videos as low value. A short video can be valuable; the question is whether NotebookLM source-content extraction produced usable text.
- Do not run another full 800-item benchmark until this probe has been completed.

Recommended live probe:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync

# Build the smallest possible source-content probe around these two IDs.
# If no dedicated probe command exists yet, implement one rather than running
# another full sharded benchmark.
$videoIds = @('juXI9QbzzgM', 'u2hmsms-alg')
```

Probe requirements:

- Add each video to a disposable or explicitly named probe notebook under both account families.
- Parse `Source ID:` from `nlm source add --wait` stdout and use that source ID directly.
- Run `nlm source content <source-id> --json` immediately, then after delayed retries such as `30s`, `60s`, and `120s`.
- Record per attempt:
  - account family and profile
  - video ID
  - source ID
  - `video_duration_s` if available from yt-dlp metadata
  - `nlm_content_chars`
  - `usable_text_chars`
  - `extraction_outcome`
  - `returncode`
  - first 500 chars of stdout/stderr

Expected interpretation:

- If `juXI9QbzzgM` consistently returns below-threshold NotebookLM content but fallback transcript extraction yields usable text, route that class to a short-video fallback rather than counting it as a failed video.
- If `u2hmsms-alg` recovers immediately or after delay, treat the benchmark `command_failed` as transient and revisit only the retry/harness timing if it reappears in full-benchmark concurrency.
- If either video recovers after delayed retry, adjust retry timing/classification before repeating the full benchmark.

Pass criteria:

- The two residual failure classes have distinct, auditable outcomes.
- Future benchmark summaries no longer report `too_short` for NotebookLM source-content results.
- The next full benchmark can distinguish hot-path failures from fallback-recoverable sparse content.
- The residual `command_failed` class is shown to be transient or harness-sensitive, not a stable content class.

Stop criteria:

- If the probe requires user passkey/auth intervention, stop and repair worker-profile auth before collecting evidence.
- If source IDs cannot be mapped directly from add stdout, stop and fix the probe harness rather than trusting `source list` order.
- If a broad benchmark is started before this probe, mark it as premature in the registry.

## Failure Triage Rules

- `PERMISSION_DENIED` during source materialization usually means profile/account mismatch or a command that was not pinned with `--profile`; fix auth/profile routing before trusting throughput.
- `source_add_failed` with zero added sources is a source-add stage failure; apply Phase 1 retry logic, then measure.
- `NOT_FOUND` during content fetch after source materialization is not the same as cleanup-race materialization timeout; use Phase 2 diagnostics.
- A run with the wrong account in either lane is invalid.
- A smoke run can validate behavior, but it cannot establish sustained VPH.
- A staggered run is not the control unless a no-stagger run with the same code path has already been recorded.

## Documentation Requirements

After each full benchmark:

- [ ] Add a row to `P:\\\\\\packages/yt-is/docs/operations/test-registry.md`.
- [ ] Update `P:\\\\\\packages/yt-is/docs/operations/sharded-lane-series.md` if the recommended method, current best, auth contract, or caveats change.
- [ ] Include the exact artifact path.
- [ ] Include combined hot-path VPH.
- [ ] Include success, failure, processed count, and wall time.
- [ ] Include whether Whisper was used and explicitly state that it was excluded from VPH.
- [ ] Mark the result `proven`, `negative`, `invalid`, or `pending`.
- [ ] Add a rerun guard naming the code path that would justify repeating the test.

## Recommended Next Action

Source-add retry, worker auth sync, auth auto-renew regression tests, source ID mapping hardening, the zero-growth add retry, notebook-reset fallback for zero-growth add failures, profile-pinned `nlm_batch` auth refresh, and the `nlm_content_below_threshold` metric rename are now implemented. The fresh source-map reruns did not improve on the current best: `pro_free_source_map_v2` regressed to `2917.93` combined hot-path VPH with the Pro lane dominated by `source_add_failed`; `pro_free_source_map_v3` improved materially to `3850.52` but still trailed the best; `pro_free_source_map_v5` completed cleanly but still showed Free lane `source_add_failed`; and `pro_free_source_map_v6` after the notebook-reset fallback was negative at `1837.24`. Phase 5 has now been executed: `juXI9QbzzgM` is stable sparse content and representative benchmark `command_failed` cases were recoverable in isolated probes, so those failures are treated as transient/harness-sensitive rather than content-specific. A fresh isolated 50-source add on Pro succeeded, and a repeated reusable Pro run succeeded twice, so the remaining open issue is now narrowed to transient NotebookLM add flakiness under the benchmark run shape rather than a deterministic add bug. The root cause for the Pro regression is now understood: `nlm source add` can return nonzero even when the notebook source count reaches the full batch size, and the batch ingestor now treats that as recovered success instead of a hard failure. `pro_free_source_map_v1` remains the best sustained Pro+Free result. Cleanup-cost optimization was attempted next, but `pro_free_cleanup_opt_v2` stayed negative, so the cleanup path was rolled back and no documented phase remains to rerun without a new hypothesis.

The completed `sweep_phase3_2lane_3w_run05` auth-check cache TTL A/B is now also negative evidence. It finished cleanly at `1958.94` combined hot-path VPH, with `132` Pro logins, `128` Free logins, `session_age_s` still in the `0-30s` band, and higher `add_elapsed_s_total`, `worker_idle_wait_s_total`, and `source_ready_age_s_avg` than the `run04` comparator. That makes auth-check cache TTL a dead branch for this cohort and shifts the next investigation toward source-add/readiness/setup cost, startup/setup overhead, or another non-TTL limiter.
The later guarded `sweep_phase3_2lane_3w_run06` rerun recovered the browser-health gate and the auth state but still only reached `2284.56` combined hot-path VPH with `794/6/800`; it improved over run05, but it remained far below the historical `3+3` leader, so browser-health hygiene is now a solved preflight issue rather than the throughput limiter.
The later single-lane calibration pair sharpened the same point: Pro-only stayed at `1980.19` combined hot-path VPH with `worker_idle_wait_s_total=243.778`, while Free-only reached `3361.75` with `worker_idle_wait_s_total=0.0`. The per-worker traces show the Pro lane also paid a much larger `extract_elapsed_s_total` on at least one worker, so the next useful probe is the Pro startup/setup -> extract path, not another auth TTL or lane-count repeat.
The fresh Pro-only rerun after lane-config repair was even weaker at `1105.3` combined hot-path VPH with `398/2/400`, `worker_idle_wait_s_total=804.382`, and `source_ready_age_s_avg=67.471`; a fresh Free-only rerun completed at `929.05` combined hot-path VPH with `199/1/200`, but that sample is only auxiliary because the processed count was `200`, not a like-for-like 400-item comparator. The branch still points at startup/setup and extract costs, but it is now noisy enough that another lane-width or TTL repeat is not the next best move.
The fresh guarded repeat `sweep_phase3_2lane_3w_run07` completed cleanly but only reached `1974.57` combined hot-path VPH. Its `37` `command_failed` events were all `NOT_FOUND`, the new source-list probe marked every one `source_validated=true`, and the failure rate by source age was sharply skewed: `0%` below `200s`, `25%` in `200-300s`, `95.65%` in `300-400s`, and `100%` above `400s`. That points more toward source-id remap/staleness or notebook-age pressure than a missing retry marker, and it makes notebook rotation/cadence or source-readiness refresh the more promising next probe than broader retry policy. The probe now also records the matched source-row metadata, so the next benchmark can distinguish a stale ID from the notebook showing the expected row.
The follow-up `run07_age_300_probe` on a single source stayed `ready` at both the immediate and 300-second fetches, so notebook age alone does not reproduce the `NOT_FOUND` behavior on a one-source notebook. That leaves the remaining hypothesis space centered on multi-source notebook load, rotation cadence, or benchmark-specific source remapping under batch pressure.
The fresh guarded repeat `sweep_phase3_2lane_3w_run08` regressed to `1779.65` combined hot-path VPH with `794/6/800`. The Pro lane saw `45` `command_failed` events and the Free lane saw `45`; the live `NOT_FOUND` completions still had `source_validated_after_not_found=true`, and the failed rows were age-skewed into the `243s`-`382s` range while fresh recreated notebook batches near `5.6s` stayed healthy. That keeps the remaining hypothesis centered on notebook-age / rotation cadence under batch pressure, not a missing retry marker or a stale-source-id-only problem. The next useful experiment is a narrower age-capped benchmark or notebook-rotation probe that keeps `source_ready_age_s` below the cliff, not another same-shape repeat.
The age-capped guarded sequence `sweep_phase3_2lane_3w_agecap_200_run02` improved the same 3+3 shape to `3084.08` combined hot-path VPH with `398/2/400` and clean post-run hygiene. The age cap held both lanes under the earlier cliff, with Pro `source_ready_age_s_max=211.292` and Free `source_ready_age_s_max=160.966`, and the residual failures shifted to `nlm_content_below_threshold` rather than `NOT_FOUND`. That means the age cap reduced the cliff but still did not beat the historical `4123.28` leader, so the next branch is a tighter age-cap / sparse-content follow-up, not broader retry markers.
The follow-up targeted probe on `juXI9QbzzgM` and `u2hmsms-alg` after auth refresh repeated the same split: `juXI9QbzzgM` stayed below threshold on every delayed retry, while `u2hmsms-alg` returned `ready` immediately on the first attempt. That reconfirms the residual pair is not a new content-class regression; it is the same stable below-threshold case plus a harness-sensitive/variable `command_failed` case, so the next live probe should target notebook-age or rotation cadence rather than the video pair itself.

## Phase 6: Source-Map Rerun After Profile-Pinned Auth Fix

Purpose: validate whether the zero-growth source-add retry plus profile-pinned `nlm_batch` auth refresh closes the remaining source-map regression. This is a benchmark-only phase. Do not add notebook-reset fallback, cleanup changes, worker-count changes, batch-size changes, or stagger changes before this rerun.

The interrupted `pro_free_source_map_v4` attempt is invalid and must not be used as throughput evidence. It stopped before a `sharded_lane_series_summary.json` was produced and exposed an unprofiled `nlm login --force` path that opened the default NotebookLM Chrome profile account chooser.

Preflight:

- Confirm no stale benchmark or auth process is running with the two process checks above.
- Run the auth renewal proof gate.
- Run `pytest tests/test_nlm_batch.py -q` after any auth-path edit.
- Confirm the lane config still uses the dedicated Pro and Free worker profiles from `pro_free_lanes.json`.

Run exactly one fresh source-map rerun under a new output root:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v5 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v5/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Live guard while the benchmark is running:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'csf-sharded-lane-series|csf-source fetch|nlm login --force|remote-debugging-port=9222|\.notebooklm-mcp-cli\\chrome-profile' } |
  Select-Object ProcessId, Name, CommandLine
```

Expected:

- `nlm login --force --profile <worker-profile>` may appear briefly only if a worker profile expires.
- `nlm login --force` without `--profile` invalidates the run; stop it and fix the caller.
- Chrome using `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile` invalidates the run for Pro+Free sharded benchmarking.
- Chrome using the dedicated Pro or Free roots is acceptable only when tied to the configured lane roots.

Extract the result:

```powershell
@'
import json
from pathlib import Path

path = Path("P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v5/sharded_lane_series_summary.json")
summary = json.loads(path.read_text())
print(json.dumps({
    "artifact": str(path),
    "combined_hot_path_vph": summary["combined"]["hot_path_videos_per_hour"],
    "success": summary["combined"]["hot_path_success_count"],
    "failure": summary["combined"]["hot_path_failure_count"],
    "processed": summary["combined"]["processed_count"],
    "wall_elapsed_s": summary["combined"]["wall_elapsed_s"],
    "lanes": {
        lane["lane"]: {
            "hot_path_vph": lane["hot_path_videos_per_hour"],
            "success": lane["hot_path_success_count"],
            "failure": lane["hot_path_failure_count"],
            "cleanup_elapsed_s": lane.get("cleanup_elapsed_s"),
            "add_elapsed_s": lane.get("add_elapsed_s"),
            "idle_elapsed_s": lane.get("idle_elapsed_s"),
            "content_fetch_status_counts_total": lane.get("content_fetch_status_counts_total"),
            "source_add_failed": lane.get("source_add_failed"),
        }
        for lane in summary["lanes"]
    },
}, indent=2))
'@ | python -
```

Decision:

Fresh rerun outcome:

- `pro_free_source_map_v5` completed cleanly with no unprofiled auth browser activity.
- Combined hot-path VPH: `3930.79`
- Hot-path successes/failures: `638/162`
- Processed: `800`
- Wall elapsed: `584.31s`
- Pro lane: `2406.66` hot-path VPH, `390/10`, `content_fetch_status_counts_total={"ready":390,"nlm_content_below_threshold":1,"command_failed":9}`
- Free lane: `2001.72` hot-path VPH, `248/152`, `content_fetch_status_counts_total={"ready":248,"nlm_content_below_threshold":1,"command_failed":1}`
- Free lane stdout still shows `source_add_failed` in batch 01 and batch 02, so the add-path fallback is still incomplete even though auth is fixed.

Decision:

- `pro_free_source_map_v5` improved over `pro_free_source_map_v3` but did not beat `pro_free_source_map_v1`.
- The next code change should be a bounded notebook-reset retry fallback for the zero-growth add failure class, targeted at the Free lane path that still emits `source_add_failed`.
- Do not rerun the same source-map shape again until that fallback exists or a stronger reason to repeat it is documented.

## Phase 7: Implement Notebook-Reset Fallback For Zero-Growth Add Failure

Purpose: recover the remaining Free lane `source_add_failed` cases shown in `pro_free_source_map_v5` without broad retries, hidden duplicate adds, or another auth regression.

Current evidence:

- `pro_free_source_map_v5` completed with profile-pinned auth, so auth is not the current blocker.
- Pro lane was comparatively healthy: `390/10`.
- Free lane had `248/152` and stdout showed zero-growth `source_add_failed` in both batch 01 and batch 02.
- The existing bounded zero-growth retry did not recover those Free lane failures.

Implementation target:

- Primary file: `P:\\\\\\packages/yt-is/csf/nlm_batch.py`
- Primary tests: `P:\\\\\\packages/yt-is/tests/test_nlm_batch.py`
- Existing code points:
  - `_add_sources_chunk(...)` contains the current zero-growth add retry.
  - `_ZERO_GROWTH_ADD_RETRY_LIMIT` and `_ZERO_GROWTH_ADD_RETRY_DELAY_S` control the first retry.
  - `_rotate_notebook()` currently clears/recycles a notebook when source count approaches the cap.
  - `_add_sources_in_subbatches(...)` records subbatch status and source counts.

Required behavior:

- Only trigger notebook-reset fallback when all of these are true:
  - `nlm source add` returned nonzero.
  - `source_count_after == source_count_before`.
  - Failure reason is `source_add_failed`.
  - The normal bounded zero-growth retry has already been used.
- On fallback:
  - Retire or reset the current worker notebook through the existing worker-owned notebook lifecycle path.
  - Prepare a fresh notebook for the same worker profile and same notebook prefix.
  - Retry the same subbatch once.
  - Keep every `nlm` command profile-pinned through `NOTEBOOKLM_PROFILE`.
  - Preserve source-ID mapping from `nlm source add --wait` stdout after the retry.
  - Do not split the batch into smaller subbatches as the fallback.
  - Do not retry content-fetch failures in this phase.

Required logs:

- Emit a distinct scheduling log such as `nlm_batch_subbatch_add_notebook_reset_retry_scheduled`.
- Include `nb_id`, `subbatch_index`, `subbatch_size`, `retry_depth`, source counts before/after, `source_profile`, `notebooklm_profile`, and the old/new notebook IDs when available.
- Emit a distinct exhausted log if the reset retry also fails.
- Keep existing `nlm_batch_subbatch_add_failed` behavior for final failure.

Required tests:

- Zero-growth add failure uses the existing in-place retry first.
- If the in-place retry also fails with zero growth, the notebook-reset fallback is scheduled once.
- The reset fallback retries the same video IDs and recovers when the fresh notebook add succeeds.
- If the reset fallback also fails, the subbatch returns empty and logs final failure.
- The fallback does not run for nonzero add returns that already grew the source count to the expected total.
- The fallback does not run for content-fetch `command_failed` or `nlm_content_below_threshold`.
- The fallback keeps configured batch size; it must not shrink recursively.
- The fallback does not create duplicate source IDs and preserves stdout-derived source-ID order.
- The auth context remains profile-pinned; no test should expect unprofiled `nlm login --force`.

Run after implementation:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python -m pytest P:\\\\\\packages/yt-is/tests/test_nlm_batch.py -q -k "zero_growth_add_failure or notebook_reset or source_id or auth_context"
python -m pytest P:\\\\\\packages/yt-is/tests/test_nlm_batch.py -q
python -m py_compile P:\\\\\\packages/yt-is/csf/nlm_batch.py P:\\\\\\packages/yt-is/tests/test_nlm_batch.py P:\\\\\\packages/yt-is/bin/csf-source
```

Then run exactly one full source-map benchmark under a new output root:

```powershell
$env:PYTHONPATH = 'P:\\\\\\packages\yt-is'
python P:\\\\\\packages/yt-is/bin/csf-nlm-worker-auth sync
python P:\\\\\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v6 `
  --cohort-json P:\\\\\\packages/yt-is/.logs/sharded_lane_series/pro_free_source_map_v6/cohort.json `
  --limit 400 `
  --batch-size 200 `
  --reusable-pipeline-mode serial
```

Decision:

- If `pro_free_source_map_v6` removes or materially reduces Free lane `source_add_failed` and beats `3930.79`, keep the fallback and compare against the current best `5572.04`.
- If it improves Free failures but still does not beat `5572.04`, record it as an improvement but not a new best.
- If it increases wall time enough to reduce VPH, record it as negative and keep the fallback only if it materially improves correctness.
- If unprofiled auth appears again, mark the run invalid and fix auth before trusting throughput.
- Do not proceed to cleanup optimization, stagger tests, worker-count changes, or content-fetch retry tuning until this phase is recorded in `test-registry.md`.

Outcome:

- `pro_free_source_map_v6` completed cleanly with profile-pinned auth but remained negative:
  - combined hot-path VPH: `1837.24`
  - hot-path success/failure: `299/501`
  - processed: `800`
  - wall elapsed: `585.88s`
- Pro lane hot-path VPH: `616.2`
- Free lane hot-path VPH: `1394.33`
- The notebook-reset fallback reduced the zero-growth add failure class but did not recover enough throughput to beat the current best.

## Phase 8: Source-Readiness And Extract Attribution Probe

Purpose: determine which part of the hot path is actually consuming the remaining wall time before any routing change is made. The first `NOT_FOUND` in a batch still justifies a source-list validation because it can confirm the source row and separate stale-ID cases from missing-item cases. Repeated validation during a `NOT_FOUND` storm is now a throughput cost, so this probe must be bounded in sustained runs. The current evidence does not justify a captioned-video switch to `yt-dlp` first. Caption-rich items stay on NotebookLM unless a same-shape A/B proves otherwise.

Current evidence:

- `idle_wait_validation_run03` shows auth accounting is now sane, but idle cost remains high.
- `repro_v1_pro_only_4w_run02` still shows large Pro idle wait and extract cost on the same 400-item shape.
- `sweep_phase3_2lane_3w_run07` and `run08` show the age cliff / `NOT_FOUND` pressure is real, but it does not by itself identify whether the bottleneck is materialization wait, content fetch latency, or notebook rotation cadence.
- `highest_vph_attribution_probe_run02` collapsed to `977.45` combined hot-path VPH; batch 1 carried heavy `NOT_FOUND` pressure and the source-list probe cost was large, so repeated validation is no longer a free diagnostic in storm conditions.
- `highest_vph_not_found_probe_cap_sequence_run01` improved the same 400-item sequence shape to `1204.97` combined hot-path VPH with the bounded probe cap in place; the first `NOT_FOUND` probe remained diagnostic, repeated validation no longer dominated the storm cost, and the run still stayed well below the age-capped control.
- `pro_free_source_map_v1` remains the sustained captioned control until a newer like-for-like run beats it.

Implementation target:

- Primary file: `P:\\packages\\yt-is\\csf\\nlm_batch.py`
- Primary tests: `P:\\packages\\yt-is\\tests\\test_nlm_batch.py`
- Primary docs: this plan, `test-registry.md`, and `observability-contract-checklist.md`

Required probe shape:

- Keep the current captioned NotebookLM-first control path.
- Add or surface per-stage timing that separates at least:
  - materialization wait
  - NotebookLM content fetch / retry work
  - source-list probe time
  - yt-dlp probe time
  - any extract-side sleep or backoff time
- Preserve the existing `combined.hot_path_videos_per_hour` contract.
- Do not change lane width, batch size, or auth TTL in the same probe.

Decision gates:

- On the first `NOT_FOUND`, run one source-list validation and keep the matched source-row metadata so the run can still separate stale-ID from missing-item cases.
- If `NOT_FOUND` keeps repeating in the same lane or subbatch, cap source-list validation at one check per 10 additional `NOT_FOUND` events, or one check per 60s, whichever is less frequent.
- If source-list validation consumes more than 5% of lane wall time in any 5-minute window, or if three sampled checks in a row add no new source-row mismatch, disable the probe until the next clean batch boundary.
- If materialization wait dominates, test a shorter readiness poll interval next.
- If content fetch dominates, profile the NotebookLM CLI boundary and subprocess overhead next.
- If source age climbs before extract completes, test rotation cadence or smaller subbatches next.
- If none of those explain the idle time, re-check worker-side auth and browser-root propagation before changing routing.

## Phase 9: Bounded Source-List Validation During NOT_FOUND Storms

Purpose: keep the first-miss diagnostic value from Phase 8 without letting repeated source-list validation distort sustained-throughput runs.

Current evidence:

- `highest_vph_attribution_probe_run02` showed the source-list probe is useful on the first `NOT_FOUND`, but repeated validation becomes a throughput drag under storm conditions.
- `highest_vph_not_found_probe_cap_sequence_run01` confirmed the bounded probe cap helps, but the remaining gap is still source-readiness / age behavior, not more probe volume.
- `small_subbatch_source_readiness_run01` improved the same 2-lane shape further to `1998.83` combined hot-path VPH with `batch-size 100`, with Pro `source_ready_age_s_max=225.028s` and Free `source_ready_age_s_max=195.392s`.
- `small_subbatch_source_readiness_run02` regressed to `1549.75` combined hot-path VPH with `batch-size 150`, with Pro `source_ready_age_s_max=416.751s` and Free `source_ready_age_s_max=438.977s`.
- `pro_free_source_map_v1` remains the historical best sustained control, so any bounded probe must be measured against that baseline, not against a single diagnostic sample.

Implementation target:

- Primary file: `P:\\packages\\yt-is\\csf\\nlm_batch.py`
- Primary tests: `P:\\packages\\yt-is\\tests\\test_nlm_batch.py`
- Preserve the first `NOT_FOUND` source-list validation from Phase 8.
- Apply the Phase 8 cap/sample/disable gate during sustained runs so repeated `NOT_FOUND` storms do not keep paying full probe cost.
- Do not change lane width, batch size, or auth TTL in the same bounded-probe change.

Do not:

- Do not switch captioned items to a `yt-dlp`-first default without a same-shape controlled A/B.
- Do not retry the old broad route-mix experiments until this attribution probe has a clean result.
- Do not treat `yt-dlp` fast-path performance on no-caption or failure-recovery cases as evidence that it should replace NotebookLM for captioned throughput.

## Phase 10: Smaller Subbatch Geometry For Source-Readiness

Purpose: test whether the remaining throughput loss comes from too much source age accumulating between notebook boundaries rather than from the bounded `NOT_FOUND` probe itself.

Current evidence:

- `highest_vph_not_found_probe_cap_sequence_run01` improved the 400-item sequence shape to `1204.97` combined hot-path VPH with the bounded probe cap in place.
- The same run still showed the Free lane crossing `source_ready_age_s_max=1026.949s` in batch 2, with `command_failed=40`, so the age cliff is still not under control.
- The Pro lane recovered much better than the uncapped attribution run, which means the probe cap is a keep, not a new variable to reopen.
- `small_subbatch_source_readiness_run02` and `small_subbatch_source_readiness_run03` both finished as partial lanes (`processed_count_total=700`), so they are not clean full-load negatives even though they were recorded as `status=ok`.
- `small_subbatch_source_readiness_run01` remains the only clean full-load subbatch result in this branch.

Implementation target:

- Primary file: `P:\\packages\\yt-is\\docs\\operations\\hot-path-throughput-next-test-plan.md`
- Primary benchmark runner: `P:\\packages\\yt-is\\bin\\csf-sharded-lane-sequence`
- Preserve `YTIS_NLM_NOT_FOUND_SOURCE_LIST_PROBE_CAP=1`.
- Do not change auth TTL.
- Do not widen `_SOURCE_AGE_CLIFF_S`.

Experiment:

1. Re-run the same 2-lane, 4-worker, captioned `limit 400` shape with `batch-size 100` and `reusable-pipeline-mode serial`.
2. Keep the same lane config, source URL, and bounded `NOT_FOUND` probe cap.
3. Compare the result against `highest_vph_not_found_probe_cap_sequence_run01` and the age-capped control `sweep_phase3_2lane_3w_agecap_200_run02`.

```powershell
$env:PYTHONPATH = 'P:\\packages\\yt-is'
python P:\\packages\\yt-is\\bin\\csf-sharded-lane-sequence `
  --lane-config P:\\packages\\yt-is\\.logs\\sharded_lane_series\\pro_free_lanes.json `
  --run-root P:\\packages\\yt-is\\.logs\\sharded_lane_series\\small_subbatch_source_readiness_run01 `
  --smoke-limit 400 `
  --smoke-batch-size 100 `
  --soak-limit 400 `
  --soak-batch-size 100 `
  --reusable-pipeline-mode serial
```

Decision gates:

- If the lane summary can finish `ok` while `processed_count_total` is below the configured `limit`, fix the validity gate first and stop using those runs as clean negatives.
- If the current summary gate is fixed, rerun a clean `3+3` pressure comparison before pursuing more subbatch tuning.
- If the clean `3+3` rerun still trails `highest_vph_agecap_400_run01`, move to earlier notebook rotation or an age-trigger refinement rather than expanding subbatch size further.
- `small_subbatch_source_readiness_run01` remains the only clean full-load subbatch result in this branch, so do not treat `run02` or `run03` as evidence against the subbatch effect until the validity gate is fixed.
- If `source_ready_age_s_max` still crosses the cliff or `command_failed` rises materially, the workload still needs earlier notebook rotation rather than more diagnostic probing.
- If `worker_idle_wait_s_total` remains high while `content_fetch_command_elapsed_s_total` does not fall, test readiness polling interval and NotebookLM CLI boundary overhead next.
- If `source_list_probe_elapsed_s_total` grows back above the current capped sequence run, stop and tighten the probe cap further before changing anything else.

Do not:

- Do not widen the source-age cliff.
- Do not change auth TTL in the same run.
- Do not switch captioned items to `yt-dlp` first without a same-shape A/B.
