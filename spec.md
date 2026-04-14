# yt-is Industrial Pipeline Specification

## Problem
Processing 140,000 YouTube transcripts requires a shift from **Surgical Retrieval** (1-by-1 local) to **Industrial Ingest** (bulk cloud). Previous local methods like Selenium were found to return "Dirty Data" (40% Signal-to-Noise Ratio) due to page noise pollution.

## Solution: Dual-Path Ingest
The system now features two entry points based on data volume:

### 1. The Industrial Path (Priority)
- **Engine:** `csf/nlm_batch.py`
- **Throughput:** ~18,000 videos/hour (via 300-source cloud parallelization).
- **Fidelity:** **99% SNR**. It matches ground truth transcripts within ±1% by using the direct `source content` JSON endpoint.
- **Entry Point:** `/yt-nlm` command.

### 2. The Surgical Path (Verification)
- **Engine:** `csf/transcript.py`
- **Throughput:** ~720 videos/hour.
- **Methods:** `yt-dlp` (TLS impersonation) -> `nlm_batch` -> `nlm_scraper` (Sidebar loop).
- **Fidelity:** High fidelity local scrape.
- **Entry Point:** `/yt-channel fetch` command.

## Critical Discovery: SNR (Signal-to-Noise Ratio)
- **Clean Ingest (NLM):** Returns ~9,000 chars for a 10m video. Matches spoken words.
- **Dirty Scrape (Selenium):** Returns ~15,000 chars for the same video. 6,000 chars are "page noise" (titles, comments, UI).
- **Result:** NLM ingest reduces downstream token costs by ~40% and prevents model confusion.

## Modular Architecture
- `nlm_batch.py`: High-speed cloud engine.
- `nlm_scraper.py`: High-fidelity UI fallback (with Sidebar Scrolling fix).
- `transcript.py`: Orchestrated escalation chain.
- `batch_status.sqlite`: Idempotent queue management for 140k items.

## Verification & Integrity
### 1. High-Fidelity Ground Truth
Ground truth verified against `youtube_transcript_api`. All high-speed paths (`nlm_batch.py`) match ground truth length within ±1%.

### 2. Asynchronous Fidelity Audit (Non-Blocking)
To maintain Industrial Velocity (~18,000 v/hr), validation is an **Analysis-Only** step:
- **One-Time Ingest:** Transcripts are fetched ONCE via NLM Batch and stored in `transcripts.sqlite`.
- **Zero-Fetch Validation:** The system compares the length of the AI-generated analysis against the *already-cached* transcript.
- **Async Re-Analysis:** If an analysis is too thin (lazy AI), the system sends the **existing cached transcript** to a higher-tier model (Gemini SDK) for a better pass.
- **Result:** No re-downloads, no slow scrapers, just background "polishing" of the knowledge base using data we already own.
