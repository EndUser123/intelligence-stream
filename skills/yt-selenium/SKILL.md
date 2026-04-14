---
name: yt-selenium
description: YouTube transcript extraction via Selenium Firefox browser automation (bypasses bot detection)
version: 1.0.0
category: tools
triggers:
  - "selenium transcript"
  - "browser automation"
  - "firefox transcript"
  - "bot detection bypass"
  - "yt-selenium"
aliases:
  - "/yt-selenium"
depends_on_skills: []
workflow_steps:
  - dry_run_check: Run without --run to see pending videos that would be processed
  - browser_setup: Verify Firefox profile with YouTube cookies exists
  - channel_selection: Optionally specify --channel URL for specific channel
  - execute_extraction: Run with --run to extract transcripts via Selenium Firefox
  - fallback_handling: Selenium is slower (15-30s/video) but bypasses TLS bot detection
enforcement: advisory
---

# /yt-selenium — YouTube Transcript Extraction via Selenium

Extract YouTube transcripts using Selenium Firefox browser automation. Bypasses YouTube's TLS fingerprinting bot detection by running a real Firefox browser with your authenticated session.

## Purpose

Selenium is a **fallback method** when faster approaches fail due to bot detection:

| Method | Speed | Reliability | When to Use |
|--------|-------|-------------|-------------|
| yt-dlp (WEB client) | Fast (~5s) | High | Public videos, no bot detection |
| yt-dlp + cookies | Medium (~10s) | Medium | Age-restricted videos |
| **Selenium Firefox** | **Slow (~15-30s)** | **High** | **Bot-check failures, TLS blocking** |
| Whisper | Very slow (~60s) | Very High | No captions available |

## Usage

```bash
# Dry run: show pending videos
python -m csf.csf_selenium

# Extract transcripts (all channels)
python -m csf.csf_selenium --run

# Specific channel only
python -m csf.csf_selenium --run --channel "https://youtube.com/@channel"

# Language preference
python -m csf.csf_selenium --run --lang es

# Parallel workers (default: 1)
python -m csf.csf_selenium --run --workers 2
```

## How It Works

### Browser Automation Flow

For each video:

1. **Launch Firefox** with your existing profile (cookies, login session)
2. **Navigate** to YouTube video page
3. **Scroll** to expose transcript button
4. **Click** transcript button via JavaScript (avoiding hover detection)
5. **Extract** transcript text from rendered page DOM
6. **Cache** transcript to `transcripts.sqlite` via `csf.cache`
7. **Close** browser (cleanup)

### Why This Works

- **Real browser TLS**: Actual Firefox TLS handshake, not impersonation
- **Authenticated session**: Uses your Firefox cookies (YouTube login)
- **Human-like interaction**: JavaScript clicks, scrolls, timing delays
- **Bypasses bot checks**: YouTube sees real browser, not API client

## Firefox Profile Setup

### Profile Discovery

The skill searches for Firefox profiles in this order:

1. `*.Profile 1*` (dedicated download profile, preferred)
2. First non-default profile (fallback)
3. Any profile except `.default`/`.default-release`

### Setup Instructions

**Option 1: Use existing profile**

```bash
# Find your Firefox profile
ls "$APPDATA/Mozilla/Firefox/Profiles/"

# Use profile path in skill invocation
python -m csf.csf_selenium --run --profile "ProfileForDownloading"
```

**Option 2: Create dedicated profile**

1. Open Firefox → `about:profiles`
2. Create new profile: "ProfileForDownloading"
3. Log in to YouTube in this profile
4. Close Firefox
5. Run skill (auto-discovers profile)

## Rate Limiting

Selenium has built-in rate limit protection:

| Metric | Value |
|--------|-------|
| Jitter range | 2-10 seconds between requests |
| Circuit breaker | Opens after 3 consecutive 429s |
| Cooldown duration | 5 minutes (300 seconds) |
| Backoff multiplier | 2x per consecutive failure (max 32x) |

## Integration Points

- Reads from `batch_status.sqlite` (pending videos marked by `/yt-channel`)
- Stores transcripts in `transcripts.sqlite` via `csf.cache`
- Cross-terminal cooldown sharing via `BatchScheduler`
- Compatible with `/yt-dlp` (can run both, compare results)

## Files

| File | Purpose |
|------|---------|
| `csf/csf_selenium.py` | CLI entry point and main loop |
| `csf/transcript.py` | `_fetch_via_selenium_firefox()` implementation |
| `csf/cache.py` | Transcript caching (`set_cached_transcript()`) |
| `csf/batch_scheduler.py` | Round-robin scheduling and rate limit tracking |

## Data Flow

```
/yt-channel sync
    ↓
batch_status.sqlite (pending videos)
    ↓
python -m csf.csf_selenium --run
    ↓
For each video:
    1. Launch Firefox with profile
    2. Navigate to YouTube page
    3. Click transcript button
    4. Extract transcript text
    5. Cache to transcripts.sqlite
    ↓
Complete: all videos processed
```

## Requirements

- **Firefox** browser (installed)
- **selenium** Python package (`pip install selenium`)
- **geckodriver** (Firefox WebDriver, on PATH)
- **Firefox profile** with YouTube login (for age-restricted videos)

### Installing geckodriver

```bash
# Windows (using winget)
winget install Mozilla.GeckoDriver

# Or download manually
# https://github.com/mozilla/geckodriver/releases
```

## Error Handling

| Error | Cause | Resolution |
|-------|-------|------------|
| `selenium not installed` | Missing Python package | `pip install selenium` |
| `geckodriver not found` | WebDriver not on PATH | Install geckodriver |
| `transcript button not found` | Video has no captions | Skip, no transcript available |
| `transcript panel was empty` | Transcript loading failed | Retry or use different method |
| `rate limited (429)` | Too many requests | Circuit breaker opens, waits 5 minutes |

## Performance Characteristics

| Metric | Value |
|--------|-------|
| Per-video time | 15-30 seconds (browser overhead) |
| Throughput (1 worker) | ~2-4 videos/minute |
| Throughput (2 workers) | ~4-8 videos/minute |
| Memory per worker | ~500MB (Firefox process) |
| CPU usage | Moderate (browser rendering) |

**Note**: Selenium is slower than API-based methods due to browser overhead, but more reliable against bot detection.

## Related Skills

- `/yt-dlp` — Fast transcript download (try first)
- `/yt-nlm` — NotebookLM transcript ingestion (high quality)
- `/yt-channel` — Video discovery and tracking

## ADR Reference

See fallback chain documentation in `csf/transcript.py`:

```python
# Chain order (fetch_transcript_chain):
#   1. yt-dlp (WEB client, curl_cffi TLS)
#   2. yt-dlp with English fallback
#   3. yt-dlp with any available language
#   4. yt-dlp with cookies (age-restricted)
#   5. Selenium Firefox ← This skill
#   6. Selenium Firefox with English fallback
#   7. Selenium Firefox with any available language
```
