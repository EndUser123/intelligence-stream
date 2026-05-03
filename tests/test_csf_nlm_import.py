"""Tests for NotebookLM transcript importer routing."""

from __future__ import annotations

import subprocess

from csf import csf_nlm_import


def test_run_nlm_query_pins_active_profile(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, '{"ok": true}', "")

    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "worker-01")
    monkeypatch.setattr(csf_nlm_import.nlm_auth_guard, "run_nlm", fake_run)

    result = csf_nlm_import.run_nlm_query("nb-1", "prompt text")

    assert result == {"ok": True}
    assert calls == [["notebook", "query", "nb-1", "prompt text", "--json", "--profile", "worker-01"]]
