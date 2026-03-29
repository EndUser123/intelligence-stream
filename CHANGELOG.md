# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0] - 2026-03-26

### Added
- Initial release of intelligence-stream
- `/csf-ingest` skill for YouTube playlist ingestion via yt-dlp
- `/csf-analyze` skill for video content analysis via Gemini
- Three-tier analysis: SDK Passthrough → Transcript Fallback → CLI Fallback
- CKS (Constitutional Knowledge System) integration for storing results
- Transcript caching with SQLite backend
- NLM (Language) export support with composite batching
- Multi-terminal safe batch processing with InterProcessLock
- Full internationalization support (i18n) with language configuration
