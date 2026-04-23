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
import shutil
import uuid
from pathlib import Path
from typing import Optional, Dict, List, Tuple

import psutil

# Ensure csf is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

# cache write done by caller after scraper returns

try:
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options as FxOptions
    from selenium.webdriver.chrome.options import Options as ChOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.remote.webelement import WebElement
    from csf.csf_logging import log_action
    from csf.nlm_config import get_nlm_config
except ImportError:
    print("Error: Selenium not installed. Run 'pip install selenium'.")
    sys.exit(1)


class NLMIndustrialScraper:
    # NotebookLM Plus limit — used to detect when to clear and reuse
    MAX_SOURCES_PER_NOTEBOOK = 300
    SELENIUM_PROCESS_NAMES = {
        "chrome.exe",
        "chromedriver.exe",
        "firefox.exe",
        "geckodriver.exe",
    }
    SELENIUM_DRIVER_NAMES = {"chromedriver.exe", "geckodriver.exe"}
    SELENIUM_PROFILE_MARKERS = (
        "yt-is\\selenium-profiles",
        "yt-is/selenium-profiles",
    )

    def __init__(
        self,
        headless: bool = True,
        *,
        browser_cfg=None,
        readiness_matrix: bool = False,
        readiness_probe_interval_s: float = 1.0,
        readiness_probe_timeout_s: float = 600.0,
    ):
        self.headless = headless
        self.browser_cfg = browser_cfg or get_nlm_config()
        self._readiness_matrix = readiness_matrix
        self._readiness_probe_interval_s = float(readiness_probe_interval_s)
        self._readiness_probe_timeout_s = float(readiness_probe_timeout_s)
        self._driver = None
        self._staging_nb_id: str | None = None
        self._source_count: int = 0
        self._consecutive_nb_create_failures: int = 0
        self._profile_session_id = f"{os.getpid()}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self._last_materialization_ready_at_epoch: float = 0.0
        self._last_vid_order: list[str] = []

    @staticmethod
    def _looks_like_request_access(text: str) -> bool:
        """Return True when the browser content looks like a Google access gate."""
        lower = (text or "").lower()
        return any(
            phrase in lower
            for phrase in (
                "request access",
                "request-access",
                "ask for access",
                "signin",
                "sign in",
            )
        )

    def _selenium_profile_root(self, browser: str) -> Path:
        """Return the per-browser Selenium-only profile root."""
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        return Path(appdata) / "yt-is" / "selenium-profiles" / browser

    def _selenium_profile_session_root(self, browser: str) -> Path:
        """Return a per-run Selenium profile root for a specific browser."""
        return self._selenium_profile_root(browser) / self._profile_session_id

    def _selenium_profile_is_persistent(self) -> bool:
        """Return True when the Selenium profile should be reused across runs."""
        mode = getattr(self.browser_cfg, "nlm_browser_mode", None) or getattr(
            self.browser_cfg, "browser_profile_mode", "persistent"
        )
        return str(mode).strip().lower() == "persistent"

    @staticmethod
    def _resolve_chrome_profile_directory(user_data_root: Path) -> str:
        """Resolve the active Chrome profile directory name from Local State."""
        local_state = user_data_root / "Local State"
        if local_state.is_file():
            try:
                data = json.loads(local_state.read_text(encoding="utf-8"))
                profile_name = str(data.get("profile", {}).get("last_used", "")).strip()
                if profile_name:
                    return profile_name
            except Exception:
                pass
        return "Default"

    @staticmethod
    def _resolve_chrome_binary() -> str | None:
        """Return the installed Chrome executable path if present."""
        candidates = [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def _seed_browser_profile_if_needed(self, source_base: Path, target_base: Path) -> bool:
        """Seed a browser profile tree only when the target profile is empty."""
        persistent = self._selenium_profile_is_persistent()
        if not persistent:
            self._seed_profile_tree(source_base, target_base)
            return True

        target_base.mkdir(parents=True, exist_ok=True)
        try:
            entries = list(target_base.iterdir())
            if entries:
                devtools_port = target_base / "DevToolsActivePort"
                if devtools_port.exists():
                    self._seed_profile_tree(source_base, target_base)
                    return True
                return False
        except Exception:
            pass
        self._seed_profile_tree(source_base, target_base)
        return True

    def _browser_auth_probe_text(self) -> str:
        """Collect a compact text snapshot for browser auth readiness checks."""
        parts: list[str] = []
        try:
            current_url = str(self._driver.current_url or "")
            if current_url:
                parts.append(current_url)
        except Exception:
            pass
        try:
            title = str(self._driver.title or "")
            if title:
                parts.append(title)
        except Exception:
            pass
        try:
            body = self._driver.find_element(By.TAG_NAME, "body")
            body_text = str(body.text or "").strip()
            if body_text:
                parts.append(body_text[:5000])
        except Exception:
            pass
        return " | ".join(parts)

    def _browser_auth_ready(self, notebook_id: str) -> bool:
        """Return True when the browser session looks signed into NotebookLM."""
        snapshot = self._browser_auth_probe_text()
        current_url = ""
        title = ""
        try:
            current_url = str(self._driver.current_url or "")
        except Exception:
            pass
        try:
            title = str(self._driver.title or "")
        except Exception:
            pass

        if self._looks_like_request_access(snapshot) or "accounts.google.com" in snapshot.lower():
            log_action(
                "selenium_browser_auth_failed",
                {
                    "nb_id": notebook_id,
                    "current_url": current_url[:300],
                    "title": title[:200],
                    "snapshot": snapshot[:500],
                    "status": "request_access",
                },
            )
            return False

        log_action(
            "selenium_browser_auth_checked",
            {
                "nb_id": notebook_id,
                "current_url": current_url[:300],
                "title": title[:200],
                "status": "ok",
            },
        )
        return True

    @staticmethod
    def _proc_name(proc) -> str:
        try:
            return (proc.name() or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _proc_cmdline(proc) -> str:
        try:
            return " ".join(proc.cmdline()).lower()
        except Exception:
            return ""

    def _has_live_fetch_ancestor(self, proc) -> bool:
        """Return True when the process is attached to a live csf-source fetch."""
        seen: set[int] = set()
        current = proc
        while current is not None:
            try:
                current = current.parent()
            except Exception:
                return False
            if current is None or current.pid in seen:
                return False
            seen.add(current.pid)
            cmdline = self._proc_cmdline(current)
            if "csf-source fetch" in cmdline or "bin\\csf-source fetch" in cmdline or "bin/csf-source fetch" in cmdline:
                return True
        return False

    def _is_orphaned_selenium_process(self, proc) -> bool:
        """Return True when a Selenium browser/driver belongs to a stale yt-is session."""
        name = self._proc_name(proc)
        if name not in self.SELENIUM_PROCESS_NAMES:
            return False
        cmdline = self._proc_cmdline(proc)
        if not any(marker in cmdline for marker in self.SELENIUM_PROFILE_MARKERS):
            return False
        if self._has_live_fetch_ancestor(proc):
            return False
        return True

    def _collect_orphaned_selenium_pids(self) -> set[int]:
        """Collect Selenium browser/driver pids from stale yt-is sessions."""
        pids: set[int] = set()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if not self._is_orphaned_selenium_process(proc):
                    continue
                pids.add(proc.pid)
                try:
                    parent = proc.parent()
                except Exception:
                    parent = None
                if parent and self._proc_name(parent) in self.SELENIUM_DRIVER_NAMES:
                    pids.add(parent.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue
        return pids

    def _terminate_process_tree(self, pid: int) -> tuple[int, int]:
        """Terminate a process and its descendants, returning (terminated, failed)."""
        try:
            root = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return 0, 0
        except Exception:
            return 0, 1

        unique: dict[int, psutil.Process] = {root.pid: root}
        try:
            for child in root.children(recursive=True):
                unique[child.pid] = child
        except Exception:
            pass

        procs = list(unique.values())
        for proc in procs:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass

        try:
            gone, alive = psutil.wait_procs(procs, timeout=3)
        except Exception:
            gone, alive = [], procs

        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            except Exception:
                pass

        try:
            gone2, alive2 = psutil.wait_procs(alive, timeout=3)
        except Exception:
            gone2, alive2 = [], alive

        terminated = {p.pid for p in gone}
        terminated.update(p.pid for p in gone2)
        failed = {p.pid for p in alive2}
        return len(terminated), len(failed)

    def preflight_browser_cleanup(self) -> tuple[int, int]:
        """Best-effort cleanup of orphaned Selenium browser sessions from prior runs."""
        pids = self._collect_orphaned_selenium_pids()
        if not pids:
            log_action(
                "selenium_preflight_cleanup_complete",
                {"killed": 0, "failed": 0, "matched_pids": 0},
            )
            return 0, 0

        print(f"[Industrial] Pre-flight Selenium cleanup: terminating {len(pids)} orphaned processes...")
        log_action(
            "selenium_preflight_cleanup_started",
            {"matched_pids": len(pids), "pids": sorted(pids)[:12]},
        )

        killed = 0
        failed = 0
        for pid in sorted(pids):
            terminated, not_terminated = self._terminate_process_tree(pid)
            killed += terminated
            failed += not_terminated

        print(f"[Industrial] Pre-flight Selenium cleanup: {killed} terminated, {failed} failed")
        log_action(
            "selenium_preflight_cleanup_complete",
            {"killed": killed, "failed": failed, "matched_pids": len(pids)},
        )
        return killed, failed

    def _chrome_profile_sources(self) -> tuple[Path, str]:
        """Return the dedicated NotebookLM browser profile root and directory."""
        browser_profile_root = Path(
            getattr(self.browser_cfg, "nlm_browser_profile_root", "")
            or getattr(self.browser_cfg, "browser_profile_seed_root", "")
            or r"P:\packages\yt-is\.browser\notebooklm"
        )
        if browser_profile_root.exists():
            local_state = browser_profile_root / "Local State"
            if local_state.is_file():
                try:
                    data = json.loads(local_state.read_text(encoding="utf-8"))
                    profile_name = str(data.get("profile", {}).get("last_used", "")).strip()
                    if profile_name:
                        return browser_profile_root, profile_name
                except Exception:
                    pass
        return browser_profile_root, "Default"

    def _should_skip_profile_item(self, name: str) -> bool:
        """Skip lock/cache files that should not be cloned into a fresh profile."""
        lower = name.lower()
        if name.startswith("Singleton") or name == "lockfile":
            return True
        if lower in {"cache", "code cache", "gpucache", "shadercache", "grshadercache"}:
            return True
        if lower.endswith(".tmp"):
            return True
        if name == "DevToolsActivePort":
            return True
        return False

    def _seed_profile_tree(self, source_base: Path, target_base: Path) -> None:
        """Best-effort clone of a browser profile tree into a dedicated target."""
        if target_base.exists():
            shutil.rmtree(target_base, ignore_errors=True)
        if not source_base.exists():
            target_base.mkdir(parents=True, exist_ok=True)
            return
        target_base.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(source_base):
            root_path = Path(root)
            rel_root = root_path.relative_to(source_base)
            target_root = target_base / rel_root
            target_root.mkdir(parents=True, exist_ok=True)
            dirs[:] = [d for d in dirs if not self._should_skip_profile_item(d)]
            for file_name in files:
                if self._should_skip_profile_item(file_name):
                    continue
                src = root_path / file_name
                dst = target_root / file_name
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    # Best-effort clone: locked cache files and transient browser
                    # state can fail to copy while the source profile is live.
                    continue

    def _init_driver(self):
        if self._driver:
            return

        chrome_source_base, profile_name = self._chrome_profile_sources()
        if self._selenium_profile_is_persistent():
            chrome_profile_base = self._selenium_profile_session_root("chrome")
            seeded = self._seed_browser_profile_if_needed(chrome_source_base, chrome_profile_base)
        else:
            chrome_profile_base = self._selenium_profile_session_root("chrome")
            seeded = self._seed_browser_profile_if_needed(chrome_source_base, chrome_profile_base)
        log_action(
            "selenium_profile_selected",
            {
                "browser": "chrome",
                "profile_root": str(chrome_profile_base),
                "profile_name": profile_name,
                "seeded_from": str(chrome_source_base),
                "persistent": self._selenium_profile_is_persistent(),
                "seeded": seeded,
                "profile_session_id": self._profile_session_id,
            },
        )

        try:
            opts = ChOptions()
            if self.headless:
                opts.add_argument("--headless=new")
            chrome_binary = self._resolve_chrome_binary()
            if chrome_binary:
                opts.binary_location = chrome_binary
            opts.add_argument(f"--user-data-dir={chrome_profile_base}")
            opts.add_argument(f"--profile-directory={profile_name}")
            opts.add_argument("--no-first-run")
            opts.add_argument("--no-default-browser-check")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--no-sandbox")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])

            print(
                f"[Industrial] Using Chrome profile clone: {chrome_profile_base}/{profile_name} "
                f"(seeded from {chrome_source_base})"
            )
            self._driver = webdriver.Chrome(options=opts)
        except Exception as e:
            print(f"[Industrial] Chrome init failed ({e}), falling back to Firefox...")
            try:
                import glob as _glob
                appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
                opts = FxOptions()
                if self.headless:
                    opts.add_argument("--headless")
                ff_profile_base = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
                profiles = _glob.glob(os.path.join(ff_profile_base, "*.Profile 1*"))
                if not profiles:
                    all_profiles = _glob.glob(os.path.join(ff_profile_base, "*"))
                    profiles = [p for p in all_profiles if ".default" not in os.path.basename(p)]
                firefox_profile_root = self._selenium_profile_session_root("firefox")
                if profiles:
                    profile_path = profiles[0]
                    seeded = self._seed_browser_profile_if_needed(Path(profile_path), firefox_profile_root)
                    log_action(
                        "selenium_profile_selected",
                        {
                            "browser": "firefox",
                            "profile_root": str(firefox_profile_root),
                            "profile_name": Path(profile_path).name,
                            "seeded_from": str(profile_path),
                            "persistent": self._selenium_profile_is_persistent(),
                            "seeded": seeded,
                            "profile_session_id": self._profile_session_id,
                        },
                    )
                    print(
                        f"[Industrial] Using Firefox profile clone: {firefox_profile_root.name} "
                        f"(seeded from {Path(profile_path).name})"
                    )
                else:
                    firefox_profile_root.mkdir(parents=True, exist_ok=True)
                    log_action(
                        "selenium_profile_selected",
                        {
                            "browser": "firefox",
                            "profile_root": str(firefox_profile_root),
                            "profile_name": "new",
                            "seeded_from": None,
                            "persistent": self._selenium_profile_is_persistent(),
                            "seeded": False,
                            "profile_session_id": self._profile_session_id,
                        },
                    )
                    print(
                        f"[Industrial] Using fresh Firefox profile clone: {firefox_profile_root.name}"
                    )
                opts.add_argument("-profile")
                opts.add_argument(str(firefox_profile_root))
                self._driver = webdriver.Firefox(options=opts)
            except Exception as e2:
                print(f"[Industrial] Firefox fallback also failed: {e2}")
                raise RuntimeError("Could not initialize any browser driver")

        self._driver.set_page_load_timeout(90)

    def get_source_ids(self, notebook_id: str) -> List[str]:
        """Get source IDs from notebook, in the same order they were added."""
        res = self._list_source_ids_process(notebook_id)
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

    def _list_source_ids_process(self, notebook_id: str) -> subprocess.CompletedProcess[str]:
        """Run `nlm source list` and return the completed process."""
        return subprocess.run(
            ["nlm", "source", "list", notebook_id, "--json"],
            capture_output=True,
            text=True,
        )

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

    def _wait_for_source_ids_ready(
        self,
        expected_count: int,
        timeout: int = 120,
    ) -> List[str]:
        """Poll source list until the expected number of sources is visible.

        NotebookLM source adds are asynchronous enough that `nlm source add --wait`
        can return before the UI/source list has finished materializing the new
        entries. We poll the notebook source list here so downstream Selenium code
        only runs after the source buttons should exist in the DOM.
        """
        start = time.time()
        last_ids: List[str] = []
        last_res: Optional[subprocess.CompletedProcess[str]] = None
        poll_count = 0
        while time.time() - start < timeout:
            if not self._staging_nb_id:
                return []
            last_res = self._list_source_ids_process(self._staging_nb_id)
            poll_count += 1
            if last_res.returncode != 0:
                log_action(
                    "staging_source_materialization_wait_poll_failed",
                    {
                        "nb_id": self._staging_nb_id,
                        "expected_total": expected_count,
                        "poll_count": poll_count,
                        "elapsed_s": round(time.time() - start, 3),
                        "returncode": last_res.returncode,
                        "stderr": (last_res.stderr or "")[:200],
                        "stdout": (last_res.stdout or "")[:200],
                    },
                )
                time.sleep(5)
                continue
            try:
                sources = json.loads(last_res.stdout)
                if isinstance(sources, dict):
                    sources = sources.get("sources", [])
                ids = [s["id"] for s in sources]
            except Exception as e:
                log_action(
                    "staging_source_materialization_wait_poll_failed",
                    {
                        "nb_id": self._staging_nb_id,
                        "expected_total": expected_count,
                        "poll_count": poll_count,
                        "elapsed_s": round(time.time() - start, 3),
                        "returncode": last_res.returncode,
                        "parse_error": str(e),
                        "stderr": (last_res.stderr or "")[:200],
                        "stdout": (last_res.stdout or "")[:200],
                    },
                )
                time.sleep(5)
                continue
            last_ids = ids
            if len(ids) >= expected_count:
                return ids
            if poll_count == 1 or poll_count % 3 == 0:
                log_action(
                    "staging_source_materialization_wait_progress",
                    {
                        "nb_id": self._staging_nb_id,
                        "expected_total": expected_count,
                        "observed_total": len(ids),
                        "poll_count": poll_count,
                        "elapsed_s": round(time.time() - start, 3),
                    },
                )
            time.sleep(5)
        timeout_payload = {
            "nb_id": self._staging_nb_id,
            "expected_total": expected_count,
            "observed_total": len(last_ids),
            "poll_count": poll_count,
            "elapsed_s": round(time.time() - start, 3),
        }
        if last_res is not None:
            timeout_payload.update(
                {
                    "returncode": last_res.returncode,
                    "stdout": (last_res.stdout or "")[:500],
                    "stderr": (last_res.stderr or "")[:500],
                }
            )
        log_action("staging_source_materialization_wait_timeout", timeout_payload)
        return []

    def _poll_source_buttons_dom(
        self,
        expected: int,
        timeout: int = 120,
    ) -> Optional[int]:
        """Poll browser DOM for source buttons until the expected number exist.

        This bridges the materialization gap: _add_sources_to_staging() confirms
        sources via CLI before NotebookLM has finished rendering them in the
        browser SPA. We poll the DOM directly rather than trusting CLI alone.

        Unlike the scrape loop which filters by real source_id+vid, this polling
        filter is source-ID-agnostic — it just checks for buttons with long
        aria-labels that don't look like UI chrome. This avoids false negatives
        when empty strings are passed for source_id/vid during the poll.
        """
        start = time.time()
        last_total = 0
        last_ready = 0
        last_processing = 0
        while time.time() - start < timeout:
            try:
                total_count = self._count_source_buttons_dom()
                ready_count = self._count_ready_source_buttons_dom()
                processing_count = self._count_processing_source_buttons_dom()
                last_total = total_count
                last_ready = ready_count
                last_processing = processing_count
                if ready_count >= expected:
                    log_action(
                        "staging_source_dom_wait_succeeded",
                        {
                            "nb_id": self._staging_nb_id,
                            "expected_total": expected,
                            "observed_total": total_count,
                            "ready_total": ready_count,
                            "processing_total": processing_count,
                            "spinner_active": processing_count > 0,
                            "elapsed_s": round(time.time() - start, 3),
                            "started_at_epoch": start,
                            "completed_at_epoch": time.time(),
                        },
                    )
                    return ready_count
                if ready_count or total_count:
                    log_action(
                        "staging_source_dom_wait_progress",
                        {
                            "nb_id": self._staging_nb_id,
                            "expected_total": expected,
                            "observed_total": total_count,
                            "ready_total": ready_count,
                            "processing_total": processing_count,
                            "spinner_active": processing_count > 0,
                            "elapsed_s": round(time.time() - start, 3),
                        },
                    )
                print(
                    f"[Industrial] DOM poll: {ready_count}/{expected} ready, "
                    f"{processing_count} still processing..."
                )
            except Exception:
                pass
            time.sleep(3)
        log_action(
            "staging_source_dom_wait_timeout",
            {
                "nb_id": self._staging_nb_id,
                "expected_total": expected,
                "observed_total": last_total,
                "ready_total": last_ready,
                "processing_total": last_processing,
                "spinner_active": last_processing > 0,
                "elapsed_s": round(time.time() - start, 3),
                "started_at_epoch": start,
                "completed_at_epoch": time.time(),
            },
        )
        return None

    def _prepare_sources_dom(self, notebook_id: str, expected_count: int) -> Optional[int]:
        """Open Sources context and wait for the source rows to materialize."""
        self._ensure_sources_context(notebook_id)
        print(f"[Industrial] Waiting up to 120s for {expected_count} source buttons to render...")
        dom_ready = self._poll_source_buttons_dom(expected=expected_count, timeout=120)
        if dom_ready:
            print(f"[Industrial] DOM buttons ready ({dom_ready} found)")
        else:
            print(f"[Industrial] DOM polling timed out — proceeding anyway (may fail)")
        return dom_ready

    def _open_notebook_and_prepare_sources(self, notebook_id: str, expected_count: int) -> Optional[int]:
        """Open a notebook URL and wait for its Sources DOM to become ready."""
        url = f"https://notebooklm.google.com/notebook/{notebook_id}"
        print(f"[Industrial] Opening {url}...")
        self._driver.get(url)
        if not self._browser_auth_ready(notebook_id):
            print("[Industrial] NotebookLM browser session is not authenticated; aborting DOM scrape.")
            return -1
        return self._prepare_sources_dom(notebook_id, expected_count)

    def _count_source_buttons_dom(self) -> int:
        """Return the current count of source-like buttons visible in the DOM."""
        try:
            return len(self._collect_source_dom_candidates())
        except Exception:
            return 0

    def _count_ready_source_buttons_dom(self) -> int:
        """Return the count of source-like buttons whose rows are no longer processing."""
        try:
            return sum(
                1
                for elem in self._collect_source_dom_candidates()
                if not self._is_processing_source_dom_candidate(elem)
            )
        except Exception:
            return 0

    def _count_processing_source_buttons_dom(self) -> int:
        """Return the count of source-like buttons whose rows still look processing."""
        try:
            return sum(
                1
                for elem in self._collect_source_dom_candidates()
                if self._is_processing_source_dom_candidate(elem)
            )
        except Exception:
            return 0

    def _is_processing_source_dom_candidate(self, elem: WebElement) -> bool:
        """Return True when a source row still looks like it is loading or processing."""
        labels: list[str] = []
        for part in (
            elem.text or "",
            elem.get_attribute("aria-label") or "",
            elem.get_attribute("title") or "",
            elem.get_attribute("href") or "",
        ):
            text = part.strip()
            if text:
                labels.append(text)
        try:
            for child in elem.find_elements(By.CSS_SELECTOR, '[aria-label], [title], [alt]'):
                for part in (
                    child.text or "",
                    child.get_attribute("aria-label") or "",
                    child.get_attribute("title") or "",
                    child.get_attribute("alt") or "",
                ):
                    text = part.strip()
                    if text:
                        labels.append(text)
        except Exception:
            pass

        combined = " | ".join(labels).lower()
        return any(
            phrase in combined
            for phrase in (
                "processing",
                "loading",
                "in progress",
                "still loading",
                "still processing",
            )
        )

    def _collect_source_dom_candidates(self) -> list[WebElement]:
        """Collect candidate source-row elements from the current DOM."""
        selectors = (
            "div.source-panel-content button.source-stretched-button",
            "div.source-panel-content a.source-stretched-button",
            "source-picker button.source-stretched-button",
            "source-picker a.source-stretched-button",
            "div.tab-container.source-tab-container button.source-stretched-button",
            "div.tab-container.source-tab-container a.source-stretched-button",
        )
        scoped = self._collect_source_dom_candidates_from_selectors(selectors)
        if scoped:
            return scoped

        fallback_selectors = (
            "button.source-stretched-button",
            "a.source-stretched-button",
            '[class*="source-stretched-button"]',
            "button",
            '[role="button"]',
            "a",
        )
        return self._collect_source_dom_candidates_from_selectors(fallback_selectors)

    def _collect_source_dom_candidates_from_selectors(self, selectors: tuple[str, ...]) -> list[WebElement]:
        """Collect candidate source rows from a specific selector set."""
        candidates: list[WebElement] = []
        seen: set[str] = set()
        for selector in selectors:
            for elem in self._driver.find_elements(By.CSS_SELECTOR, selector):
                marker = self._source_dom_signature(elem)
                if marker in seen:
                    continue
                seen.add(marker)
                candidates.append(elem)
        return [elem for elem in candidates if self._is_source_dom_candidate(elem)]

    def _source_dom_signature(self, elem: WebElement) -> str:
        """Return a stable signature for deduping mirrored source-row nodes."""
        parts = [
            (elem.get_attribute("class") or "").strip().lower(),
            (elem.get_attribute("aria-label") or "").strip(),
            (elem.get_attribute("title") or "").strip(),
            (elem.get_attribute("href") or "").strip(),
            (elem.text or "").strip(),
            (elem.get_attribute("outerHTML") or "").strip(),
        ]
        return "\u241f".join(parts)

    def _is_source_dom_candidate(self, elem: WebElement) -> bool:
        """Return True when an element looks like a NotebookLM source row."""
        text = (elem.text or "").strip()
        aria = (elem.get_attribute("aria-label") or "").strip()
        title = (elem.get_attribute("title") or "").strip()
        href = (elem.get_attribute("href") or "").strip()
        classes = (elem.get_attribute("class") or "").strip().lower()
        combined = " | ".join(part for part in (text, aria, title, href) if part)
        if not combined:
            return False
        if "source-stretched-button" in classes:
            return True
        lower = combined.lower()
        if any(
            phrase in lower
            for phrase in (
                "chat panel",
                "save to note",
                "add to note",
                "view source",
                "back",
                "close",
                "more options",
                "scrolls the chat panel",
                "scroll to top",
                "scroll to bottom",
                "send message",
                "settings",
                "create notebook",
                "google apps",
                "google account",
                "notebooklm homepage",
            )
        ):
            return False
        if "/source/" in lower:
            return True
        if "youtube.com/watch?v=" in lower:
            return True
        if len(combined) > 40 and not text.strip():
            return True
        return False

    def _navigate_to_sources_tab(self) -> bool:
        """Navigate to the Sources control, waiting for it to be interactive."""
        try:
            candidates: list[WebElement] = []
            seen: set[int] = set()
            for selector in ('[role="tab"]', "button", "a"):
                for elem in self._driver.find_elements(By.CSS_SELECTOR, selector):
                    marker = id(elem)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    candidates.append(elem)

            for elem in candidates:
                text = (elem.text or "").strip()
                aria = (elem.get_attribute("aria-label") or "").strip()
                title = (elem.get_attribute("title") or "").strip()
                combined = " | ".join(part for part in (text, aria, title) if part)
                if "sources" not in combined.lower():
                    continue
                if any(
                    phrase in combined.lower()
                    for phrase in (
                        "chat panel",
                        "save to note",
                        "add to note",
                        "view source",
                        "back",
                        "close",
                        "more options",
                        "scrolls the chat panel",
                        "scroll to top",
                        "scroll to bottom",
                        "send message",
                    )
                ):
                    continue
                self._driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", elem
                )
                time.sleep(0.3)
                self._driver.execute_script("arguments[0].click();", elem)
                time.sleep(1)
                print("[Industrial] Switched to Sources control")
                return True

            print(
                f"[Industrial] Could not find Sources control: "
                f"{self._button_label_preview(candidates)}"
            )
        except Exception as e:
            print(f"[Industrial] Could not click Sources control: {e}")
        return False

    def _is_sources_list_url(self, current_url: str) -> bool:
        """Return True when the browser is on the Sources list page."""
        if "/source/" not in current_url:
            return False
        tail = current_url.split("/source/", 1)[1].strip("/")
        return "/" not in tail

    def _ensure_sources_context(self, notebook_id: str) -> bool:
        """Ensure the browser is on the Sources list page with a cheap URL check."""
        current_url = self._driver.current_url or ""
        if self._is_sources_list_url(current_url):
            return True

        page_state = self._page_state(current_url)

        def _log_context_recovery(phase: str, method: str, **extra) -> None:
            payload = {
                "nb_id": notebook_id,
                "current_url": current_url[:300],
                "page_state": page_state,
                "method": method,
            }
            payload.update(extra)
            log_action(f"sources_context_recovery_{phase}", payload)

        # If we drifted onto a transcript page, Back is cheaper than a full reload.
        if "/source/" in current_url:
            _log_context_recovery("started", "back")
            try:
                for b in self._driver.find_elements(By.TAG_NAME, "button"):
                    if b.get_attribute("aria-label") == "Back":
                        self._driver.execute_script("arguments[0].click();", b)
                        time.sleep(1.0)
                        current_url = self._driver.current_url or ""
                        if self._is_sources_list_url(current_url):
                            _log_context_recovery("finished", "back", status="ok")
                            return True
                        break
            except Exception:
                _log_context_recovery("finished", "back", status="exception")
            else:
                _log_context_recovery("finished", "back", status="not_recovered")

        # Normal recovery path: click the Sources control from the current notebook page.
        _log_context_recovery("started", "sources_tab")
        self._navigate_to_sources_tab()
        current_url = self._driver.current_url or ""
        if self._is_sources_list_url(current_url) or self._count_source_buttons_dom() > 0:
            _log_context_recovery("finished", "sources_tab", status="ok")
            return True
        _log_context_recovery("finished", "sources_tab", status="not_recovered")

        # Last resort: reload the notebook root, then try Sources again.
        _log_context_recovery("started", "reload")
        self._driver.get(f"https://notebooklm.google.com/notebook/{notebook_id}")
        time.sleep(3)
        self._navigate_to_sources_tab()
        current_url = self._driver.current_url or ""
        recovered = self._is_sources_list_url(current_url) or self._count_source_buttons_dom() > 0
        _log_context_recovery("finished", "reload", status="ok" if recovered else "not_recovered")
        return recovered

    def _page_state(self, current_url: str) -> str:
        """Classify the current page into a coarse NotebookLM state."""
        if not current_url:
            return "unknown"
        if self._is_sources_list_url(current_url) or self._count_source_buttons_dom() > 0:
            return "sources_list"
        if "/source/" in current_url:
            return "transcript_or_source_detail"
        if "notebooklm.google.com/notebook/" in current_url:
            return "notebook_shell"
        return "other"

    def _button_label_preview(self, buttons: List[WebElement], limit: int = 3) -> str:
        """Return a compact preview of candidate button labels for logging."""
        previews: List[str] = []
        for btn in buttons[:limit]:
            parts = [
                (btn.text or "").replace("\n", " ").strip(),
                (btn.get_attribute("aria-label") or "").replace("\n", " ").strip(),
                (btn.get_attribute("title") or "").replace("\n", " ").strip(),
                (btn.get_attribute("href") or "").replace("\n", " ").strip(),
            ]
            label = " | ".join(part for part in parts if part)
            if len(label) > 120:
                label = label[:117] + "..."
            previews.append(label or "(no aria-label)")
        return " | ".join(previews) if previews else "(none)"

    def _is_source_button_label(self, label: str, source_id: str, vid: str) -> bool:
        """Return True when a label looks like a NotebookLM source entry."""
        norm = label.lower()
        if any(
            phrase in norm
            for phrase in (
                "chat panel",
                "save to note",
                "add to note",
                "view source",
                "back",
                "close",
                "more options",
                "scrolls the chat panel",
                "scroll to top",
                "scroll to bottom",
                "send message",
                "google apps",
                "google account",
                "notebooklm homepage",
            )
        ):
            return False

        video_url = f"youtube.com/watch?v={vid}"
        if source_id in label:
            return True
        if video_url in label:
            return True
        if f"/source/{source_id}" in label:
            return True
        if "open source" in norm and vid in label:
            return True
        return False

    def _is_source_element(self, elem: WebElement, source_id: str, vid: str) -> bool:
        """Return True when an element looks like a NotebookLM source entry."""
        text = (elem.text or "").strip()
        aria = (elem.get_attribute("aria-label") or "").strip()
        title = (elem.get_attribute("title") or "").strip()
        href = (elem.get_attribute("href") or "").strip()
        classes = (elem.get_attribute("class") or "").strip().lower()
        combined = " | ".join(part for part in (text, aria, title, href) if part)
        if not combined:
            return False
        if "source-stretched-button" in classes:
            return True
        lower = combined.lower()
        if any(
            phrase in lower
            for phrase in (
                "chat panel",
                "save to note",
                "add to note",
                "view source",
                "back",
                "close",
                "more options",
                "scrolls the chat panel",
                "scroll to top",
                "scroll to bottom",
                "send message",
                "settings",
                "create notebook",
            )
        ):
            return False
        video_url = f"youtube.com/watch?v={vid}"
        if source_id in combined:
            return True
        if video_url in lower:
            return True
        if f"/source/{source_id}" in lower:
            return True
        if f"watch?v={vid}" in lower:
            return True
        return False

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
            login_started = time.perf_counter()
            log_action(
                "nlm_login_started",
                {"component": "nlm_scraper", "mode": "force", "status": "started"},
            )
            login = subprocess.run(
                ["nlm", "login", "--force"],
                capture_output=True, text=True, timeout=120,
            )
            login_elapsed = round(time.perf_counter() - login_started, 3)
            if login.returncode == 0:
                log_action(
                    "nlm_login_completed",
                    {
                        "component": "nlm_scraper",
                        "mode": "force",
                        "status": "ok",
                        "elapsed_s": login_elapsed,
                    },
                )
                log_action(
                    "nlm_auth_refreshed",
                    {"component": "nlm_scraper", "status": "mid_session_ok"},
                )
                res = subprocess.run(
                    ["nlm"] + args,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                log_action(
                    "nlm_login_failed",
                    {
                        "component": "nlm_scraper",
                        "mode": "force",
                        "status": "failed",
                        "elapsed_s": login_elapsed,
                        "returncode": login.returncode,
                    },
                )
                log_action(
                    "nlm_auth_failed",
                    {"component": "nlm_scraper", "status": "mid_session_refresh_failed"},
                )
        return res

    def _create_staging_notebook(self) -> str | None:
        """Create a new staging notebook and return its ID.

        Retries up to 3 times, re-authenticating before each retry, to handle
        both auth expiry and transient server-side failures.
        """
        name = f"staging_{int(time.time())}"
        for attempt in range(3):
            log_action(
                "staging_notebook_create_started",
                {
                    "name": name,
                    "attempt": attempt + 1,
                },
            )
            res = self._run_nlm(["notebook", "create", name])
            if res.returncode == 0:
                self._consecutive_nb_create_failures = 0
                log_action(
                    "staging_notebook_create_succeeded",
                    {
                        "name": name,
                        "attempt": attempt + 1,
                    },
                )
                break
            log_action(
                "staging_notebook_create_failed",
                {
                    "name": name,
                    "attempt": attempt + 1,
                    "error": (res.stderr or res.stdout or "(empty)")[:200],
                },
            )
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
        contaminating the next batch. After each sub-batch add, waits for the
        notebook source list to reflect the expected total count before moving on.
        """
        if not self._staging_nb_id:
            return None
        all_source_ids: List[str] = []
        for i in range(0, len(video_ids), self._CLI_SUBBATCH):
            subbatch = video_ids[i : i + self._CLI_SUBBATCH]
            expected_total = len(all_source_ids) + len(subbatch)
            log_action(
                "staging_source_add_started",
                {
                    "nb_id": self._staging_nb_id,
                    "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                    "subbatch_size": len(subbatch),
                    "expected_total": expected_total,
                },
            )
            add_cmd = [
                "source", "add", self._staging_nb_id,
            ]
            for vid in subbatch:
                add_cmd.extend(["--url", f"https://www.youtube.com/watch?v={vid}"])
            res = self._run_nlm(add_cmd, timeout=900)
            if res.returncode != 0:
                log_action(
                    "staging_source_add_failed",
                    {
                        "nb_id": self._staging_nb_id,
                        "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                        "subbatch_size": len(subbatch),
                        "error": (res.stderr or res.stdout or "(empty)")[:200],
                    },
                )
                # On sub-batch failure, clear the notebook so the next call
                # starts with a fresh state instead of a corrupted one.
                print(f"[Industrial] Sub-batch add failed ({i}-{i+len(subbatch)}): {res.stderr or '(no output)'} — clearing notebook")
                self._clear_staging_notebook()
                return None
            log_action(
                "staging_source_add_completed",
                {
                    "nb_id": self._staging_nb_id,
                    "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                    "subbatch_size": len(subbatch),
                    "expected_total": expected_total,
                },
            )
            # Wait for NotebookLM to finish materializing the added sources.
            log_action(
                "staging_source_materialization_wait_started",
                {
                    "nb_id": self._staging_nb_id,
                    "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                    "expected_total": expected_total,
                },
            )
            print(
                f"[Industrial] Sub-batch {i // self._CLI_SUBBATCH + 1}: "
                f"waiting for {expected_total} sources in NLM..."
            )
            ids = self._wait_for_source_ids_ready(expected_total, timeout=120)
            if len(ids) < expected_total:
                log_action(
                    "staging_source_materialization_wait_failed",
                    {
                        "nb_id": self._staging_nb_id,
                        "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                        "expected_total": expected_total,
                        "observed_total": len(ids),
                    },
                )
                # The notebook did not reflect the expected source count in time.
                self._clear_staging_notebook()
                return None
            self._last_materialization_ready_at_epoch = time.time()
            log_action(
                "staging_source_materialization_wait_succeeded",
                {
                    "nb_id": self._staging_nb_id,
                    "subbatch_index": (i // self._CLI_SUBBATCH) + 1,
                    "expected_total": expected_total,
                    "observed_total": len(ids),
                },
            )
            # Source IDs returned are ordered newest-first; the newly added
            # ones are at the START of the list.  Figure out how many we
            # just added and keep only those from the front.
            added = len(subbatch)
            all_source_ids.extend(ids[:added])
        return all_source_ids

    def _find_source_dom_candidate(self, source_id: str, vid: str) -> tuple[Optional[WebElement], int, bool]:
        """Find the best DOM candidate for a specific source row."""
        all_candidates = self._collect_source_dom_candidates()
        source_buttons = [
            elem
            for elem in all_candidates
            if self._is_source_element(elem, source_id, vid)
        ]
        target_btn = source_buttons[0] if source_buttons else None
        fallback_used = False
        if not target_btn:
            src_pos = -1
            try:
                src_pos = self._last_vid_order.index(vid)  # type: ignore[attr-defined]
            except Exception:
                src_pos = -1
            if src_pos >= 0 and src_pos < len(all_candidates):
                target_btn = all_candidates[src_pos]
                fallback_used = True
        return target_btn, len(source_buttons), fallback_used

    def _probe_source_content_readiness(
        self,
        source_id: str,
        vid_hint: str,
        *,
        ready_reference_epoch: float,
    ) -> dict[str, object]:
        """Poll a single source until NotebookLM content becomes readable."""
        probe_started_at = time.monotonic()
        probe_started_at_epoch = time.time()
        probe_deadline = probe_started_at + self._readiness_probe_timeout_s
        probe_attempt = 0
        while True:
            probe_attempt += 1
            started_at_epoch = time.time()
            ready_age_s = round(started_at_epoch - ready_reference_epoch, 3) if ready_reference_epoch else 0.0
            log_action(
                "staging_source_content_readiness_probe_started",
                {
                    "nb_id": self._staging_nb_id,
                    "source_id": source_id,
                    "video_id": vid_hint,
                    "probe_attempt": probe_attempt,
                    "timeout_s": self._readiness_probe_timeout_s,
                    "poll_interval_s": self._readiness_probe_interval_s,
                    "probe_started_at_epoch": started_at_epoch,
                    "source_ready_age_s": ready_age_s,
                    "materialization_ready_at_epoch": ready_reference_epoch,
                },
            )
            res = self._run_nlm(["source", "content", source_id, "--json"], timeout=30)
            completed_at_epoch = time.time()
            content = ""
            content_length = 0
            status = "command_failed" if res.returncode != 0 else "parse_failed"
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout)
                    if isinstance(data, dict):
                        content = data.get("value", {}).get("content", "")
                        if not content:
                            content = data.get("content", "")
                    content_length = len(content)
                    if content_length > 100:
                        status = "ready"
                        log_action(
                            "staging_source_content_readiness_probe_completed",
                            {
                                "nb_id": self._staging_nb_id,
                                "source_id": source_id,
                                "video_id": vid_hint,
                                "probe_attempt": probe_attempt,
                                "timeout_s": self._readiness_probe_timeout_s,
                                "poll_interval_s": self._readiness_probe_interval_s,
                                "probe_started_at_epoch": started_at_epoch,
                                "probe_completed_at_epoch": completed_at_epoch,
                                "elapsed_s": round(completed_at_epoch - started_at_epoch, 3),
                                "returncode": res.returncode,
                                "content_length": content_length,
                                "status": status,
                                "ready_threshold": 100,
                                "source_ready_age_s": ready_age_s,
                                "materialization_ready_at_epoch": ready_reference_epoch,
                            },
                        )
                        return {
                            "status": status,
                            "attempts": probe_attempt,
                            "content_length": content_length,
                            "ready_at_epoch": completed_at_epoch,
                        }
                    status = "too_short"
                except Exception:
                    status = "parse_failed"
            log_action(
                "staging_source_content_readiness_probe_completed",
                {
                    "nb_id": self._staging_nb_id,
                    "source_id": source_id,
                    "video_id": vid_hint,
                    "probe_attempt": probe_attempt,
                    "timeout_s": self._readiness_probe_timeout_s,
                    "poll_interval_s": self._readiness_probe_interval_s,
                    "probe_started_at_epoch": started_at_epoch,
                    "probe_completed_at_epoch": completed_at_epoch,
                    "elapsed_s": round(completed_at_epoch - started_at_epoch, 3),
                    "returncode": res.returncode,
                    "content_length": content_length,
                    "status": status,
                    "ready_threshold": 100,
                    "source_ready_age_s": ready_age_s,
                    "materialization_ready_at_epoch": ready_reference_epoch,
                    "stdout": (res.stdout or "")[:200],
                    "stderr": (res.stderr or "")[:200],
                },
            )
            if time.monotonic() >= probe_deadline:
                return {
                    "status": status,
                    "attempts": probe_attempt,
                    "content_length": content_length,
                    "ready_at_epoch": 0.0,
                }
            time.sleep(self._readiness_probe_interval_s)

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
                log_action(
                    "nlm_auth_checked",
                    {"component": "nlm_scraper", "status": "expired"},
                )
                login = subprocess.run(["nlm", "login", "--force"], capture_output=True, text=True, timeout=120)
                if login.returncode != 0:
                    log_action(
                        "nlm_auth_failed",
                        {"component": "nlm_scraper", "status": "refresh_failed"},
                    )
                    print(f"[Industrial] Re-auth failed: {login.stderr}")
                    self._consecutive_nb_create_failures += 1
                    return False
                auth_ok = self._run_nlm(["notebook", "list"], timeout=30)
                if auth_ok.returncode != 0:
                    log_action(
                        "nlm_auth_failed",
                        {"component": "nlm_scraper", "status": "post_refresh_failed"},
                    )
                    print(f"[Industrial] Auth smoke-test still failing after re-auth: {auth_ok.stderr}")
                    self._consecutive_nb_create_failures += 1
                    return False
                log_action(
                    "nlm_auth_refreshed",
                    {"component": "nlm_scraper", "status": "ok"},
                )
            else:
                log_action(
                    "nlm_auth_failed",
                    {"component": "nlm_scraper", "status": "non_auth_failure"},
                )
                print(f"[Industrial] Notebook list failed (non-auth): {auth_ok.stderr}")
                self._consecutive_nb_create_failures += 1
                return False
        else:
            log_action("nlm_auth_checked", {"component": "nlm_scraper", "status": "ok"})
        if self._staging_nb_id and self._source_count < self.MAX_SOURCES_PER_NOTEBOOK:
            remaining = self.MAX_SOURCES_PER_NOTEBOOK - self._source_count
            print(
                f"[Industrial] Reusing staging notebook ({self._source_count} sources, "
                f"room for {remaining})"
            )
            return True
        if self._staging_nb_id:
            print(f"[Industrial] Staging notebook at capacity ({self._source_count}), clearing...")
            log_action(
                "staging_notebook_clearing",
                {
                    "nb_id": self._staging_nb_id,
                    "source_count": self._source_count,
                },
            )
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
        batch_started_at = time.monotonic()
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
            print(
                f"[Industrial] Adding sub-batch of {len(batch_ids)} sources "
                f"(current={self._source_count}, limit={self.MAX_SOURCES_PER_NOTEBOOK})"
            )
            source_ids = self._add_sources_to_staging(batch_ids)
            if not source_ids:
                return {vid: (False, None, "source add failed") for vid in batch_ids}
            print(
                f"[Industrial] Source list ready for scrape: {len(source_ids)} total IDs "
                f"after add"
            )
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
            print(
                f"[Industrial] Adding overflow sub-batch of {len(batch_ids)} sources "
                f"(current={self._source_count}, remaining_capacity={remaining})"
            )
            source_ids = self._add_sources_to_staging(batch_ids)
            if not source_ids:
                for vid in batch_ids:
                    results[vid] = (False, None, "source add failed")
                continue
            print(
                f"[Industrial] Source list ready for overflow scrape: {len(source_ids)} total IDs "
                f"after add"
            )
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
        log_action(
            "industrial_batch_complete",
            {
                "nb_id": self._staging_nb_id,
                "total": total,
                "succeeded": succeeded,
                "failed": total - succeeded,
                "elapsed_s": round(time.monotonic() - batch_started_at, 3),
            },
        )
        return results

    def _scrape_sources(
        self,
        vid_to_src: Dict[str, str],
    ) -> Dict[str, Tuple[bool, Optional[str], Optional[str]]]:
        """Scrape a set of already-mapped video_id -> source_id pairs from the open notebook."""
        if not self._staging_nb_id:
            return {vid: (False, None, "no staging notebook") for vid in vid_to_src}

        dom_ready = self._open_notebook_and_prepare_sources(self._staging_nb_id, len(vid_to_src))
        if dom_ready == -1:
            return {vid: (False, None, "browser auth unavailable") for vid in vid_to_src}
        batch_started_at = time.monotonic()
        log_action(
            "industrial_scrape_batch_started",
            {
                "nb_id": self._staging_nb_id,
                "batch_size": len(vid_to_src),
            },
        )

        results: Dict[str, Tuple[bool, Optional[str], Optional[str]]] = {}
        context_not_ready_streak = 0

        for vid, source_id in vid_to_src.items():
            idx = list(vid_to_src.keys()).index(vid) + 1
            video_started_at = time.monotonic()

            if not self._ensure_sources_context(self._staging_nb_id):
                context_not_ready_streak += 1
                current_url = self._driver.current_url or ""
                page_state = self._page_state(current_url)
                results[vid] = (False, None, "sources context not available")
                log_action(
                    "industrial_scrape_video_finished",
                    {
                        "nb_id": self._staging_nb_id,
                        "video_id": vid,
                        "source_id": source_id,
                        "index": idx,
                        "batch_size": len(vid_to_src),
                        "status": "context_not_ready",
                        "current_url": current_url[:300],
                        "page_state": page_state,
                        "elapsed_s": round(time.monotonic() - video_started_at, 3),
                    },
                )
                print(
                    f"[Industrial] Sources context not ready for vid={vid[:12]} "
                    f"source={source_id[:12]} url={current_url} state={page_state}"
                )
                print("✗ sources context not available")
                if context_not_ready_streak >= 5:
                    log_action(
                        "industrial_scrape_context_recovery_started",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "streak": context_not_ready_streak,
                            "current_url": current_url[:300],
                            "page_state": page_state,
                        },
                    )
                    recovered = self._ensure_sources_context(self._staging_nb_id)
                    log_action(
                        "industrial_scrape_context_recovery_finished",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "streak": context_not_ready_streak,
                            "recovered": recovered,
                            "current_url": (self._driver.current_url or "")[:300],
                            "page_state": self._page_state(self._driver.current_url or ""),
                        },
                    )
                    if recovered:
                        context_not_ready_streak = 0
                continue

            print(
                f"[{idx}/{len(vid_to_src)}] Scraping {vid[:12]} via {source_id[:12]}...",
                end=" ",
                flush=True,
            )
            log_action(
                "industrial_scrape_video_started",
                {
                    "nb_id": self._staging_nb_id,
                    "video_id": vid,
                    "source_id": source_id,
                    "index": idx,
                    "batch_size": len(vid_to_src),
                    "elapsed_s": round(video_started_at - batch_started_at, 3),
                },
            )

            did_click = False
            try:
                # Always find the button fresh from current DOM state.  Caching
                # button references across navigations is the root cause of the stale-
                # element cascade: after the first click navigates to a transcript
                # page, all 299 remaining cached WebElement references go stale.
                # By scanning the DOM fresh for every video, we eliminate stale
                # references entirely.
                self._last_vid_order = list(vid_to_src.keys())  # type: ignore[attr-defined]
                target_btn, source_button_match_count, fallback_used = self._find_source_dom_candidate(source_id, vid)

                if not target_btn:
                    context_not_ready_streak = 0
                    print(
                        f"[Industrial] button lookup failed for vid={vid} source={source_id} "
                        f"buttons={source_button_match_count} url={self._driver.current_url}"
                    )
                    if source_button_match_count:
                        preview_buttons = [
                            elem
                            for elem in self._collect_source_dom_candidates()
                            if self._is_source_element(elem, source_id, vid)
                        ]
                    else:
                        preview_buttons = self._collect_source_dom_candidates()
                    print(
                        f"[Industrial] candidate labels: "
                        f"{self._button_label_preview(preview_buttons)}"
                    )
                    results[vid] = (False, None, "source button not found")
                    log_action(
                        "industrial_scrape_video_finished",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "index": idx,
                            "batch_size": len(vid_to_src),
                            "status": "button_not_found",
                            "elapsed_s": round(time.monotonic() - video_started_at, 3),
                        },
                    )
                    print("✗ button not found")
                    continue

                dom_spinner_active = self._is_processing_source_dom_candidate(target_btn)
                dom_snapshot_payload = {
                    "nb_id": self._staging_nb_id,
                    "video_id": vid,
                    "source_id": source_id,
                    "index": idx,
                    "batch_size": len(vid_to_src),
                    "spinner_active": dom_spinner_active,
                    "dom_ready": not dom_spinner_active,
                    "dom_checkmark_visible": not dom_spinner_active,
                    "candidate_match_count": source_button_match_count,
                    "fallback_used": fallback_used,
                    "ready_total": self._count_ready_source_buttons_dom(),
                    "processing_total": self._count_processing_source_buttons_dom(),
                    "source_ready_age_s": round(
                        time.time() - self._last_materialization_ready_at_epoch, 3
                    )
                    if self._last_materialization_ready_at_epoch
                    else 0.0,
                }
                log_action("staging_source_readiness_snapshot", dom_snapshot_payload)

                if self._readiness_matrix:
                    probe_window_started_at = time.time()
                    log_action(
                        "staging_source_content_readiness_probe_window_started",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "timeout_s": self._readiness_probe_timeout_s,
                            "poll_interval_s": self._readiness_probe_interval_s,
                            "materialization_ready_at_epoch": self._last_materialization_ready_at_epoch,
                            "spinner_active": dom_spinner_active,
                            "dom_checkmark_visible": not dom_spinner_active,
                        },
                    )
                    probe_result = self._probe_source_content_readiness(
                        source_id,
                        vid,
                        ready_reference_epoch=self._last_materialization_ready_at_epoch,
                    )
                    log_action(
                        "staging_source_content_readiness_probe_window_completed",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "timeout_s": self._readiness_probe_timeout_s,
                            "poll_interval_s": self._readiness_probe_interval_s,
                            "probe_result": probe_result,
                            "materialization_ready_at_epoch": self._last_materialization_ready_at_epoch,
                            "probe_window_elapsed_s": round(time.time() - probe_window_started_at, 3),
                            "spinner_active": dom_spinner_active,
                            "dom_checkmark_visible": not dom_spinner_active,
                        },
                    )

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
                    context_not_ready_streak = 0
                    results[vid] = (True, transcript, None)
                    log_action(
                        "industrial_scrape_video_finished",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "index": idx,
                            "batch_size": len(vid_to_src),
                            "status": "success",
                            "transcript_chars": len(transcript),
                            "elapsed_s": round(time.monotonic() - video_started_at, 3),
                        },
                    )
                    print(f"{len(transcript)} chars")
                else:
                    context_not_ready_streak = 0
                    results[vid] = (False, None, "content too short or empty")
                    log_action(
                        "industrial_scrape_video_finished",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "index": idx,
                            "batch_size": len(vid_to_src),
                            "status": "too_short",
                            "elapsed_s": round(time.monotonic() - video_started_at, 3),
                        },
                    )
                    print("✗ too short")

            except Exception as e:
                error_msg = str(e)
                # Check if this is a stale element reference during click —
                # if so, attempt one recovery click before giving up.
                if "stale element" in error_msg.lower():
                    time.sleep(2)
                    try:
                        # Re-locate the button from current DOM state
                        fresh_candidates = self._collect_source_dom_candidates()
                        fresh_source_buttons = [
                            elem
                            for elem in fresh_candidates
                            if self._is_source_element(elem, source_id, vid)
                        ]
                        target_btn = None
                        if fresh_source_buttons:
                            target_btn = fresh_source_buttons[0]
                        if not target_btn:
                            src_pos = list(vid_to_src.keys()).index(vid)
                            if src_pos < len(fresh_candidates):
                                target_btn = fresh_candidates[src_pos]
                        if target_btn:
                            self._driver.execute_script(
                                "arguments[0].scrollIntoView({block:'center'});", target_btn
                            )
                            time.sleep(0.3)
                            self._driver.execute_script("arguments[0].click();", target_btn)
                            did_click = True
                            body_text = self._wait_for_transcript_ready(timeout=20.0)
                            transcript = self._extract_transcript_from_body(body_text)
                            if transcript:
                                context_not_ready_streak = 0
                                results[vid] = (True, transcript, None)
                                log_action(
                                    "industrial_scrape_video_finished",
                                    {
                                        "nb_id": self._staging_nb_id,
                                        "video_id": vid,
                                        "source_id": source_id,
                                        "index": idx,
                                        "batch_size": len(vid_to_src),
                                        "status": "success_stale_recovery",
                                        "transcript_chars": len(transcript),
                                        "elapsed_s": round(time.monotonic() - video_started_at, 3),
                                    },
                                )
                                print(f"{len(transcript)} chars (stale recovery)")
                            else:
                                context_not_ready_streak = 0
                                results[vid] = (False, None, "content too short or empty")
                                log_action(
                                    "industrial_scrape_video_finished",
                                    {
                                        "nb_id": self._staging_nb_id,
                                        "video_id": vid,
                                        "source_id": source_id,
                                        "index": idx,
                                        "batch_size": len(vid_to_src),
                                        "status": "too_short_stale_recovery",
                                        "elapsed_s": round(time.monotonic() - video_started_at, 3),
                                    },
                                )
                                print("✗ too short")
                        else:
                            print(
                                f"[Industrial] stale recovery lookup failed for vid={vid} "
                                f"source={source_id} buttons={len(fresh_source_buttons)} "
                                f"url={self._driver.current_url}"
                            )
                            if fresh_source_buttons:
                                recovery_preview = fresh_source_buttons
                            else:
                                recovery_preview = fresh_candidates
                            print(
                                f"[Industrial] recovery candidates: "
                                f"{self._button_label_preview(recovery_preview)}"
                            )
                            results[vid] = (False, None, "source button not found after stale recovery")
                            context_not_ready_streak = 0
                            log_action(
                                "industrial_scrape_video_finished",
                                {
                                    "nb_id": self._staging_nb_id,
                                    "video_id": vid,
                                    "source_id": source_id,
                                    "index": idx,
                                    "batch_size": len(vid_to_src),
                                    "status": "button_not_found_stale_recovery",
                                    "elapsed_s": round(time.monotonic() - video_started_at, 3),
                                },
                            )
                            print("✗ button not found after stale recovery")
                    except Exception:
                        # Failed even recovery — reload the notebook root so the
                        # next video iteration starts from a clean DOM state.
                        self._driver.get(
                            f"https://notebooklm.google.com/notebook/{self._staging_nb_id}"
                        )
                        time.sleep(3)
                        self._navigate_to_sources_tab()
                        results[vid] = (False, None, error_msg)
                        context_not_ready_streak = 0
                        log_action(
                            "industrial_scrape_video_finished",
                            {
                                "nb_id": self._staging_nb_id,
                                "video_id": vid,
                                "source_id": source_id,
                                "index": idx,
                                "batch_size": len(vid_to_src),
                                "status": "stale_recovery_exception",
                                "error": error_msg[:200],
                                "elapsed_s": round(time.monotonic() - video_started_at, 3),
                            },
                        )
                        print(f"✗ {error_msg}")
                else:
                    results[vid] = (False, None, error_msg)
                    context_not_ready_streak = 0
                    log_action(
                        "industrial_scrape_video_finished",
                        {
                            "nb_id": self._staging_nb_id,
                            "video_id": vid,
                            "source_id": source_id,
                            "index": idx,
                            "batch_size": len(vid_to_src),
                            "status": "error",
                            "error": error_msg[:200],
                            "elapsed_s": round(time.monotonic() - video_started_at, 3),
                        },
                    )
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
                            # Fallback: reload the notebook root and re-open Sources.
                            self._driver.get(
                                f"https://notebooklm.google.com/notebook/{self._staging_nb_id}"
                            )
                            time.sleep(3)
                            self._navigate_to_sources_tab()

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
        dom_ready = self._open_notebook_and_prepare_sources(notebook_id, len(vid_to_src))
        if dom_ready == -1:
            return {vid: (False, None, "browser auth unavailable") for vid in video_ids}

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
        log_action(
            "preflight_cleanup_complete",
            {
                "deleted": deleted,
                "failed": failed,
            },
        )
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
    parser.add_argument(
        "--readiness-matrix",
        action="store_true",
        help="Log per-source DOM spinner and CLI content-readiness timing during scraping",
    )
    parser.add_argument(
        "--readiness-probe-interval-s",
        type=float,
        default=1.0,
        help="Polling interval for readiness probes when --readiness-matrix is enabled",
    )
    parser.add_argument(
        "--readiness-probe-timeout-s",
        type=float,
        default=600.0,
        help="Timeout for readiness probes when --readiness-matrix is enabled",
    )
    args = parser.parse_args()

    video_ids: List[str] = []
    if args.video_ids:
        video_ids = args.video_ids.split(",")
    else:
        print("Error: --video-ids is required")
        sys.exit(1)

    scraper = NLMIndustrialScraper(
        headless=not args.no_headless,
        readiness_matrix=args.readiness_matrix,
        readiness_probe_interval_s=args.readiness_probe_interval_s,
        readiness_probe_timeout_s=args.readiness_probe_timeout_s,
    )
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
