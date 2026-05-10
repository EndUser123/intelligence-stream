# Source-Shape Routing Ladder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make no-caption routing configurable, expose the mode in logs, and add a ladder scenario that compares NotebookLM-first and fallback-first routing on the same frozen cohort.

**Architecture:** Add a small env-driven routing switch in `csf-source`, then teach the ladder runner about the new scenario. Keep the benchmark engine intact so the new run reuses existing frozen-cohort and summary machinery. Tests should pin both the default route and the opt-in fallback-first route.

**Tech Stack:** Python 3.14, pytest, JSONL trace fixtures, existing benchmark runners.

---

### Task 1: Add a routing toggle to `csf-source`

**Files:**
- Modify: `P:\\\\\\packages/yt-is/bin/csf-source`
- Test: `P:\\\\\\packages/yt-is/tests/test_csf_source_fetch_timing.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cmd_fetch_routes_no_captions_to_transcript_fallback_when_enabled():
    mod = _load_csf_source_module()
    channel_rows = [("https://www.youtube.com/@active", "pl-1")]
    pending_entries = [{
        "video_id": "vid000",
        "status": "pending",
        "has_captions": False,
        "privacy_status": "public",
        "upload_status": "uploaded",
        "is_live_content": False,
        "unavailable_reason": None,
        "source": "https://www.youtube.com/@active",
    }]
    with mock.patch.object(mod, "_get_batch_status_storage", return_value=FakeStorage(channel_rows)):
        with mock.patch.object(mod, "is_channel_blocked", return_value=False):
            with mock.patch.object(mod, "get_entries_for_source_details", return_value=pending_entries):
                with mock.patch.object(mod, "has_cached_transcript", return_value=False):
                    with mock.patch.dict(mod.os.environ, {
                        "YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK": "true",
                        "YTIS_TRANSCRIPT_FALLBACK_MIN_START_INTERVAL_S": "0",
                        "YTIS_TRANSCRIPT_FALLBACK_WORKERS": "4",
                    }, clear=False):
                        with mock.patch.object(mod.subprocess, "run") as mock_run:
                            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
                            with mock.patch("csf.transcript.fetch_transcript_chain", return_value=transcript_result) as mock_fetch:
                                with mock.patch.object(mod, "process_industrial_batch_reusable") as mock_process:
                                    with mock.patch.object(mod, "close_reusable_ingestor"):
                                        with mock.patch.object(mod, "set_cached_transcript"):
                                            with mock.patch.object(mod, "mark_complete"):
                                                with mock.patch.object(mod, "log_action") as mock_log:
                                                    mod.cmd_fetch(dry_run=False, workers=1)

    assert mock_process.call_count == 0
    assert mock_fetch.call_count == 1
    fetch_invoked = next(call.args[1] for call in mock_log.call_args_list if call.args[0] == "fetch_invoked")
    assert fetch_invoked["route_no_captions_to_fallback"] is True
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_csf_source_fetch_timing.py -k no_captions_to_fallback -q`
Expected: fail because the env flag and route log field do not exist yet.

- [ ] **Step 3: Implement the routing switch**

```python
def _env_bool(primary: str, fallback: str | None, default: bool) -> bool:
    value = os.getenv(primary)
    if value is None or not value.strip():
        value = os.getenv(fallback) if fallback else None
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

route_no_captions_to_fallback = _env_bool(
    "YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK",
    "YTIS_TRANSCRIPT_ROUTE_NO_CAPTIONS_TO_FALLBACK",
    False,
)

log_action(
    "fetch_invoked",
    {
        ...
        "route_no_captions_to_fallback": route_no_captions_to_fallback,
    },
)

def _classify_pending_entry(entry: dict[str, object | None]) -> tuple[str, str | None]:
    terminal_reason = _terminal_failure_reason(entry)
    if terminal_reason:
        return "terminal", terminal_reason
    is_live_content = bool(entry.get("is_live_content"))
    upload_status = str(entry.get("upload_status") or "").strip().lower()
    if is_live_content or upload_status in {"live", "live_stream", "premiere"}:
        return "transcript_fallback", "live"
    has_captions = entry.get("has_captions")
    if has_captions in (True, 1):
        return "notebooklm", "captioned"
    if has_captions in (False, 0):
        if route_no_captions_to_fallback:
            return "transcript_fallback", "no_captions"
        return "notebooklm", "no_captions"
    return "notebooklm", "unknown"
```

