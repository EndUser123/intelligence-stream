#!/usr/bin/env python3
"""NotebookLM High-Fidelity Industrial Scraper via Selenium.

This module provides a high-throughput, data-efficient method for retrieving
full, word-for-word transcripts from NotebookLM by automating the web UI.

Strategy:
1. Add sources using CLI (preserving input order).
2. Map source IDs to video IDs by order (source list returns them in add order).
3. Open the NotebookLM web interface once per notebook (up to 300 sources).
4. Loop through the sidebar clicking each source and scraping the preview pane.
5. Save directly to the transcript cache.

Clears a 140k backlog in ~8-12 hours when run in parallel across terminals.
"""

import os
import sys
import time
import json
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Tuple

# Ensure csf is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

# cache write done by caller after scraper returns

try:
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options as FxOptions
    from selenium.webdriver.chrome.options import Options as ChOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.remote.webelement import WebElement
except ImportError:
    print("Error: Selenium not installed. Run 'pip install selenium'.")
    sys.exit(1)


class NLMIndustrialScraper:
    # NotebookLM Plus limit — used to detect when to clear and reuse
    MAX_SOURCES_PER_NOTEBOOK = 300

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._driver = None
        self._staging_nb_id: str | None = None
        self._source_count: int = 0
        self._consecutive_nb_create_failures: int = 0

    def _init_driver(self):
        if self._driver:
            return

        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""

        # Try ms-playwright Chrome first (nlm MCP auth lives here)
        playwright_chrome_base = os.path.join(
            appdata, "ms-playwright", "mcp-chrome-9050243"
        )
        if os.path.isdir(playwright_chrome_base):
            chrome_profile_base = playwright_chrome_base
            profile_name = "Default"
        else:
            chrome_profile_base = os.path.join(appdata, "Google", "Chrome", "User Data")
            profile_name = "default"

        try:
            opts = ChOptions()
            if self.headless:
                opts.add_argument("--headless=new")
            opts.add_argument(f"--user-data-dir={chrome_profile_base}")
            opts.add_argument(f"--profile-directory={profile_name}")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--no-sandbox")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])

            print(f"[Industrial] Using Chrome profile: {chrome_profile_base}/{profile_name}")
            self._driver = webdriver.Chrome(options=opts)
        except Exception as e:
            print(f"[Industrial] Chrome init failed ({e}), falling back to Firefox...")
            try:
                import glob as _glob
                opts = FxOptions()
                if self.headless:
                    opts.add_argument("--headless")
                ff_profile_base = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
                profiles = _glob.glob(os.path.join(ff_profile_base, "*.Profile 1*"))
                if not profiles:
                    all_profiles = _glob.glob(os.path.join(ff_profile_base, "*"))
                    profiles = [p for p in all_profiles if ".default" not in os.path.basename(p)]
                if profiles:
                    profile_path = profiles[0]
                    print(f"[Industrial] Using Firefox profile: {Path(profile_path).name}")
                    opts.add_argument("-profile")
                    opts.add_argument(profile_path)
                self._driver = webdriver.Firefox(options=opts)
            except Exception as e2:
                print(f"[Industrial] Firefox fallback also failed: {e2}")
                raise RuntimeError("Could not initialize any browser driver")

        self._driver.set_page_load_timeout(90)

    def get_source_ids(self, notebook_id: str) -> List[str]:
        """Get source IDs from notebook, in the same order they were added."""
        res = subprocess.run(
            ["nlm", "source", "list", notebook_id, "--json"],
            capture_output=True, text=True
        )
        if res.returncode != 0:
            print(f"[Industrial] CLI Error: {res.stderr}")
            return []

        try:
            sources = json.loads(res.stdout)
            if isinstance(sources, dict):
                sources = sources.get("sources", [])
            return [s["id"] for s in sources]
        except Exception as e:
            print(f"[Industrial] Parse Error: {e}")
            return []

    def _wait_for_transcript_ready(self, timeout: float = 20.0) -> Optional[str]:
        """Poll for transcript content to load after clicking a source.

        Returns body text when at least 200 chars of content are present
        (indicating transcript loaded), or None on timeout.
        """
        waited = 0.0
        interval = 0.5
        while waited < timeout:
            body = self._driver.find_element(By.TAG_NAME, "body")
            text = body.text
            # Check if we have substantial content (transcript loaded)
            long_lines = [ln for ln in text.split("\n") if len(ln) > 50]
            if sum(len(ln) for ln in long_lines) > 200:
                return text
            time.sleep(interval)
            waited += interval
        # Timeout — return whatever we have (may be empty or partial)
        return self._driver.find_element(By.TAG_NAME, "body").text

    def _extract_transcript_from_body(self, body_text: str) -> Optional[str]:
        """Extract clean transcript text from NotebookLM source preview body text."""
        if len(body_text) < 200:
            return None
        lines = body_text.split("\n")
        transcript_lines = []
        capture = False
        for line in lines:
            # Skip short lines (UI chrome)
            if len(line) > 50:
                capture = True
            if capture:
                transcript_lines.append(line)
        transcript = "\n".join(transcript_lines)
        # Clean trailing UI elements
        for ui_marker in ["Save to note", "Add to note", "View source"]:
            if ui_marker in transcript:
                transcript = transcript.split(ui_marker)[0].strip()
        if len(transcript) > 100:
            return transcript
        return None

    # --- Staging notebook management (terminal-local reuse) ---

    def _run_nlm(self, args: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
        """Run an nlm CLI command. Uses the system PATH.

        On auth errors (expired token between sessions), re-authenticates
        and retries once — matching the pattern from nlm_batch.py.
        """
        res = subprocess.run(
            ["nlm"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0:
            return res

        # Check for auth errors — retry with re-auth if token expired mid-session
        combined = (res.stderr or "") + (res.stdout or "")
        is_auth_error = any(
            kw in combined
            for kw in ["Authentication Error", "authentication error", "Auth Error", "auth error"]
        )
        if is_auth_error:
            login = subprocess.run(
                ["nlm", "login", "--force"],
                capture_output=True, text=True, timeout=120,
            )
            if login.returncode == 0:
                res = subprocess.run(
                    ["nlm"] + args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
        return res

    def _create_staging_notebook(self) -> str | None:
        """Create a new staging notebook and return its ID.

        Retries up to 3 times, re-authenticating before each retry, to handle
        both auth expiry and transient server-side failures.
        """
        name = f"staging_{int(time.time())}"
        for attempt in range(3):
            res = self._run_nlm(["notebook", "create", name])
            if res.returncode == 0:
                self._consecutive_nb_create_failures = 0
                break
            print(f"[Industrial] Notebook create attempt {attempt + 1} failed: {res.stderr or '(empty)'}")
            if attempt < 2:
                print(f"[Industrial] Re-authenticating before retry...")
                login = subprocess.run(
                    ["nlm", "login", "--force"],
                    capture_output=True, text=True, timeout=120,
                )
                if login.returncode != 0:
                    print(f"[Industrial] Re-auth failed: {login.stderr}")
                    break
        else:
            print("[Industrial] Notebook create failed after 3 attempts")
            self._consecutive_nb_create_failures += 1
            return None

        # Parse "ID: <uuid>" from stdout
        for line in res.stdout.split("\n"):
            if "ID:" in line:
                return line.split("ID:")[-1].strip()
        # Fallback: last line if format is unexpected
        return res.stdout.strip() or None

    # Sub-batch size for CLI source adds — keeps NLM responsive and avoids
    # overwhelming the backend when the staging notebook is fresh.
    _CLI_SUBBATCH = 50

    def _add_sources_to_staging(self, video_ids: List[str]) -> List[str] | None:
        """Add YouTube sources to the staging notebook in sub-batches.

        Returns source IDs in add order, or None if any sub-batch fails.
        On failure, clears the staging notebook to prevent stale state
        contaminating the next batch.
        """
        if not self._staging_nb_id:
            return None
        all_source_ids: List[str] = []
        for i in range(0, len(video_ids), self._CLI_SUBBATCH):
            subbatch = video_ids[i : i + self._CLI_SUBBATCH]
            add_cmd = [
                "source", "add", self._staging_nb_id,
                "--wait", "--wait-timeout", "600",
            ]
            for vid in subbatch:
                add_cmd.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
            res = self._run_nlm(add_cmd, timeout=900)
            if res.returncode != 0:
                # On sub-batch failure, clear the notebook so the next call
                # starts with a fresh state instead of a corrupted one.
                print(f"[Industrial] Sub-batch add failed ({i}-{i+len(subbatch)}): {res.stderr or '(no output)'} — clearing notebook")
                self._clear_staging_notebook()
                return None
            # Get IDs for this sub-batch
            ids = self.get_source_ids(self._staging_nb_id)
            if ids is None or len(ids) == 0:
                # None = error, [] = query succeeded but notebook unexpectedly empty
                # Both indicate a bad state — clear and retry from scratch.
                self._clear_staging_notebook()
                return None
            # Source IDs returned are ordered newest-first; the newly added
            # ones are at the START of the list.  Figure out how many we
            # just added and keep only those from the front.
            added = len(subbatch)
            all_source_ids.extend(ids[:added])
        return all_source_ids

    def _clear_staging_notebook(self) -> bool:
        """Delete all sources from the staging notebook by deleting and recreating it."""
        if not self._staging_nb_id:
            return True
        res = self._run_nlm(["notebook", "delete", self._staging_nb_id, "--confirm"])
        self._staging_nb_id = None
        self._source_count = 0
        return res.returncode == 0

    def _ensure_staging_notebook(self) -> bool:
        """Ensure a staging notebook exists, creating one if needed or if at capacity."""
        if self._consecutive_nb_create_failures >= 3:
            print("[Industrial] FATAL: 3 consecutive notebook creation failures — bailing out")
            return False
        # Auth smoke test: verify nlm CLI is authenticated before attempting notebook ops.
        # On auth failure, re-auth and retry once.
        auth_ok = self._run_nlm(["notebook", "list"], timeout=30)
        if auth_ok.returncode != 0:
            combined = auth_ok.stderr + auth_ok.stdout
            if any(kw in combined for kw in ["Authentication Error", "authentication error", "Auth Error", "auth error"]):
                print("[Industrial] Auth smoke-test failed — re-authing...")
                login = subprocess.run(["nlm", "login", "--force"], capture_output=True, text=True, timeout=120)
                if login.returncode != 0:
                    print(f"[Industrial] Re-auth failed: {login.stderr}")
                    self._consecutive_nb_create_failures += 1
                    return False
                auth_ok = self._run_nlm(["notebook", "list"], timeout=30)
                if auth_ok.returncode != 0:
                    print(f"[Industrial] Auth smoke-test still failing after re-auth: {auth_ok.stderr}")
                    self._consecutive_nb_create_failures += 1
                    return False
            else:
                print(f"[Industrial] Notebook list failed (non-auth): {auth_ok.stderr}")
                self._consecutive_nb_create_failures += 1
                return False
        if self._staging_nb_id and self._source_count < self.MAX_SOURCES_PER_NOTEBOOK:
            return True
        if self._staging_nb_id:
            print(f"[Industrial] Staging notebook at capacity ({self._source_count}), clearing...")
            self._clear_staging_notebook()
        nb_id = self._create_staging_notebook()
        if not nb_id:
            return False
        self._staging_nb_id = nb_id
        self._source_count = 0
        print(f"[Industrial] Staging notebook ready: {nb_id}")
        return True

    def scrape_with_staging(
        self,
        video_ids: List[str],
    ) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Scrape transcripts using a terminal-local staging notebook.

        Reuses a single staging notebook across calls, adding sources until
        approaching the 300-limit, then clearing and recreating.

        Args:
            video_ids: YouTube video IDs in the order they should be added.

        Returns:
            dict mapping video_id -> (success, transcript_text, error)
        """
        if not self._ensure_staging_notebook():
            return {vid: (False, None, "staging notebook unavailable") for vid in video_ids}

        # Check how many we can add before hitting the limit
        remaining = self.MAX_SOURCES_PER_NOTEBOOK - self._source_count
        if len(video_ids) > remaining:
            # Add as many as will fit, then recursively handle the rest
            batch_ids = video_ids[:remaining] if remaining > 0 else []
            rest = video_ids if remaining == 0 else video_ids[remaining:]
        else:
            batch_ids = video_ids
            rest = []

        # Map video_ids -> source_ids by position
        vid_to_src: Dict[str, str] = {}
        results: Dict[str, Tuple[bool, Optional[str], Optional[str]]] = {}

        if batch_ids:
            # Add sources to staging notebook
            source_ids = self._add_sources_to_staging(batch_ids)
            if not source_ids:
                return {vid: (False, None, "source add failed") for vid in batch_ids}
            # _add_sources_to_staging returns ALL source IDs in the notebook,
            # ordered newest-first. The newly added sources are at the START
            # of the list (indices 0 through len(batch_ids)-1).
            added = len(batch_ids)
            self._source_count += added
            new_source_ids = source_ids[:added]
            for i, vid in enumerate(batch_ids):
                if i < len(new_source_ids):
                    vid_to_src[vid] = new_source_ids[i]
            # Scrape the newly added sources (init driver first)
            self._init_driver()
            results.update(self._scrape_sources(vid_to_src))

        # Iteratively process any remaining videos (overflow into next notebook)
        while rest:
            if not self._ensure_staging_notebook():
                # Record failures for all remaining videos
                for vid in rest:
                    results[vid] = (False, None, "staging notebook unavailable")
                break
            remaining = self.MAX_SOURCES_PER_NOTEBOOK - self._source_count
            if remaining == 0:
                # At capacity; clear and recreate notebook to get fresh headroom
                self._clear_staging_notebook()
                self._ensure_staging_notebook()
                remaining = self.MAX_SOURCES_PER_NOTEBOOK - self._source_count
            batch_ids = rest[:remaining]
            rest = rest[remaining:]
            source_ids = self._add_sources_to_staging(batch_ids)
            if not source_ids:
                for vid in batch_ids:
                    results[vid] = (False, None, "source add failed")
                continue
            # Newly added sources are at the START (newest-first ordering)
            added = len(batch_ids)
            self._source_count += added
            new_source_ids = source_ids[:added]
            vid_to_src = {}
            for i, vid in enumerate(batch_ids):
                if i < len(new_source_ids):
                    vid_to_src[vid] = new_source_ids[i]
            self._init_driver()
            results.update(self._scrape_sources(vid_to_src))

        # Batch success summary
        total = len(results)
        succeeded = sum(1 for ok, _, _ in results.values() if ok)
        if total > 0:
            pct = succeeded / total * 100
            print(f"[Industrial] Batch complete: {succeeded}/{total} succeeded ({pct:.0f}%)")
        return results

    def _scrape_sources(
        self,
        vid_to_src: Dict[str, str],
    ) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Scrape a set of already-mapped video_id -> source_id pairs from the open notebook."""
        if not self._staging_nb_id:
            return {vid: (False, None, "no staging notebook") for vid in vid_to_src}

        url = f"https://notebooklm.google.com/notebook/{self._staging_nb_id}"
        self._driver.get(url)
        time.sleep(15)

        # Click Sources tab
        try:
            tabs = self._driver.find_elements(By.CSS_SELECTOR, '[role="tab"]')
            for tab in tabs:
                if tab.text.strip() == "Sources":
                    self._driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", tab
                    )
                    time.sleep(0.3)
                    self._driver.execute_script("arguments[0].click();", tab)
                    time.sleep(1)
                    print("[Industrial] Switched to Sources tab")
                    break
        except Exception as e:
            print(f"[Industrial] Could not click Sources tab: {e}")

        # Build button map ONCE before any clicking — DOM state is stable at this
        # point.  Each video's button is located by matching its source_id against
        # the button's aria-label (which contains the YouTube URL for video sources).
        # This replaces the fragile positional indexing that broke after the first
        # click shifted the DOM.
        buttons = self._driver.find_elements(By.TAG_NAME, "button")
        source_buttons = [
            btn
            for btn in buttons
            if btn.get_attribute("aria-label")
            and len(btn.get_attribute("aria-label") or "") > 20
            and not btn.text.strip()
        ]
        # Map: source_id -> button element
        button_by_source: Dict[str, WebElement] = {}
        for btn in source_buttons:
            label = btn.get_attribute("aria-label") or ""
            for vid, source_id in vid_to_src.items():
                if source_id in label or f"youtube.com/watch?v={vid}" in label:
                    button_by_source[vid] = btn
                    break

        results: Dict[str, Tuple[bool, Optional[str], Optional[str]]] = {}

        for vid, source_id in vid_to_src.items():
            idx = list(vid_to_src.keys()).index(vid) + 1
            print(f"[{idx}/{len(vid_to_src)}] Scraping: {vid[:20]}...", end=" ", flush=True)

            did_click = False
            try:
                target_btn = button_by_source.get(vid)
                if not target_btn:
                    # Fallback: positional index from initial stable scan
                    src_idx = list(vid_to_src.keys()).index(vid)
                    if src_idx < len(source_buttons):
                        target_btn = source_buttons[src_idx]

                if not target_btn:
                    # Stale-element recovery: re-scan the DOM to find buttons, then retry.
                    # This handles the case where a prior stale-element exception left
                    # driver on the Sources tab with stale button references.
                    time.sleep(2)
                    all_buttons = self._driver.find_elements(By.TAG_NAME, "button")
                    source_buttons_fresh = [
                        b for b in all_buttons
                        if b.get_attribute("aria-label")
                        and len(b.get_attribute("aria-label") or "") > 20
                        and not b.text.strip()
                    ]
                    button_by_source_fresh: Dict[str, WebElement] = {}
                    for b in source_buttons_fresh:
                        label = b.get_attribute("aria-label") or ""
                        if source_id in label or f"youtube.com/watch?v={vid}" in label:
                            button_by_source_fresh[vid] = b
                            break
                    target_btn = button_by_source_fresh.get(vid)
                    if not target_btn:
                        src_idx = list(vid_to_src.keys()).index(vid)
                        if src_idx < len(source_buttons_fresh):
                            target_btn = source_buttons_fresh[src_idx]

                if not target_btn:
                    results[vid] = (False, None, "source button not found")
                    print("✗ button not found")
                    continue

                self._driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", target_btn
                )
                time.sleep(0.3)
                self._driver.execute_script("arguments[0].click();", target_btn)
                did_click = True
                print("✓ ", end="", flush=True)
                body_text = self._wait_for_transcript_ready(timeout=20.0)
                transcript = self._extract_transcript_from_body(body_text)

                if transcript:
                    results[vid] = (True, transcript, None)
                    print(f"{len(transcript)} chars")
                else:
                    results[vid] = (False, None, "content too short or empty")
                    print("✗ too short")

            except Exception as e:
                error_msg = str(e)
                # Check if this is a stale element reference during click —
                # if so, attempt one recovery click before giving up.
                if "stale element" in error_msg.lower():
                    time.sleep(2)
                    try:
                        # Re-locate the button from current DOM state
                        fresh_buttons = self._driver.find_elements(By.TAG_NAME, "button")
                        fresh_source_buttons = [
                            b for b in fresh_buttons
                            if b.get_attribute("aria-label")
                            and len(b.get_attribute("aria-label") or "") > 20
                            and not b.text.strip()
                        ]
                        for b in fresh_source_buttons:
                            label = b.get_attribute("aria-label") or ""
                            if source_id in label or f"youtube.com/watch?v={vid}" in label:
                                self._driver.execute_script(
                                    "arguments[0].scrollIntoView({block:'center'});", b
                                )
                                time.sleep(0.3)
                                self._driver.execute_script("arguments[0].click();", b)
                                did_click = True
                                body_text = self._wait_for_transcript_ready(timeout=20.0)
                                transcript = self._extract_transcript_from_body(body_text)
                                if transcript:
                                    results[vid] = (True, transcript, None)
                                    print(f"{len(transcript)} chars (stale recovery)")
                                else:
                                    results[vid] = (False, None, "content too short or empty")
                                    print("✗ too short")
                                break
                        else:
                            results[vid] = (False, None, "source button not found after stale recovery")
                            print("✗ button not found after stale recovery")
                    except Exception:
                        results[vid] = (False, None, error_msg)
                        print(f"✗ {error_msg}")
                else:
                    results[vid] = (False, None, error_msg)
                    print(f"✗ {error_msg}")

            finally:
                # Always navigate back if we left the Sources tab, so the next
                # iteration's button lookups start from a stable page state.
                if did_click:
                    # Only click Back if we're actually on a transcript/source page.
                    # If we're already on the Sources tab, clicking Back would
                    # navigate backwards OUT of the Sources list — the opposite of
                    # what we want after a stale-element exception.
                    current_url = self._driver.current_url
                    # Distinguish Sources tab list page from transcript/source-detail page.
                    # Sources tab: .../notebook/{nb}/source/{srcId}       (no trailing segment)
                    # Transcript page: .../notebook/{nb}/source/{srcId}/hash (has trailing segment)
                    # Both contain "/source/", so we check for an additional path segment.
                    on_transcript_page = (
                        "/source/" in current_url
                        and len(current_url.split("/source/")[-1].split("/")) >= 2
                    )
                    if not on_transcript_page:
                        # We're on Sources tab (or any non-source page) — no back-nav needed
                        pass
                    else:
                        try:
                            for b in self._driver.find_elements(By.TAG_NAME, "button"):
                                if b.get_attribute("aria-label") == "Back":
                                    self._driver.execute_script("arguments[0].click();", b)
                                    time.sleep(1.5)
                                    break
                        except Exception:
                            # Fallback: reload the notebook to get back to Sources tab
                            self._driver.get(
                                f"https://notebooklm.google.com/notebook/{self._staging_nb_id}"
                            )
                            time.sleep(3)

        return results

    # --- Original per-notebook scrape (kept for explicit --notebook usage) ---

    def scrape_notebook(
        self,
        notebook_id: str,
        video_ids: List[str],
    ) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Scrape transcripts from all sources in a notebook.

        Args:
            notebook_id: The NotebookLM notebook ID. Pass "staging" (or any falsy
                         value when used via scrape_with_staging) to use the
                         terminal-local staging notebook instead.
            video_ids: The input video IDs in the SAME ORDER they were added.
                       Source IDs are mapped to video IDs by position.

        Returns:
            dict mapping video_id -> (success, transcript_text, error)
        """
        # Auto-use staging notebook when called without an explicit notebook
        # (e.g. from batch.py which doesn't pass notebook_id directly)
        if not notebook_id or notebook_id == "staging":
            return self.scrape_with_staging(video_ids)

        source_ids = self.get_source_ids(notebook_id)
        if not source_ids:
            return {vid: (False, None, "no sources found") for vid in video_ids}

        vid_to_src: Dict[str, str] = {}
        for i, vid in enumerate(video_ids):
            if i < len(source_ids):
                vid_to_src[vid] = source_ids[i]

        missing = [v for v in video_ids if v not in vid_to_src]
        if missing:
            print(f"[Industrial] Warning: {len(missing)} video IDs have no source mapping")

        self._init_driver()
        url = f"https://notebooklm.google.com/notebook/{notebook_id}"
        print(f"[Industrial] Opening {url}...")
        self._driver.get(url)

        # Initial wait for heavy UI load
        time.sleep(15)

        # Click the Sources tab to reveal the source list
        try:
            tabs = self._driver.find_elements(By.CSS_SELECTOR, '[role="tab"]')
            for tab in tabs:
                if tab.text.strip() == 'Sources':
                    self._driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                    time.sleep(0.3)
                    self._driver.execute_script("arguments[0].click();", tab)
                    # Wait for tab content to render (not transcript — just UI switch)
                    time.sleep(1)
                    print("[Industrial] Switched to Sources tab")
                    break
        except Exception as e:
            print(f"[Industrial] Could not click Sources tab: {e}")

        # Build button map ONCE before any clicking — stable DOM at this point.
        # Buttons are matched by source_id via aria-label, with positional fallback.
        buttons = self._driver.find_elements(By.TAG_NAME, "button")
        source_buttons = [
            btn for btn in buttons
            if btn.get_attribute("aria-label")
            and len(btn.get_attribute("aria-label") or "") > 20
            and not btn.text.strip()
        ]
        button_by_source: Dict[str, WebElement] = {}
        for btn in source_buttons:
            label = btn.get_attribute("aria-label") or ""
            for vid, source_id in vid_to_src.items():
                if source_id in label or f"youtube.com/watch?v={vid}" in label:
                    button_by_source[vid] = btn
                    break

        results: Dict[str, Tuple[bool, Optional[str], Optional[str]]] = {}

        for idx, (vid, source_id) in enumerate(vid_to_src.items(), 1):
            print(f"[{idx}/{len(vid_to_src)}] Scraping: {vid[:20]}...", end=" ", flush=True)

            did_click = False
            try:
                target_btn = button_by_source.get(vid)
                if not target_btn:
                    src_pos = list(vid_to_src.keys()).index(vid)
                    if src_pos < len(source_buttons):
                        target_btn = source_buttons[src_pos]

                if not target_btn:
                    results[vid] = (False, None, "source button not found")
                    print("✗ button not found")
                    continue

                self._driver.execute_script("arguments[0].scrollIntoView({block:'center'});", target_btn)
                time.sleep(0.3)
                self._driver.execute_script("arguments[0].click();", target_btn)
                did_click = True
                print("✓ ", end="", flush=True)
                # Dynamically wait for transcript content (poll every 0.5s, up to 20s)
                body_text = self._wait_for_transcript_ready(timeout=20.0)
                transcript = self._extract_transcript_from_body(body_text)

                if transcript:
                    results[vid] = (True, transcript, None)
                    print(f"{len(transcript)} chars")
                else:
                    results[vid] = (False, None, "content too short or empty")
                    print("✗ too short")

            except Exception as e:
                results[vid] = (False, None, str(e))
                print(f"✗ {e}")

            finally:
                if did_click:
                    current_url = self._driver.current_url
                    on_transcript_page = (
                        "/source/" in current_url
                        and len(current_url.split("/source/")[-1].split("/")) >= 2
                    )
                    if not on_transcript_page:
                        pass  # On Sources tab or other page — no back-nav needed
                    else:
                        try:
                            for b in self._driver.find_elements(By.TAG_NAME, "button"):
                                if b.get_attribute("aria-label") == "Back":
                                    self._driver.execute_script("arguments[0].click();", b)
                                    time.sleep(1.5)
                                    break
                        except Exception:
                            self._driver.get(
                                f"https://notebooklm.google.com/notebook/{notebook_id}"
                            )
                            time.sleep(3)

        return results

    def close(self):
        if self._driver:
            self._driver.quit()
            self._driver = None
        self._cleanup_staging_on_close()
        self._staging_nb_id = None
        self._source_count = 0

    # --- Pre-flight cleanup: remove orphaned staging notebooks from prior runs ---

    ORPHAN_PREFIXES = ("staging_", "Industrial_Batch_")

    def preflight_cleanup(self) -> tuple[int, int]:
        """Delete orphaned staging/industrial notebooks from prior runs.

        Called once before a fetch starts. Lists all NLM notebooks, keeps
        any with unknown name patterns, and attempts to delete those matching
        ORPHAN_PREFIXES with a short timeout (orphaned notebooks timeout on
        the NLM server side, so we use a short timeout to avoid blocking).

        Returns:
            (deleted_count, failed_count)
        """
        res = self._run_nlm(["notebook", "list", "--json"], timeout=30)
        if res.returncode != 0:
            print(f"[Industrial] Pre-flight cleanup: list failed — {res.stderr or '(no output)'}")
            return (0, 0)

        try:
            notebooks = json.loads(res.stdout)
            if isinstance(notebooks, dict):
                notebooks = notebooks.get("notebooks", [])
        except Exception as e:
            print(f"[Industrial] Pre-flight cleanup: parse error — {e}")
            return (0, 0)

        deleted = 0
        failed = 0
        for nb in notebooks:
            name = nb.get("name", "") or ""
            nb_id = nb.get("id") or nb.get("notebookId")
            if not nb_id:
                continue
            if not any(name.startswith(p) for p in self.ORPHAN_PREFIXES):
                continue

            print(f"[Industrial] Pre-flight: removing orphaned notebook '{name}' ({nb_id})...")
            # Use a short timeout — orphaned notebooks hang server-side but
            # we still want to report the failure rather than block the run.
            result = self._run_nlm(["notebook", "delete", str(nb_id), "--confirm"], timeout=15)
            if result.returncode == 0:
                print(f"  ✓ deleted '{name}'")
                deleted += 1
            else:
                print(f"  ✗ could not delete '{name}' — {result.stderr or '(timeout?)'}")
                failed += 1

        print(f"[Industrial] Pre-flight cleanup: {deleted} deleted, {failed} orphaned/unreachable")
        return (deleted, failed)

    def _cleanup_staging_on_close(self) -> None:
        """Attempt graceful cleanup of the current staging notebook on close.

        Uses a longer timeout than pre-flight cleanup since the staging notebook
        is still valid at this point (not orphaned). Logs success or failure.
        """
        if not self._staging_nb_id:
            return
        print(f"[Industrial] Closing: attempting to delete staging notebook {self._staging_nb_id}...")
        res = self._run_nlm(["notebook", "delete", self._staging_nb_id, "--confirm"], timeout=60)
        if res.returncode == 0:
            print(f"[Industrial] Staging notebook deleted successfully")
        else:
            print(f"[Industrial] Staging notebook cleanup failed (may be orphaned server-side): {res.stderr or '(timeout)'}")
            self._staging_nb_id = None  # clear even on failure so we don't retain a dead ID


def main():
    import argparse
    parser = argparse.ArgumentParser(description="NotebookLM Industrial Scraper")
    parser.add_argument(
        "--notebook",
        help="Notebook ID (omit to use terminal-local staging notebook)",
    )
    parser.add_argument(
        "--video-ids",
        help="Comma-separated video IDs in add order (required)",
    )
    parser.add_argument("--no-headless", action="store_true", help="Run with visible browser")
    parser.add_argument(
        "--staging",
        action="store_true",
        help="Force use of terminal-local staging notebook (default when no --notebook)",
    )
    args = parser.parse_args()

    video_ids: List[str] = []
    if args.video_ids:
        video_ids = args.video_ids.split(",")
    else:
        print("Error: --video-ids is required")
        sys.exit(1)

    scraper = NLMIndustrialScraper(headless=not args.no_headless)
    try:
        if args.staging or not args.notebook:
            # Staging mode: uses persistent per-terminal staging notebook
            results = scraper.scrape_with_staging(video_ids)
        else:
            # Explicit notebook mode
            results = scraper.scrape_notebook(args.notebook, video_ids)
        print("\nResults:")
        for vid, (ok, text, err) in results.items():
            status = f"OK {len(text) if text else 0} chars" if ok else f"FAIL: {err}"
            print(f"  {vid}: {status}")
    finally:
        scraper.close()


if __name__ == "__main__":
    main()