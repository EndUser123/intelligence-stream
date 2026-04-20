"""Tests for nlm_scraper.py — terminal-local staging notebook."""

from __future__ import annotations

import json
# import time  # noqa: F401
from pathlib import Path
from unittest import mock

import pytest

# Smuggle in conftest fixtures via the package's test setup
import sys

sys.path.insert(0, str(Path(r"P:\packages\yt-is").absolute()))


class TestNLMIndustrialScraperStagingNotebook:
    """Test staging notebook lifecycle: create, add, auto-clear at 300."""

    @pytest.fixture
    def scraper(self):
        """Build a scraper instance without initializing the driver."""
        from csf.nlm_scraper import NLMIndustrialScraper

        sc = NLMIndustrialScraper(headless=True)
        # Don't init driver — we mock all driver interactions
        return sc

    def test_staging_notebook_created_on_first_use(self, scraper):
        """Staging notebook is not created until first scrape call."""
        assert scraper._staging_nb_id is None
        assert scraper._source_count == 0

    def test_auto_switch_to_staging_when_no_notebook_id(self, scraper):
        """scrape_notebook(None, ...) delegates to scrape_with_staging."""
        with mock.patch.object(scraper, "scrape_with_staging") as mock_sw:
            mock_sw.return_value = {}
            scraper.scrape_notebook(None, ["vid1"])
            mock_sw.assert_called_once_with(["vid1"])

    def test_auto_switch_to_staging_when_notebook_is_staging(self, scraper):
        """scrape_notebook('staging', ...) delegates to scrape_with_staging."""
        with mock.patch.object(scraper, "scrape_with_staging") as mock_sw:
            mock_sw.return_value = {}
            scraper.scrape_notebook("staging", ["vid1"])
            mock_sw.assert_called_once_with(["vid1"])

    def test_ensure_staging_creates_notebook_on_first_call(self, scraper):
        """_ensure_staging_notebook creates a new notebook when none exists."""
        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="✓ Created notebook: test\n  ID: nb-12345",
                stderr="",
            )
            result = scraper._ensure_staging_notebook()
            assert result is True
            assert scraper._staging_nb_id == "nb-12345"
            assert scraper._source_count == 0

    def test_create_staging_notebook_logs_lifecycle(self, scraper):
        """_create_staging_notebook emits start and success markers."""
        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="✓ Created notebook: test\n  ID: nb-12345",
                stderr="",
            )
            with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                nb_id = scraper._create_staging_notebook()

        assert nb_id == "nb-12345"
        assert [c.args[0] for c in mock_log.call_args_list] == [
            "staging_notebook_create_started",
            "staging_notebook_create_succeeded",
        ]

    def test_ensure_staging_reuses_existing_notebook_below_limit(self, scraper):
        """_ensure_staging_notebook reuses the current notebook below 300 sources."""
        scraper._staging_nb_id = "nb-existing"
        scraper._source_count = 50

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch("builtins.print") as mock_print:
                result = scraper._ensure_staging_notebook()

        assert result is True
        assert scraper._staging_nb_id == "nb-existing"
        assert scraper._source_count == 50
        mock_run.assert_called_once_with(["notebook", "list"], timeout=30)
        mock_print.assert_any_call("[Industrial] Reusing staging notebook (50 sources, room for 250)")

    def test_ensure_staging_clears_and_recreates_at_capacity(self, scraper):
        """_ensure_staging_notebook clears and recreates at 300 sources."""
        scraper._staging_nb_id = "nb-old"
        scraper._source_count = 300

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(scraper, "_clear_staging_notebook") as mock_clear:
                mock_clear.return_value = True
                with mock.patch.object(scraper, "_create_staging_notebook") as mock_create:
                    mock_create.return_value = "nb-new"
                    scraper._ensure_staging_notebook()

        mock_clear.assert_called_once()
        mock_create.assert_called_once()
        assert scraper._staging_nb_id == "nb-new"
        assert scraper._source_count == 0

    def test_clear_staging_notebook_deletes_and_resets_state(self, scraper):
        """_clear_staging_notebook calls delete, clears ID and count."""
        scraper._staging_nb_id = "nb-to-delete"
        scraper._source_count = 150

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            result = scraper._clear_staging_notebook()

        assert result is True
        assert scraper._staging_nb_id is None
        assert scraper._source_count == 0

    def test_add_sources_returns_source_ids_in_order(self, scraper):
        """_add_sources_to_staging returns source IDs in the same order as input video IDs."""
        scraper._staging_nb_id = "nb-test"

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            # Simulate nlm returning source IDs
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(scraper, "_list_source_ids_process") as mock_list:
                mock_list.return_value = mock.MagicMock(
                    returncode=0,
                    stdout=json.dumps({"sources": [{"id": "src-A"}, {"id": "src-B"}, {"id": "src-C"}]}),
                    stderr="",
                )
                source_ids = scraper._add_sources_to_staging(["vid1", "vid2", "vid3"])

        assert source_ids == ["src-A", "src-B", "src-C"]
        assert "--wait" not in mock_run.call_args.args[0]

    def test_add_sources_waits_for_source_list_materialization(self, scraper):
        """_add_sources_to_staging waits until the added sources appear in source list."""
        scraper._staging_nb_id = "nb-test"

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(scraper, "_list_source_ids_process") as mock_list:
                mock_list.side_effect = [
                    mock.MagicMock(returncode=0, stdout=json.dumps({"sources": []}), stderr=""),
                    mock.MagicMock(returncode=0, stdout=json.dumps({"sources": []}), stderr=""),
                    mock.MagicMock(
                        returncode=0,
                        stdout=json.dumps({"sources": [{"id": "src-B"}, {"id": "src-A"}]}),
                        stderr="",
                    ),
                ]
                with mock.patch("csf.nlm_scraper.time.sleep"):
                    with mock.patch("builtins.print") as mock_print:
                        source_ids = scraper._add_sources_to_staging(["vid1", "vid2"])

        assert source_ids == ["src-B", "src-A"]
        assert mock_list.call_count == 3
        mock_print.assert_any_call("[Industrial] Sub-batch 1: waiting for 2 sources in NLM...")

    def test_add_sources_logs_timeout_details_when_source_list_never_materializes(self, scraper):
        """_wait_for_source_ids_ready logs the raw source-list output on timeout."""
        scraper._staging_nb_id = "nb-test"

        class FakeClock:
            def __init__(self):
                self.value = 0

            def __call__(self):
                current = self.value
                self.value += 3
                return current

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(scraper, "_list_source_ids_process") as mock_list:
                mock_list.return_value = mock.MagicMock(
                    returncode=0,
                    stdout=json.dumps({"sources": []}),
                    stderr="",
                )
                with mock.patch("csf.nlm_scraper.time.sleep"):
                    with mock.patch("csf.nlm_scraper.time.time", new=FakeClock()):
                        with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                            source_ids = scraper._add_sources_to_staging(["vid1", "vid2"])

        assert source_ids is None
        assert mock_list.call_count > 0
        assert any(c.args[0] == "staging_source_materialization_wait_timeout" for c in mock_log.call_args_list)

    def test_scrape_with_staging_increments_source_count(self, scraper):
        """scrape_with_staging increments _source_count after adding sources."""
        scraper._staging_nb_id = "nb-test"
        scraper._source_count = 0

        with mock.patch.object(scraper, "_ensure_staging_notebook", return_value=True):
            with mock.patch.object(scraper, "_add_sources_to_staging") as mock_add:
                mock_add.return_value = ["src-1", "src-2"]
                with mock.patch.object(scraper, "_scrape_sources") as mock_scrape:
                    mock_scrape.return_value = {"vid1": (True, "text", None), "vid2": (True, "text", None)}
                    scraper.scrape_with_staging(["vid1", "vid2"])

        assert scraper._source_count == 2

    def test_scrape_with_staging_overflow_loops(self, scraper):
        """scrape_with_staging processes >300 videos via a while-loop, not recursion."""
        scraper._staging_nb_id = "nb-test"
        scraper._source_count = 0

        call_count = [0]

        def ensure_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return True
            # Simulate: at capacity → clears → recreates → returns True
            scraper._source_count = 0
            return True

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper, "_ensure_staging_notebook", side_effect=ensure_side_effect):
                with mock.patch.object(scraper, "_add_sources_to_staging") as mock_add:
                    mock_add.side_effect = [
                        [f"src-{i}" for i in range(300)],
                        [f"src-{i}" for i in range(50)],
                    ]
                    with mock.patch.object(scraper, "_scrape_sources") as mock_scrape:
                        mock_scrape.return_value = {}
                        scraper.scrape_with_staging([f"vid{i}" for i in range(350)])

        # Two batches: 300 + 50
        assert mock_add.call_count == 2
        # Scrape was called twice (once per batch)
        assert mock_scrape.call_count == 2

    def test_close_clears_staging_notebook(self, scraper):
        """close() calls _cleanup_staging_on_close before quitting driver."""
        scraper._staging_nb_id = "nb-to-clean"
        scraper._source_count = 50
        scraper._driver = mock.MagicMock()

        with mock.patch.object(scraper, "_cleanup_staging_on_close") as mock_cleanup:
            mock_cleanup.return_value = None
            scraper.close()

        mock_cleanup.assert_called_once()
        assert scraper._staging_nb_id is None
        assert scraper._source_count == 0


