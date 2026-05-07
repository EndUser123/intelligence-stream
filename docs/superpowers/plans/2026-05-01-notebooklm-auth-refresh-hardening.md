# NotebookLM Auth Refresh Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make forced NotebookLM auth refreshes fail closed if they touch the shared default `chrome-profile`, then rerun the auth smoke against the pinned `notebooklm-mcp-cli` `0.6.2` build.

**Architecture:** Keep known `ytis-*` worker refreshes inside `csf.nlm_worker_auth.refresh_source_profile()`, which launches the dedicated family CDP browser root and calls `nlm login --profile <source> --provider openclaw --cdp-url ... --force`. Add a Windows process guard that detects any Chrome process using `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile` during noninteractive refresh, stops newly-created offenders, restores the source-profile snapshot, and returns failure. Do not add another fallback auth route.

**Tech Stack:** Python 3.14, pytest, PowerShell `Get-CimInstance Win32_Process`, `notebooklm-mcp-cli` pinned to GitHub commit `3711e782cfa63db948bd34f9ae6e97210821223c`.

---

## Current Evidence

- `nlm --version` reports `0.6.2`.
- Profile checks pass for at least `ytis-pro-worker-01` and `ytis-free2-worker-01`.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=5` completed a short smoke but did not hit the forced-refresh marker.
- `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=1` touched `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`, so the stress drill is invalid.
- Stop condition: do not rerun benchmarks until this plan is implemented and the process guard is passing.

## Files

- Modify: `P:\\packages/yt-is/csf/nlm_worker_auth.py`
- Modify: `P:\\packages/yt-is/tests/test_nlm_worker_auth.py`
- Modify: `P:\\packages/yt-is/docs/operations/notebooklm-auth-robustness-test-plan.md`
- Optional cleanup after verification: `P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v1`

## Task 1: Add A Default Chrome Profile Process Guard

**Files:**
- Modify: `P:\\packages/yt-is/csf/nlm_worker_auth.py`
- Test: `P:\\packages/yt-is/tests/test_nlm_worker_auth.py`

- [ ] **Step 1: Add failing tests**

Append these tests near the existing `refresh_source_profile` tests in `tests/test_nlm_worker_auth.py`.

```python
def test_refresh_source_profile_fails_closed_when_default_chrome_profile_appears(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-pro-worker-01", "a.hominidae@gmail.com", "fresh-pro")
    before_metadata = (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8")
    before_cookies = (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8")
    pid_snapshots = iter([set(), {12345}])
    stopped_pids: list[int] = []

    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_for_root", lambda browser_root: None)
    monkeypatch.setattr(nlm_worker_auth, "_mark_browser_profile_clean", lambda browser_root, profile: None)
    monkeypatch.setattr(nlm_worker_auth, "_wait_for_cdp", lambda port, timeout_s=20.0: True)
    monkeypatch.setattr(nlm_worker_auth, "_close_cdp_noise_tabs", lambda port: 0)
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(nlm_worker_auth, "_chrome_pids_for_root", lambda browser_root: next(pid_snapshots))
    monkeypatch.setattr(nlm_worker_auth, "_stop_chrome_pids", lambda pids: stopped_pids.extend(sorted(pids)))

    def fake_run(cmd, **kwargs):
        assert "--provider" in cmd
        assert "--cdp-url" in cmd
        return subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", "")

    monkeypatch.setattr(nlm_worker_auth.subprocess, "run", fake_run)

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[0], timeout_s=1)

    assert ok is False
    assert stopped_pids == [12345]
    assert (root / "ytis-pro-worker-01" / "metadata.json").read_text(encoding="utf-8") == before_metadata
    assert (root / "ytis-pro-worker-01" / "cookies.json").read_text(encoding="utf-8") == before_cookies
```

Add this second test to prove existing default Chrome processes also fail the noninteractive refresh before mutation.

```python
def test_refresh_source_profile_refuses_existing_default_chrome_profile_in_noninteractive_mode(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    _write_profile(root, "ytis-free1-worker-01", "troup.hominidae@gmail.com", "fresh-free")
    popen_calls: list[object] = []

    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    monkeypatch.setattr(nlm_worker_auth, "DEFAULT_PROFILE_ROOT", root)
    monkeypatch.setattr(nlm_worker_auth, "_chrome_pids_for_root", lambda browser_root: {999})
    monkeypatch.setattr(nlm_worker_auth.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append(args))

    ok = nlm_worker_auth.refresh_source_profile(nlm_worker_auth.DEFAULT_FAMILIES[1], timeout_s=1)

    assert ok is False
    assert popen_calls == []
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest P:\\packages/yt-is/tests/test_nlm_worker_auth.py -q -k "default_chrome_profile"
```

Expected before implementation: both tests fail because `_chrome_pids_for_root` and `_stop_chrome_pids` do not exist.

- [ ] **Step 3: Implement the guard helpers**

In `csf/nlm_worker_auth.py`, add this constant near `DEFAULT_PROFILE_ROOT`.

```python
DEFAULT_NLM_CHROME_PROFILE_ROOT = Path.home() / ".notebooklm-mcp-cli" / "chrome-profile"
```

Add these helpers after `_chrome_executable()`.

```python
def _is_noninteractive_auth() -> bool:
    value = os.getenv("YTIS_NLM_AUTH_NONINTERACTIVE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _chrome_pids_for_root(browser_root: str | Path) -> set[int]:
    if os.name != "nt" or not browser_root:
        return set()
    root = str(browser_root)
    ps = (
        "$root = "
        + _ps_single_quote(root)
        + "; "
        + "$matches = Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
        + "Where-Object { $_.CommandLine -like \"*$root*\" }; "
        + "$matches | ForEach-Object { $_.ProcessId }"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if res.returncode != 0:
        return set()
    pids: set[int] = set()
    for line in (res.stdout or "").splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def _stop_chrome_pids(pids: set[int]) -> None:
    if os.name != "nt" or not pids:
        return
    pid_list = ",".join(str(pid) for pid in sorted(pids))
    ps = (
        "$pids = @("
        + pid_list
        + "); "
        + "$pids | ForEach-Object { "
        + "$p = Get-Process -Id $_ -ErrorAction SilentlyContinue; "
        + "if ($p) { [void]$p.CloseMainWindow() } "
        + "}; "
        + "Start-Sleep -Seconds 2; "
        + "$pids | ForEach-Object { "
        + "$p = Get-Process -Id $_ -ErrorAction SilentlyContinue; "
        + "if ($p -and -not $p.HasExited) { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } "
        + "}"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20, check=False)
```

- [ ] **Step 4: Wire the guard into `refresh_source_profile()`**

At the start of the CDP branch in `refresh_source_profile()`, after `use_cdp` is calculated and before `_stop_chrome_for_root(...)`, add:

```python
    default_profile_pids_before: set[int] = set()
    if _is_noninteractive_auth():
        default_profile_pids_before = _chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT)
        if default_profile_pids_before:
            if snapshot is not None:
                _restore_profile_state(profile_root, family.source_profile, snapshot)
            return False
```

Immediately after the `subprocess.run(_nlm_command(...))` block and before computing `success`, add:

```python
    default_profile_pids_after: set[int] = set()
    if _is_noninteractive_auth():
        default_profile_pids_after = _chrome_pids_for_root(DEFAULT_NLM_CHROME_PROFILE_ROOT)
        new_default_profile_pids = default_profile_pids_after - default_profile_pids_before
        if new_default_profile_pids:
            _stop_chrome_pids(new_default_profile_pids)
            if snapshot is not None:
                _restore_profile_state(profile_root, family.source_profile, snapshot)
            return False
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest P:\\packages/yt-is/tests/test_nlm_worker_auth.py -q -k "refresh_source_profile or default_chrome_profile"
```

Expected: all selected tests pass.

## Task 2: Prove `nlm_batch` Fails Closed When The Guard Trips

**Files:**
- Modify: `P:\\packages/yt-is/tests/test_nlm_batch.py`

- [ ] **Step 1: Add the failing test**

Add this test to `TestAuthAutoLogin` near the forced-refresh tests.

```python
def test_ensure_nlm_auth_forced_refresh_fails_when_source_profile_guard_rejects_default_chrome(self, monkeypatch):
    """Forced refresh must stop when the account-family CDP guard reports default chrome-profile usage."""
    import subprocess

    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "ytis-pro-worker-02")
    monkeypatch.setenv("YTIS_NLM_AUTH_NONINTERACTIVE", "1")
    monkeypatch.setenv("YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS", "1")
    called: list[list[str]] = []

    def mock_run(cmd, **kwargs):
        called.append(cmd)
        if cmd == ["nlm", "login", "--check", "--profile", "ytis-pro-worker-02"]:
            return subprocess.CompletedProcess(cmd, 0, "Account: a.hominidae@gmail.com\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    with mock.patch("csf.nlm_batch.refresh_source_profile", return_value=False):
        with mock.patch("csf.nlm_batch.subprocess.run", side_effect=mock_run):
            result = nlm_batch._ensure_nlm_auth()

    assert result is False
    assert called == [
        ["nlm", "login", "--check", "--profile", "ytis-pro-worker-02"],
        ["nlm", "login", "--check", "--profile", "ytis-pro-worker-02"],
    ]
```

- [ ] **Step 2: Run the focused test**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest P:\\packages/yt-is/tests/test_nlm_batch.py -q -k "forced_refresh_fails"
```

Expected: pass after Task 1, because `_refresh_nlm_auth_session()` already returns `False` when `refresh_source_profile()` returns `False`.

## Task 3: Add Operational Guardrails To The Runbook

**Files:**
- Modify: `P:\\packages/yt-is/docs/operations/notebooklm-auth-robustness-test-plan.md`

- [ ] **Step 1: Patch the CLI update note**

Add this sentence to the `NotebookLM CLI Update Note` section:

```markdown
- Even on `0.6.2`, benchmark validation must still watch for `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`; the local guard treats that profile as invalid during noninteractive forced refresh.
```

- [ ] **Step 2: Patch Phase 4 pass criteria**

Add this bullet to `Pass criteria`:

```markdown
- If `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=1` is used, logs must show `nlm_auth_forced_refresh_scheduled`, `nlm_login_started`, and either `nlm_auth_refreshed` or a fail-closed `nlm_auth_failed` without any default `chrome-profile` process.
```

- [ ] **Step 3: Patch the invalid-run notes**

Add this short note near the existing invalid run evidence:

```markdown
The `pro_free_auth_stress_v1` drill is invalid if present in `.logs`; it was stopped after the default NotebookLM `chrome-profile` was touched during forced refresh. Do not use it as auth evidence.
```

## Task 4: Verify Locally Without Starting A Long Benchmark

**Files:**
- No code edits.

- [ ] **Step 1: Check no invalid NotebookLM auth processes are alive**

Run:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'csf-sharded-lane-series|csf-nlm-worker-auth|nlm login --force|\.notebooklm-mcp-cli\\chrome-profile|remote-debugging-port=9222' } |
  Select-Object ProcessId, Name, CommandLine
```

Expected: no `csf-sharded-lane-series`, no unprofiled `nlm login --force`, and no Chrome using `C:\Users\brsth\.notebooklm-mcp-cli\chrome-profile`.

- [ ] **Step 2: Run static and unit gates**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
pytest P:\\packages/yt-is/tests/test_nlm_worker_auth.py P:\\packages/yt-is/tests/test_nlm_batch.py -q -k "auth or refresh_source_profile or default_chrome_profile"
python -m py_compile P:\\packages/yt-is/csf/nlm_worker_auth.py P:\\packages/yt-is/csf/nlm_batch.py P:\\packages/yt-is/tests/test_nlm_worker_auth.py P:\\packages/yt-is/tests/test_nlm_batch.py
```

Expected: selected tests pass and `py_compile` exits `0`.

- [ ] **Step 3: Run profile sync**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
python P:\\packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
```

Expected: exits `0` and prints `synced worker auth profiles`.

## Task 5: Rerun The Minimal Forced-Refresh Drill

**Files:**
- No code edits.

- [ ] **Step 1: Remove only the invalid stress output root**

Verify the absolute path first:

```powershell
Resolve-Path P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v1
```

If the resolved path is exactly under `$CLAUDE_PLUGIN_ROOT/.logs\sharded_lane_series`, remove it:

```powershell
Remove-Item -LiteralPath P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v1 -Recurse -Force
```

- [ ] **Step 2: Run the drill**

Run:

```powershell
$env:PYTHONPATH = 'P:\\packages\yt-is'
$env:YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS = '1'
python P:\\packages/yt-is/bin/csf-nlm-worker-auth --no-backup sync
python P:\\packages/yt-is/bin/csf-sharded-lane-series `
  --lane-config P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_lanes.json `
  --output-root P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v2 `
  --cohort-json P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v2/cohort.json `
  --limit 1 `
  --batch-size 1 `
  --reusable-pipeline-mode serial
Remove-Item Env:\YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS
```

- [ ] **Step 3: Check evidence**

Run:

```powershell
rg -n "nlm_auth_forced_refresh_scheduled|nlm_login_started|nlm_login_completed|nlm_auth_refreshed|nlm_auth_failed|chrome-profile" P:\\packages/yt-is/.logs/sharded_lane_series/pro_free_auth_stress_v2
```

Expected:

- `nlm_auth_forced_refresh_scheduled` appears.
- `nlm_login_started` appears with `notebooklm_profile` set to the worker profile.
- `chrome-profile` does not appear in logs.
- Process guard query from Task 4 still shows no Chrome using the default profile.

## Task 6: Only Then Return To The Short Smoke And Long Soak

**Files:**
- No code edits.

- [ ] **Step 1: Run the short smoke with cadence 5**

Run the Phase 4 command in `P:\\packages/yt-is/docs/operations/notebooklm-auth-robustness-test-plan.md`, using a fresh root such as `pro_free_auth_forced_smoke_v2`.

- [ ] **Step 2: Promote to long soak only if the smoke is clean**

Use `YTIS_NLM_AUTH_FORCE_REFRESH_EVERY_CHECKS=5` for auth evidence, or leave it unset for throughput-only evidence. Do not use `1` for the long soak unless the explicit test is browser churn.

## Self-Review

- Spec coverage: This plan covers the observed invalid default-profile launch, the pinned `0.6.2` context, unit tests, docs, process cleanup, minimal stress rerun, and promotion back to soak testing.
- Placeholder scan: No `TBD`, `TODO`, or unspecified test steps remain.
- Type consistency: Helper names are consistent across tests and implementation: `_chrome_pids_for_root`, `_stop_chrome_pids`, `_is_noninteractive_auth`, and `DEFAULT_NLM_CHROME_PROFILE_ROOT`.
