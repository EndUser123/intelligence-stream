"""Shared NotebookLM runtime configuration for yt-is.

This module centralizes the NotebookLM notebook policy and auth policy so the
rest of the codebase can import a single source of truth for NotebookLM
settings.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass


_nlm_config_lock = threading.Lock()
_nlm_config: "NLMConfig | None" = None


@dataclass(frozen=True)
class NLMConfig:
    """Runtime configuration for NotebookLM operations."""

    notebook_batch_size: int = 50
    notebook_source_cap: int = 50
    notebook_source_materialization_timeout_s: int = 600
    max_sources_per_notebook: int = 300
    auth_check_interval: float = 60.0
    auth_max_calls_per_window: int = 10
    auth_cooldown: float = 300.0
    browser_profile_mode: str = "persistent"
    browser_profile_name: str = "notebooklm"
    browser_profile_seed_root: str = "P:/__csf/.data/yt-is/notebooklm-browser-session"
    nlm_browser_mode: str = "persistent"
    nlm_browser_profile_root: str = r"P:\packages\yt-is\.browser\notebooklm"
    nlm_browser_executable: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    nlm_browser_channel: str = "chrome"
    nlm_browser_bootstrap_headless: bool = False
    nlm_browser_start_timeout_ms: int = 30000
    nlm_preflight_url_timeout_ms: int = 60000
    nlm_preflight_ui_timeout_ms: int = 15000


def get_nlm_config() -> NLMConfig:
    """Return the singleton NotebookLM config, initializing from env vars."""
    global _nlm_config
    with _nlm_config_lock:
        if _nlm_config is None:
            _nlm_config = NLMConfig(
                notebook_batch_size=int(os.environ.get("YTIS_NLM_BATCH_SIZE", "50")),
                notebook_source_cap=int(os.environ.get("YTIS_NLM_SOURCE_CAP", "50")),
                notebook_source_materialization_timeout_s=int(
                    os.environ.get("YTIS_NLM_SOURCE_MATERIALIZATION_TIMEOUT_S", "600")
                ),
                max_sources_per_notebook=int(
                    os.environ.get("YTIS_NLM_MAX_SOURCES_PER_NOTEBOOK", "300")
                ),
                auth_check_interval=float(os.environ.get("YTIS_NLM_AUTH_CHECK_INTERVAL", "60.0")),
                auth_max_calls_per_window=int(
                    os.environ.get("YTIS_NLM_AUTH_MAX_CALLS_PER_WINDOW", "10")
                ),
                auth_cooldown=float(os.environ.get("YTIS_NLM_AUTH_COOLDOWN", "300.0")),
                browser_profile_mode=os.environ.get("YTIS_NLM_BROWSER_PROFILE_MODE", "persistent").strip().lower()
                or "persistent",
                browser_profile_name=os.environ.get("YTIS_NLM_BROWSER_PROFILE_NAME", "notebooklm").strip()
                or "notebooklm",
                browser_profile_seed_root=os.environ.get(
                    "YTIS_NLM_BROWSER_PROFILE_SEED_ROOT",
                    "P:/__csf/.data/yt-is/notebooklm-browser-session",
                ).strip()
                or "P:/__csf/.data/yt-is/notebooklm-browser-session",
                nlm_browser_mode=os.environ.get("YTIS_NLM_BROWSER_MODE", "persistent").strip().lower()
                or "persistent",
                nlm_browser_profile_root=os.environ.get(
                    "YTIS_NLM_BROWSER_PROFILE_ROOT",
                    r"P:\packages\yt-is\.browser\notebooklm",
                ).strip()
                or r"P:\packages\yt-is\.browser\notebooklm",
                nlm_browser_executable=os.environ.get(
                    "YTIS_NLM_BROWSER_EXECUTABLE",
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                ).strip()
                or r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                nlm_browser_channel=os.environ.get("YTIS_NLM_BROWSER_CHANNEL", "chrome").strip().lower()
                or "chrome",
                nlm_browser_bootstrap_headless=(
                    os.environ.get("YTIS_NLM_BROWSER_BOOTSTRAP_HEADLESS", "false").strip().lower()
                    in {"1", "true", "yes", "on"}
                ),
                nlm_browser_start_timeout_ms=int(
                    os.environ.get("YTIS_NLM_BROWSER_START_TIMEOUT_MS", "30000")
                ),
                nlm_preflight_url_timeout_ms=int(
                    os.environ.get("YTIS_NLM_PRELIGHT_URL_TIMEOUT_MS", os.environ.get("YTIS_NLM_PREFLIGHT_URL_TIMEOUT_MS", "60000"))
                ),
                nlm_preflight_ui_timeout_ms=int(
                    os.environ.get("YTIS_NLM_PREFLIGHT_UI_TIMEOUT_MS", "15000")
                ),
            )
        return _nlm_config


def set_nlm_config(config: NLMConfig) -> None:
    """Set the singleton NotebookLM config (primarily for tests)."""
    global _nlm_config
    with _nlm_config_lock:
        _nlm_config = config


def reset_nlm_config() -> None:
    """Clear the singleton NotebookLM config so it reloads from env on demand."""
    global _nlm_config
    with _nlm_config_lock:
        _nlm_config = None