- [ ] **Step 4: Run the test again and confirm it passes**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_csf_source_fetch_timing.py -k no_captions_to_fallback -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add P:\\\\\\packages/yt-is/bin/csf-source P:\\\\\\packages/yt-is/tests/test_csf_source_fetch_timing.py
git commit -m "feat: add no-caption routing switch"
```

### Task 2: Add the route-split ladder scenario

**Files:**
- Modify: `P:\\\\\\packages/yt-is/csf/load_ladder.py`
- Modify: `P:\\\\\\packages/yt-is/bin/csf-load-ladder`
- Test: `P:\\\\\\packages/yt-is/tests/test_load_ladder.py`

- [ ] **Step 1: Write the failing test**

```python
def test_default_load_ladder_scenarios_include_no_caption_route_split():
    names = [scenario.name for scenario in default_load_ladder_scenarios()]
    assert "route_no_captions_to_fallback" in names

def test_build_fallback_benchmark_command_keeps_worker_state_override():
    command = build_fallback_benchmark_command(
        python_executable="python",
        fallback_benchmark_script=Path("P:\\\\\\packages/yt-is/bin/csf-fallback-crossover-benchmark"),
        trace_root=Path("P:\\\\\\packages/yt-is/.logs/worker_count_trials"),
        cohort_json=Path("P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/cohort.json"),
        output_root=Path("P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/route_test"),
        source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
        workers=2,
        limit=10,
        batch_size=10,
        policy="notebooklm_only_30s",
        worker_state_root=Path("P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/worker_states"),
        preserve_worker_state_root=False,
    )
    assert "--worker-state-root" in command
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_load_ladder.py -q`
Expected: fail because the route scenario is not present yet.

- [ ] **Step 3: Implement the route scenario**

```python
LadderScenario(
    name="route_no_captions_to_fallback",
    description="Route no-caption items directly to transcript fallback to measure source-shape split behavior.",
    env_overrides={"YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK": "true"},
)
```

Keep the existing baseline scenario as the comparison point.

- [ ] **Step 4: Run the test again and confirm it passes**

Run: `python -m pytest $CLAUDE_PLUGIN_ROOT/tests\test_load_ladder.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add P:\\\\\\packages/yt-is/csf/load_ladder.py P:\\\\\\packages/yt-is/bin/csf-load-ladder P:\\\\\\packages/yt-is/tests/test_load_ladder.py
git commit -m "feat: add no-caption routing benchmark scenario"
```

### Task 3: Run the route benchmark and inspect the summary

**Files:**
- Use: `P:\\\\\\packages/yt-is/bin/csf-load-ladder`
- Inspect: `P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/benchmark_summary.json`

- [ ] **Step 1: Dry-run the ladder**

Run:
`python P:\\\\\\packages/yt-is/bin/csf-load-ladder --dry-run --limit 10 --workers 2 --scenarios baseline,route_no_captions_to_fallback`

Expected:
- One baseline command.
- One route-split command with `YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK=true`.

- [ ] **Step 2: Run the real benchmark**

Run:
`python P:\\\\\\packages/yt-is/bin/csf-load-ladder --limit 10 --workers 2 --scenarios baseline,route_no_captions_to_fallback`

Expected:
- Two scenario directories under `.logs/load_ladder_benchmark/`.
- A combined summary at `.logs/load_ladder_benchmark/benchmark_summary.json`.

- [ ] **Step 3: Compare the outputs**

Check:
- elapsed time
- success / fail counts
- `worker_idle_wait_s`
- `add_elapsed_s`
- `cleanup_elapsed_s`
- `content_fetch_status_counts`

Expected:
- If the route split is useful, the no-caption route scenario should show less idle time or better throughput than baseline.
- If it is not useful, the benchmark should make that plain without changing the default path.

- [ ] **Step 4: Commit the benchmark output only if we need a durable record**

If the user wants the result documented in-repo, save the conclusion in docs first and keep the runtime logs outside git.

