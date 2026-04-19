# Worker-Pool Dev Sandbox

This folder is intentionally isolated from the production `csf-source fetch` path.
It is for answering one question with real trace data before we touch the live
industrial pipeline:

> If we split notebook work across isolated workers, how much throughput do we gain?

## What this sandbox does

- Reads existing JSONL traces from `.logs/`
- Extracts completed NotebookLM batch timings
- Models how 1..N workers would schedule those batches
- Reports:
  - modeled success/hour
  - modeled processed/hour
  - makespan
  - worker utilization

## Why this is useful

The live path currently shows:

- one reusable notebook worker
- serial industrial batch processing
- expensive setup per 300-source batch

This sandbox lets us test the worker-count question with data before changing:

- `bin/csf-source`
- `csf/nlm_batch.py`
- `csf/batch.py`

## Review findings that matter here

These elephant-alpha points are actually relevant if we move to a worker pool:

- `batch.py`: non-thread-safe global `_analyze_video_ref`
- `batch.py`: progress callback exceptions need to be isolated
- `batch.py`: worker-count calculation should be consistent

These are not throughput blockers for this experiment:

- `orchestrator.py` DST edge cases
- `orchestrator.py` lazy import / cache cleanup notes
- `summarize.py` `shell=True` claim, which does not appear in the current file

## Example

```powershell
python dev\worker_pool\planner.py .logs\term_052c5133.jsonl --max-workers 8
```