class TestNLMIndustrialScraperPerNotebook:
    """Test the original scrape_notebook path (explicit notebook ID)."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        sc = NLMIndustrialScraper(headless=True)
        return sc

    def test_explicit_notebook_id_does_not_use_staging(self, scraper):
        """scrape_notebook with a real notebook ID does NOT delegate to staging."""
        with mock.patch.object(scraper, "get_source_ids", return_value=[]):
            with mock.patch.object(scraper, "_init_driver"):
                with mock.patch.object(scraper._driver.__class__.get if scraper._driver else mock.MagicMock(), "get"):
                    pass
            # Should NOT call scrape_with_staging
            with mock.patch.object(scraper, "scrape_with_staging") as mock_sw:
                mock_sw.return_value = {}
                scraper.scrape_notebook("real-nb-id", ["vid1"])
                mock_sw.assert_not_called()

    def test_scrape_notebook_does_not_use_fixed_startup_sleep(self, scraper):
        """The explicit notebook path should not wait a fixed 15 seconds before readiness checks."""
        scraper._driver = mock.MagicMock()
        with mock.patch.object(scraper, "get_source_ids", return_value=["src-1"]):
            with mock.patch.object(scraper, "_init_driver"):
                with mock.patch.object(scraper._driver, "get"):
                    with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                        with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                            with mock.patch.object(scraper._driver, "find_elements", return_value=[]):
                                with mock.patch("csf.nlm_scraper.time.sleep") as mock_sleep:
                                    scraper.scrape_notebook("real-nb-id", ["vid1"])

        assert not any(call.args and call.args[0] == 15 for call in mock_sleep.call_args_list)

    def test_scrape_notebook_does_not_double_navigate_sources_tab(self, scraper):
        """The explicit notebook path should only establish Sources context once."""
        scraper._driver = mock.MagicMock()
        with mock.patch.object(scraper, "get_source_ids", return_value=["src-1"]):
            with mock.patch.object(scraper, "_init_driver"):
                with mock.patch.object(scraper._driver, "get"):
                    with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                        with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                            with mock.patch.object(scraper._driver, "find_elements", return_value=[]):
                                with mock.patch.object(scraper, "_navigate_to_sources_tab") as nav:
                                    with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                                        scraper.scrape_notebook("real-nb-id", ["vid1"])

        nav.assert_not_called()


class TestConsecutiveFailureBail:
    """Test Fix 3: consecutive notebook-creation failure counter."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper
        sc = NLMIndustrialScraper(headless=True)
        return sc

    def test_consecutive_failure_counter_initialized_to_zero(self, scraper):
        """_consecutive_nb_create_failures starts at 0."""
        assert scraper._consecutive_nb_create_failures == 0

    def test_consecutive_failures_bails_after_3(self, scraper):
        """_ensure_staging_notebook returns False after 3 consecutive creation failures."""
        # Simulate 3 failed creation attempts via the retry loop
        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=1, stdout="", stderr="fail")
            with mock.patch("subprocess.run") as mock_subproc:
                mock_subproc.return_value = mock.MagicMock(returncode=0)
                result = scraper._ensure_staging_notebook()

        assert result is False
        assert scraper._consecutive_nb_create_failures == 1

    def test_consecutive_failures_resets_on_success(self, scraper):
        """_consecutive_nb_create_failures resets to 0 on successful notebook creation."""
        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="✓ Created notebook: test\n  ID: nb-abc123",
                stderr="",
            )
            scraper._ensure_staging_notebook()

        assert scraper._consecutive_nb_create_failures == 0

    def test_scrape_with_staging_returns_error_on_consecutive_failure_bail(self, scraper):
        """scrape_with_staging returns failure dict when consecutive failure limit is hit."""
        scraper._consecutive_nb_create_failures = 3

        with mock.patch.object(scraper, "_ensure_staging_notebook", return_value=False):
            result = scraper.scrape_with_staging(["vid1", "vid2"])

        assert result == {
            "vid1": (False, None, "staging notebook unavailable"),
            "vid2": (False, None, "staging notebook unavailable"),
        }


