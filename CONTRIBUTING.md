# Contributing to yt-is

## Development Setup

```powershell
# Clone the repository
git clone <repo-url>
cd yt-is

# Create skill junctions (Windows)
New-Item -ItemType Junction -Path "P:\.claude\skills\yt-is-analyze" -Target "P:\\packages\\yt-is\skills\analyze"
New-Item -ItemType Junction -Path "P:\.claude\skills\yt-is-ingest" -Target "P:\\packages\\yt-is\skills\ingest"
```

## Running Tests

```bash
pytest tests/ -v
pytest tests/ --timeout=30  # with timeout
pytest tests/ --cov=csf     # with coverage
```

## Industrial Ingest Principles (Backlog Scale)

This project is optimized to process a 140,000-video backlog. All contributions must adhere to these scale-driven principles:

1.  **Mandatory Staging:** Do not implement per-video setup/cleanup logic. All new fetchers must support a **Persistent Staging** pattern to minimize cloud-environment overhead.
2.  **300-Source Limit:** NotebookLM-related batching must respect the 300-source limit per notebook.
3.  **Positional Mapping Verification:** Any bulk-add operations must verify the 1:1 positional mapping between input video IDs and output transcripts to prevent data corruption.
4.  **Deep Discovery:** Do not rely solely on RSS for discovery. Any new channel monitoring logic must include a periodic **Full Playlist Enumeration** safety net.
5.  **Self-Healing:** Always implement retry windows for transient failures. Use the `BatchScheduler` retry pattern (default 24h) for all network-bound operations.

## Code Quality

```bash
ruff check .
ruff format .
mypy csf/
```

## Pull Request Process

1. Ensure tests pass (`pytest tests/`)
2. Run ruff check and format
3. Update CHANGELOG.md if applicable
4. PR description should reference any related issues

