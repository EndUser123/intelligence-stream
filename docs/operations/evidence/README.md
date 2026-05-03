# Evidence Index

This folder holds the durable, human-readable evidence index for auth and throughput work.

## Policy

- Keep only small, intentional proof artifacts here or in the adjacent operation docs.
- Treat full `.logs/sharded_lane_series/*` trees as runtime output, not the source of truth.
- Use fresh output roots for each benchmark or smoke run.
- Do not reuse a previous root name for new evidence.
- If a run is only useful as a marker drill, capture the smallest proof artifact and stop there.
- Keep NotebookLM auth profile snapshots under the profile root as operational recovery data, not benchmark evidence.
- Validate a completed run root with `python P:/packages/yt-is/bin/csf-run-evidence-check --run-root <path>` before promoting it to canonical evidence.
- The evidence checker reads structured JSONL event fields for `default_profile_running`, `source_add_failed`, `nlm_batch_subbatch_add_split_circuit_opened`, and `nlm_auth_forced_refresh_scheduled`. Do not rely on string fragments in free-form text as proof.

## Canonical Auth Evidence

| Case | What It Proves | Canonical Evidence |
|---|---|---|
| `pro_free_auth_marker_v4` | Explicit forced-refresh marker proof for the Pro family | [`term_ad61538d.jsonl`](../../../.logs/sharded_lane_series/pro_free_auth_marker_v4/logs/term_ad61538d.jsonl) |
| `pro_free_auth_forced_smoke_v3` | Clean post-hardening smoke with no `default_profile_running` or `source_add_failed` | [`sharded_lane_series_summary.json`](../../../.logs/sharded_lane_series/pro_free_auth_forced_smoke_v3/sharded_lane_series_summary.json) |
| `pro_free_auth_forced_smoke_v7` | Benchmark-shaped marker proof with lane-root forced-refresh events for both lanes | [`sharded_lane_series_summary.json`](../../../.logs/sharded_lane_series/pro_free_auth_forced_smoke_v7/sharded_lane_series_summary.json) |

## How To Use

- Read this index first when you need the current auth evidence contract.
- Use the full docs for procedure details.
- Use the `.logs` roots only to inspect the underlying artifact, not to redefine the current proof set.
