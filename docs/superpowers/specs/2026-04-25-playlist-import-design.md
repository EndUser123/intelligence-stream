# Playlist Import and Channel-State Preservation Design

## Problem

The current `yt-is` code can already import channels from YouTube watch history via `csf-source history`, but it does not yet expose a matching path for YouTube Watch Later, and it does not persist a durable append-only record of playlist imports.

We need a design that:

- extracts channels from both watch history and Watch Later
- captures as much useful metadata as possible at import time
- preserves a complete append-only audit trail
- keeps the live channel allow/block lists protected with the same backup/staging workflow as transcripts
- allows the live channel inventory to be rebuilt from durable import records

## Goals

1. Add a `watchlater` import path alongside the existing watch-history import path.
2. Use `yt-dlp` with authenticated cookies to read both watch history and Watch Later playlists.
3. Persist append-only import records with both run-level and item-level detail.
4. Keep the live channel allowlist and blocklist in `P:/.data/yt-is/batch_status.sqlite`.
5. Protect the live channel-state DB with backup and staging/promotion commands, similar to transcripts.
6. Make it possible to rebuild the live channel inventory from durable import records.

## Non-goals

- Replacing the existing YouTube API based channel validation flow.
- Tracking arbitrary YouTube URLs as the primary source of truth.
- Storing playlist state as JSON blobs only.
- Moving the live channel-state tables back into `P:/packages/yt-is`.
- Changing the transcript fetch pipeline or NotebookLM throughput policy.

## Current State

### Existing behavior

- `csf-source history` already imports channels from `https://www.youtube.com/feed/history`.
- The history import uses `yt-dlp` plus YouTube Data API calls to resolve channel IDs and filter channels.
- The live channel allow/block state is stored in:
  - `P:/.data/yt-is/batch_status.sqlite`
  - `channel_metadata`
  - `channel_blocklist`

### Gaps

- There is no equivalent `watchlater` import command yet.
- There is no append-only import log for playlist imports.
- There is no durable run/item audit trail for history or Watch Later imports.

## Proposed Architecture

Use a **hybrid append-only log** plus **derived live tables**.

### Append-only import log

Store every playlist import as:

- one run row per import invocation
- one item row per imported playlist item

This log is the durable audit trail.

### Derived live state

Maintain live tables separately in `P:/.data/yt-is/batch_status.sqlite`:

- `channel_metadata`
- `channel_blocklist`

These are the operational current-state tables used by `/yt-is sync` and related workflows.

### Backups and staging

Protect the live channel-state DB with the same workflow used for transcripts:

- backup before risky changes
- stage updates in a separate SQLite DB
- promote from staging into live with a blocking, fail-closed command

## Data Model

### Import log DB

Use a separate SQLite DB under the live data root, for example:

- `P:/.data/yt-is/playlists.sqlite`

Suggested tables:

#### `playlist_import_run`

One row per import invocation.

Columns:

- `run_id` (primary key)
- `playlist_kind` (`history` | `watch_later`)
- `playlist_url`
- `started_at`
- `finished_at`
- `status` (`success` | `partial` | `failed`)
- `command`
- `cookie_source`
- `total_items`
- `resolved_items`
- `new_channels`
- `already_tracked_channels`
- `blocked_channels`
- `failed_items`
- `notes_json`

#### `playlist_import_item`

One row per item captured in a run.

Columns:

- `run_id`
- `item_id` or `playlist_item_id`
- `playlist_kind`
- `playlist_url`
- `playlist_position`
- `video_id`
- `video_url`
- `video_title`
- `channel_id`
- `channel_url`
- `channel_title`
- `published_at`
- `duration_seconds`
- `availability`
- `is_live`
- `raw_json`
- `resolved_channel_json`
- `classification`
- `created_at`

Primary key can be `(run_id, item_id)` or `(run_id, video_id, playlist_position)` depending on the stability of the raw playlist item identifier.

### Live channel-state DB

Keep the live operational tables in:

- `P:/.data/yt-is/batch_status.sqlite`

Use the existing tables:

- `channel_metadata`
- `channel_blocklist`

## Command Design

### Existing command

- `csf-source history`

Keep this command as the watch-history import path.

### New command

- `csf-source watchlater`

This command should:

1. read `https://www.youtube.com/playlist?list=WL` using authenticated `yt-dlp`
2. resolve the items to channel IDs and metadata
3. write a full append-only run/item record into the playlist import log
4. update the derived live channel-state tables as appropriate

### Shared options

Both import commands should support:

- `--dry-run`
- `--min-history-videos <n>` for history
- a cookie/auth source derived from the existing YouTube auth helpers

## Workflow

### History import

1. Use `yt-dlp` against watch history.
2. Resolve video IDs to channel IDs via the YouTube Data API.
3. Filter candidate channels using the existing thresholds.
4. Write a run row and item rows to the import log.
5. Upsert accepted channels into `channel_metadata`.
6. Leave blocked channels in `channel_blocklist`.

### Watch Later import

1. Use authenticated `yt-dlp` against the Watch Later playlist.
2. Resolve the playlist items to video IDs and channel IDs.
3. Write the full run/item record to the import log.
4. If a channel should be tracked, upsert it into `channel_metadata`.
5. If a channel should be blocked, leave it in `channel_blocklist`.

### Rebuild path

If the live channel-state DB needs to be restored:

1. Restore from backup if available.
2. Re-run the staged import log promotion.
3. Rehydrate `channel_metadata` and `channel_blocklist` from the append-only import records.

## Protection and Recovery

### Channel-state backup

Before any risky sync or blocklist change:

- run `python P:/packages/yt-is/bin/csf-backup-channel-state`

This snapshots:

- `P:/.data/yt-is/batch_status.sqlite`

into:

- `P:/.data/yt-is/backups/`

### Staging and promotion

For staged channel-state work:

1. point `YTIS_BATCH_STATUS_DB_PATH` at a staging SQLite DB
2. run `yt-is sync` or the import command against that staging DB
3. promote with `python P:/packages/yt-is/bin/csf-promote-channel-state`

The promote command must be blocking and fail-closed:

- refuse missing source DBs
- refuse empty staging DBs
- refuse source/destination collisions

## Error Handling

- If `yt-dlp` cannot read Watch Later because the session is not authenticated, fail clearly and do not write partial live state.
- If the import log write succeeds but live-state promotion fails, preserve the import log and allow retry.
- If a channel is already tracked, record it in the import log but do not duplicate it in `channel_metadata`.
- If a channel is blocked, preserve that state in `channel_blocklist` and record it in the import log.
- If a run only partially resolves, mark the run `partial` and keep the raw item rows.

## Testing

Add tests for:

1. `history` import still resolves watch history channels.
2. `watchlater` import reads `WL` with cookies and captures item rows.
3. Import log tables are append-only and preserve historical runs.
4. Live channel-state backup creates a restorable SQLite snapshot.
5. Staging promotion merges channel metadata and blocklist rows into live state.
6. Blocking behavior rejects missing source DBs, empty staging DBs, and same-path promotion.
7. Existing tests do not wipe the live `.data` DBs.

## Success Criteria

The design is successful if:

- watch history and Watch Later can both be imported
- the raw import history is append-only and auditable
- the current allow/block lists remain protected under `P:/.data/yt-is`
- live state can be backed up and promoted safely
- the workflow supports rebuilds without depending on mutable package-local state

## Decision

Use the hybrid model:

- append-only import log for history and Watch Later
- derived live state in `batch_status.sqlite`
- blocking backup/promote for channel-state changes

That gives us durable auditability without losing operational simplicity.
