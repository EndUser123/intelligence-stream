"""Tests for csf/ocr_client.py — EasyOCR code snippet extraction."""

import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.ocr_client import extract_code_snippets, _is_boilerplate


def _skip_if_no_easyocr():
    """Raise skip if easyocr is not available."""
    pytest.importorskip("easyocr")


class TestExtractCodeSnippets:
    """Tests for extract_code_snippets() EasyOCR wrapper."""

    def test_extract_code_snippets_returns_strings(self):
        """With synthetic images, verify list of strings is returned."""
        _skip_if_no_easyocr()
        mock_reader = mock.Mock()
        mock_reader.readtext.return_value = [
            (None, "def hello():", None),
            (None, "    print('hi')", None),
        ]

        with mock.patch("easyocr.Reader", return_value=mock_reader):
            with tempfile.TemporaryDirectory() as tmpdir:
                image_paths = [Path(tmpdir) / f"frame_{i:03d}.jpg" for i in range(3)]
                for p in image_paths:
                    p.touch()

                result = extract_code_snippets(image_paths)

        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_timeout_returns_empty_list(self):
        """Per-image timeout results in empty list for that image, not a crash."""
        _skip_if_no_easyocr()
        mock_reader = mock.Mock()
        mock_reader.readtext.side_effect = TimeoutError("timeout")

        with mock.patch("easyocr.Reader", return_value=mock_reader):
            with tempfile.TemporaryDirectory() as tmpdir:
                image_paths = [Path(tmpdir) / "frame_001.jpg"]
                image_paths[0].touch()

                result = extract_code_snippets(image_paths, timeout_per_image=0.001)

        assert result == []

    def test_ocr_exception_returns_empty_list(self):
        """Exception from OCR reader returns empty list, not a crash."""
        _skip_if_no_easyocr()
        mock_reader = mock.Mock()
        mock_reader.readtext.side_effect = RuntimeError("OCR error")

        with mock.patch("easyocr.Reader", return_value=mock_reader):
            with tempfile.TemporaryDirectory() as tmpdir:
                image_paths = [Path(tmpdir) / "frame_001.jpg"]
                image_paths[0].touch()

                result = extract_code_snippets(image_paths)

        assert result == []

    def test_boilerplate_filtered(self):
        """Boilerplate strings like 'Subscribe', 'Like', pure numbers are filtered out."""
        # Test the _is_boilerplate function directly
        assert _is_boilerplate("Subscribe") is True
        assert _is_boilerplate("Like") is True
        assert _is_boilerplate("Share") is True
        assert _is_boilerplate("123") is True
        assert _is_boilerplate("!!") is True
        assert _is_boilerplate("ab") is True  # too short
        assert _is_boilerplate("  ") is True  # whitespace only

        # Real code should NOT be filtered
        assert _is_boilerplate("def hello():") is False
        assert _is_boilerplate("import os") is False
        assert _is_boilerplate("x = 1 + 2") is False

    def test_deduplication(self):
        """Duplicate strings from multiple frames are deduplicated."""
        _skip_if_no_easyocr()
        mock_reader = mock.Mock()
        # Same text appears across multiple frames
        mock_reader.readtext.return_value = [
            (None, "def hello():", None),
            (None, "def hello():", None),  # duplicate
            (None, "print('hi')", None),
        ]

        with mock.patch("easyocr.Reader", return_value=mock_reader):
            with tempfile.TemporaryDirectory() as tmpdir:
                image_paths = [Path(tmpdir) / f"frame_{i:03d}.jpg" for i in range(3)]
                for p in image_paths:
                    p.touch()

                result = extract_code_snippets(image_paths)

        # Duplicates should be removed, order preserved
        assert "def hello():" in result
        assert "print('hi')" in result
        # Only 2 unique items
        assert len(result) == 2

    def test_empty_images_returns_empty(self):
        """Empty image list returns empty list."""
        result = extract_code_snippets([])
        assert result == []
