---
name: yt-nlm
description: YouTube transcript extraction via NotebookLM batch notebooks
version: "2.0.0"
status: stable
enforcement: strict
category: ingestion
triggers:
  - 'notebooklm'
  - 'nlm extract'
  - 'youtube transcripts'
  - 'transcript extraction'
aliases:
  - '/nlm'
  - '/yt-nlm'

suggest: []

workflow_steps:
  - Check for pending videos in batch_status.sqlite
  - Check auth with `nlm login --check`, auto-recover if expired
  - Create batch notebook (up to 300 YouTube sources per notebook)
  - Add sources in sub-batches of 50 with `nlm source add --url ... --wait`
  - Heartbeat-poll `source list --json` between sub-batches to confirm NLM async processing completes
  - Extract content via `nlm source content <source-id>` (returns raw JSON)
  - Delete batch notebook (cleanup)
  - Write transcripts to database cache (transcripts.sqlite)
  - Combine transcripts into batches for external use
allowed_first_tools:
  - Bash
required_first_command_patterns:
  - '^nlm\s+login\s+--check(?:\s|$)'
required_first_command_hint: Run `nlm login --check` first so auth is validated before notebook work starts.

parameters:
  - name: dry-run
    description: Preview what will be ingested without processing
    type: boolean
    required: false
  - name: channel
    description: Process only one channel (by URL)
    type: string
    required: false
  - name: batch-size
    description: Transcripts per combined source
    type: integer
    default: 20

---

# /yt-nlm — NotebookLM Transcript Extraction

Extract YouTube transcripts using NotebookLM's batch notebook workflow.

## Purpose

Uses batch notebooks (up to 300 YouTube sources per notebook) via `_fetch_via_notebooklm_batch()` in `transcript.py`:
- Reuses a single notebook for up to 300 videos (vs. 1 notebook per video)
- Uses `nlm source content` (raw text) instead of `nlm notebook query` (LLM)
- Has auth auto-recovery built in

The old ephemeral notebook pattern (1 notebook per video) is deprecated.

## Commands

```bash
# Ingest pending videos (default behavior)
yt-nlm

# Dry run: preview what will be ingested
yt-nlm --dry-run

# Specific channel only
yt-nlm --channel "https://youtube.com/@channel"

# Batch size for combined sources
yt-nlm --batch-size 20
```

## How It Works

**Batch Notebook Workflow (up to 300 sources per notebook):**

1. **Auth check**: `nlm login --check` before commands
2. **Auto-recovery**: If expired, `nlm login --force` runs automatically (no user prompt)
3. **Create batch notebook**: `nlm notebook create "batch_transcript_{id}"`
4. **Add sources**: `nlm source add <nb-id> --url <url1> --url <url2> ... --wait` in sub-batches of 50; heartbeat-poll `source list --json` between sub-batches to wait for NLM async processing
5. **Get content**: `nlm source content <source-id>` — returns JSON with `{"value": {"content": "..."}}`
6. **Delete notebook**: `nlm notebook delete <nb-id> --confirm`

**vs. Deprecated Ephemeral Workflow (1 notebook per video):**
- Slow: Creates/deletes notebook for each video
- Wasteful: Burns NotebookLM slot for each video
- No auth recovery: Fails when session expires

**Batch Combination:**
- Combines N transcripts into single markdown source
- Injects structural headers (video ID, URL, separators)
- Output ready for your knowledge system (CKS, Obsidian, analysis tools)

## Integration Points

- Reads from `batch_status.sqlite` (pending videos marked by `/yt-is`)
- Writes to `transcripts.sqlite` cache via `csf.cache.set_cached_transcript()`
- Stores combined markdown files in `P:/.data/yt-is/transcripts/`
- Compatible with `/yt-is fetch` (both write to same cache database, different sources)
- External provider hook: `register_external_transcript_provider()` for custom sources

## Data Flow

```
/yt-is sync
    ↓
batch_status.sqlite (pending videos)
    ↓
/yt-nlm (batch workflow)
    ↓
Batch notebook → nlm source content → transcripts.sqlite
    ↓
Combined markdown files → Your knowledge system
```

## Storage

- **Transcripts:** `transcripts.sqlite` (cache database, keyed by video_id)
- **Combined batches:** `combined_batch_1.md`, etc. in `P:/.data/yt-is/`
- **Database:** `batch_status.sqlite` (status updates: pending → complete/failed, last_stage, failure_reason, and canonical `channel_id` identity for tracked channel state)

## Configuration

| Environment Variable | Default | Description |
|--------------------|---------|-------------|
| `YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK` | 300 | Max YouTube sources per NotebookLM notebook |

## Failure Taxonomy

When transcript fetch fails, `last_stage` and `failure_reason` are recorded:

| failure_reason | Meaning |
|----------------|---------|
| `quota_exceeded` | NotebookLM API quota exceeded |
| `region_block` | Video not available in NotebookLM region |
| `auth_failed` | NotebookLM auth expired and recovery failed |
| `captcha` | Captcha challenge during extraction |
| `timeout` | Source content extraction timed out |
| `no_transcript` | Video has no captions/transcript |
| `unavailable` | Video unavailable or deleted |
| `unknown` | Unclassified error |

## Requirements

- `nlm` CLI (NotebookLM command-line interface)
- NotebookLM Pro/Plus account (300 source limit per notebook)
- Internet connection for NotebookLM API

## Related Skills

- `/nlm` — NotebookLM CLI operations
- `/yt-is` — Video discovery and tracking
- `/yt-is fetch` — yt-dlp → Selenium transcript download (escalation chain)
- `/yt-dlp` — Local transcript download via yt-dlp

## ADR Reference

See `P:/__csf/arch_decisions/ADR-20260410-notebooklm-ephemeral-notebooks.md` for architecture decision and performance characteristics.

**Note:** The ADR describes the ephemeral pattern. The batch workflow is the current implementation.
