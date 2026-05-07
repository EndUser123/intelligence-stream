"""Tests for csf_selenium module."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\packages\yt-is").absolute()))

import pytest

from csf.csf_selenium import _fetch_via_selenium_only, _process_video


def test_csf_selenium_imports():
    """Smoke test: csf_selenium functions can be imported."""
    assert _fetch_via_selenium_only is not None
    assert _process_video is not None


# TODO: Add more tests based on actual functionality
# Run: pytest tests/test_csf_selenium.py -v
