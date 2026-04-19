"""Dev-only worker pool planning helpers.

This package is intentionally isolated from the production fetch path.
It is used to model notebook-worker concurrency and estimate throughput
from trace logs before any live industrial-path changes are made.
"""