class TestRunNlmAuthRetry:
    """Test Fix 4: auth-error retry loop in _run_nlm — 4 auth/stability fixes."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper
        sc = NLMIndustrialScraper(headless=True)
        return sc

    def test_run_nlm_succeeds_no_retry(self, scraper):
        """_run_nlm returns immediately on success — no auth check needed."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            res = scraper._run_nlm(["notebook", "list"])

        assert res.returncode == 0
        assert mock_run.call_count == 1

    def test_run_nlm_non_auth_failure_no_retry(self, scraper):
        """_run_nlm returns failure immediately when error is NOT auth-related."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="Server error 500"
            )
            res = scraper._run_nlm(["notebook", "create", "test"])

        assert res.returncode == 1
        assert mock_run.call_count == 1  # No retry for non-auth errors

    def test_run_nlm_auth_error_retries_after_reauth(self, scraper):
        """_run_nlm detects auth error, re-auths, then retries the command once."""
        call_count = [0]

        def run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock.MagicMock(returncode=1, stdout="", stderr="Authentication Error: token expired")
            return mock.MagicMock(returncode=0, stdout="retry-success", stderr="")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = run_side_effect
            res = scraper._run_nlm(["notebook", "list"])

        # call_count: 1 for failed attempt, 2 for login, 3 for retry
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        # First call: the actual command (failed)
        assert calls[0][0][0] == ["nlm", "notebook", "list"]
        # Second call: login --force
        assert calls[1][0][0] == ["nlm", "login", "--force"]
        # Third call: retry of original command
        assert calls[2][0][0] == ["nlm", "notebook", "list"]
        assert res.stdout == "retry-success"

    def test_run_nlm_auth_error_login_fails_no_retry(self, scraper):
        """_run_nlm does NOT retry command if re-auth also fails."""
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="Authentication Error"
            )
            res = scraper._run_nlm(["notebook", "list"])

        assert mock_run.call_count == 2  # Original + login (both fail)
        assert res.returncode == 1

    def test_run_nlm_auth_error_case_insensitive(self, scraper):
        """Auth error detection matches 'auth error' (lowercase) and 'Auth Error' (title case)."""
        for err_text in ["Authentication Error", "authentication error", "Auth Error", "auth error"]:
            call_count = [0]

            def run_side_effect(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    return mock.MagicMock(returncode=1, stdout="", stderr=err_text)
                return mock.MagicMock(returncode=0, stdout="ok", stderr="")

            with mock.patch("subprocess.run") as mock_run:
                mock_run.side_effect = run_side_effect
                scraper._consecutive_nb_create_failures = 0
                res = scraper._run_nlm(["notebook", "list"])

            assert mock_run.call_count == 3, f"Failed for: {err_text}"

    def test_run_nlm_mixed_case_auth_in_stdout(self, scraper):
        """Auth error is detected in stdout as well as stderr."""
        call_count = [0]

        def run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock.MagicMock(returncode=1, stdout="auth error detected", stderr="")
            return mock.MagicMock(returncode=0, stdout="ok", stderr="")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = run_side_effect
            res = scraper._run_nlm(["notebook", "list"])

        assert mock_run.call_count == 3  # Original + login + retry


class TestNlmAuthLogging:
    """Auth helpers should emit explicit NotebookLM auth markers."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        sc = NLMIndustrialScraper(headless=True)
        sc._staging_nb_id = "nb-test"
        sc._source_count = 0
        return sc

    def test_ensure_staging_notebook_logs_auth_ok(self, scraper):
        """A clean notebook list check should log auth-ok."""
        scraper._consecutive_nb_create_failures = 0
        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                assert scraper._ensure_staging_notebook() is True

        assert [c.args[0] for c in mock_log.call_args_list] == ["nlm_auth_checked"]
        assert mock_log.call_args.args[1]["component"] == "nlm_scraper"

    def test_run_nlm_auth_retry_logs_refresh(self, scraper):
        """A mid-session auth retry should log auth-refresh."""
        call_count = [0]

        def run_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock.MagicMock(returncode=1, stdout="", stderr="Authentication Error: token expired")
            if call_count[0] == 2:
                return mock.MagicMock(returncode=0, stdout="", stderr="")
            return mock.MagicMock(returncode=0, stdout="ok", stderr="")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = run_side_effect
            with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                res = scraper._run_nlm(["notebook", "list"])

        assert res.returncode == 0
        assert [c.args[0] for c in mock_log.call_args_list] == [
            "nlm_login_started",
            "nlm_login_completed",
            "nlm_auth_refreshed",
        ]
        assert mock_log.call_args.args[1]["component"] == "nlm_scraper"


