"""Tests for shared NotebookLM auth command routing."""

from __future__ import annotations

import pytest

from csf import nlm_auth_guard


@pytest.mark.parametrize(
    "args, expected",
    [
        (["source", "list", "nb-1"], ["source", "list", "nb-1", "--profile", "worker-01"]),
        (["notebook", "query", "nb-1", "prompt"], ["notebook", "query", "nb-1", "prompt", "--profile", "worker-01"]),
        (["audio", "create", "nb-1", "--confirm"], ["audio", "create", "nb-1", "--confirm", "--profile", "worker-01"]),
    ],
)
def test_add_profile_args_pins_non_login_commands(monkeypatch, args, expected):
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "worker-01")

    assert nlm_auth_guard.add_profile_args(args) == expected


def test_add_profile_args_leaves_login_commands_unpinned(monkeypatch):
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "worker-01")

    assert nlm_auth_guard.add_profile_args(["login", "--check"]) == ["login", "--check"]
