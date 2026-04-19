"""Tests for dev-only worker pool helpers."""

from __future__ import annotations

import json

from dev.worker_pool import parallel_batches, worker_main


class TestParallelBatches:
    def test_load_batches_from_json(self, tmp_path):
        """JSON batch input should parse into cleaned batch lists."""
        path = tmp_path / "batches.json"
        path.write_text(json.dumps([[" a ", "b"], ["c"], []]), encoding="utf-8")

        batches = parallel_batches._load_batches(path)

        assert batches == [["a", "b"], ["c"]]

    def test_group_batches_for_workers_round_robin(self):
        """Batches should be split round-robin across workers."""
        batches = [["a"], ["b"], ["c"], ["d"], ["e"]]

        grouped = parallel_batches._group_batches_for_workers(batches, 2)

        assert grouped == [[["a"], ["c"], ["e"]], [["b"], ["d"]]]


class TestWorkerMain:
    def test_load_batches_from_json(self, tmp_path):
        """Worker input JSON should parse into a cleaned list of batches."""
        path = tmp_path / "videos.json"
        path.write_text(json.dumps([[" a ", "", "b"], ["c"]]), encoding="utf-8")

        batches = worker_main._load_batches(path)

        assert batches == [["a", "b"], ["c"]]

    def test_load_batches_from_text(self, tmp_path):
        """Worker input text should parse into a single batch."""
        path = tmp_path / "videos.txt"
        path.write_text("a\n\n b \n", encoding="utf-8")

        batches = worker_main._load_batches(path)

        assert batches == [["a", "b"]]

    def test_main_processes_multiple_batches_in_one_worker(self, tmp_path, monkeypatch, capsys):
        """A worker should process multiple batches sequentially and summarize totals."""
        input_path = tmp_path / "batches.json"
        input_path.write_text(json.dumps([["a"], ["b", "c"]]), encoding="utf-8")

        prewarm_calls: list[str] = []
        reset_calls: list[dict[str, str]] = []
        result_path = tmp_path / "result.json"

        class DummyReusableIngestor:
            def __init__(self):
                prewarm_calls.append("init")

            def prepare(self):
                prewarm_calls.append("prepare")
                return True, "create"

        monkeypatch.setattr(worker_main, "process_industrial_batch_reusable", lambda vids: {
            vid: (True, f"text-{vid}", None) for vid in vids
        })
        monkeypatch.setattr(worker_main, "NLMReusableIngestor", DummyReusableIngestor)
        monkeypatch.setattr(worker_main, "retire_reusable_notebook_state", lambda: reset_calls.append({"status": "deleted"}) or {"status": "deleted"})
        monkeypatch.setattr(worker_main, "close_reusable_ingestor", lambda delete=False: None)
        saved: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(worker_main, "set_cached_transcript", lambda vid, lang, src, text: saved.append((vid, lang, src, text)))
        monkeypatch.setattr(worker_main, "mark_complete", lambda vid, last_stage=None: saved.append((vid, "complete", last_stage or "", "")))

        rc = worker_main.main(
            [
                "--input",
                str(input_path),
                "--state-path",
                str(tmp_path / "state.json"),
                "--notebook-title",
                "yt-is::dev::worker-01",
                "--notebooklm-profile",
                "ytis-worker-01",
                "--worker-id",
                "worker-01",
                "--result-path",
                str(result_path),
            ]
        )

        assert rc == 0
        assert prewarm_calls == ["init", "prepare"]
        assert reset_calls == [{"status": "deleted"}]
        output_lines = capsys.readouterr().out.strip().splitlines()
        assert any('"event":"worker_notebook_reset_started"' in line for line in output_lines)
        assert any('"event":"worker_notebook_reset_completed"' in line for line in output_lines)
        assert any('"event":"notebook_prewarm"' in line for line in output_lines)
        output = output_lines[-1]
        summary = json.loads(output)
        assert summary["batch_count"] == 2
        assert summary["video_count"] == 3
        assert summary["succeeded"] == 3
        assert summary["failed"] == 0
        assert summary["notebooklm_profile"] == "ytis-worker-01"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["batch_count"] == 2
        assert result["video_count"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert result["status"] == "ok"
        assert result["returncode"] == 0
        assert len(saved) == 6
        assert worker_main.os.environ["NOTEBOOKLM_PROFILE"] == "ytis-worker-01"

    def test_main_resets_stale_notebook_before_prewarm(self, tmp_path, monkeypatch, capsys):
        """A stale notebook should be deleted and recreated before worker prewarm."""
        input_path = tmp_path / "batches.json"
        input_path.write_text(json.dumps([["a"]]), encoding="utf-8")

        init_calls: list[int] = []
        reset_calls: list[dict[str, str]] = []
        result_path = tmp_path / "result.json"

        class DummyReusableIngestor:
            def __init__(self):
                init_calls.append(len(init_calls) + 1)

            def prepare(self):
                return True, "create"

        monkeypatch.setattr(worker_main, "process_industrial_batch_reusable", lambda vids: {
            vid: (True, f"text-{vid}", None) for vid in vids
        })
        monkeypatch.setattr(worker_main, "NLMReusableIngestor", DummyReusableIngestor)
        monkeypatch.setattr(worker_main, "retire_reusable_notebook_state", lambda: reset_calls.append({"status": "deleted"}) or {"status": "deleted"})
        monkeypatch.setattr(worker_main, "close_reusable_ingestor", lambda delete=False: None)
        monkeypatch.setattr(worker_main, "set_cached_transcript", lambda *args, **kwargs: None)
        monkeypatch.setattr(worker_main, "mark_complete", lambda *args, **kwargs: None)

        rc = worker_main.main(
            [
                "--input",
                str(input_path),
                "--state-path",
                str(tmp_path / "state.json"),
                "--notebook-title",
                "yt-is::dev::worker-01",
                "--notebooklm-profile",
                "ytis-worker-01",
                "--worker-id",
                "worker-01",
                "--result-path",
                str(result_path),
            ]
        )

        assert rc == 0
        assert init_calls == [1]
        assert reset_calls == [{"status": "deleted"}]
        output_lines = capsys.readouterr().out.strip().splitlines()
        assert any('"event":"worker_notebook_reset_started"' in line for line in output_lines)
        assert any('"event":"worker_notebook_reset_completed"' in line for line in output_lines)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["status"] == "ok"
        assert result["returncode"] == 0
