---
# ADR-20260404: Round-Robin Batch Scheduler for 429-Resilient Transcript Downloads

**Status:** Accepted
**Date:** 2026-04-04
**Accepted:** 2026-04-08
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

---

## Consequences

**Positive:**
- Round-robin diffusion: time between requests to same channel is maximized
- Persistent archive: restart after 429 skips failed videos without YouTube metadata API calls
- Shared channel cooldown: one terminal's 429 protects all other terminals from same channel
- Jitter: desynchronizes workers so they do not slam server simultaneously during recovery
- Archive write-before-fetch: crash-safe, no lost work

**Negative:**
- Throughput per channel is lower — deliberate tradeoff against 429 blackouts
- Scheduler is stateful (SQLite WAL) — must survive crashes cleanly
- `status='attempting'` in archive requires restart-safe cleanup (stale entries = re-process)

**Risks:**
- Stale `attempting` entries: if terminal crashes, `attempting` videos never get `failed` written. Fix: on scheduler start, promote `status='attempting'` older than a threshold (e.g., 30 min) to `status='failed'`.
- `time.monotonic()` for cooldown: not wall-clock. YouTube rate limits are real-time. If a terminal was idle for hours then wakes up, its cooldown timers may have expired but YouTube limit has not. Minor — circuit re-opens on first 429 if needed.

---

## Contract Authority Packet

```yaml
contract_authority_packet:
  packet_version: "1"
  contract_sensitive: true
  authority:
    closure_source: "adr_20260404_round_robin_scheduler"
    prose_role: "explanatory_only"
  boundaries:

    - boundary_id: "scheduler-archive-read"
      producer: "BatchScheduler.yield_next()"
      consumer: "ThreadPoolExecutor workers"
      schema:
        id: "video_id"
        version: "1"
      required_fields: ["video_id", "source"]
      optional_fields: []
      freshness_authority: "download_archive SQLite"
      invalidation_trigger: "video_id appears in download_archive with status='success'|'failed'"
      precedence_rule: "archive supersedes pending queue"
      failure_behavior: "yield skips archived video_id silently"
      validator_owner: "batch_scheduler.py"
      proof_owner: "test_batch_scheduler.py"
      downstream_consumers: ["transcript.py"]

    - boundary_id: "scheduler-cooldown-write"
      producer: "BatchScheduler.record_429()"
      consumer: "BatchScheduler.yield_next() (other terminals)"
      schema:
        id: "channel_cooldown"
        version: "1"
      required_fields: ["source", "cooldown_until", "consecutive_429s"]
      optional_fields: []
      freshness_authority: "channel_cooldown SQLite table"
      invalidation_trigger: "cooldown_until < time.monotonic()"
      precedence_rule: "channel_cooldown entry blocks source from yield"
      failure_behavior: "cooldown expires after cooldown_until → source unblocked"
      validator_owner: "batch_scheduler.py"
      proof_owner: "test_batch_scheduler.py"
      downstream_consumers: ["batch_scheduler.py"]

    - boundary_id: "transcript-archive-write"
      producer: "transcript.py fetch_transcript_chain()"
      consumer: "BatchScheduler.yield_next()"
      schema:
        id: "download_archive"
        version: "1"
      required_fields: ["video_id", "status", "source", "attempted_at"]
      optional_fields: ["error"]
      freshness_authority: "transcript.py (result of fetch)"
      invalidation_trigger: "new entry for same video_id"
      precedence_rule: "newer entry wins"
      failure_behavior: "archive write failure does NOT propagate to transcript result"
      validator_owner: "batch_status.py"
      proof_owner: "test_batch_scheduler.py"
      downstream_consumers: ["batch_scheduler.py"]

    - boundary_id: "worker-scheduler-handoff"
      producer: "ThreadPoolExecutor worker"
      consumer: "BatchScheduler"
      schema:
        id: "worker-result"
        version: "1"
      required_fields: ["video_id", "success", "error"]
      optional_fields: []
      freshness_authority: "worker result"
      invalidation_trigger: "N/A — write once"
      precedence_rule: "N/A"
      failure_behavior: "failed video_id written to download_archive as 'failed'"
      validator_owner: "batch_scheduler.py"
      proof_owner: "test_batch_status.py"
      downstream_consumers: ["batch_status.py"]
```

---

## Rejected Alternatives

**A. Global circuit breaker in transcript.py (process-local only)**
Rejected: Only protects the process that hits 429. Other terminals remain exposed.

**B. yt-dlp --download-archive at CLI level**
Rejected: We use transcript.py Python API, not yt-dlp CLI. Archive must cover all 6 transcript methods.

**C. asyncio-based scheduler with rate-limit-aware backoff**
Rejected: transcript.py is synchronous. asyncio workers with sync workers adds complexity without benefit.

**D. Proxy rotation (residential/mobile proxies)**
Rejected: Requires third-party proxy service ($). Does not help when rate limit is per-channel, not per-IP.

---

## Gaps Not Yet Closed

| Gap | Status | Notes |
|---|---|---|
| `channel_metadata.last_fetch_attempt` column migration | OPEN | Needs migration script for existing DBs |
| `BatchScheduler` test suite | OPEN | Round-robin ordering, cooldown blocking, archive skip |
| Stale `attempting` entry recovery | OPEN | On startup, promote old `attempting` → `failed` |
| yt-dlp --impersonate chrome via curl-cffi | FUTURE | Stronger TLS fingerprint impersonation — pip install + Windows testing |
| --sleep-subtitles 61 for auto-translated subs | FUTURE | Additional yt-dlp flag for auto-sub 429 avoidance |
