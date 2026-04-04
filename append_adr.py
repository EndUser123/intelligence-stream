from pathlib import Path
existing = Path("P:/packages/intelligence-stream/.claude/arch_decisions/ADR-20260404-round-robin-batch-scheduler.md").read_text(encoding="utf-8")
more = "---

## Consequences

**Positive:**
- Round-robin diffusion: time between requests to same channel is maximized
- Persistent archive: restart after 429 skips failed videos without YouTube metadata API calls
- Shared channel cooldown: one terminal 429 protects all other terminals from same channel
- Jitter: desynchronizes workers so they do not slam server simultaneously during recovery

**Negative:**
- Throughput per channel is lower ( deliberate — feature, not bug)
- Scheduler is stateful (SQLite) — must survive crashes cleanly
- status=attempting in archive requires restart-safe cleanup (stale attempting entries = re-process)

**Risks:**
- Stale attempting entries: if terminal crashes, attempting videos never get failed written. Fix: on scheduler start, promote status=attempting to status=failed for all entries older than some threshold.
- time.monotonic() for cooldown: not wall-clock. Cooldown expires based on process uptime, not real time. YouTube rate limits are real-time, so if a terminal was idle for hours then wakes up, its cooldown timers may have expired but YouTube limit has not. Minor — circuit will re-open on first 429 if needed.
"
Path("P:/packages/intelligence-stream/.claude/arch_decisions/ADR-20260404-round-robin-batch-scheduler.md").write_text(existing + more, encoding="utf-8")
print(len(more), "chars appended")
