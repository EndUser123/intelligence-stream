"""Tests for nlm_command_failed_classifier."""

import json
import pytest
import tempfile
from pathlib import Path

from csf.nlm_command_failed_classifier import (
    classify_command_failed_event,
    classify_run,
    format_report,
    _scan_sweep_summary_for_content_fetch_events,
    _scan_term_jsonl_for_content_fetch_events,
)


class TestClassifyCommandFailedEvent:
    """Unit tests for classify_command_failed_event."""

    def test_not_found_transient(self):
        event = {
            "status": "command_failed",
            "video_id": "vid123",
            "source_id": "src456",
            "returncode": 1,
            "stdout": "",
            "stderr": "ERROR: Source NOT_FOUND for id src456",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "not_found_transient"
        assert result["retry_recommendation"] == "candidate_retry"
        assert result["matched_marker"] == "NOT_FOUND"
        assert result["is_auth_or_permission"] is False
        assert result["is_transient"] is True

    def test_rate_limited_429(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "rate limit exceeded",
            "stderr": "ERROR 429: Too Many Requests",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "rate_limited"
        assert result["retry_recommendation"] == "candidate_retry"
        assert result["matched_marker"] in ("429", "TOO MANY REQUESTS", "RATE LIMIT")
        assert result["is_transient"] is True

    def test_rate_limited_too_many_requests(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "ERROR: TOO MANY REQUESTS — please wait",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "rate_limited"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_rate_limited_rate_limit_text(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "RATE LIMIT hit — backing off",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "rate_limited"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_service_unavailable_503(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "HTTP 503: SERVICE UNAVAILABLE",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "service_unavailable"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_service_unavailable_bad_gateway(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "502 Bad Gateway",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "service_unavailable"

    def test_service_unavailable_gateway_timeout(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "504 GATEWAY TIMEOUT",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "service_unavailable"

    def test_network_transient_econnreset(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "ECONNRESET: connection reset by peer",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_network_transient_etimedout(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "ETIMEDOUT: connection timed out",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_network_transient_econnrefused(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "ECONNREFUSED",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"

    def test_network_transient_connection_reset(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "CONNECTION RESET",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"

    def test_network_transient_connection_timed_out(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "CONNECTION TIMED OUT",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"

    def test_network_transient_request_timed_out(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "REQUEST TIMED OUT after 30s",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"

    def test_network_transient_deadline_exceeded(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "deadline exceeded",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "network_transient"

    def test_tls_transient_tls(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "TLS handshake failed",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "tls_transient"
        assert result["retry_recommendation"] == "candidate_retry"

    def test_tls_transient_ssl(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "SSL certificate error",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "tls_transient"

    def test_tls_transient_certificate(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "CERTIFICATE verify failed",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "tls_transient"

    def test_empty_output_retcode_nonzero_empty_streams(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "empty_output"
        assert result["retry_recommendation"] == "candidate_retry_once"
        assert result["matched_marker"] is None
        assert result["is_transient"] is True

    def test_empty_output_whitespace_streams(self):
        event = {
            "status": "command_failed",
            "returncode": 2,
            "stdout": "   \n\t  ",
            "stderr": "  \n",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "empty_output"
        assert result["retry_recommendation"] == "candidate_retry_once"

    def test_empty_output_with_content_not_classified(self):
        """Non-empty stdout/stderr should not be classified as empty_output."""
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "some output",
            "stderr": "",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] != "empty_output"

    def test_empty_output_retcode_zero_not_classified(self):
        """returncode=0 should not be classified as empty_output."""
        event = {
            "status": "command_failed",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] != "empty_output"

    def test_auth_permission_permission_denied(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "ERROR: PERMISSION_DENIED — access denied",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"
        assert result["is_auth_or_permission"] is True
        assert result["is_transient"] is False

    def test_auth_permission_unauthorized(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "UNAUTHORIZED — invalid credentials",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"
        assert result["is_auth_or_permission"] is True

    def test_auth_permission_auth(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "AUTH failure: token expired",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"

    def test_auth_permission_login(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "LOGIN required",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"

    def test_auth_permission_credential(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "Invalid CREDENTIAL",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"

    def test_auth_permission_profile(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "PROFILE mismatch",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "auth_or_permission"
        assert result["retry_recommendation"] == "do_not_retry"

    def test_unknown_generic_api_error(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "API ERROR: something went wrong",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "unknown"
        assert result["retry_recommendation"] == "unknown_do_not_change_policy"
        assert result["is_auth_or_permission"] is False
        assert result["is_transient"] is False

    def test_unknown_bare_error(self):
        event = {
            "status": "command_failed",
            "returncode": 1,
            "stdout": "generic error",
            "stderr": "command failed",
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "unknown"
        assert result["retry_recommendation"] == "unknown_do_not_change_policy"

    def test_not_command_failed(self):
        event = {
            "status": "ready",
            "returncode": 0,
            "attempts": 1,
        }
        result = classify_command_failed_event(event)
        assert result["error_class"] == "not_command_failed"
        assert result["retry_recommendation"] == "unknown_do_not_change_policy"


class TestScanSweepSummary:
    """Tests for sweep_summary.json scanning."""

    def test_extracts_source_statuses_from_result(self, tmp_path):
        sweep_path = tmp_path / "sweep_summary.json"
        sweep_path.write_text(json.dumps({
            "results": [{
                "worker_id": "worker-01",
                "batch_timestamp": "20260507_085411",
                "content_fetch_status_counts": {"ready": 10, "command_failed": 2},
                "source_statuses": [
                    {
                        "source_id": "src001",
                        "video_id": "vid001",
                        "status": "ready",
                        "attempts": 1,
                    },
                    {
                        "source_id": "src002",
                        "video_id": "vid002",
                        "status": "command_failed",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "NOT_FOUND",
                        "attempts": 1,
                    },
                    {
                        "source_id": "src003",
                        "video_id": "vid003",
                        "status": "command_failed",
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "PERMISSION_DENIED",
                        "attempts": 1,
                    },
                ],
            }]
        }), encoding="utf-8")

        events = _scan_sweep_summary_for_content_fetch_events(sweep_path)
        assert len(events) == 2
        assert events[0]["source_id"] == "src002"
        assert events[0]["status"] == "command_failed"
        assert events[1]["source_id"] == "src003"
        assert events[1]["stderr"] == "PERMISSION_DENIED"

    def test_returns_empty_for_summary_counts_only(self, tmp_path):
        sweep_path = tmp_path / "sweep_summary.json"
        sweep_path.write_text(json.dumps({
            "results": [{
                "worker_id": "worker-01",
                "content_fetch_status_counts": {"ready": 10, "command_failed": 2},
                # No source_statuses key
            }]
        }), encoding="utf-8")

        events = _scan_sweep_summary_for_content_fetch_events(sweep_path)
        assert events == []

    def test_returns_empty_for_missing_file(self, tmp_path):
        events = _scan_sweep_summary_for_content_fetch_events(tmp_path / "nonexistent.json")
        assert events == []


class TestScanTermJsonl:
    """Tests for term JSONL log scanning."""

    def test_extracts_nlm_batch_content_fetch_completed_events(self, tmp_path):
        term_path = tmp_path / "term_abc123.jsonl"
        term_path.write_text('\n'.join([
            json.dumps({
                "timestamp": "2026-05-07T14:54:12Z",
                "action": "fetch_invoked",
                "data": {},
            }),
            json.dumps({
                "timestamp": "2026-05-07T15:06:54Z",
                "action": "nlm_batch_source_content_fetch_completed",
                "data": {
                    "source_statuses": [
                        {
                            "source_id": "src001",
                            "video_id": "vid001",
                            "status": "command_failed",
                            "returncode": 1,
                            "stdout": "",
                            "stderr": "NOT_FOUND",
                            "attempts": 1,
                        },
                        {
                            "source_id": "src002",
                            "video_id": "vid002",
                            "status": "ready",
                            "attempts": 1,
                        },
                    ],
                },
            }),
        ]), encoding="utf-8")

        events = _scan_term_jsonl_for_content_fetch_events(term_path)
        assert len(events) == 1
        assert events[0]["source_id"] == "src001"
        assert events[0]["status"] == "command_failed"
        assert events[0]["stderr"] == "NOT_FOUND"

    def test_extracts_direct_nlm_batch_source_content_fetch_completed_event(self, tmp_path):
        term_path = tmp_path / "term_direct.jsonl"
        term_path.write_text(json.dumps({
            "timestamp": "2026-05-07T15:06:54Z",
            "action": "nlm_batch_source_content_fetch_completed",
            "data": {
                "source_id": "src-direct",
                "video_id": "vid-direct",
                "status": "command_failed",
                "returncode": 1,
                "stdout": "",
                "stderr": "ECONNRESET from NotebookLM",
                "attempts": 1,
                "source_id_validated_after_not_found": False,
                "source_list_probe_returncode": 0,
                "source_list_probe_count": 1,
                "source_list_probe_elapsed_s": 0.123,
            },
        }), encoding="utf-8")

        events = _scan_term_jsonl_for_content_fetch_events(term_path)

        assert len(events) == 1
        assert events[0]["source_id"] == "src-direct"
        assert events[0]["video_id"] == "vid-direct"
        assert events[0]["status"] == "command_failed"
        assert events[0]["stderr"] == "ECONNRESET from NotebookLM"
        assert events[0]["source_id_validated_after_not_found"] is False
        assert events[0]["source_list_probe_returncode"] == 0
        assert events[0]["source_list_probe_count"] == 1
        assert events[0]["source_list_probe_elapsed_s"] == 0.123

    def test_returns_empty_for_no_content_fetch_events(self, tmp_path):
        term_path = tmp_path / "term_abc123.jsonl"
        term_path.write_text('\n'.join([
            json.dumps({"timestamp": "2026-05-07T14:54:12Z", "action": "fetch_invoked", "data": {}}),
            json.dumps({"timestamp": "2026-05-07T14:54:12Z", "action": "fetch_worker_finished", "data": {}}),
        ]), encoding="utf-8")

        events = _scan_term_jsonl_for_content_fetch_events(term_path)
        assert events == []


class TestClassifyRun:
    """Tests for full-run classification."""

    def test_classifies_per_source_events_and_summarizes_counts(self, tmp_path):
        run_root = tmp_path / "test_run"
        run_root.mkdir()
        (run_root / "soak" / "a_hominidae_pro" / "batch_01").mkdir(parents=True)
        sweep_path = (
            run_root / "soak" / "a_hominidae_pro" / "batch_01"
            / "notebooklm_route_plus_fallback_30s_1w" / "20260507_085411" / "sweep_summary.json"
        )
        sweep_path.parent.mkdir(parents=True)
        sweep_path.write_text(json.dumps({
            "results": [{
                "worker_id": "worker-01",
                "content_fetch_status_counts": {"ready": 10, "command_failed": 2},
                "source_statuses": [
                    {
                        "source_id": "src001",
                        "status": "command_failed",
                        "returncode": 1,
                        "stderr": "ECONNRESET",
                        "attempts": 1,
                    },
                    {
                        "source_id": "src002",
                        "status": "command_failed",
                        "returncode": 1,
                        "stderr": "PERMISSION_DENIED",
                        "attempts": 1,
                    },
                ],
            }]
        }), encoding="utf-8")

        report = classify_run(run_root, "test_run")

        assert report["run_name"] == "test_run"
        assert report["sufficiency"] == "has_event_level_data"
        assert report["event_count"] == 2
        assert report["class_counts"]["network_transient"] == 1
        assert report["class_counts"]["auth_or_permission"] == 1
        assert report["auth_or_permission_count"] == 1
        assert report["transient_retry_candidate_count"] == 1

    def test_reports_summary_counts_only_when_no_event_level_data(self, tmp_path):
        run_root = tmp_path / "test_run"
        run_root.mkdir()
        (run_root / "soak" / "a_hominidae_pro" / "batch_01").mkdir(parents=True)
        sweep_path = (
            run_root / "soak" / "a_hominidae_pro" / "batch_01"
            / "notebooklm_route_plus_fallback_30s_1w" / "20260507_085411" / "sweep_summary.json"
        )
        sweep_path.parent.mkdir(parents=True)
        sweep_path.write_text(json.dumps({
            "results": [{
                "worker_id": "worker-01",
                "content_fetch_status_counts": {"ready": 10, "command_failed": 2},
                # No source_statuses
            }]
        }), encoding="utf-8")

        report = classify_run(run_root, "test_run")

        assert report["sufficiency"] == "summary_counts_only"
        assert report["event_count"] == 0
        assert report["summary_counts"]["command_failed"] == 2
        assert report["class_counts"] == {}

    def test_reports_no_content_fetch_events_when_none_found(self, tmp_path):
        run_root = tmp_path / "test_run"
        run_root.mkdir()
        # No sweep_summary.json at all
        (run_root / "soak" / "a_hominidae_pro").mkdir(parents=True)

        report = classify_run(run_root, "test_run")

        assert report["sufficiency"] == "no_content_fetch_events"
        assert report["event_count"] == 0


class TestFormatReport:
    """Tests for report formatting."""

    def test_markdown_format_shows_insufficient_data_warning(self):
        runs = [{
            "run_name": "test_run",
            "run_root": "/tmp/test_run",
            "sufficiency": "summary_counts_only",
            "event_count": 0,
            "class_counts": {},
            "retry_counts": {},
            "auth_or_permission_count": 0,
            "transient_retry_candidate_count": 0,
            "events": [],
            "summary_counts": {"ready": 198, "command_failed": 36},
        }]

        report = format_report(runs, fmt="markdown")
        assert "summary_counts_only" in report
        assert "Summary-level counts" in report or "Insufficient data" in report
        assert "Insufficient data for retry-policy changes" in report
        assert "Run a new instrumented probe" in report

    def test_text_format_shows_summary_counts(self):
        runs = [{
            "run_name": "test_run",
            "run_root": "/tmp/test_run",
            "sufficiency": "summary_counts_only",
            "event_count": 0,
            "class_counts": {},
            "retry_counts": {},
            "auth_or_permission_count": 0,
            "transient_retry_candidate_count": 0,
            "events": [],
            "summary_counts": {"ready": 198, "command_failed": 36},
        }]

        report = format_report(runs, fmt="text")
        assert "test_run" in report
        assert "summary_counts_only" in report
        assert "command_failed" in report

    def test_markdown_format_shows_not_found_probe_fields(self):
        runs = [{
            "run_name": "test_run",
            "run_root": "/tmp/test_run",
            "sufficiency": "has_event_level_data",
            "event_count": 1,
            "class_counts": {"not_found_transient": 1},
            "retry_counts": {"candidate_retry": 1},
            "auth_or_permission_count": 0,
            "transient_retry_candidate_count": 1,
            "events": [
                (
                    {
                        "video_id": "vid-direct",
                        "source_id": "src-direct",
                        "returncode": 1,
                        "attempts": 2,
                        "source_id_validated_after_not_found": False,
                        "source_list_probe_returncode": 0,
                    },
                    {
                        "matched_marker": "NOT_FOUND",
                        "error_class": "not_found_transient",
                        "retry_recommendation": "candidate_retry",
                    },
                )
            ],
            "summary_counts": {},
        }]

        report = format_report(runs, fmt="markdown")

        assert "source_validated" in report
        assert "| 1 | vid-direct | src-direct | 1 | 2 | False | 0 | None |  | NOT_FOUND | not_found_transient | candidate_retry |" in report
