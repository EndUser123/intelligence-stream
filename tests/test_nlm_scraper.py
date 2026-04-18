"""Tests for nlm_scraper.py — terminal-local staging notebook."""

from __future__ import annotations

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

    def test_ensure_staging_reuses_existing_notebook_below_limit(self, scraper):
        """_ensure_staging_notebook reuses the current notebook below 300 sources."""
        scraper._staging_nb_id = "nb-existing"
        scraper._source_count = 50

        with mock.patch.object(scraper, "_run_nlm") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            result = scraper._ensure_staging_notebook()

        assert result is True
        assert scraper._staging_nb_id == "nb-existing"
        assert scraper._source_count == 50
        mock_run.assert_called_once_with(["notebook", "list"], timeout=30)

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
            with mock.patch.object(scraper, "get_source_ids") as mock_list:
                mock_list.return_value = ["src-A", "src-B", "src-C"]
                source_ids = scraper._add_sources_to_staging(["vid1", "vid2", "vid3"])

        assert source_ids == ["src-A", "src-B", "src-C"]

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
                        with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
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
                        with mock.patch.object(scraper._driver, "find_elements", side_effect=find_side_effect):
                            result = scraper._scrape_sources({"vid1": "src-1"})

        back_button.click.assert_called_once()