class TestPreflightCleanupLogging:
    """Preflight cleanup should emit a structured outcome marker."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        return NLMIndustrialScraper(headless=True)

    def test_preflight_cleanup_logs_summary(self, scraper):
        """preflight_cleanup logs a structured completion summary."""
        def run_side_effect(args, timeout=0):
            if args[:3] == ["notebook", "list", "--json"]:
                return mock.MagicMock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "notebooks": [
                                {"name": "staging_123", "id": "nb-orphan"},
                                {"name": "regular", "id": "nb-keep"},
                            ]
                        }
                    ),
                    stderr="",
                )
            if args[:4] == ["notebook", "delete", "nb-orphan", "--confirm"]:
                return mock.MagicMock(returncode=0, stdout="", stderr="")
            return mock.MagicMock(returncode=1, stdout="", stderr="unexpected call")

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.side_effect = run_side_effect
            with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                deleted, failed = scraper.preflight_cleanup()

        assert deleted == 1
        assert failed == 0
        assert mock_log.call_args_list[-1].args == (
            "preflight_cleanup_complete",
            {"deleted": 1, "failed": 0},
        )


class TestSeleniumPreflightBrowserCleanup:
    """Selenium preflight cleanup should only target orphaned yt-is sessions."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        return NLMIndustrialScraper(headless=True)

    def test_collect_orphaned_selenium_pids_skips_live_fetch_sessions(self, scraper):
        """Only stale Selenium processes without a live fetch ancestor should be targeted."""

        class FakeProc:
            def __init__(self, pid, name, cmdline, parent=None):
                self.pid = pid
                self._name = name
                self._cmdline = cmdline
                self._parent = parent

            def name(self):
                return self._name

            def cmdline(self):
                return self._cmdline

            def parent(self):
                return self._parent

        live_fetch = FakeProc(100, "python.exe", ["python", "bin/csf-source", "fetch"])
        live_driver = FakeProc(101, "geckodriver.exe", ["geckodriver.exe", "--port", "59291"], parent=live_fetch)
        live_browser = FakeProc(
            102,
            "firefox.exe",
            [
                "firefox.exe",
                "--marionette",
                "--headless",
                "-profile",
                r"C:\Users\brsth\AppData\Local\yt-is\selenium-profiles\firefox\live",
            ],
            parent=live_driver,
        )
        stale_driver = FakeProc(201, "geckodriver.exe", ["geckodriver.exe", "--port", "65235"], parent=None)
        stale_browser = FakeProc(
            202,
            "firefox.exe",
            [
                "firefox.exe",
                "--marionette",
                "--headless",
                "-profile",
                r"C:\Users\brsth\AppData\Local\yt-is\selenium-profiles\firefox\stale",
            ],
            parent=stale_driver,
        )
        unrelated = FakeProc(301, "firefox.exe", ["firefox.exe", "--profile", r"C:\Users\brsth\AppData\Local\Mozilla\Firefox\Profiles\default"], parent=None)

        with mock.patch("psutil.process_iter", return_value=[live_fetch, live_driver, live_browser, stale_driver, stale_browser, unrelated]):
            pids = scraper._collect_orphaned_selenium_pids()

        assert 202 in pids
        assert 201 in pids
        assert 102 not in pids
        assert 101 not in pids
        assert 301 not in pids

    def test_preflight_browser_cleanup_logs_summary(self, scraper):
        """preflight_browser_cleanup logs a structured completion summary."""
        with mock.patch.object(scraper, "_collect_orphaned_selenium_pids", return_value={201, 202}) as mock_collect:
            with mock.patch.object(scraper, "_terminate_process_tree", side_effect=[(2, 0), (1, 1)]) as mock_terminate:
                with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                    killed, failed = scraper.preflight_browser_cleanup()

        assert mock_collect.called
        assert mock_terminate.call_count == 2
        assert killed == 3
        assert failed == 1
        mock_log.assert_any_call(
            "selenium_preflight_cleanup_started",
            {"matched_pids": 2, "pids": [201, 202]},
        )
        mock_log.assert_any_call(
            "selenium_preflight_cleanup_complete",
            {"killed": 3, "failed": 1, "matched_pids": 2},
        )


