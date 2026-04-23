# Worker-Owned NotebookLM Notebooks

## Problem

`yt-is` has accumulated duplicate NotebookLM notebooks because different paths have treated the reusable notebook as a separate concept from the worker identity. The result is:

- duplicate worker-owned notebooks with the same title
- stale notebook state files that can resurrect old notebook IDs
- cleanup logic that depends on delete succeeding every time

The user wants a simpler model:

- one worker, one notebook
- the same rule applies to all NotebookLM paths
- the notebook is reused across batches in the steady state

## Goals

1. Every NotebookLM path resolves notebooks by deterministic worker title.
2. If exactly one notebook exists for that title, reuse it.
3. If multiple notebooks exist for that title, delete the duplicates and resolve back to one worker-owned notebook.
4. If no notebook exists, create one.
5. The steady state should be exactly one notebook per worker title, reused across batches.
6. The code should not depend on the old reusable notebook title to preserve behavior.

## Non-goals

- Changing the transcript extraction strategy.
- Changing batch-size tuning or throughput measurements.
- Introducing a shared multi-worker notebook again.
- Preserving legacy duplicate notebooks as a supported state.

## Proposed Design

### 1. Single ownership rule

Notebook ownership is based on the notebook title, not the notebook ID. The title is deterministic and derived from the worker identity.

Examples:

- `yt-is-worker-01`
- `yt-is-worker-02`
- `yt-is-worker-03`
- `yt-is-worker-04`

For the serial path, use the same model with the worker-01 title unless the caller explicitly selects a different worker title. The important rule is that each active worker has exactly one owned notebook title.

### 2. Exact-title resolution

When a path needs a notebook, it must:

1. List NotebookLM notebooks.
2. Find exact title matches for the target worker title.
3. If exactly one match exists, reuse that notebook ID.
4. If no match exists, create a notebook with that title and save the new ID.
5. If multiple matches exist, treat that as a dirty state:
   - delete all exact-title matches through the CDP cleanup path
   - if one notebook remains, reuse it
   - if none remain, create one with that title and save the new ID

This keeps the implementation deterministic and avoids “pick one and hope it is the right one.”

### 3. State files

Worker state files remain the local source of truth for the last notebook ID a worker used, but they are advisory only.

The startup flow must not trust a state file unless:

- it belongs to the current run when run-scoping is enabled
- and the exact-title notebook lookup confirms the notebook is still the worker’s notebook

If the state file points at a notebook that no longer exists or no longer matches the exact-title lookup, the path must recover by resolving the title again.

### 4. Cleanup behavior

Cleanup is not a separate “best effort someday” path. It is part of notebook resolution.

If duplicate exact-title notebooks are found:

- delete the duplicates
- resolve back to a single worker-owned notebook
- continue from that notebook

The reusable notebook title should no longer be used as a steady-state owner notebook once this design is implemented.

## Data Flow

1. Worker starts.
2. Worker computes its notebook title.
3. Worker lists notebooks and resolves an exact-title match.
4. Worker either reuses the notebook, creates one, or deletes duplicates and recreates.
5. Worker writes the resolved notebook ID back to local state.
6. Worker adds sources to that notebook for the life of the worker run.
7. On shutdown, the worker clears its state so stale IDs do not persist.

## Error Handling

- If notebook listing fails, the worker should log the failure and stop instead of creating an unbounded duplicate path.
- If title cleanup fails, the worker should surface the failure clearly rather than silently creating another same-name notebook.
- If the notebook delete path fails in CDP, the behavior should remain deterministic: either retry within bounded limits or fail the startup path. Do not silently fall back to a new duplicate with the same title.

## Testing

Add or keep regression coverage for:

- exact-title match reuse
- duplicate-title cleanup via CDP
- clean recreation after duplicate cleanup
- worker state invalidation across runs
- local cleanup of stale notebook IDs on shutdown

Recommended verification:

- unit tests for notebook resolution and cleanup branching
- a short live fetch / worker run that starts from a clean slate and confirms each worker title resolves to one notebook only

## Success Criteria

The design is working when:

- each worker title maps to one NotebookLM notebook
- each worker notebook is reused across batches in steady state
- duplicate same-title notebooks are removed instead of accumulating
- a fresh worker run starts cleanly without manual notebook cleanup

## Open Question Resolved by This Design

The old reusable notebook title is no longer the owner model. The owner model is the worker title itself.
