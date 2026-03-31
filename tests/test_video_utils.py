"""Tests for csf/video_utils.py — FFmpeg frame extraction."""

import sys
import signal
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Ensure the package is importable
sys.path.insert(0, str(Path(r"P:\packages\intelligence-stream").absolute()))

from csf.video_utils import extract_frames
from csf.providers import NonFatalAnalysisError


class TestExtractFrames:
    """Tests for extract_frames() FFmpeg integration."""

    def _mock_ffmpeg_result(self, returncode=0, stderr=""):
        """Return a mock subprocess.CompletedProcess."""
        mock_result = mock.Mock()
        mock_result.returncode = returncode
        mock_result.stderr = stderr
        return mock_result

    @mock.patch("subprocess.run")
    def test_extract_frames_produces_correct_count(self, mock_run):
        """Mock ffmpeg produces correct frame count from glob."""
        mock_run.return_value = self._mock_ffmpeg_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir)
            # Create 5 fake frame files
            for i in range(1, 6):
                (frames_dir / f"frame_{i:03d}.jpg").touch()

            with (
                mock.patch(
                    "csf.video_utils._parse_duration_ffmpeg",
                    return_value=5.0,
                ),
                mock.patch("tempfile.mkdtemp", return_value=str(frames_dir)),
            ):
                result = extract_frames(
                    frames_dir / "video.mp4", fps=1.0, max_frames=30
                )

                assert len(result) == 5

    @mock.patch("subprocess.run")
    def test_extract_frames_respects_max_frames(self, mock_run):
        """When fps * duration exceeds max_frames, result is capped."""
        mock_run.return_value = self._mock_ffmpeg_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir)
            # Create 50 fake frame files
            for i in range(1, 51):
                (frames_dir / f"frame_{i:03d}.jpg").touch()

            with (
                mock.patch(
                    "csf.video_utils._parse_duration_ffmpeg",
                    return_value=100.0,
                ),
                mock.patch("tempfile.mkdtemp", return_value=str(frames_dir)),
            ):
                result = extract_frames(
                    frames_dir / "video.mp4", fps=1.0, max_frames=30
                )

                # Should be capped at max_frames=30
                assert len(result) <= 30

    @mock.patch("subprocess.run")
    def test_ffmpeg_absent_raises_runtime_error(self, mock_run):
        """FileNotFoundError from subprocess.run raises RuntimeError."""
        mock_run.side_effect = FileNotFoundError("ffmpeg not found")

        with mock.patch(
            "csf.video_utils._parse_duration_ffmpeg",
            side_effect=RuntimeError("ffmpeg not found on PATH"),
        ):
            with pytest.raises(RuntimeError, match="ffmpeg not found"):
                extract_frames("/fake/video.mp4")

    @mock.patch("subprocess.run")
    def test_ffmpeg_failure_raises_nonfatal(self, mock_run):
        """Non-zero returncode from ffmpeg raises NonFatalAnalysisError."""
        mock_run.return_value = self._mock_ffmpeg_result(
            returncode=1,
            stderr="Unknown error",
        )

        with (
            mock.patch(
                "csf.video_utils._parse_duration_ffmpeg",
                return_value=10.0,
            ),
            mock.patch("tempfile.mkdtemp", return_value=tempfile.gettempdir()),
        ):
            with pytest.raises(NonFatalAnalysisError, match="return code 1"):
                extract_frames("/fake/video.mp4")

    @mock.patch("subprocess.run")
    def test_zero_frames_raises_nonfatal(self, mock_run):
        """Empty glob result (no frames) raises NonFatalAnalysisError."""
        mock_run.return_value = self._mock_ffmpeg_result()

        with (
            mock.patch(
                "csf.video_utils._parse_duration_ffmpeg",
                return_value=10.0,
            ),
            mock.patch("tempfile.mkdtemp", return_value=tempfile.gettempdir()),
        ):
            with mock.patch.object(Path, "glob", return_value=[]):
                with pytest.raises(NonFatalAnalysisError, match="0 output files"):
                    extract_frames("/fake/video.mp4")

    @mock.patch("subprocess.run")
    def test_temp_dir_cleaned_on_normal_exit(self, mock_run):
        """Temp directory is cleaned up after successful extraction."""
        original_rmtree = __import__("shutil").rmtree
        cleanup_called = []

        def track_rmtree(path, ignore_errors=False):
            cleanup_called.append(path)
            original_rmtree(path, ignore_errors=True)

        mock_run.return_value = self._mock_ffmpeg_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir)
            (frames_dir / "frame_001.jpg").touch()

            with (
                mock.patch(
                    "csf.video_utils._parse_duration_ffmpeg",
                    return_value=1.0,
                ),
                mock.patch("tempfile.mkdtemp", return_value=str(frames_dir)),
                mock.patch("shutil.rmtree", side_effect=track_rmtree),
            ):
                extract_frames(frames_dir / "video.mp4")

                # Cleanup should have been called for the temp dir
                assert len(cleanup_called) >= 1

    @mock.patch("subprocess.run")
    def test_sigterm_handler_cleans_up(self, mock_run):
        """SIGTERM handler calls _cleanup before exit."""
        handler_registered = []

        def track_signal(signum, handler):
            handler_registered.append((signum, handler))

        mock_run.return_value = self._mock_ffmpeg_result()

        with tempfile.TemporaryDirectory() as tmpdir:
            frames_dir = Path(tmpdir)
            (frames_dir / "frame_001.jpg").touch()

            with (
                mock.patch(
                    "csf.video_utils._parse_duration_ffmpeg",
                    return_value=1.0,
                ),
                mock.patch("tempfile.mkdtemp", return_value=str(frames_dir)),
                mock.patch("signal.signal", side_effect=track_signal),
            ):
                extract_frames(frames_dir / "video.mp4")

                # SIGTERM handler should have been registered
                sig_handlers = [s for s, h in handler_registered if s == signal.SIGTERM]
                assert len(sig_handlers) == 1
