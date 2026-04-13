# Refactor Plan: Cookie Caching Optimization

**File:** `csf/transcript.py`
**Date:** 2026-04-09
**Priority:** P0 (TOCTOU) → P1 (performance) → P2 (DRY)

## Problem Summary

Currently `_get_firefox_cookie_file()` is called PER VIDEO, causing:
1. **P0:** TOCTOU race condition - concurrent requests delete cookie file before others finish
2. **P1:** 19-80ms wasted per video on file I/O
3. **P1:** Silent cleanup failures leak temp files
4. **P2:** No module-level caching pattern

## Solution: Module-Level Cookie Cache

Implement singleton caching pattern (similar to `_scheduler` at lines 28-37):
- Cache cookie file path with 5-minute TTL
- Reference counting for concurrent access
- Thread-safe with lock protection
- Cleanup on expiry or explicit release

## Changes Required

### P0: Fix TOCTOU Race Condition

**Location:** `transcript.py:500-630`

**Change:** Add module-level cache with reference counting

```python
# Add after line 37 (after _scheduler pattern)
_cookie_cache: dict[str, str | int | float] = {}  # {path: str, refcount: int, expiry: float}
_cookie_lock = threading.Lock()
COOKIE_CACHE_TTL = 300  # 5 minutes

def _get_cookie_file() -> str | None:
    """Get cached cookie file with reference counting.

    Returns:
        Cookie file path, or None if unavailable.
    """
    global _cookie_cache

    with _cookie_lock:
        # Check cache validity
        if _cookie_cache:
            path = _cookie_cache.get("path")
            expiry = _cookie_cache.get("expiry", 0)
            if path and os.path.exists(path) and time.time() < expiry:
                _cookie_cache["refcount"] = _cookie_cache.get("refcount", 0) + 1
                return path
            else:
                # Cleanup stale cache
                _cleanup_cookie_cache()

        # Generate new cookie file
        cookie_file = _generate_cookie_file()
        if cookie_file:
            _cookie_cache = {
                "path": cookie_file,
                "refcount": 1,
                "expiry": time.time() + COOKIE_CACHE_TTL
            }
        return cookie_file

def _release_cookie_file(cookie_file: str) -> None:
    """Release reference to cached cookie file."""
    global _cookie_cache

    with _cookie_lock:
        if _cookie_cache.get("path") == cookie_file:
            _cookie_cache["refcount"] = _cookie_cache.get("refcount", 1) - 1
            if _cookie_cache["refcount"] <= 0:
                _cleanup_cookie_cache()

def _cleanup_cookie_cache() -> None:
    """Cleanup cached cookie file and reset cache."""
    global _cookie_cache

    path = _cookie_cache.get("path")
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception as e:
            logging.warning(f"Failed to cleanup cookie file {path}: {e}")
    _cookie_cache = {}
```

**Modify:** `_fetch_via_ytdlp_with_cookies()` at line 512

```python
# OLD:
cookie_file = _get_firefox_cookie_file()  # Line 512

# NEW:
cookie_file = _get_cookie_file()
try:
    # ... existing yt-dlp logic ...
finally:
    if cookie_file:
        _release_cookie_file(cookie_file)
```

### P1: Add Logging for Cleanup Failures

**Location:** `transcript.py:606-607, 614-615, 620-621`

**Change:** Replace silent `except Exception: pass` with logging

```python
# OLD:
except Exception:
    pass

# NEW:
except Exception as e:
    logging.warning(f"Failed to cleanup cookie file: {e}")
```

### P2: Add Thread Safety to Scheduler Singleton

**Location:** `transcript.py:28-37`

**Change:** Add lock protection (optional, already works in practice)

```python
_scheduler_lock = threading.Lock()

def _get_scheduler() -> BatchScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BatchScheduler()
    return _scheduler
```

## Characterization Tests

Create `tests/test_cookie_cache_refactor.py`:

1. **Test concurrent access** - Multiple threads request cookie file simultaneously
2. **Test reference counting** - Verify file not deleted while in use
3. **Test TTL expiry** - Verify cache expires after 5 minutes
4. **Test cleanup on error** - Verify reference decremented even if exception raised

## Verification Plan

1. Run characterization tests → ensure they FAIL (current buggy behavior)
2. Apply refactor changes
3. Run characterization tests → ensure they PASS (fixed behavior)
4. Run full test suite → verify no regressions
5. Manual test: `yt-batch-fetch --run --workers 4` on known age-restricted videos

## Rollback Plan

If tests fail:
1. Revert `transcript.py` to git HEAD
2. Remove new cache module variables
3. Delete characterization tests
4. Report findings to user

## Success Criteria

- [ ] Characterization tests capture current buggy behavior
- [ ] Refactor eliminates TOCTOU race condition
- [ ] Refactor reduces per-video overhead (verified via timing)
- [ ] All existing tests pass
- [ ] Manual batch run completes without file-not-found errors
