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
        log_calls: list[tuple[str, dict[str, object]]] = []

        class DummyReusableIngestor:
            def __init__(self):
                prewarm_calls.append("init")
                self._prepare_metrics = {
                    "retire_elapsed_s": 0.0,
                    "notebook_check_elapsed_s": 0.0,
                    "create_elapsed_s": 0.0,
                    "cleanup_elapsed_s": 0.0,
                    "total_elapsed_s": 0.0,
                }
                self._process_metrics = {
                    "setup_mode": "create",
                    "notebook_reused": False,
                    "setup_elapsed_s": 1.25,
                    "notebook_check_elapsed_s": 0.10,
                    "notebook_create_elapsed_s": 0.60,
                    "notebook_retire_elapsed_s": 0.05,
                    "add_sources_elapsed_s": 0.50,
                    "add_cmd_elapsed_s": 0.30,
                    "materialization_wait_elapsed_s": 0.20,
                    "extract_elapsed_s": 2.5,
                    "cleanup_elapsed_s": 0.75,
                    "subbatch_metrics": [
                        {
                            "subbatch_index": 1,
                            "subbatch_size": 2,
                            "target_subbatch_size": 2,
                            "attempted_count": 2,
                            "added_count": 2,
                            "add_cmd_elapsed_s": 0.25,
                            "materialization_wait_elapsed_s": 0.15,
                            "elapsed_s": 0.4,
                            "returncode": 0,
                            "failure_reason": None,
                            "status": "ok",
                            "source_profile": {
                                "total": 2,
                                "matched": 2,
                                "missing": 0,
                                "source_class_counts": {"captioned": 2},
                                "status_counts": {"pending": 2},
                                "privacy_status_counts": {"public": 2},
                                "upload_status_counts": {"processed": 2},
                                "unavailable_reason_counts": {"unknown": 2},
                                "failure_reason_counts": {"unknown": 2},
                            },
                        },
                        {
                            "subbatch_index": 2,
                            "subbatch_size": 1,
                            "target_subbatch_size": 1,
                            "attempted_count": 1,
                            "added_count": 1,
                            "add_cmd_elapsed_s": 0.05,
                            "materialization_wait_elapsed_s": 0.05,
                            "elapsed_s": 0.1,
                            "returncode": 0,
                            "failure_reason": None,
                            "status": "ok",
                            "source_profile": {
                                "total": 1,
                                "matched": 1,
                                "missing": 0,
                                "source_class_counts": {"captioned": 1},
                                "status_counts": {"pending": 1},
                                "privacy_status_counts": {"public": 1},
                                "upload_status_counts": {"processed": 1},
                                "unavailable_reason_counts": {"unknown": 1},
                                "failure_reason_counts": {"unknown": 1},
                            },
                        },
                    ],
                    "total_elapsed_s": 4.5,
                }

            def prepare(self):
                prewarm_calls.append("prepare")
                return True, "create"

            def get_last_prepare_metrics(self):
                return dict(self._prepare_metrics)

            def get_last_process_metrics(self):
                return dict(self._process_metrics)

        monkeypatch.setattr(worker_main, "process_industrial_batch_reusable", lambda vids: {
            vid: (True, f"text-{vid}", None) for vid in vids
        })
        monkeypatch.setattr(
            worker_main,
            "get_last_reusable_process_metrics",
            lambda: {
                "setup_mode": "create",
                "notebook_reused": False,
                "setup_elapsed_s": 1.25,
                "notebook_check_elapsed_s": 0.10,
                "notebook_create_elapsed_s": 0.60,
                "notebook_retire_elapsed_s": 0.05,
                "add_sources_elapsed_s": 0.50,
                "add_cmd_elapsed_s": 0.30,
                "materialization_wait_elapsed_s": 0.20,
                "extract_elapsed_s": 2.5,
                "cleanup_elapsed_s": 0.75,
                "subbatch_metrics": [
                    {
                        "subbatch_index": 1,
                        "subbatch_size": 2,
                        "target_subbatch_size": 2,
                        "attempted_count": 2,
                        "added_count": 2,
                        "add_cmd_elapsed_s": 0.25,
                        "materialization_wait_elapsed_s": 0.15,
                        "elapsed_s": 0.4,
                        "returncode": 0,
                        "failure_reason": None,
                        "status": "ok",
                        "source_profile": {
                            "total": 2,
                            "matched": 2,
                            "missing": 0,
                            "source_class_counts": {"captioned": 2},
                            "status_counts": {"pending": 2},
                            "privacy_status_counts": {"public": 2},
                            "upload_status_counts": {"processed": 2},
                            "unavailable_reason_counts": {"unknown": 2},
                            "failure_reason_counts": {"unknown": 2},
                        },
                    },
                    {
                        "subbatch_index": 2,
                        "subbatch_size": 1,
                        "target_subbatch_size": 1,
                        "attempted_count": 1,
                        "added_count": 1,
                        "add_cmd_elapsed_s": 0.05,
                        "materialization_wait_elapsed_s": 0.05,
                        "elapsed_s": 0.1,
                        "returncode": 0,
                        "failure_reason": None,
                        "status": "ok",
                        "source_profile": {
                            "total": 1,
                            "matched": 1,
                            "missing": 0,
                            "source_class_counts": {"captioned": 1},
                            "status_counts": {"pending": 1},
                            "privacy_status_counts": {"public": 1},
                            "upload_status_counts": {"processed": 1},
                            "unavailable_reason_counts": {"unknown": 1},
                            "failure_reason_counts": {"unknown": 1},
                        },
                    },
                ],
                "total_elapsed_s": 4.5,
            },
        )
        monkeypatch.setattr(
            worker_main,
            "get_last_prepare_metrics",
            lambda: {
                "created_new_notebook": True,
                "setup_mode": "create",
                "notebook_check_elapsed_s": 0.20,
                "create_elapsed_s": 0.40,
                "retire_elapsed_s": 0.30,
                "cleanup_elapsed_s": 0.60,
                "total_elapsed_s": 1.50,
            },
        )
        installed_ingestors: list[object] = []
        monkeypatch.setattr(worker_main, "set_reusable_ingestor", lambda ingestor: installed_ingestors.append(ingestor))
        monkeypatch.setattr(worker_main, "NLMReusableIngestor", DummyReusableIngestor)
        monkeypatch.setattr(worker_main, "retire_reusable_notebook_state", lambda: reset_calls.append({"status": "deleted"}) or {"status": "deleted"})
        monkeypatch.setattr(worker_main, "close_reusable_ingestor", lambda delete=False: None)
        monkeypatch.setattr(
            worker_main,
            "summarize_video_ids",
            lambda vids: {
                "total": len(vids),
                "matched": len(vids),
                "missing": 0,
                "source_class_counts": {"captioned": len(vids)},
                "status_counts": {"pending": len(vids)},
                "privacy_status_counts": {"public": len(vids)},
                "upload_status_counts": {"processed": len(vids)},
                "unavailable_reason_counts": {"unknown": len(vids)},
                "failure_reason_counts": {"unknown": len(vids)},
            },
        )
        saved: list[tuple[str, str, str, str]] = []
        monkeypatch.setattr(worker_main, "set_cached_transcript", lambda vid, lang, src, text: saved.append((vid, lang, src, text)))
        monkeypatch.setattr(worker_main, "mark_complete", lambda vid, last_stage=None: saved.append((vid, "complete", last_stage or "", "")))
        monkeypatch.setattr(worker_main, "log_action", lambda name, payload: log_calls.append((name, payload)))

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
        assert len(installed_ingestors) == 1
        assert isinstance(installed_ingestors[0], DummyReusableIngestor)
        assert reset_calls == [{"status": "deleted"}]
        output_lines = capsys.readouterr().out.strip().splitlines()
        assert any('"event":"worker_notebook_reset_started"' in line for line in output_lines)
        assert any('"event":"worker_notebook_reset_completed"' in line for line in output_lines)
        assert any('"event":"notebook_prewarm"' in line for line in output_lines)
        assert any(name == "worker_batch_started" for name, _ in log_calls)
        assert any(name == "worker_batch_completed" for name, _ in log_calls)
        assert any(payload.get("source_profile") for name, payload in log_calls if name == "worker_batch_started")
        assert any(payload.get("source_profile") for name, payload in log_calls if name == "worker_batch_completed")
        metrics_logs = [payload for name, payload in log_calls if name == "worker_batch_metrics"]
        assert len(metrics_logs) == 2
        assert metrics_logs[0]["setup_elapsed_s"] == 1.25
        assert metrics_logs[0]["notebook_check_elapsed_s"] == 0.10
        assert metrics_logs[0]["notebook_create_elapsed_s"] == 0.60
        assert metrics_logs[0]["notebook_retire_elapsed_s"] == 0.05
        assert metrics_logs[0]["add_sources_elapsed_s"] == 0.50
        assert metrics_logs[0]["add_cmd_elapsed_s"] == 0.30
        assert metrics_logs[0]["materialization_wait_elapsed_s"] == 0.20
        assert metrics_logs[0]["extract_elapsed_s"] == 2.5
        assert metrics_logs[0]["cleanup_elapsed_s"] == 0.75
        assert metrics_logs[0]["batch_elapsed_s"] == 4.5
        assert metrics_logs[0]["source_profile"]["source_class_counts"]["captioned"] == 1
        assert metrics_logs[0]["subbatch_count"] == 2
        assert len(metrics_logs[0]["subbatch_metrics"]) == 2
        assert metrics_logs[0]["subbatch_metrics"][0]["source_profile"]["source_class_counts"]["captioned"] == 2
        worker_completed = next(payload for name, payload in log_calls if name == "worker_completed")
        assert worker_completed["source_profile"]["total"] == 3
        assert worker_completed["source_profile"]["source_class_counts"]["captioned"] == 3
        assert len(worker_completed["subbatch_metrics"]) == 4
        assert any(name == "worker_cleanup_started" for name, _ in log_calls)
        assert any(name == "worker_cleanup_ingestor_close_started" for name, _ in log_calls)
        assert any(name == "worker_cleanup_ingestor_close_completed" for name, _ in log_calls)
        assert any(name == "worker_cleanup_completed" for name, _ in log_calls)
        output = output_lines[-1]
        summary = json.loads(output)
        assert summary["batch_count"] == 2
        assert summary["video_count"] == 3
        assert summary["succeeded"] == 3
        assert summary["failed"] == 0
        assert summary["startup_retire_elapsed_s"] == 0.30
        assert summary["startup_notebook_check_elapsed_s"] == 0.20
        assert summary["startup_notebook_create_elapsed_s"] == 0.40
        assert summary["startup_prepare_cleanup_elapsed_s"] == 0.60
        assert summary["startup_prepare_total_elapsed_s"] == 1.50
        assert summary["setup_elapsed_s_total"] == 2.5
        assert summary["notebook_check_elapsed_s_total"] == 0.2
        assert summary["notebook_create_elapsed_s_total"] == 1.2
        assert summary["notebook_retire_elapsed_s_total"] == 0.1
        assert summary["add_sources_elapsed_s_total"] == 1.0
        assert summary["extract_elapsed_s_total"] == 5.0
        assert summary["cleanup_elapsed_s_total"] == 1.5
        assert summary["batch_elapsed_s_total"] == 9.0
        assert summary["source_profile"]["total"] == 3
        assert summary["source_profile"]["source_class_counts"]["captioned"] == 3
        assert len(summary["subbatch_metrics"]) == 4
        assert summary["notebooklm_profile"] == "ytis-worker-01"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["batch_count"] == 2
        assert result["video_count"] == 3
        assert result["succeeded"] == 3
        assert result["failed"] == 0
        assert result["startup_retire_elapsed_s"] == 0.30
        assert result["startup_notebook_check_elapsed_s"] == 0.20
        assert result["startup_notebook_create_elapsed_s"] == 0.40
        assert result["startup_prepare_cleanup_elapsed_s"] == 0.60
        assert result["startup_prepare_total_elapsed_s"] == 1.50
        assert result["setup_elapsed_s_total"] == 2.5
        assert result["notebook_check_elapsed_s_total"] == 0.2
        assert result["notebook_create_elapsed_s_total"] == 1.2
        assert result["notebook_retire_elapsed_s_total"] == 0.1
        assert result["add_sources_elapsed_s_total"] == 1.0
        assert result["extract_elapsed_s_total"] == 5.0
        assert result["cleanup_elapsed_s_total"] == 1.5
        assert result["batch_elapsed_s_total"] == 9.0
        assert result["source_profile"]["total"] == 3
        assert result["source_profile"]["source_class_counts"]["captioned"] == 3
        assert len(result["subbatch_metrics"]) == 4
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
                self._prepare_metrics = {
                    "retire_elapsed_s": 0.0,
                    "notebook_check_elapsed_s": 0.0,
                    "create_elapsed_s": 0.0,
                    "cleanup_elapsed_s": 0.0,
                    "total_elapsed_s": 0.0,
                }

            def prepare(self):
                return True, "create"

            def get_last_prepare_metrics(self):
                return dict(self._prepare_metrics)

            def get_last_process_metrics(self):
                return {}

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
