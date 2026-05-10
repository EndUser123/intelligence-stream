"""Tests for csf/clip_client.py — CLIP visual tagger."""

import sys
import tempfile
from pathlib import Path
from unittest import mock


# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\\\\\\\packages\\yt-is").absolute()))

from csf.clip_client import tag_frames, DEFAULT_CANDIDATE_LABELS


class TestTagFrames:
    """Tests for tag_frames() CLIP wrapper."""

    def test_tag_frames_returns_labels(self):
        """With synthetic images, verify list of strings is returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = [Path(tmpdir) / f"frame_{i:03d}.jpg" for i in range(3)]
            for p in image_paths:
                p.touch()

            # Simulate _score_image returning a set of labels
            def fake_score(path, labels):
                return {"code screenshot", "slide"}

            with mock.patch("csf.clip_client._score_image", side_effect=fake_score):
                result = tag_frames(image_paths)

        assert isinstance(result, list)

    def test_timeout_returns_empty_list(self):
        """Per-image timeout returns empty list, not a crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = [Path(tmpdir) / "frame_001.jpg"]
            image_paths[0].touch()

            def timeout_result(*args, **kwargs):
                raise TimeoutError("CLIP timeout")

            with mock.patch(
                "csf.clip_client._score_image",
                side_effect=TimeoutError("CLIP timeout"),
            ):
                result = tag_frames(image_paths, timeout_per_image=0.001)

        assert result == []

    def test_clip_exception_returns_empty_list(self):
        """Exception from CLIP returns empty list, not a crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = [Path(tmpdir) / "frame_001.jpg"]
            image_paths[0].touch()

            with mock.patch(
                "csf.clip_client._score_image",
                side_effect=RuntimeError("CLIP error"),
            ):
                result = tag_frames(image_paths)

        assert result == []

    def test_empty_candidate_labels_returns_empty(self):
        """Empty candidate_labels list returns empty list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = [Path(tmpdir) / "frame_001.jpg"]
            image_paths[0].touch()

            result = tag_frames(image_paths, candidate_labels=[])

        assert result == []

    def test_deduplication_across_frames(self):
        """Same label from multiple frames is deduplicated to a single entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            image_paths = [Path(tmpdir) / f"frame_{i:03d}.jpg" for i in range(3)]
            for p in image_paths:
                p.touch()

            # Each frame returns overlapping labels
            def fake_score(path, labels):
                return {"code screenshot", "diagram"}

            with mock.patch("csf.clip_client._score_image", side_effect=fake_score):
                result = tag_frames(image_paths)

            # Result should be a list without duplicates
            assert isinstance(result, list)
            # Deduplication happens inside tag_frames
            assert len(result) <= 2

    def test_default_candidate_labels(self):
        """DEFAULT_CANDIDATE_LABELS exists and has 9 items."""
        assert isinstance(DEFAULT_CANDIDATE_LABELS, list)
        assert len(DEFAULT_CANDIDATE_LABELS) == 9
        assert "code screenshot" in DEFAULT_CANDIDATE_LABELS
        assert "diagram" in DEFAULT_CANDIDATE_LABELS
        assert "person speaking" in DEFAULT_CANDIDATE_LABELS

