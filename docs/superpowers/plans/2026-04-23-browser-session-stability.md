# Persistent NotebookLM Browser Session Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the NotebookLM DOM/readiness test run against a dedicated persistent browser profile so authentication is stable and preflight fails fast on `Request access`.

**Architecture:** Centralize browser-session settings in `csf/nlm_config.py`. Move browser ownership into `bin/nlm-playwright`, which will bootstrap and run a dedicated persistent NotebookLM profile rooted outside the human Chrome profile. Refactor `csf/nlm_scraper.py` to consume an explicit browser-session config and page/context object instead of discovering, cloning, or reseeding the human Chrome profile. Keep CLI auth separate and keep CDP only as a manual fallback/debug path.

**Tech Stack:** Python 3.11, Playwright, pytest, Windows filesystem paths, NotebookLM CLI.

---

### Task 1: Add explicit browser-session config

**Files:**
- Modify: `P:/packages/yt-is/csf/nlm_config.py`
- Test: `P:/packages/yt-is/tests/test_nlm_config.py`

- [ ] **Step 1: Write the failing test**

```python
from csf.nlm_config import get_nlm_config


def test_browser_session_defaults_are_explicit():
    cfg = get_nlm_config()
    assert cfg.nlm_browser_mode == "persistent"
    assert cfg.nlm_browser_profile_root.endswith(r".browser\notebooklm")
    assert cfg.nlm_browser_executable.endswith(r"chrome.exe")
    assert cfg.nlm_browser_bootstrap_headless is False
    assert cfg.nlm_browser_start_timeout_ms == 30000
    assert cfg.nlm_preflight_url_timeout_ms == 60000
    assert cfg.nlm_preflight_ui_timeout_ms == 15000
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_config.py -q
```
Expected: fail because the new browser-session config fields do not exist yet.

- [ ] **Step 3: Add the minimal config implementation**

```python
@dataclass(frozen=True)
class NLMConfig:
    notebook_batch_size: int = 50
    notebook_source_cap: int = 50
    notebook_source_materialization_timeout_s: int = 600
    max_sources_per_notebook: int = 300
    auth_check_interval: float = 60.0
    auth_max_calls_per_window: int = 10
    auth_cooldown: float = 300.0
    nlm_browser_mode: str = "persistent"  # "persistent" | "cdp"
    nlm_browser_profile_root: str = r"P:\packages\yt-is\.browser\notebooklm"
    nlm_browser_executable: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    nlm_browser_channel: str = "chrome"
    nlm_browser_bootstrap_headless: bool = False
    nlm_browser_start_timeout_ms: int = 30000
    nlm_preflight_url_timeout_ms: int = 60000
    nlm_preflight_ui_timeout_ms: int = 15000
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_config.py -q
```
Expected: pass.

---

### Task 2: Make `nlm-playwright` the canonical browser bootstrap helper

**Files:**
- Modify: `P:/packages/yt-is/bin/nlm-playwright`
- Test: `P:/packages/yt-is/tests/test_nlm_playwright.py`

- [ ] **Step 1: Write the failing test**

```python
from csf.nlm_config import get_nlm_config


def test_browser_bootstrap_uses_dedicated_profile_root():
    cfg = get_nlm_config()
    assert cfg.nlm_browser_profile_root.endswith(r".browser\notebooklm")


def test_browser_mode_defaults_to_persistent():
    cfg = get_nlm_config()
    assert cfg.nlm_browser_mode == "persistent"
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_playwright.py -q
```
Expected: fail until the script reads the shared config and uses the dedicated profile root.

- [ ] **Step 3: Add the browser session helper behavior**

```python
def launch_persistent_browser(page_url: str, *, headless: bool, profile_root: str, executable_path: str):
    """
    Launch Playwright persistent context with the dedicated NotebookLM profile.
    Bootstrap mode forces headed browsing; normal runs may still be headed or headless
    depending on config, but they always reuse the same profile_root.
    """
```

```python
def bootstrap_auth(page_url: str) -> None:
    """
    Open NotebookLM in the dedicated profile, stop if the browser lands on
    Google sign-in or /accessrequest/, and leave the browser open for manual login.
    """
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_playwright.py -q
```
Expected: pass.

---

### Task 3: Refactor the DOM scraper to consume an explicit browser session

**Files:**
- Modify: `P:/packages/yt-is/csf/nlm_scraper.py`
- Modify: `P:/packages/yt-is/csf/nlm_config.py`
- Test: `P:/packages/yt-is/tests/test_nlm_scraper.py`

- [ ] **Step 1: Write the failing test**

```python
from csf.nlm_config import get_nlm_config
from csf.nlm_scraper import NLMIndustrialScraper


def test_scraper_requires_explicit_browser_config():
    cfg = get_nlm_config()
    scraper = NLMIndustrialScraper(headless=False, browser_cfg=cfg)
    assert scraper.browser_cfg.nlm_browser_mode == "persistent"
```

```python
def test_preflight_rejects_request_access():
    scraper = NLMIndustrialScraper(headless=False, browser_cfg=get_nlm_config())
    assert scraper._looks_like_request_access("Request access | Close | Google apps")
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_scraper.py -q
```
Expected: fail until the scraper accepts explicit browser config and stops trying to own the human Chrome profile.

- [ ] **Step 3: Replace profile discovery/cloning with the explicit browser session**

