---
name: yt-dlp
description: YouTube transcript extraction via yt-dlp Python API with Chrome TLS impersonation
version: 1.0.0
status: stable
enforcement: strict
category: ingestion
triggers:
  - 'yt-dlp'
  - 'transcript download'
  - 'local transcript'
aliases:
  - '/yt-dlp'
  - '/ytdlp'

workflow_steps:
  - Parse video ID from URL or database
  - Call fetch_transcript_chain() with yt-dlp as primary method
  - On bot-check failure, recursive fallback to ytdlp_with_cookies
  - Cache successful transcript to transcripts.sqlite

parameters:
  - name: dry-run
    description: Show what would be downloaded without downloading
    type: boolean
    required: false
  - name: channel
    description: Process only one channel (by URL)
    type: string
    required: false
  - name: workers
    description: Number of parallel workers (default: 1)
    type: integer
    required: false
---

# /yt-dlp — Local Transcript Download via yt-dlp

Fast transcript extraction using yt-dlp's Python API with Chrome TLS impersonation.

## Purpose

Uses `csf/transcript.py::fetch_transcript_chain()` with `_fetch_via_ytdlp()` as the primary method.

## Commands

```bash
# Download transcripts (recommended: use yt-is fetch instead)
yt-dlp --run

# Dry run: show missing counts
yt-dlp

# Process specific channel only
yt-dlp --channel "https://youtube.com/@channel"

# Parallel workers
yt-dlp --workers 2
```

## Escalation Chain

When yt-dlp fails, the chain escalates automatically:

| Step | Method | Source | Sleep Interval |
|------|--------|--------|-----------------|
| 1 | yt-dlp (WEB client, curl_cffi TLS) | ytdlp | 15-60s |
| 2 | yt-dlp with cookies (age-restricted) | ytdlp_ejs | 20-90s |
| 3 | Selenium Firefox | selenium | varies |
| 4 | NotebookLM | notebooklm | varies |
| 5 | faster-whisper (audio) | whisper | N/A |
| 6 | Direct API | direct_api | varies |

## How It Works

**Primary method: `_fetch_via_ytdlp()` (transcript.py:646)**
- Uses `yt_dlp.YoutubeDL` with `client_name=WEB` for public videos
- Chrome TLS impersonation via `curl_cffi` for subtitle URL fetch (bypasses bot detection)
- Sleep interval: 15-60 seconds between requests (humanized rate limiting)
- On "sign in to confirm you're not a bot" error: recursively calls `_fetch_via_ytdlp_with_cookies()`

**Second attempt: `_fetch_via_ytdlp_with_cookies()` (transcript.py:784)**
- Called automatically when primary method hits bot-check
- Gets Firefox cookies via `_get_cookie_file()` with reference counting
- Uses `external_downloader: "ejs:github"` to resolve YouTube's JS challenge for age-restricted videos
- Must release cookie file on exit (reference counting via `_release_cookie_file()`)
- Sleep interval: 20-90 seconds (more conservative with authenticated requests)

**Full chain: `fetch_transcript_chain()` (transcript.py:1560)**
1. `_fetch_via_ytdlp()` — WEB client, public videos
2. `_fetch_via_ytdlp_with_cookies()` — cookies + EJS, age-restricted
3. `_fetch_via_selenium_firefox()` — full browser, bot-blocked
4. `_fetch_via_notebooklm()` — NotebookLM batch
5. `_fetch_via_whisper()` — audio download + transcription
6. `_fetch_via_direct_api()` — final fallback

## Error Handling

| Error | Reason | Next Step |
|-------|--------|-----------|
| "no subtitles available" | Video has no captions | Try next language or method |
| "rate limited (429)" | Quota exceeded | Circuit breaker, skip source |
| "sign in to confirm you're not a bot" | Bot detection | Recursive fallback to cookies |
| "no firefox cookie file" | Firefox not running | Skip to Selenium |

## Data Flow

```
fetch_transcript_chain()
  │
  ├─► _fetch_via_ytdlp() ──► yt_dlp.YoutubeDL (WEB) ──► curl_cffi (chrome) ──► transcripts.sqlite
  │                           (15-60s sleep, curl_cffi TLS)
  │
  └─ On bot-check ─► _fetch_via_ytdlp_with_cookies() ──► yt_dlp + Firefox cookies + EJS
                        (20-90s sleep, cookie reference counting)
                                                    │
                                                    └─► On failure ─► Selenium ─► NLM ─► Whisper
```

## Integration Points

- **csf/transcript.py:646** — `_fetch_via_ytdlp()` implementation
- **csf/transcript.py:784** — `_fetch_via_ytdlp_with_cookies()` implementation
- **csf/transcript.py:1560** — `fetch_transcript_chain()` orchestration
- **csf/youtube_auth.py** — Firefox cookie extraction via `_get_firefox_cookie_file()`
- **csf/cache.py** — Transcript caching via `set_cached_transcript()`
- **bin/yt-dlp** — CLI entry point (stub that calls csf-transcript-fetch)

## Storage

- **Transcripts:** `transcripts.sqlite` (cached, keyed by video_id + lang)
- **Batch status:** `batch_status.sqlite` (last_stage: 'ytdlp', 'ytdlp_ejs', 'selenium', 'notebooklm')

## Requirements

- `yt-dlp>=2024.0.0`
- `curl_cffi` (for TLS impersonation)
- Firefox browser (for cookie-based age-restricted access)
- Internet connection

## Related Skills

- `/yt-is` — Channel management + full fetch workflow
- `/yt-nlm` — NotebookLM batch transcript extraction
- `/yt-selenium` — Selenium-based fallback extraction

## Recommended Workflow

```bash
# 1. Discover new videos (RSS + API gap fill)
/yt-is sync

# 2. Download transcripts (yt-dlp → Selenium → NLM escalation)
# Recommended: use yt-is fetch instead of yt-dlp directly
/yt-is fetch

# 3. Or use yt-dlp directly (bypasses full escalation chain)
/yt-dlp --run
```