class TestSeleniumProfileIsolation:
    """Selenium should use a dedicated profile directory."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        return NLMIndustrialScraper(headless=True)

    def test_init_driver_uses_dedicated_chrome_profile_root(self, scraper, tmp_path, monkeypatch):
        """Chrome should launch with a Selenium-only user-data-dir, not the shared MCP profile."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)

        chrome_mock = mock.MagicMock()
        with mock.patch("csf.nlm_scraper.webdriver.Chrome", return_value=chrome_mock) as mock_chrome:
            with mock.patch.object(scraper, "_seed_profile_tree") as mock_seed:
                with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                    scraper._init_driver()

        mock_seed.assert_called_once()
        mock_log.assert_any_call(
            "selenium_profile_selected",
            mock.ANY,
        )
        opts = mock_chrome.call_args.kwargs["options"]
        user_data_dir_args = [arg for arg in opts.arguments if arg.startswith("--user-data-dir=")]
        assert len(user_data_dir_args) == 1
        assert "selenium-profiles/chrome" in user_data_dir_args[0].replace("\\", "/")
        assert "mcp-chrome-9050243" not in user_data_dir_args[0]

    def test_init_driver_uses_dedicated_firefox_profile_root(self, scraper, tmp_path, monkeypatch):
        """Firefox fallback should also use a Selenium-only profile root."""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("APPDATA", raising=False)

        ff_source = tmp_path / "Mozilla" / "Firefox" / "Profiles" / "6wM6Ep4x.Profile 1"
        ff_source.mkdir(parents=True, exist_ok=True)
        (ff_source / "prefs.js").write_text('user_pref("browser.startup.homepage", "about:blank");', encoding="utf-8")

        chrome_fail = RuntimeError("Chrome boom")
        firefox_mock = mock.MagicMock()
        with mock.patch("csf.nlm_scraper.webdriver.Chrome", side_effect=chrome_fail):
            with mock.patch("csf.nlm_scraper.webdriver.Firefox", return_value=firefox_mock) as mock_firefox:
                with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                    scraper._init_driver()

        opts = mock_firefox.call_args.kwargs["options"]
        profile_args = [arg for arg in opts.arguments if arg == "-profile"]
        assert len(profile_args) == 1
        profile_index = opts.arguments.index("-profile") + 1
        profile_root = opts.arguments[profile_index]
        assert "selenium-profiles/firefox" in profile_root.replace("\\", "/")
        assert "Firefox/Profiles" not in profile_root.replace("\\", "/")
        assert scraper._profile_session_id in profile_root
        mock_log.assert_any_call(
            "selenium_profile_selected",
            mock.ANY,
        )


