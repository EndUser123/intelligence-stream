# yt-is Industrial Pipeline Specification (v3 Master)

## Problem: The "Scale Wall"
Processing 140,000 YouTube transcripts is currently blocked by three "Scale Walls":
1.  **The Notebook Tax:** 60-90s overhead per video (Create/Delete) in the cloud.
2.  **Discovery Blindness:** RSS feeds only show the last 15 videos, causing "Deep Gaps".
3.  **The Retry Deadlock:** Transient failures permanently block videos from being re-fetched.

## Solution: Industrial Orchestration
The system provides a self-healing, high-throughput ingestion engine with **Automated Triage**.

---

## 1. Automated Triage (Orchestration)
The system analyzes the pending backlog and selects the optimal ingestion path:

- **The Industrial Path (High-Volume):**
    - **Trigger:** `pending_count >= 50`.
    - **Strategy:** Proactively groups videos into batches of 300.
    - **Efficiency:** Bypasses local `yt-dlp` to leverage cloud-parallel ingestion immediately.
- **The Surgical Path (Low-Volume):**
    - **Trigger:** `pending_count < 50`.
    - **Strategy:** Sequential escalation via `yt-dlp` -> `NLM (Single)` -> `Selenium`.

---

## 2. Persistent Staging (Ingest Efficiency)
To eliminate the "Notebook Tax," the system implements **Terminal-Local Staging**:

- **Engine:** `NLMIndustrialScraper` (Selenium-based).
- **Pattern:** A module-level **Singleton** scraper instance per terminal process.
- **Rule:** A single staging notebook is reused across all fetch calls until it reaches the **300-source limit**.
- **Lifecycle:**
    1.  `_ensure_staging_notebook()`: Create or reuse a persistent notebook.
    2.  `_add_sources_to_staging([vids])`: Bulk-add to the existing cloud environment.
    3.  `_scrape_sources()`: Sidebar-loop extraction within the *same* notebook.
    4.  `_clear_staging_notebook()`: Delete/recreate only at the 300-limit or on termination.
- **Impact:** Reduces total setup/cleanup time by **99.7%**.

---

## 3. Deep Discovery (Data Completeness)
To ensure 100% database fidelity, discovery uses a two-tier safety net:

- **Tier 1: RSS (Speed):** Monitors the 15 most recent videos (low latency).
- **Tier 2: Deep Discovery (Fullness):** Performs a **Full Playlist Enumeration** via YouTube Data API if:
    - RSS returns no results (broken/disabled).
    - A "Deep Gap" is detected (no overlap between RSS and DB).
    - More than **7 days** have passed since the last check.
- **Mandate:** All discovery MUST capture rich metadata (`title`, `description`, `duration`) to prevent redundant API calls later.

---

## 4. Intelligent Retry Logic (Self-Healing)
The system recovers from transient environment failures:

- **Window:** Failed videos in the `download_archive` are eligible for retry after **24 hours**.
- **Scope:** Recovers from 429 rate limits, network timeouts, and temporary private/member-only locks.
- **Manual Reset:** `BatchScheduler.reset_failed_videos()` allows manual promotion of all failed items back to the pending pool.

---

## 5. Known Risks & Integrity Challenges

### A. Positional Mapping Risk (High Severity)
*   **Problem:** `NLMIndustrialScraper` and `_fetch_via_notebooklm_batch` map video IDs to transcripts based on their position in the NotebookLM source list.
*   **Risk:** If a single source fails to add or the list order changes silently, the system will attribute transcripts to the wrong videos.
*   **Mitigation:** Single-video staging calls are 100% safe (1:1 mapping). Batch operations (>1 video) require future work to implement "ID Tagging" inside the transcript text for verification.

---

## 6. Modular Implementation Map

| Component | Responsibility | Pattern |
|-----------|----------------|---------|
| `bin/csf-source` | Triage Controller | High-level orchestration |
| `csf/nlm_scraper.py` | Persistent Staging | Selenium-based singleton |
| `csf/transcript.py` | Escalation Chain | Fallback logic & Batch-call routing |
| `csf/source_enumerator.py` | Discovery & Enumeration | RSS + Deep Discovery fallback |
| `csf/batch_scheduler.py` | Queue Management | Round-robin with 24h retry window |
| `batch_status.sqlite` | Central State | Rich metadata & provenace tracking |

## Capacity Target
- **Industrial Path:** ~18,000 v/hr (across 10 terminals).
- **Surgical Path:** ~720 v/hr.
- **6-Month Horizon:** 140,000 video backlog cleared in **~3-4 days** of total processing time.
