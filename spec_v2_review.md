# yt-is Industrial Pipeline Specification (v2 Draft)

## Problem
Processing 140,000 YouTube transcripts requires a shift from **Surgical Retrieval** (1-by-1 local) to **Industrial Ingest** (bulk cloud). The primary bottleneck is the "Notebook Tax" (60-90s overhead per video) and the fragility of RSS-only discovery.

## Solution: Dual-Path Ingest with Automated Triage
The system orchestrates two paths based on the pending backlog size:

### 1. The Industrial Path (High-Volume)
- **Trigger:** Automatically engaged when `pending_count >= 50`.
- **Engine:** `NLMIndustrialScraper` using **Persistent Staging**.
- **Efficiency:** Reuses a single staging notebook for up to 300 videos. This reduces setup overhead by **99.7%**.
- **Throughput:** ~18,000 videos/hour.
- **Fidelity:** **99% SNR** (Signal-to-Noise Ratio).

### 2. The Surgical Path (Low-Volume)
- **Trigger:** Default for small updates or specific manual fetches (`pending_count < 50`).
- **Engine:** `csf/transcript.py` escalation chain.
- **Workflow:** Sequential attempts via `yt-dlp` -> `NLM (Single)` -> `Selenium` -> `Whisper`.

---

## Architectural Mandates (The "Industrial" Rules)

### 1. Persistent Staging (Ingest Efficiency)
*   **Pattern:** Use a module-level singleton for `NLMIndustrialScraper`.
*   **Rule:** A staging notebook must stay alive across multiple video requests until it reaches the 300-source limit or the process terminates.
*   **Benefit:** Eliminates the "Amnesia Loop" where the system re-authenticates and re-creates environments for every video.

### 2. Automated Triage (Orchestration)
*   **Pattern:** `cmd_fetch` analyzes the total `pending` count before starting.
*   **Rule:** If the backlog is substantial, the system MUST group videos into batches of 300 and bypass the local `yt-dlp` stage to leverage cloud-parallel ingestion immediately.

### 3. Deep Discovery (Data Completeness)
*   **Pattern:** Two-tier discovery in `source_enumerator.py`.
*   **Rule:** RSS is used for speed, but the system MUST fallback to **Full Playlist Enumeration** if:
    1.  The RSS feed is empty/broken.
    2.  The last check was > 7 days ago.
    3.  A "Deep Gap" is suspected (no overlap between RSS and DB).

### 4. Rich Metadata Persistence
*   **Rule:** The discovery phase MUST capture `title`, `description`, `duration`, and `thumbnail` into the `analysis_status` table.
*   **Benefit:** Prevents thousands of redundant "Metadata-only" API calls during the analysis and summary phases.

### 5. Intelligent Retry Logic
*   **Rule:** Videos marked as `failed` in the `download_archive` are eligible for retry after **24 hours**.
*   **Benefit:** Automatically recovers from transient issues like 429 rate limits, network timeouts, or temporary private/member-only locks.

---

## Modular Implementation
- **`csf/nlm_scraper.py`**: Implementation of the Persistent Staging Scraper.
- **`csf/source_enumerator.py`**: Logic for RSS + Deep Discovery fallback.
- **`csf/batch_scheduler.py`**: Round-robin scheduling with 24-hour retry windows.
- **`bin/csf-source`**: The Triage controller for Industrial vs. Surgical routing.