class TestBackNavPageStateGuard:
    """Test Fix 2: back-nav skips click when already on Sources tab."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper
        sc = NLMIndustrialScraper(headless=True)
        sc._driver = mock.MagicMock()
        return sc

    def test_back_nav_skipped_when_no_nav_needed(self, scraper):
        """_scrape_sources skips back-nav when current_url already has /source/ (no navigation happened)."""
        scraper._staging_nb_id = "nb-test"
        # Start on a source page — no navigation occurred, still on same URL
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        back_button = mock.MagicMock()
        back_button.get_attribute.return_value = "Back"
        back_button.text.strip.return_value = ""
        source_button = mock.MagicMock()
        source_button.get_attribute.return_value = "Open source src-1 for youtube.com/watch?v=vid1"
        source_button.text.strip.return_value = ""

        # No navigation occurs (no execute_script click simulation)
        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_wait_for_transcript_ready", return_value="transcript text"):
                    with mock.patch.object(scraper, "_extract_transcript_from_body", return_value="transcript text"):
                        def find_side_effect(*args, **kwargs):
                            if len(args) >= 2 and args[1] == "button":
                                return [back_button, source_button]
                            return []

                        scraper._driver.execute_script = lambda *a, **k: None
                        with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                            with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
                                with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                                        result = scraper._scrape_sources({"vid1": "src-1"})

        # current_url still has /source/ → guard sees it → skips back-nav
        back_button.click.assert_not_called()

    def test_back_nav_fires_after_navigation(self, scraper):
        """_scrape_sources clicks Back when a source click caused navigation to transcript."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123/hash"

        back_button = mock.MagicMock()
        back_button.get_attribute.return_value = "Back"
        back_button.text.strip.return_value = ""
        source_button = mock.MagicMock()
        source_button.get_attribute.return_value = "Open source src-1 for youtube.com/watch?v=vid1"
        source_button.text.strip.return_value = ""

        def mock_execute_script(script, *elems):
            """Emulate execute_script: click elements and simulate navigation."""
            if "click" in script and elems:
                elems[0].click()

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_wait_for_transcript_ready", return_value="transcript text"):
                    with mock.patch.object(scraper, "_extract_transcript_from_body", return_value="transcript text"):
                        def find_side_effect(*args, **kwargs):
                            if len(args) >= 2 and args[1] == "button":
                                return [back_button, source_button]
                            return []

                        scraper._driver.execute_script = mock_execute_script
                        with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                            with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
                                with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                                        result = scraper._scrape_sources({"vid1": "src-1"})

        back_button.click.assert_called_once()

    def test_chat_panel_button_is_not_selected_as_source(self, scraper):
        """_scrape_sources must ignore generic chat-panel buttons."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        chat_button = mock.MagicMock()
        chat_button.get_attribute.return_value = "Scrolls the chat panel to the bottom"
        chat_button.text.strip.return_value = ""

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_wait_for_transcript_ready", return_value="transcript text"):
                    with mock.patch.object(scraper, "_extract_transcript_from_body", return_value="transcript text"):
                        def find_side_effect(*args, **kwargs):
                            if len(args) >= 2 and args[1] == "button":
                                return [chat_button]
                            return []

                        scraper._driver.execute_script = lambda *a, **k: None
                        with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                            with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
                                with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                                        result = scraper._scrape_sources({"vid1": "src-1"})

        chat_button.click.assert_not_called()
        assert result["vid1"][0] is False
        assert result["vid1"][2] == "source button not found"

    def test_source_anchor_with_href_is_selected_as_source(self, scraper):
        """_scrape_sources should accept a source row rendered as an anchor."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        source_link = mock.MagicMock()
        source_link.text = "Episode 1"

        def link_attrs(name):
            return {
                "aria-label": "",
                "title": "Open source",
                "href": "https://notebooklm.google.com/notebook/nb-test/source/src-1",
            }.get(name, "")

        source_link.get_attribute.side_effect = link_attrs

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_wait_for_transcript_ready", return_value="transcript text"):
                    with mock.patch.object(scraper, "_extract_transcript_from_body", return_value="transcript text"):
                        def find_side_effect(*args, **kwargs):
                            if len(args) >= 2 and args[1] == "a":
                                return [source_link]
                            return []

                        scraper._driver.execute_script = lambda *a, **k: None
                        with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                            with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
                                with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                                        result = scraper._scrape_sources({"vid1": "src-1"})

        assert result["vid1"][0] is True
        assert result["vid1"][1] == "transcript text"

    def test_source_candidate_positional_fallback_is_used_when_exact_match_missing(self, scraper):
        """If exact matching fails, the source-row fallback should still click by position."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        source_row = mock.MagicMock()
        source_row.text = "Episode 1 - A long source title that looks like a source row"
        source_row.get_attribute.side_effect = lambda name: {
            "aria-label": "",
            "title": "Episode 1 - A long source title that looks like a source row",
            "href": "https://notebooklm.google.com/notebook/nb-test/source/row-1",
        }.get(name, "")

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_wait_for_transcript_ready", return_value="transcript text"):
                    with mock.patch.object(scraper, "_extract_transcript_from_body", return_value="transcript text"):
                        with mock.patch.object(scraper, "_collect_source_dom_candidates", return_value=[source_row]):
                            with mock.patch.object(scraper, "_is_source_element", return_value=False):
                                scraper._driver.execute_script = lambda *a, **k: None
                                with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=1):
                                        result = scraper._scrape_sources({"vid1": "src-1"})

        assert result["vid1"][0] is True
        assert result["vid1"][1] == "transcript text"

    def test_scrape_sources_does_not_use_fixed_startup_sleep(self, scraper):
        """The scrape startup path should not wait a fixed 15 seconds before checking readiness."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test"

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_ensure_sources_context", return_value=True):
                    with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=0):
                        with mock.patch.object(scraper._driver, "find_elements", return_value=[]):
                            with mock.patch("csf.nlm_scraper.time.sleep") as mock_sleep:
                                scraper._scrape_sources({"vid1": "src-1"})

        assert not any(call.args and call.args[0] == 15 for call in mock_sleep.call_args_list)


