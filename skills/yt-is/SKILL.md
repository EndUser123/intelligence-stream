---
name: yt-is
description: YouTube channel management — check for new videos, manage tracked channels, and add new channels with validation
version: 1.0.0
enforcement: strict
triggers:
  - User asks to check for new YouTube videos
  - User asks to list tracked channels
  - User asks to add a YouTube channel
  - User asks about YouTube channel status or transcripts
workflow_steps:
  - Parse command and arguments
  - Delegate to csf-source backend
  - Paste raw output explicitly (Bash output gets compressed, user can't see it)
  - Display results
allowed_first_tools:
  - Bash
required_first_command_patterns:
  - '^csf-source\s+sync(?:\s|$)'
required_first_command_hint: Use `csf-source sync` first, then `list` or `fetch` as needed.
aliases:
  - yt-is
  - check youtube channels
  - new youtube videos
  - youtube channel management
depends_on_skills: []
---

# /yt-is — YouTube Channel Management

Check all tracked YouTube channels for new videos and manage your channel list.

## Commands

- `sync` — Check all tracked channels for new videos
- `sync --verbose` — Show detailed output during check
- `list` — List all tracked channels with metadata
- `add <url>` — **Add a new channel** with full validation (YouTube Data API resolves @handle, rejects fake/empty/single-video channels, captures title/thumbnail/subscriber/view counts)
- `fetch` — **ESCALATION BATCH PROCESS**: Download transcripts for all pending videos using yt-dlp → Selenium fallback (RECOMMENDED)
  - `fetch --dry-run` — Preview what would be fetched
  - `fetch --source <url>` — Process only one channel
  - `fetch --workers <n>` — Use N parallel workers (default: 1)

## /yt-is add — Channel Validation Workflow

When adding a channel, the system:

1. **Resolves @handle** via YouTube Data API (`channels.list` with `contentDetails,statistics,snippet`)
2. **Rejects fake/empty channels** — channels with ≤1 video are rejected (not stored)
3. **Captures full metadata** — channel_title, thumbnail_url, subscriber_count, view_count stored in `channel_metadata`
4. **Enumerates all videos** via API pagination and marks them as pending

The validation is strict — only channels with 2+ videos are accepted. This keeps the transcript corpus free of noise from abandoned or test channels.

## Your Workflow

1. Parse the user's command (sync/list/add/fetch)
2. Run the appropriate `csf-source` backend command
3. **MANDATORY — Copy and paste the output verbatim:**
   - After the Bash command completes, copy the ENTIRE output text
   - Paste it directly in your response (inside a code block)
   - DO NOT summarize or abbreviate the output
   - DO NOT say "output shown above" or "the Bash tool result"
   - DO NOT reference the output indirectly — paste it literally
4. Why: The Bash tool output is compressed in the UI; pasting the raw text ensures the user can see it

## Output Format

Paste the raw output from each command — the format is determined by the backend, not this skill. The backend output will include its own legend where applicable.

**IMPORTANT:** Bash output gets compressed in the UI. Always paste the raw output explicitly so the user can see it.

## Your Tracked Channels

Run `csf-source list` to see current channel data. The backend output will include a legend explaining the column format.

## How It Works

**`yt-is sync`** runs the daily check workflow on ALL tracked channels:

1. **RSS Check** - Fetches exactly 15 most recent videos per channel via RSS feed
2. **Gap Detection** - If RSS shows videos that don't exist in local database (no overlap), triggers gap resolution
3. **API Gap Resolution** - Uses YouTube Data API with `publishedAfter` cursor to fill gaps
4. **Mark Pending** - New videos are marked as pending for transcript download

Channels are checked in order of `last_checked` (oldest first) to ensure fair coverage.

## Escalation Batch Process (`csf-source fetch`)

The `fetch` command implements automatic escalation for transcript downloading:

**Escalation Chain (per video):**
1. **yt-dlp (WEB client)** - Fastest method (~5 seconds), works for most public videos
2. **yt-dlp with cookies** - For age-restricted videos
3. **Selenium Firefox** - Fallback for bot-check failures (~15-30 seconds)

**Features:**
- **Automatic retries**: Each video tries all methods until one succeeds
- **Resume support**: Interrupted runs skip already-cached videos
- **Parallel processing**: Use `--workers N` for concurrent downloads
- **Status tracking**: Automatically marks videos complete/failed in batch_status

**Recommended Workflow:**
```bash
# 1. Discover new videos
csf-source check-all

# 2. Download transcripts (all channels, automatic escalation)
csf-source fetch

# 3. Or dry-run first to see what will be fetched
csf-source fetch --dry-run

# 4. Or process only one channel
csf-source fetch --source "https://youtube.com/@channel"

# 5. Or use parallel workers for faster processing
csf-source fetch --workers 2
```

## Data Flow

```
channel_metadata table (SQLite)
  │
  ├─► yt-is sync ──► RSS check ──► Gap detection ──► API resolution
  │                                                │
  │                                                ▼
  │                                       batch_status table (pending)
  │
  └─► csf-source fetch ──► ESCALATION CHAIN (yt-dlp → Selenium) ──► transcripts.sqlite
```

## Storage

All data is stored in `batch_status.sqlite`:
- `channel_metadata` — tracked channels with playlist IDs and metadata
- `analysis_status` — video tracking (pending/complete/failed)

## Files

- `bin/yt-is` — CLI entry point
- `bin/csf-source` — Backend implementation
- `csf/source_enumerator.py` — RSS + API enumeration
- `csf/batch_status.py` — SQLite storage

## Requirements

- `YOUTUBE_API_KEY` — For gap resolution (API calls)
- Internet connection — For RSS feeds and YouTube Data API
