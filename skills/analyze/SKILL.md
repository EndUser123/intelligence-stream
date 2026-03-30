---
name: intelligence-stream-analyze
version: "1.0.0"
status: "stable"
category: intelligence
description: Analyze video content using Gemini API and store results in CKS.
---

# /csf-analyze — Video Content Analysis

Analyze video content using Gemini API and store results in CKS.

## Usage

```
/csf-analyze <video_id_or_url>
```

## Implementation

Invokes `bin/csf-analyze` script to run Gemini analysis on video content.

## Architecture

```
csf-analyze
├── bin/csf-analyze          # CLI entry point + API key rotation
├── csf/video.py             # YouTube metadata extraction
├── csf/transcript.py        # Transcript fetch + translation chain
├── csf/analyze.py           # Gemini video analysis (Part.from_uri passthrough)
├── csf/cache.py             # SQLite transcript cache
└── csf/batch.py             # ThreadPoolExecutor batch processing
```

## Video Passthrough Models

| Model | Video Passthrough | Notes |
|-------|-----------------|-------|
| `gemini-3.1-flash-lite-preview` | ✅ | **Default** — generous quota, preview suffix required |
| `gemini-2.5-flash` | ✅ | Free tier stuck at 20 RPD (counter stale) |

## API Key Rotation

On 429 (quota error), `bin/csf-analyze` rotates to the next key in the pool and retries:

```
GEMINI_API_KEY_2 → GEMINI_API_KEY_1 → GEMINI_PAID_API_KEY → GEMINI_API_KEY
```

Per-process `_exhausted_keys` set tracks which keys have returned 429 this session.

## Transcript Caching

First run → fetch + cache transcript (free). Subsequent runs → `mode="transcript"` bypasses API call entirely. Pre-check via `csf/cache.py:has_cached_transcript()`.

## Batch Processing

```bash
csf-batch <video_id> [<video_id>...] [options]
csf-batch --list <file>       # File with one video ID per line
csf-batch --workers N         # Parallel workers (default: 4, max: 8)
csf-batch --force             # Re-process even if complete
csf-batch --progress          # Show real-time pending/done/failed/cached
csf-batch --reset             # Clear all status entries first
```

## References

- **Quota findings**: `P:/.claude/skills/google-ai-usage-monitor/SKILL.md` — Gemini Model Quota section
- **ADR-20260329**: `P:/.claude/arch_decisions/ADR-20260329-intelligence-stream-nlm-export-and-i18n.md`
