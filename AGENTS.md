# AGENTS.md

## Overview

`yt-is` (YouTube Intelligence System) is a transcript ingestion and analysis pipeline that integrates with Claude Code through skills. It provides two main capabilities:

1. **Channel Management**: Discover new YouTube videos via RSS + API gap resolution
2. **Transcript Ingestion**: Download transcripts via yt-dlp, Selenium, or NotebookLM

## Architecture

```
User Input → Skill Invocation → CLI Script / Python → Transcript Sources → SQLite Cache
```

## Skills

### `/yt-channel` — YouTube Channel Management

Check all tracked YouTube channels for new videos and manage your channel list.

**Entry point**: `bin/yt-channel` (wraps `bin/csf-source`)

**Commands:**
- `sync` — Check all tracked channels for new videos (RSS + gap detection + API)
- `list` — List all tracked channels with metadata
- `add <url>` — Add a new channel or playlist to track
- `fetch` — Download pending transcripts via escalation chain (yt-dlp → Selenium)

**Escalation Chain:**
1. yt-dlp (WEB client) — fastest, works for most public videos
2. yt-dlp with cookies — for age-restricted videos
3. Selenium Firefox — fallback for bot-check failures

**Key files:**
- `bin/yt-channel` — CLI entry point
- `bin/csf-source` — Backend implementation
- `csf/source_enumerator.py` — RSS + API enumeration
- `csf/batch_status.py` — SQLite storage (`channel_metadata`, `analysis_status` tables)

**Dependencies:**
- `yt-dlp>=2024.0.0`
- Firefox (Selenium fallback)
- `YOUTUBE_API_KEY` (for gap resolution)

### `/yt-nlm` — NotebookLM Transcript Extraction

Extract YouTube transcripts using NotebookLM's batch notebook workflow.

**Entry point**: `csf/transcript.py` via `bin/csf-source fetch --method nlm`

**Why batch over ephemeral:**
- **Ephemeral (deprecated)**: 1 notebook per video — wastes NotebookLM slots, slow
- **Batch**: Up to 300 YouTube sources per notebook — reuses a single notebook

**Workflow:**
1. Create batch notebook: `nlm notebook create "batch_transcript_{id}"`
2. Add sources: `nlm source add <nb-id> --youtube <url1> --youtube <url2> ... --wait`
3. Get content: `nlm source content <source-id>` (returns raw JSON with `{"value": {"content": "..."}}`)
4. Delete notebook: `nlm notebook delete <nb-id> --confirm`

**Auth auto-recovery:**
- Before commands: `nlm login --check`
- If expired: `nlm login --force` (no user prompt)

**Key files:**
- `csf/transcript.py` — `_fetch_via_notebooklm_batch()` with auth recovery
- `csf/cache.py` — `set_cached_transcript()` for database caching

**Dependencies:**
- `nlm` CLI (NotebookLM command-line interface)
- NotebookLM Pro/Plus account (300 source limit per notebook)

## CLI Tools

### `yt-channel`

Channel management CLI. Delegates to `csf-source` backend.

```powershell
yt-channel sync             # Check all tracked channels
yt-channel list             # List all tracked channels
yt-channel add <url>        # Add a channel
yt-channel fetch            # Download pending transcripts
```

### `csf-source`

Backend implementation for all channel and transcript operations.

```powershell
csf-source list              # List tracked sources
csf-source add <url>         # Add a source
csf-source check <source>    # Check one source for new videos
csf-source check-all        # Check all sources
csf-source sync <source>    # Process pending videos for a source
csf-source fetch            # Download pending transcripts
```

## Data Flow

```
/yt-channel sync
    │
    ├─► RSS fetch (15 most recent per channel)
    ├─► Gap detection (new videos not in local DB)
    └─► API resolution (YouTube Data API with publishedAfter cursor)
            │
            ▼
    batch_status.sqlite: analysis_status (pending)
            │
            ├─► /yt-channel fetch ──► yt-dlp ──► transcripts.sqlite
            │                              └─► Selenium ──► transcripts.sqlite
            │
            └─► /yt-nlm ──► NotebookLM batch ──► transcripts.sqlite
                        │
                        ▼
            Combined markdown batches → CKS / Obsidian / analysis tools
```

## Storage

- **batch_status.sqlite** — Video tracking
  - `channel_metadata`: tracked channels, playlist IDs, last_checked
  - `analysis_status`: video_id, status (pending/complete/failed), last_stage, failure_reason
- **transcripts.sqlite** — Cached transcripts (video_id, lang, source, content)

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `YOUTUBE_API_KEY` | — | YouTube Data API v3 key (for gap resolution) |
| `YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK` | 300 | Max YouTube sources per NotebookLM notebook |

### External Transcript Provider

Register a custom transcript provider:

```python
from yt_is.csf.transcript import register_external_transcript_provider

def my_provider(video_id: str, prefer_lang: str | None):
    # Return (success: bool, transcript: str | None, error: str | None)
    return True, "transcript content", None

register_external_transcript_provider(my_provider)
```

Called after all built-in methods fail, before returning final failure.

## Troubleshooting

### "No new videos found" after sync

The RSS feed only returns 15 most recent videos. If your tracked videos are older than that, the sync reports no new videos — even if there are unprocessed pending videos from prior syncs.

### NotebookLM auth expired

The batch workflow has auth auto-recovery: `nlm login --check` runs before commands, and `nlm login --force` runs automatically if expired. No manual intervention required.

### Transcript fetch fails for all methods

Check:
1. Video has captions (YouTube Studio → Subtitles)
2. Video is not age-restricted or region-blocked
3. `YOUTUBE_API_KEY` is set for gap resolution
4. Firefox is installed (for Selenium fallback)