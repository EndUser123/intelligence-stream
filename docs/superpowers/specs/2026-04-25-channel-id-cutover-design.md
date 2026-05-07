# Channel ID Cutover Design

## Problem

The current `yt-is` channel-state model keys live storage by `channel_url`.
That has already allowed malformed handle forms to appear in the database,
including `https://www.youtube.com@ryanrumsey`.

We need a more robust identity model that survives handle changes, blocks
duplicates, and prevents future malformed URL drift.

## Goal

Cut over the channel-state model so that `channel_id` becomes the canonical
storage identity, while `channel_url` remains a human-readable alias/display
field.

## Non-goals

- Changing the meaning of the existing block policy.
- Reworking NotebookLM or transcript behavior.
- Tracking arbitrary YouTube URLs as a primary source of truth.
- Adding a second identity system beyond `channel_id` and alias URLs.

## Why `channel_id`

`channel_id` is the stable identifier for a YouTube channel.

Compared with URL-keyed storage, it:

- avoids alias drift between `/channel/UC...`, `@handle`, and custom URLs
- survives handle changes
- removes the malformed `youtube.com@handle` failure mode
- makes imports, blocks, and replays referentially stable

## New Identity Contract

### Canonical identity

- Storage key: `channel_id`
- Required for all persistent channel-state rows
- Must be the YouTube channel's resolved ID whenever available

### Display / alias data

- `channel_url` remains stored for display, diagnostics, and user-facing output
- Multiple aliases may resolve to the same `channel_id`
- Any URL passed in by a writer is resolved before storage

## Current State

The current codebase already has a partial identity model:

- `parse_channel_url()` can extract `UC...`, `@handle`, `c/...`, and `user/...`
- `resolve_to_uc_channel_id()` can resolve handles/custom URLs to `UC...`
- `batch_status._normalize_channel_url()` fixes some malformed handle forms

But the actual live tables are still keyed by URL:

- `channel_metadata`
- `channel_blocklist`
- `provider_score`
- channel source references in `analysis_status`

That means the current model still allows alias drift unless every writer and
reader normalizes perfectly.

## Proposed Architecture

### Storage layer

Migrate live channel-state tables so that they are keyed by `channel_id`.

Suggested shape:

- `channel_metadata(channel_id PRIMARY KEY, channel_url, ... )`
- `channel_blocklist(channel_id PRIMARY KEY, channel_url, blocked_at, ... )`
- `provider_score(channel_id, provider, ... )`
- `analysis_status(..., channel_id, source_url, ... )`

The exact column set can remain close to the current schema, but the stable
key should be `channel_id`.

### Writer layer

All channel-facing writers should:

1. accept a URL or identifier for convenience
2. resolve to a `channel_id`
3. store by `channel_id`
4. preserve the current `channel_url` as the alias/display field

### Reader layer

All lookups should use `channel_id` internally.

Any user-facing printout should render:

- canonical URL if known
- otherwise the best resolved alias URL

## Cutover Plan

This is a one-time migration, not a long coexistence period.

### Step 1: Snapshot the live DB

Before any write migration:

- back up `P:\\.data/yt-is/batch_status.sqlite`
- keep the backup in `P:\\.data/yt-is/backups/`

### Step 2: Create v2 schema

Introduce `channel_id` columns and/or new v2 tables with `channel_id` as the
primary key.

Prefer a migration path that is atomic and idempotent:

- create new tables
- copy rows from the old tables after resolving IDs
- validate row counts
- swap the live tables in one transaction where feasible

### Step 3: Resolve existing rows

For each existing tracked or blocked channel:

- if the stored value is already a UC ID, keep it
- if it is a handle/custom/user URL, resolve it to `channel_id`
- preserve the original URL as the alias/display value
- refuse to invent an ID for an unresolvable channel

### Step 4: Update writers

Update the code paths that currently write channel state:

- manual add / block / unblock / remove commands
- history import
- Watch Later import
- playlist import replay
- sync / enumeration paths
- any helper that stores channel metadata or block state

### Step 5: Update readers and queries

Update all lookups and filters to use `channel_id`.
This includes:

- channel metadata fetches
- block checks
- provider score lookups
- pending-by-source / newest-published queries
- UI/output formatting

### Step 6: Clean up malformed rows

Use the migration to rewrite the existing malformed Ryan-style rows and any
other URL drift that is already present.

## Error Handling

- If a channel cannot be resolved to `channel_id`, fail closed and surface it
  for manual review.
- If a migration step cannot prove the row count or mapping, abort and leave
  the backup intact.
- If a writer is given an invalid or malformed URL, normalize it first and
  then resolve it; never persist the malformed form.

## Testing

The migration needs coverage for:

- `@handle` URLs resolving to stable `channel_id`
- `/channel/UC...` URLs resolving to the same `channel_id`
- malformed `youtube.com@handle` inputs normalizing before storage
- existing live rows migrating without losing block/allow state
- blocklist checks still working after cutover
- playlist import replay still restoring state

Suggested regression checks:

1. Write a channel by `@handle`, read it back by URL, and assert the same
   `channel_id`.
2. Write the same channel by `/channel/UC...` and confirm no duplicate row is
   created.
3. Block a channel, migrate, and confirm it remains blocked under the same
   `channel_id`.
4. Confirm the malformed `youtube.com@...` form never persists.

## Success Criteria

The cutover is successful if:

- all live channel-state writes use `channel_id`
- no malformed handle-form URL can be persisted
- existing tracked and blocked channels survive the migration
- user-facing output still shows understandable URLs
- future imports cannot create duplicate rows for the same channel

## Decision Summary

This design chooses the more invasive but more durable path:

- `channel_id` becomes the canonical storage identity
- `channel_url` becomes alias/display data
- the migration is one-time and cut over immediately

That is the right tradeoff for a robust long-term platform.
