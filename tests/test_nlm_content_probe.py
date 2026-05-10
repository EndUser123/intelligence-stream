from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from csf import nlm_content_probe


def test_parse_source_ids_extracts_ordered_ids():
    stdout = "\n".join(
        [
            "Adding 2 URLs and waiting for processing...",
            "  Source ID: src-first",
            "  Source ID: src-second",
        ]
    )

    assert nlm_content_probe._parse_source_ids(stdout) == ["src-first", "src-second"]


def test_probe_status_distinguishes_ready_and_below_threshold():
    assert nlm_content_probe._probe_status(0, 101, True) == "ready"
    assert nlm_content_probe._probe_status(0, 50, True) == "nlm_content_below_threshold"
    assert nlm_content_probe._probe_status(1, 0, False) == "command_failed"


def test_run_probe_writes_summary_and_stops_after_ready(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(nlm_content_probe, "_create_probe_notebook", lambda profile, title: f"nb-{profile}")
    monkeypatch.setattr(nlm_content_probe, "_add_video_source", lambda profile, notebook_id, video_id: {
        "profile": profile,
        "notebook_id": notebook_id,
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "returncode": 0,
        "stdout": "  Source ID: src-1",
        "stderr": "",
        "elapsed_s": 0.1,
        "source_ids": ["src-1"],
        "source_id": "src-1",
    })
    monkeypatch.setattr(
        nlm_content_probe,
        "inspect_youtube_watch_page_via_ytdlp",
        lambda video_id: {"classification": "ok", "available": True, "availability": "public", "duration": 10},
    )

    attempts = [
        {"profile": "p1", "source_id": "src-1", "status": "nlm_content_below_threshold", "returncode": 0, "content_length": 50, "nlm_content_chars": 50, "usable_text_chars": 0, "stdout": "", "stderr": "", "started_at_epoch": 1.0, "completed_at_epoch": 1.1, "elapsed_s": 0.1},
        {"profile": "p1", "source_id": "src-1", "status": "ready", "returncode": 0, "content_length": 101, "nlm_content_chars": 101, "usable_text_chars": 101, "stdout": "", "stderr": "", "started_at_epoch": 2.0, "completed_at_epoch": 2.1, "elapsed_s": 0.1},
    ]

    def fake_fetch(profile, source_id, timeout_s=30):
        calls.append((profile, source_id))
        return attempts[len(calls) - 1]

    monkeypatch.setattr(nlm_content_probe, "_fetch_content", fake_fetch)
    monkeypatch.setattr(nlm_content_probe.time, "sleep", lambda seconds: None)

    summary = nlm_content_probe.run_probe(["p1"], ["vid1"], output_root=tmp_path, retry_delays_s=(0, 30))

    assert summary["profiles"] == ["p1"]
    assert summary["video_ids"] == ["vid1"]
    assert calls == [("p1", "src-1"), ("p1", "src-1")]
    run_dir = Path(summary["output_root"])
    assert (run_dir / "probe_summary.json").exists()
    payload = json.loads((run_dir / "probe_summary.json").read_text(encoding="utf-8"))
    assert payload["results"][0]["attempts"][-1]["status"] == "ready"


def test_run_probe_supports_preload_sources_before_target(tmp_path, monkeypatch):
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(nlm_content_probe, "_create_probe_notebook", lambda profile, title: f"nb-{profile}")
    monkeypatch.setattr(
        nlm_content_probe,
        "_add_video_source",
        lambda profile, notebook_id, video_id: {
            "profile": profile,
            "notebook_id": notebook_id,
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "returncode": 0,
            "stdout": f"Source ID: src-{video_id}",
            "stderr": "",
            "elapsed_s": 0.1,
            "source_ids": [f"src-{video_id}"],
            "source_id": f"src-{video_id}",
        },
    )
    monkeypatch.setattr(
        nlm_content_probe,
        "inspect_youtube_watch_page_via_ytdlp",
        lambda video_id: {"classification": "ok", "available": True, "availability": "public", "duration": 10},
    )

    def fake_fetch(profile, source_id, timeout_s=30):
        calls.append((profile, source_id))
        return {
            "profile": profile,
            "source_id": source_id,
            "status": "ready",
            "returncode": 0,
            "content_length": 101,
            "nlm_content_chars": 101,
            "usable_text_chars": 101,
            "stdout": "",
            "stderr": "",
            "started_at_epoch": 1.0,
            "completed_at_epoch": 1.1,
            "elapsed_s": 0.1,
        }

    monkeypatch.setattr(nlm_content_probe, "_fetch_content", fake_fetch)
    monkeypatch.setattr(nlm_content_probe.time, "sleep", lambda seconds: None)

    summary = nlm_content_probe.run_probe(
        ["p1"],
        ["target"],
        output_root=tmp_path,
        retry_delays_s=(0, 30),
        continue_after_ready=True,
        preload_video_ids=["pre-1", "pre-2"],
        target_video_id="target",
    )

    assert summary["preload_video_ids"] == ["pre-1", "pre-2"]
    assert summary["video_ids"] == ["target"]
    assert calls == [("p1", "src-target"), ("p1", "src-target")]
    result = summary["results"][0]
    assert [item["video_id"] for item in result["preloads"]] == ["pre-1", "pre-2"]
    assert result["attempts"][-1]["status"] == "ready"
