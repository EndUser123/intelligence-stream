# NotebookLM Auth Pre-Mortem

> Read this before a long Pro+Free soak or any run that depends on repeated auth refreshes.

## Purpose

This document lists the failure modes that are most likely to waste a long auth benchmark if they are not checked up front.
It is deliberately pessimistic: the goal is to catch the obvious ways the system can lie to us before a soak burns time.

## What Can Go Wrong

1. The Pro lane points at the wrong Chrome profile directory.
   - Symptom: auth refresh appears to work, but the browser is actually bound to a different account.
   - Guardrail: `csf/nlm_worker_auth.py` must use the signed-in Pro browser profile, and the live `Local State` file must confirm it before any benchmark starts.

2. A valid session is on the wrong Google account.
   - Symptom: `nlm login --check` succeeds, but the account name does not match the lane.
   - Guardrail: treat the wrong account as auth failure everywhere, including worker sync and benchmark preflight.

3. CDP opens the wrong browser instance or lands on a stale tab.
   - Symptom: `0.0.0.2`, `about:blank`, or restored tabs appear before NotebookLM loads.
   - Guardrail: use the lane-specific user-data-dir and profile-directory, close harmless noise tabs only, and fail closed if the dedicated root cannot be reached.

4. Chrome crashes or is force-killed and the profile comes back in a dirty state.
   - Symptom: restored tabs, crashed profile flags, or unpredictable account selection on the next launch.
   - Guardrail: prefer graceful shutdown, restore only the local source-profile snapshot when auth repair fails, and do not silently switch to a different auth path.

5. The benchmark artifacts look healthy even though auth did not refresh.
   - Symptom: throughput numbers exist, but the run never exercised the intended re-auth path.
   - Guardrail: for a soak that is supposed to prove re-auth, require explicit evidence of refresh events in the logs.

6. Cleanup drifts out of sync with the actual auth contract.
   - Symptom: docs still mention fallback diagnostics or a deprecated browser profile path after the code has changed.
   - Guardrail: update the operations guide, rerun recipe, and robustness plan in the same change as the code fix.

7. Generated benchmark debris accumulates and obscures the real signal.
   - Symptom: stale run folders, deleted worker-state snapshots, and smoke outputs remain mixed with the current evidence.
   - Guardrail: keep benchmark evidence under a clearly named run root, prune only after the result is captured, and do not let old output roots become the source of truth.

## Stop Conditions

Stop the run immediately if any of these happen:

- Pro refresh uses anything other than the signed-in Pro profile directory.
- `nlm login --check` reports a different account than the lane expects.
- The default NotebookLM Chrome profile appears.
- CDP reaches a different browser root than the configured lane root.
- A worker profile is repaired by changing the code path instead of the profile root.

## Cleanup Expectations

After the run:

- Re-run `csf-nlm-worker-auth sync` and confirm the three current account families still map correctly.
- Record the run root and the exact auth cadence used.
- If the run was only a smoke or failed early, delete or archive the transient output root before the next trial so it does not get mistaken for fresh evidence.
- If the run produced a benchmark result worth keeping, link it from `docs/operations/test-registry.md` and leave the run root intact.