```python
class NLMIndustrialScraper:
    def __init__(self, headless: bool = True, *, browser_cfg=None, readiness_matrix: bool = False, readiness_probe_interval_s: float = 1.0, readiness_probe_timeout_s: float = 600.0):
        self.headless = headless
        self.browser_cfg = browser_cfg or get_nlm_config()
        self._readiness_matrix = readiness_matrix
        self._readiness_probe_interval_s = float(readiness_probe_interval_s)
        self._readiness_probe_timeout_s = float(readiness_probe_timeout_s)
```

```python
def _preflight_browser(self, page) -> bool:
    """
    Hard gate:
    - fail on accounts.google.com
    - fail on /accessrequest/
    - fail if the Sources shell is not visible
    """
```

Remove or retire the code paths that:
- read Chrome `Local State`
- clone `Profile 2`
- reseed from the human Chrome profile
- fall back to a copied browser profile as the primary path

- [ ] **Step 4: Run the focused test to verify it passes**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_scraper.py -q
```
Expected: pass.

---

### Task 4: Wire `csf-source` to the new browser contract

**Files:**
- Modify: `P:/packages/yt-is/bin/csf-source`
- Modify: `P:/packages/yt-is/csf/nlm_scraper.py`
- Test: `P:/packages/yt-is/tests/test_csf_source_fetch_timing.py`

- [ ] **Step 1: Write the failing test**

```python
def test_source_launcher_uses_visible_browser_for_dom_matrix():
    # The DOM/browser path must be able to bootstrap a visible session.
    assert True
```

Add an assertion around the launcher/wiring so the DOM test path does not hardcode headless mode for auth-sensitive runs.

- [ ] **Step 2: Run the focused test to verify it fails**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_csf_source_fetch_timing.py -q
```
Expected: fail or remain insufficient until the launcher passes browser config through and respects the visible bootstrap flow.

- [ ] **Step 3: Update the launcher wiring**

```python
industrial_scraper = NLMIndustrialScraper(
    headless=False,
    browser_cfg=get_nlm_config(),
)
```

For the DOM/auth path:
- use visible browser bootstrap by default
- only start readiness timing after the browser preflight passes
- keep CLI-only fetch timing unchanged

- [ ] **Step 4: Run the focused test to verify it passes**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_csf_source_fetch_timing.py -q
```
Expected: pass.

---

### Task 5: Update docs and run the browser/auth smoke test

**Files:**
- Modify: `P:/packages/yt-is/docs/operations/worker-count-trial-run-sheet.md`
- Modify: `P:/packages/yt-is/docs/operations/worker-owned-notebooks-handoff.md`
- Modify: `P:/packages/yt-is/README.md`
- Modify: `P:/packages/yt-is/CHANGELOG.md`

- [ ] **Step 1: Write the doc assertions**

Document:
- CLI auth uses `nlm login`
- browser auth uses the dedicated persistent NotebookLM profile
- `Request access` is a hard preflight failure, not a readiness failure
- CDP remains fallback/debug only
- the DOM matrix should not start until the browser preflight passes

- [ ] **Step 2: Update the run sheet with the bootstrap flow**

Add the exact bootstrap sequence:
```powershell
P:\packages\yt-is\bin\nlm-playwright bootstrap
P:\packages\yt-is\bin\csf-source --readiness-matrix --video-ids KvC7ct1UVBs,cbfnFt9lLV4,mzKV2BoSPvs
```

- [ ] **Step 3: Update the changelog**

Record that:
- browser-session ownership moved to a dedicated automation profile
- the DOM path now fails fast on sign-in/access-request pages
- the test no longer depends on cloning the human Chrome profile

- [ ] **Step 4: Run the verification slice**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_config.py P:\packages\yt-is\tests\test_nlm_playwright.py P:\packages\yt-is\tests\test_nlm_scraper.py P:\packages\yt-is\tests\test_csf_source_fetch_timing.py -q
```

Then run one manual smoke:
1. bootstrap the dedicated browser profile
2. confirm NotebookLM opens without `Request access`
3. run the 3-source matrix
4. confirm the browser session stays on NotebookLM and the readiness logs start only after preflight

---

### Task 6: Final regression check

**Files:**
- Test: `P:/packages/yt-is/tests/test_nlm_config.py`
- Test: `P:/packages/yt-is/tests/test_nlm_playwright.py`
- Test: `P:/packages/yt-is/tests/test_nlm_scraper.py`
- Test: `P:/packages/yt-is/tests/test_csf_source_fetch_timing.py`

- [ ] **Step 1: Run the full targeted slice**

Run:
```powershell
PYTHONPATH=P:\packages\yt-is python -m pytest P:\packages\yt-is\tests\test_nlm_config.py P:\packages\yt-is\tests\test_nlm_playwright.py P:\packages\yt-is\tests\test_nlm_scraper.py P:\packages\yt-is\tests\test_csf_source_fetch_timing.py -q
```

- [ ] **Step 2: Confirm the browser contract**

Verify these are true before calling the work done:
- no code path still prefers the human Chrome `Profile 2` as the primary runtime target
- no readiness timer starts on `Request access`
- `nlm-playwright` can bootstrap a dedicated persistent profile
- the DOM path can run one notebook without browser auth failure

If any of those still fail, treat it as a browser-session architecture bug, not a NotebookLM content bug.

