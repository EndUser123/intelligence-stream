import json
from pathlib import Path

import pytest

from csf.benchmark_manifest import load_benchmark_manifest


def test_load_manifest_filters_live_trace_cases_only():
    manifest = load_benchmark_manifest(Path("P:\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"))
    live_ids = [case.case_id for case in manifest.cases_for_benchmark()]
    assert "whisper-skip-music-001" in live_ids
    assert "whisper-recover-001" in live_ids
    assert "whisper-admit-live-001" not in live_ids


def test_load_manifest_filters_live_trace_cases_by_family():
    manifest = load_benchmark_manifest(Path("P:\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"))
    live_ids = [case.case_id for case in manifest.cases_for_benchmark(("whisper_admission", "fallback_recovery"))]
    assert live_ids == [
        "whisper-skip-music-001",
        "whisper-recover-001",
        "whisper-recover-002",
        "whisper-recover-003",
    ]


def test_manifest_rejects_duplicate_case_ids(tmp_path):
    manifest_path = tmp_path / "dup.json"
    manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "generated_at": "2026-04-26T00:00:00Z",
                "cases": [
                    {
                        "case_id": "dup",
                        "family": "routing",
                        "source_type": "live_trace",
                        "video_id": "dQw4w9WgXcQ",
                        "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "title": "A",
                        "description": "",
                        "duration": 0,
                        "privacy_status": "public",
                        "upload_status": "",
                        "is_live_content": False,
                        "unavailable_reason": None,
                        "has_captions": True,
                        "expected": {
                            "hot_path": True,
                            "route_to_fallback": False,
                            "attempt_whisper": False,
                            "skip_whisper": False,
                            "recover_success": False,
                            "terminal_skip": False,
                        },
                    },
                    {
                        "case_id": "dup",
                        "family": "routing",
                        "source_type": "live_trace",
                        "video_id": "dQw4w9WgXcQ",
                        "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                        "title": "B",
                        "description": "",
                        "duration": 0,
                        "privacy_status": "public",
                        "upload_status": "",
                        "is_live_content": False,
                        "unavailable_reason": None,
                        "has_captions": True,
                        "expected": {
                            "hot_path": True,
                            "route_to_fallback": False,
                            "attempt_whisper": False,
                            "skip_whisper": False,
                            "recover_success": False,
                            "terminal_skip": False,
                        },
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate case_id"):
        load_benchmark_manifest(manifest_path)
