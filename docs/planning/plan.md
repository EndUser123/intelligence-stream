# Plan: intelligence-stream CLI restructuring

## Tasks

- [x] TASK-1: Add `get_newest_published_for_source` to batch_status.py public API — ALREADY EXISTS (batch_status.py:375, 566)
- [x] TASK-2: Add `mark_complete` overload to batch_status.py public API — ALREADY EXISTS (batch_status.py:452)
- [x] TASK-3: Add `set_status_batch` bulk operation to batch_status.py
- [x] TASK-4: Fix bin/csf-source imports + replace N+1 loops with batch calls
- [x] TASK-5: Add InterProcessLock to cmd_sync for multi-terminal coordination
