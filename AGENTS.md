# AGENTS.md

## Overview

`intelligence-stream` is a YouTube playlist analysis pipeline that integrates with Claude Code through skills. It provides two main capabilities:

1. **Playlist Ingestion**: Download and catalog YouTube playlists using `yt-dlp`
2. **Video Analysis**: Analyze video content using Gemini AI with CKS storage

## Architecture

```
User Input → Skill Invocation → CLI Script → yt-dlp/Gemini → CKS Storage
```

## Skills

### `/csf-analyze`

Analyzes video content using Gemini and stores results in CKS.

**Entry point**: `bin/csf-analyze`

**Analysis chain**:
1. SDK video passthrough (if `GOOGLE_API_KEY` set) — `google.genai.Client` + `Part.from_uri()`
2. Transcript fallback (via `youtube-transcript-api`) + Gemini CLI
3. Legacy CLI fallback — URL as text prompt

**Key files**:
- `bin/csf-analyze` — Main CLI entry point
- `csf/cks_store.py` — CKS integration (`append_to_cks()`)
- `csf/logging.py` — Action logging (`log_action()`)

**Dependencies**:
- `google-genai>=0.8.0` (SDK mode)
- `youtube-transcript-api>=0.6.0` (transcript fallback)
- `gemini` CLI (CLI fallback)

### `/csf-ingest`

Ingests YouTube playlist videos and stores metadata in CKS.

**Entry point**: `bin/csf-ingest`

**Features**:
- Cookie-based auth via browser (`--cookies-from-browser`)
- Idempotent ingestion (skips already-ingested videos)
- Optional auto-analysis (`--analyze` flag)

**Key files**:
- `bin/csf-ingest` — Main CLI entry point
- `csf/cks_store.py` — CKS integration

## Development

### Skill Registration

Skills are registered via junctions:

```powershell
New-Item -ItemType Junction -Path "P:\.claude\skills\intelligence-stream-analyze" -Target "P:\packages\intelligence-stream\skills\analyze"
New-Item -ItemType Junction -Path "P:\.claude\skills\intelligence-stream-ingest" -Target "P:\packages\intelligence-stream\skills\ingest"
```

### Local Development

1. Edit source files in `P:/packages/intelligence-stream/`
2. Changes are immediately available (no reinstall)
3. Test with `/cs-analyze <video_url>` or `/cs-ingest <playlist_url>`

### Configuration

Edit `config/intelligence_stream.yaml` to customize:
- Download format
- Logging level
- Timeout values

## Troubleshooting

### "gemini CLI not found"

Install Gemini CLI or set `GOOGLE_API_KEY` to use SDK mode instead.

### Transcript fetch fails

Some videos don't have transcripts. The system falls back to CLI analysis in this case.

### Playlist ingestion skips videos

Videos may be skipped if they're already in `.ingested_ids`. Delete the tracking file to re-ingest.