class TestSourcesContextGuard:
    """Test cheap URL-based Sources context recovery."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        sc = NLMIndustrialScraper(headless=True)
        sc._driver = mock.MagicMock()
        return sc

    def test_ensure_sources_context_skips_when_already_on_sources_page(self, scraper):
        """Already being on the Sources list should not trigger recovery work."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        with mock.patch.object(scraper, "_navigate_to_sources_tab") as nav:
            with mock.patch.object(scraper, "_count_source_buttons_dom", return_value=0):
                with mock.patch.object(scraper._driver, "get") as get:
                    assert scraper._ensure_sources_context("nb-test") is True

        nav.assert_not_called()
        get.assert_not_called()

    def test_ensure_sources_context_recovers_without_reload_when_drifted(self, scraper):
        """A shell-page drift should recover by clicking Sources, not reloading first."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test"

        def nav_side_effect():
            scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        with mock.patch.object(scraper, "_navigate_to_sources_tab", side_effect=nav_side_effect) as nav:
            with mock.patch.object(scraper, "_count_source_buttons_dom", side_effect=[0, 1]):
                with mock.patch.object(scraper._driver, "get") as get:
                    assert scraper._ensure_sources_context("nb-test") is True

        nav.assert_called_once()
        get.assert_not_called()

    def test_ensure_sources_context_logs_back_recovery(self, scraper):
        """A transcript-page drift should log back-button recovery."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123/details"

        back_button = mock.MagicMock()
        back_button.get_attribute.return_value = "Back"

        def exec_side_effect(script, element):
            scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/"

        scraper._driver.find_elements.return_value = [back_button]
        with mock.patch.object(scraper._driver, "execute_script", side_effect=exec_side_effect):
            with mock.patch.object(scraper, "_count_source_buttons_dom", return_value=0):
                with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                    assert scraper._ensure_sources_context("nb-test") is True

        assert mock_log.call_args_list[0].args[0] == "sources_context_recovery_started"
        assert mock_log.call_args_list[-1].args[0] == "sources_context_recovery_finished"
        assert mock_log.call_args_list[0].args[1]["method"] == "back"
        assert mock_log.call_args_list[-1].args[1]["method"] == "back"
        assert mock_log.call_args_list[-1].args[1]["status"] == "ok"

    def test_context_not_ready_streak_triggers_recovery_attempt(self, scraper):
        """Five consecutive context-not-ready results should trigger a recovery attempt."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test"

        vid_to_src = {f"vid{i}": f"src-{i}" for i in range(5)}
        ensure_calls = [False, False, False, False, False, False, True, True]

        def ensure_side_effect(*args, **kwargs):
            return ensure_calls.pop(0)

        with mock.patch.object(scraper, "_init_driver"):
            with mock.patch.object(scraper._driver, "get"):
                with mock.patch.object(scraper, "_poll_source_buttons_dom", return_value=None):
                    with mock.patch.object(scraper, "_ensure_sources_context", side_effect=ensure_side_effect):
                        with mock.patch.object(scraper, "_button_label_preview", return_value="(none)"):
                            with mock.patch("csf.nlm_scraper.time.sleep", return_value=None):
                                with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                                    result = scraper._scrape_sources(vid_to_src)

        assert all(not ok for ok, _, _ in result.values())
        actions = [c.args[0] for c in mock_log.call_args_list]
        assert "industrial_scrape_context_recovery_started" in actions
        assert "industrial_scrape_context_recovery_finished" in actions

    def test_ensure_sources_context_logs_reload_recovery_when_tab_click_fails(self, scraper):
        """If Sources-tab click doesn't land on Sources, the reload fallback should be logged."""
        scraper._staging_nb_id = "nb-test"
        scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test"

        nav_calls = [0]

        def nav_side_effect():
            nav_calls[0] += 1
            if nav_calls[0] >= 2:
                scraper._driver.current_url = "https://notebooklm.google.com/notebook/nb-test/source/abc123"

        with mock.patch.object(scraper, "_navigate_to_sources_tab", side_effect=nav_side_effect):
            with mock.patch.object(scraper, "_count_source_buttons_dom", side_effect=[0, 0, 1]):
                with mock.patch.object(scraper._driver, "get") as get:
                    with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                        assert scraper._ensure_sources_context("nb-test") is True

        get.assert_called_once_with("https://notebooklm.google.com/notebook/nb-test")
        methods = [c.args[1]["method"] for c in mock_log.call_args_list if c.args[0].startswith("sources_context_recovery_")]
        assert methods == ["sources_tab", "sources_tab", "reload", "reload"]
        statuses = [c.args[1].get("status") for c in mock_log.call_args_list if c.args[0] == "sources_context_recovery_finished"]
        assert statuses == ["not_recovered", "ok"]

    def test_ready_source_button_count_excludes_processing_rows_when_status_is_visible(self, scraper):
        """A source row still showing Processing should not be treated as ready."""

        class FakeElement:
            def __init__(self, text="", attrs=None, children=None):
                self._text = text
                self._attrs = attrs or {}
                self._children = children or []

            @property
            def text(self):
                return self._text

            def get_attribute(self, name):
                return self._attrs.get(name, "")

            def find_elements(self, by, selector):
                if selector == '[aria-label], [title], [alt]':
                    return self._children
                return []

        ready_row = FakeElement(
            text="Episode 1",
            attrs={
                "aria-label": "Open source src-1 for youtube.com/watch?v=vid1",
                "href": "https://notebooklm.google.com/notebook/nb-test/source/src-1",
            },
        )
        processing_icon = FakeElement(attrs={"aria-label": "Processing"})
        processing_row = FakeElement(
            text="Episode 2",
            attrs={
                "aria-label": "Open source src-2 for youtube.com/watch?v=vid2",
                "href": "https://notebooklm.google.com/notebook/nb-test/source/src-2",
            },
            children=[processing_icon],
        )

        with mock.patch.object(scraper, "_collect_source_dom_candidates", return_value=[ready_row, processing_row]):
            assert scraper._count_ready_source_buttons_dom() == 1

    def test_collect_source_dom_candidates_prefers_source_row_buttons_over_generic_chrome(self, scraper):
        """Source-row discovery should not count generic Google chrome buttons."""

        class FakeElement:
            def __init__(self, text="", attrs=None):
                self._text = text
                self._attrs = attrs or {}

            @property
            def text(self):
                return self._text

            def get_attribute(self, name):
                return self._attrs.get(name, "")

        source_row = FakeElement(
            attrs={
                "class": "source-stretched-button ng-tns-c3169959573-2",
                "aria-label": "Episode 1",
            }
        )
        google_apps = FakeElement(
            attrs={
                "class": "gb_C",
                "aria-label": "Google apps",
                "href": "https://www.google.ca/intl/en-GB/about/products",
            }
        )

        def find_side_effect(by, selector):
            if selector == "button.source-stretched-button":
                return [source_row]
            if selector in ("button", '[role="button"]', "a"):
                return [source_row, google_apps]
            return []

        with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
            candidates = scraper._collect_source_dom_candidates()

        assert candidates == [source_row]

    def test_collect_source_dom_candidates_dedupes_identical_source_rows(self, scraper):
        """Duplicate DOM mirrors of the same source row should count once."""

        class FakeElement:
            def __init__(self, text="", attrs=None):
                self._text = text
                self._attrs = attrs or {}

            @property
            def text(self):
                return self._text

            def get_attribute(self, name):
                return self._attrs.get(name, "")

        source_row_a = FakeElement(
            attrs={
                "class": "source-stretched-button ng-tns-c3169959573-2",
                "aria-label": "Episode 1",
            }
        )
        source_row_b = FakeElement(
            attrs={
                "class": "source-stretched-button ng-tns-c3169959573-2",
                "aria-label": "Episode 1",
            }
        )

        def find_side_effect(by, selector):
            if selector == "button.source-stretched-button":
                return [source_row_a, source_row_b]
            return []

        with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
            candidates = scraper._collect_source_dom_candidates()

        assert candidates == [source_row_a]


class TestBatchSummaryLogging:
    """Batch-level summary logging should be emitted once per scrape batch."""

    @pytest.fixture
    def scraper(self):
        from csf.nlm_scraper import NLMIndustrialScraper

        return NLMIndustrialScraper(headless=True)

    def test_scrape_with_staging_logs_batch_complete(self, scraper):
        """scrape_with_staging should log a final batch summary."""
        with mock.patch.object(scraper, "_ensure_staging_notebook", return_value=True):
            with mock.patch.object(scraper, "_add_sources_to_staging", return_value=["src-1"]):
                with mock.patch.object(scraper, "_init_driver"):
                    with mock.patch.object(scraper, "_scrape_sources", return_value={"vid1": (True, "t", None)}):
                        with mock.patch("csf.nlm_scraper.log_action") as mock_log:
                            result = scraper.scrape_with_staging(["vid1"])

        assert result == {"vid1": (True, "t", None)}
        actions = [c.args[0] for c in mock_log.call_args_list]
        assert "industrial_batch_complete" in actions
