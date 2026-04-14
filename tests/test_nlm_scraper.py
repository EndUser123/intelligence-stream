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
            result = scraper._ensure_staging_notebook()

        assert result is True
        assert scraper._staging_nb_id == "nb-existing"
        assert scraper._source_count == 50
        mock_run.assert_not_called()  # No CLI call needed

    def test_ensure_staging_clears_and_recreates_at_capacity(self, scraper):
        """_ensure_staging_notebook clears and recreates at 300 sources."""
        scraper._staging_nb_id = "nb-old"
        scraper._source_count = 300

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

        with mock.patch.object(scraper, "_ensure_staging_notebook", return_value=True):
            with mock.patch.object(scraper, "_add_sources_to_staging") as mock_add:
                # First call: 300 sources added, then _source_count becomes 300
                # Second call: 50 sources added (the remainder)
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
        """close() calls _clear_staging_notebook before quitting driver."""
        scraper._staging_nb_id = "nb-to-clean"
        scraper._source_count = 50
        scraper._driver = mock.MagicMock()

        with mock.patch.object(scraper, "_clear_staging_notebook") as mock_clear:
            mock_clear.return_value = True
            scraper.close()

        mock_clear.assert_called_once()
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