# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-04-12

### Added
- **Batch NotebookLM workflow** — Up to 300 YouTube sources per notebook via `_fetch_via_notebooklm_batch()`, reuses notebooks instead of creating one per video
- **Auth auto-recovery** — `nlm login --check` before commands, `nlm login --force` on expiry; no manual intervention needed
- **External transcript provider hook** — `register_external_transcript_provider()` allows custom transcript sources to be injected into the fallback chain
- **Configurable NLM batch size** — `YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK` env var controls max sources per notebook (default: 300)
- **`last_stage` and `failure_reason` columns** — Structured failure taxonomy: `quota_exceeded`, `region_block`, `auth_failed`, `captcha`, `timeout`, `no_transcript`, `unavailable`, `unknown`
- **Schema migrations** — `last_stage` and `failure_reason` columns auto-added to existing `analysis_status` table
- **`/yt-is fetch`** — Explicitly documented skill command for transcript downloading with escalation chain

### Changed
- **`csf_nlm_ingest.py` deprecated** — Replaced docstring with deprecation notice pointing to `transcript.py` batch workflow
- **Source list parsing fixed** — `nlm source list --json` returns array not `{"sources": [...]}`, handling both formats correctly
- **Video ID extraction improved** — Extracts from `title` field when `url` is null for YouTube sources

### Fixed
- **Lazy init position** — `_NLM_MAX_SOURCES_PER_NOTEBOOK` now initialized before early return for empty video list
- **Duplicate code block** — Removed stray duplicate in `transcript.py` methods_to_try list

## [0.1.0] - 2026-03-26

### Added
- Initial release of yt-is (YouTube Intelligence System)
- `/yt-is` skill for YouTube channel management via RSS + API gap resolution
- `/yt-nlm` skill for NotebookLM transcript extraction
- `csf-source` backend with `add`, `list`, `check`, `check-all`, `sync`, `fetch` commands
- `yt-is` CLI wrapper for `csf-source`
- Transcript caching with SQLite backend (`transcripts.sqlite`)
- Batch processing with InterProcessLock for multi-terminal safety
- Full internationalization support (i18n) with language configuration
