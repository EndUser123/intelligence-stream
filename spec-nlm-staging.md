# yt-is Industrial Pipeline — NLM Staging Scraper Spec

## Problem

Processing 140,000 YouTube transcripts required a new notebook per video — ~60–90s overhead per video in sequential operation. `NLMIndustrialScraper` existed in `nlm_scraper.py` with terminal-local staging notebook logic, but was unreachable from the transcript fetch chain (`transcript.py`).

## Solution: Terminal-Local Staging Notebook

Wire `NLMIndustrialScraper` into the transcript fetch chain via a module-level singleton. One staging notebook per terminal process, auto-cleared at the 300-source NotebookLM Plus limit, reused across all `_fetch_via_notebooklm` calls.

## Architecture

```
transcript_fetch_chain()
  │
  └─► _fetch_via_notebooklm(video_id)
          │
          └─► _get_nlm_scraper()         ← module singleton (one per terminal)
                   │
                   └─► NLMIndustrialScraper.scrape_with_staging([video_id])
                             │
                             ├─► _ensure_staging_notebook()
                             │     ├─► _create_staging_notebook()   [first call]
                             │     └─► _clear_staging_notebook()   [at 300 limit]
                             │
                             ├─► _add_sources_to_staging([video_id])
                             │     └─► nlm source add --url --wait
                             │
                             └─► _scrape_sources({video_id: source_id})
                                   └─► Selenium: open notebook, click Sources,
                                       click source button, poll for transcript
```

## Key Design Decisions

### Terminal-local singleton
- One staging notebook per terminal process — independent, no cross-terminal coordination.
- vs. per-channel: wastes slots when channels have <300 lifetime uploads.
- vs. shared pool: simpler failure isolation (one terminal's crash doesn't corrupt another's notebook).

### Iterative overflow handling
- While-loop clears and recreates notebook at 300 limit.
- Not recursive (caused stack overflow in unit tests under mocked context).

### Order-based source mapping
- Source IDs mapped to video IDs by insertion order.
- Single-video calls are trivially 1:1 (safe).
- Batch path (`_fetch_via_notebooklm_batch`) inherits the same risk — hardening requires source-ID tagging inside transcript content.

## Files Changed

| File | Change |
|------|--------|
| `csf/transcript.py` | `TYPE_CHECKING` guard; `_nlm_scraper` singleton; `_fetch_via_notebooklm` and `_fetch_via_notebooklm_batch` now delegate to `scrape_with_staging()` |
| `csf/nlm_scraper.py` | Lint fixes only (unused imports, ambiguous var `l`) |

## Capacity Impact

| Scenario | Before | After |
|---------|--------|-------|
| 1 video | 1× notebook create+delete | 1× source add + Selenium scrape |
| 300 videos (sequential) | 300× notebook overhead | 1× notebook + 300× source add + 1× Selenium scrape loop |
| 140k videos (one terminal) | ~97 days (inferred) | ~2–3 days (inferred, pending measurement) |

Numbers are inferred from the ~60–90s overhead estimate — not yet instrumented. Confirm with real latency measurements before planning capacity.

## Open Risks

| Risk | Severity | Status |
|------|----------|--------|
| Positional mapping corruption in batch path | HIGH | Known — single-video path is safe |
| Selenium UI changes breaking transcript extraction | MEDIUM | Deferred — requires monitoring |
| Selenium 4.40.0 CVE exposure | MEDIUM | Deferred — needs `pip-audit` when online |
| 22 pre-existing mypy `Optional[driver]` errors | LOW | Deferred — type narrowing debt |
| `test_selenium_fails_notebooklm_succeeds` fails on `main` | LOW | Pre-existing — chain-order mismatch in test |

## Rollback

To revert the integration:
1. Restore `_fetch_via_notebooklm()` to call `process_industrial_batch([video_id])`
2. Restore `_fetch_via_notebooklm_batch()` to call `process_industrial_batch(video_ids)`
3. Delete `_nlm_scraper`, `_get_nlm_scraper()`, and the `TYPE_CHECKING` import block

`nlm_scraper.py` is never imported in the hot path without this change — no side effects from deletion.
