---
# ADR-20260404: Round-Robin Batch Scheduler for 429-Resilient Transcript Downloads

**Status:** Proposed
**Date:** 2026-04-04
**Deciders:** Bruce Thomson

---

## Context

Our batch pipeline (batch.py) processes videos from a single channel in parallel via ThreadPoolExecutor, submitting all video IDs simultaneously with no channel interleaving. When processing multiple channels sequentially (Channel A 100 videos, then Channel B 100 videos), each channel gets hit with a burst of concurrent requests — the exact pattern that triggers YouTube per-channel adaptive rate limits producing HTTP 429 errors.

Existing mitigations in transcript.py:35-54:
- Jitter between parallel workers (2-10s random delay)
- Per-source circuit breaker (3 consecutive 429s -> 5 min cooldown, exponential backoff)
- Transcript cache skip (has_cached_transcript() — zero re-fetch for cached)
- Batch idempotency (is_complete skip on restart)

What is missing:
1. **No channel interleaving** — all videos from one channel submitted at once
2. **No persistent download archive** — restart after 429 re-fetches video metadata to check status
3. **Per-channel circuit breakers are process-local** — Terminal A circuit open does not protect Terminal B

---

## Decision

### New module: csf/batch_scheduler.py

### Algorithm: Jittered Round-Robin

channels = get_all_pending_channels()   -- SELECT DISTINCT source FROM analysis_status WHERE status=pending
round_robin = cycle(channels)           -- Channel A -> B -> C -> A -> B -> C...

for channel in round_robin:
    if is_channel_in_cooldown(channel):
        continue                         -- skip cooling-down channel
    videos = get_pending_by_source(channel)
    for video in videos:
        yield video                     -- one video per channel per iteration
        sleep(random(2, 10))           -- jitter between dispatches
        if is_channel_in_cooldown(channel):
            break                        -- move to next channel
### New SQLite tables in batch_status.db

-- Download archive: persistent record of ALL attempted video IDs
CREATE TABLE download_archive (
    video_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,           -- success | failed | skipped | attempting
    source TEXT NOT NULL,           -- channel_url
    attempted_at TEXT NOT NULL,
    error TEXT                       -- last error if failed
);

-- Per-channel cooldown shared across all terminals
CREATE TABLE channel_cooldown (
    source TEXT PRIMARY KEY,         -- channel_url
    cooldown_until REAL NOT NULL,   -- unix timestamp (time.monotonic())
    consecutive_429s INTEGER NOT NULL DEFAULT 0
);

Schema migrations (backward-compatible):
ALTER TABLE channel_metadata ADD COLUMN last_fetch_attempt TEXT;
ALTER TABLE channel_metadata ADD COLUMN cooldown_until REAL;

### Integration with existing components

- BatchScheduler.yield_next() yields video IDs in round-robin order
- ThreadPoolExecutor workers call transcript.py::fetch_transcript_chain() independently
- transcript.py records 429s to channel_cooldown SQLite table (shared across terminals)
- On 429: channel_cooldown.consecutive_429s += 1, cooldown_until = now + 300s
- On success: DELETE FROM channel_cooldown WHERE source = ? (cooldown resets)
- Archive write happens BEFORE transcript fetch (idempotent on retry)

### Multi-terminal coordination

| Mechanism | Location | Purpose |
|---|---|---|
| InterProcessLock | bin/csf-source:312 (existing) | Only one cmd_sync per channel at a time |
| WAL mode | batch_status.py:62 (existing) | Non-blocking reads during writes |
| channel_cooldown SQLite | batch_status.db (new) | Shared circuit breaker state across terminals |
| download_archive SQLite | batch_status.db (new) | Cross-terminal skipped-video set |

Per-source circuit breakers in transcript.py remain process-local. channel_cooldown SQLite table provides coarser-grained channel-level protection shared across all terminals.

### Scheduler owns round-robin, transcript.py owns fetch

The BatchScheduler decides **which** video to process next (channel interleaving + archive skipping). Each worker calls transcript.py::fetch_transcript_chain() independently, which already owns per-source (yt-dlp, youtube_transcript_api, youtubei, SDK) circuit breakers. The channel_cooldown is an additional layer above all sources at channel granularity.

### Download archive vs batch_status idempotency

Existing batch_status.is_complete(vid) only skips videos on restart. During a running batch, if a worker fails mid-attempt, there is no record until mark_complete or mark_failed is called.

The download archive closes this:
- Every worker first writes (video_id, attempting, ...) before calling transcript
- On success -> update to (video_id, success, ...)
- On 429/error -> update to (video_id, failed, ...)
- On restart -> yield_next() skips all status != pending entries
