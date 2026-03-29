# Contributing to intelligence-stream

## Development Setup

```powershell
# Clone the repository
git clone <repo-url>
cd intelligence-stream

# Create skill junctions (Windows)
New-Item -ItemType Junction -Path "P:\.claude\skills\intelligence-stream-analyze" -Target "P:\packages\intelligence-stream\skills\analyze"
New-Item -ItemType Junction -Path "P:\.claude\skills\intelligence-stream-ingest" -Target "P:\packages\intelligence-stream\skills\ingest"
```

## Running Tests

```bash
pytest tests/ -v
pytest tests/ --timeout=30  # with timeout
pytest tests/ --cov=csf     # with coverage
```

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
