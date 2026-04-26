import unittest

from csf.youtube_page_inspector import (
    classify_youtube_watch_page,
    classify_ytdlp_watch_info,
    inspect_youtube_watch_page,
    inspect_youtube_watch_page_via_ytdlp,
    extract_yt_initial_player_response,
)


class TestYouTubePageInspector(unittest.TestCase):
    def test_extract_yt_initial_player_response_parses_embedded_json(self):
        html = (
            "<html><head></head><body>"
            "<script>var ytInitialPlayerResponse = "
            '{"playabilityStatus":{"status":"OK"},"videoDetails":{"title":"Demo"}};'
            "</script></body></html>"
        )

        data = extract_yt_initial_player_response(html)

        assert data["playabilityStatus"]["status"] == "OK"
        assert data["videoDetails"]["title"] == "Demo"

    def test_classify_youtube_watch_page_marks_not_yet_live(self):
        player = {
            "playabilityStatus": {
                "status": "LIVE_STREAM_OFFLINE",
                "reason": "This live event will begin in a few moments.",
            },
            "videoDetails": {
                "isLiveContent": True,
                "title": "Upcoming live event",
            },
        }

        result = classify_youtube_watch_page(player)

        assert result["classification"] == "not_yet_live"
        assert result["available"] is False
        assert result["is_live_content"] is True

    def test_classify_youtube_watch_page_marks_removed_by_owner(self):
        player = {
            "playabilityStatus": {
                "status": "ERROR",
                "reason": "Video unavailable",
                "errorScreen": {
                    "playerErrorMessageRenderer": {
                        "subreason": {"simpleText": "This video has been removed by the uploader"}
                    }
                },
            },
            "videoDetails": {},
        }

        result = classify_youtube_watch_page(player)

        assert result["classification"] == "removed_by_owner"
        assert result["available"] is False

    def test_classify_youtube_watch_page_marks_ok(self):
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "isLiveContent": False,
                "title": "Available video",
            },
        }

        result = classify_youtube_watch_page(player)

        assert result["classification"] == "ok"
        assert result["available"] is True
        assert result["title"] == "Available video"

    def test_inspect_youtube_watch_page_fetches_and_classifies(self):
        html = (
            "<html><script>var ytInitialPlayerResponse = "
            '{"playabilityStatus":{"status":"ERROR","reason":"Video unavailable","errorScreen":{"playerErrorMessageRenderer":{"subreason":{"simpleText":"This video has been removed by the uploader"}}}},'
            '"videoDetails":{}};'
            "</script></html>"
        )

        class _Resp:
            status = 200

            def read(self):
                return html.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        with unittest.mock.patch("urllib.request.urlopen", return_value=_Resp()) as mock_urlopen:
            result = inspect_youtube_watch_page("abc123def45", timeout_s=1)

        assert result["classification"] == "removed_by_owner"
        assert result["available"] is False
        assert result["status"] == "ERROR"
        assert result["subreason"] == "This video has been removed by the uploader"
        assert result["video_id"] == "abc123def45"
        assert result["elapsed_s"] >= 0
        assert mock_urlopen.call_count == 1

    def test_classify_ytdlp_watch_info_marks_public_video_ok(self):
        info = {
            "availability": "public",
            "live_status": "not_live",
            "was_live": False,
            "is_live": False,
            "title": "Available video",
        }

        result = classify_ytdlp_watch_info(info)

        assert result["classification"] == "ok"
        assert result["available"] is True
        assert result["availability"] == "public"
        assert result["live_status"] == "not_live"

    def test_classify_ytdlp_watch_info_marks_removed_by_owner(self):
        info = {
            "availability": "unavailable",
            "live_status": "not_live",
            "was_live": False,
            "title": "Removed video",
        }

        result = classify_ytdlp_watch_info(info)

        assert result["classification"] == "unavailable"
        assert result["available"] is False

    def test_inspect_youtube_watch_page_via_ytdlp_classifies_error_output(self):
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "ERROR: [youtube] VdunqscAV5Q: Video unavailable. This video has been removed by the uploader"

        with unittest.mock.patch("shutil.which", return_value="yt-dlp") as mock_which:
            with unittest.mock.patch("subprocess.run", return_value=_Proc()) as mock_run:
                result = inspect_youtube_watch_page_via_ytdlp("VdunqscAV5Q", timeout_s=1)

        assert result["classification"] == "removed_by_owner"
        assert result["available"] is False
        assert result["elapsed_s"] >= 0
        assert result["returncode"] == 1
        assert result["video_id"] == "VdunqscAV5Q"
        assert mock_which.call_count == 1
        assert mock_run.call_count == 1
