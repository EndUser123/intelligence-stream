"""Tests for bin/yt-is wrapper dispatch logging."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock


def _load_yt_is_module():
    """Load the extensionless bin/yt-is script as a module."""
    path = Path(r"P:\packages\yt-is\bin\yt-is")
    loader = SourceFileLoader("yt_is_wrapper_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load yt-is")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_logs_dispatch_and_uses_repo_local_backend():
    """fetch should log dispatch metadata and invoke the repo-local backend path."""
    mod = _load_yt_is_module()

    with mock.patch.object(mod.subprocess, "run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0)
        with mock.patch.object(mod, "log_action") as mock_log:
            with mock.patch.object(mod.sys, "argv", ["yt-is", "fetch", "--dry-run"]):
                with mock.patch.object(mod.sys, "exit", side_effect=SystemExit) as mock_exit:
                    try:
                        mod.main()
                    except SystemExit:
                        pass

    assert mock_log.call_args_list[0].args[0] == "yt_is_dispatch_started"
    assert mock_log.call_args_list[1].args[0] == "yt_is_dispatch_finished"
    argv = mock_log.call_args_list[0].args[1]["argv"]
    assert argv[0] == mod.sys.executable
    assert Path(argv[1]).name == "csf-source"
    assert argv[2] == "fetch"
    assert "--dry-run" in argv
    mock_exit.assert_called_once_with(0)
