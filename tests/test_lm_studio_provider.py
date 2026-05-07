"""Tests for LocalModelProvider and OllamaVisionProvider."""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(r"P:\\\packages\\yt-is").absolute()))


@pytest.fixture(autouse=True)
def _reset_provider():
    import csf.providers.lm_studio_provider
    csf.providers.lm_studio_provider._local_model_provider = None
    yield
    csf.providers.lm_studio_provider._local_model_provider = None


class TestLocalModelProvider:
    @mock.patch("csf.providers.lm_studio_provider.TranscriptProvider")
    def test_analyze_success(self, mock_transcript):
        mock_transcript.return_value.analyze.return_value = mock.MagicMock(
            summary="Test transcript summary"
        )
        mock_response = {
            "choices": [
                {
                    "message": {
                        "content": '{"title":"Test","summary":"OK","key_topics":["a"],"key_points":["b"]}'
                    }
                }
            ]
        }
        with mock.patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value.raise_for_status.return_value = None
            mock_client.return_value.__enter__.return_value.post.return_value.json.return_value = mock_response
            from csf.providers.lm_studio_provider import LocalModelProvider
            provider = LocalModelProvider()
            result = provider.analyze("dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ")
        assert result.title == "Test"
        assert result.mode == "local_model"

    def test_transcript_failure_raises_nonfatal(self):
        from csf.providers.lm_studio_provider import LocalModelProvider
        from csf.providers import NonFatalAnalysisError

        with mock.patch(
            "csf.providers.lm_studio_provider.TranscriptProvider"
        ) as mock_tp:
            mock_tp.return_value.analyze.side_effect = NonFatalAnalysisError("no transcript")
            provider = LocalModelProvider()
            with pytest.raises(NonFatalAnalysisError):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"
                )

    @mock.patch("csf.providers.lm_studio_provider.TranscriptProvider")
    def test_http_failure_raises_nonfatal(self, mock_transcript):
        mock_transcript.return_value.analyze.return_value = mock.MagicMock(
            summary="Test transcript"
        )
        with mock.patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = Exception(
                "Connection refused"
            )
            from csf.providers.lm_studio_provider import LocalModelProvider

            provider = LocalModelProvider()
            with pytest.raises(
                Exception, match="LocalModelProvider: LM Studio call failed"
            ):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"
                )

    @mock.patch("csf.providers.lm_studio_provider.TranscriptProvider")
    def test_json_parse_failure_raises_nonfatal(self, mock_transcript):
        mock_transcript.return_value.analyze.return_value = mock.MagicMock(
            summary="Test transcript"
        )
        mock_response = {
            "choices": [{"message": {"content": "not valid json at all"}}]
        }
        with mock.patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value.raise_for_status.return_value = None
            mock_client.return_value.__enter__.return_value.post.return_value.json.return_value = mock_response
            from csf.providers.lm_studio_provider import LocalModelProvider

            provider = LocalModelProvider()
            with pytest.raises(
                Exception, match="LocalModelProvider: JSON parse failed"
            ):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"
                )


class TestOllamaVisionProvider:
    @mock.patch("csf.providers.lm_studio_provider.TranscriptProvider")
    def test_analyze_success(self, mock_transcript):
        mock_transcript.return_value.analyze.return_value = mock.MagicMock(
            summary="Test transcript"
        )
        mock_response = {
            "message": {
                "content": '{"title":"OllamaTest","summary":"OK","key_topics":["x"],"key_points":["y"]}'
            }
        }
        with mock.patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.post.return_value.raise_for_status.return_value = None
            mock_client.return_value.__enter__.return_value.post.return_value.json.return_value = mock_response
            from csf.providers.lm_studio_provider import OllamaVisionProvider
            provider = OllamaVisionProvider()
            result = provider.analyze("dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ")
        assert result.title == "OllamaTest"
        assert result.mode == "ollama_vision"

    def test_transcript_failure_raises_nonfatal(self):
        from csf.providers.lm_studio_provider import OllamaVisionProvider
        from csf.providers import NonFatalAnalysisError

        with mock.patch(
            "csf.providers.lm_studio_provider.TranscriptProvider"
        ) as mock_tp:
            mock_tp.return_value.analyze.side_effect = NonFatalAnalysisError("no transcript")
            provider = OllamaVisionProvider()
            with pytest.raises(NonFatalAnalysisError):
                provider.analyze(
                    "dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"
                )

