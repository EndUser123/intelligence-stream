"""RED phase test for TASK-2565: BatchConfig dataclass."""

import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(r"P:\\\packages\\yt-is")
sys.path.insert(0, str(_ROOT))

from csf.batch import BatchConfig, analyze_videos_parallel  # noqa: E402


class TestBatchConfigDataclass:
    """Verify BatchConfig dataclass structure and backward compatibility."""

    def test_batch_config_is_dataclass(self):
        """BatchConfig is a dataclass with max_workers, force, progress_callback fields."""
        assert hasattr(
            BatchConfig, "__dataclass_fields__"
        ), "BatchConfig should be a dataclass"
        fields = BatchConfig.__dataclass_fields__
        assert "max_workers" in fields, "BatchConfig needs max_workers field"
        assert "force" in fields, "BatchConfig needs force field"
        assert (
            "progress_callback" in fields
        ), "BatchConfig needs progress_callback field"

    def test_batch_config_defaults(self):
        """BatchConfig has sensible defaults: max_workers=4, force=False, progress_callback=None."""
        config = BatchConfig()
        assert config.max_workers == 4
        assert config.force is False
        assert config.progress_callback is None

    def test_batch_config_accepts_values(self):
        """BatchConfig can be constructed with explicit values."""
        callback = mock.Mock()
        config = BatchConfig(max_workers=8, force=True, progress_callback=callback)
        assert config.max_workers == 8
        assert config.force is True
        assert config.progress_callback is callback

    def test_analyze_videos_parallel_accepts_batch_config(self, tmp_path):
        """analyze_videos_parallel accepts BatchConfig as second positional argument."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.batch._get_analyze_video") as mock_get,
        ):
            mock_analyze = mock.Mock(return_value={"title": "test"})
            mock_get.return_value = mock_analyze
            config = BatchConfig(max_workers=2, force=True)
            result = analyze_videos_parallel(["dQw4w9WgXcQ"], config)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_analyze_videos_parallel_batch_config_kwarg(self, tmp_path):
        """analyze_videos_parallel accepts BatchConfig as keyword argument."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.batch._get_analyze_video") as mock_get,
        ):
            mock_analyze = mock.Mock(return_value={"title": "test"})
            mock_get.return_value = mock_analyze
            config = BatchConfig(max_workers=2, force=True)
            result = analyze_videos_parallel(["dQw4w9WgXcQ"], batch_config=config)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_backward_compat_individual_kwargs(self, tmp_path):
        """Existing callers using individual kwargs still work (backward compat)."""
        with (
            mock.patch.dict(
                "os.environ", {"INTELLIGENCE_STREAM_LOG_DIR": str(tmp_path / ".logs")}
            ),
            mock.patch("csf.batch._get_analyze_video") as mock_get,
        ):
            mock_analyze = mock.Mock(return_value={"title": "test"})
            mock_get.return_value = mock_analyze
            # Old-style call: individual kwargs, no BatchConfig
            result = analyze_videos_parallel(["dQw4w9WgXcQ"], max_workers=2, force=True)
        assert isinstance(result, tuple)
        assert len(result) == 2

