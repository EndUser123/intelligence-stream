"""FFmpeg frame extraction utilities for the OCR/CLIP video analysis pipeline."""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from csf.providers import NonFatalAnalysisError


def _parse_duration_ffmpeg(video_path: Path) -> float:
    """Parse video duration in seconds using ffmpeg -i.

    Extracts the Duration line from ffmpeg's stderr output and converts
    it to seconds. Returns 0.0 if the duration cannot be determined.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(video_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH")

    for line in result.stderr.splitlines():
        if "Duration:" not in line:
            continue
        # Format: "Duration: HH:MM:SS.ms"
        token = line.split("Duration:", 1)[1].split(",")[0].strip()
        if token:
            try:
                h, m, s = token.split(":")
                return float(h) * 3600 + float(m) * 60 + float(s)
            except (ValueError, IndexError):
                pass
    return 0.0


def extract_frames(
    video_path: str | Path,
    fps: float = 1.0,
    max_frames: int = 30,
) -> list[Path]:
    """Extract frames from a video file using FFmpeg.

    Uses uniform frame sampling at the requested FPS and returns paths
    to the extracted JPEG files in a scoped temporary directory that is
    automatically cleaned up on exit or signal termination.

    Args:
        video_path: Path to the input video file.
        fps: Frames per second for uniform sampling (default 1.0).
        max_frames: Maximum number of frames to extract (default 30).

    Returns:
        List of Path objects for the extracted frame JPEG files, sorted
        by name (i.e. chronological order).

    Raises:
        NonFatalAnalysisError: FFmpeg ran but failed (non-zero return code)
            or the output was empty — indicates a recoverable error that
            the analysis pipeline should handle gracefully.
        RuntimeError: FFmpeg is not installed or not on PATH — a truly
            unrecoverable state.
    """
    video_path = Path(video_path)
    temp_dir: Path | None = None

    def _cleanup() -> None:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _sigterm_handler(_sig: int, _frame) -> None:
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        duration = _parse_duration_ffmpeg(video_path)
        target_count = min(int(duration * fps), max_frames)

        temp_dir = Path(tempfile.mkdtemp(prefix="video_frames_"))

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps}",
            "-q:v",
            "2",
            str(temp_dir / "frame_%03d.jpg"),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg not found on PATH") from exc

        if result.returncode != 0:
            raise NonFatalAnalysisError(
                f"ffmpeg frame extraction failed for {video_path} "
                f"(return code {result.returncode}): {result.stderr[:500]}"
            )

        frames = sorted(temp_dir.glob("frame_*.jpg"))

        if not frames:
            raise NonFatalAnalysisError(
                f"No frames extracted for {video_path} — "
                f"ffmpeg produced 0 output files"
            )

        # Return only up to max_frames
        return frames[:target_count]

    finally:
        _cleanup()
