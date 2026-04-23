"""Tests for the shared NotebookLM config module."""

from csf import nlm_config, transcript


class TestSharedNlmConfig:
    """The NotebookLM config should live in one shared module."""

    def test_defaults_cover_batch_and_auth_policy(self):
        """The shared config should expose the batch and auth defaults together."""
        cfg = nlm_config.get_nlm_config()
        assert cfg.notebook_batch_size == 50
        assert cfg.notebook_source_cap == 50
        assert cfg.notebook_source_materialization_timeout_s == 600
        assert cfg.max_sources_per_notebook == 300
        assert cfg.auth_check_interval == 60.0
        assert cfg.auth_max_calls_per_window == 10
        assert cfg.auth_cooldown == 300.0
        assert cfg.browser_profile_mode == "persistent"
        assert cfg.browser_profile_name == "notebooklm"
        assert cfg.browser_profile_seed_root.endswith("notebooklm-browser-session")
        assert cfg.nlm_browser_mode == "persistent"
        assert cfg.nlm_browser_profile_root.endswith(r".browser\notebooklm")
        assert cfg.nlm_browser_executable.endswith(r"chrome.exe")
        assert cfg.nlm_browser_channel == "chrome"
        assert cfg.nlm_browser_bootstrap_headless is False
        assert cfg.nlm_browser_start_timeout_ms == 30000
        assert cfg.nlm_preflight_url_timeout_ms == 60000
        assert cfg.nlm_preflight_ui_timeout_ms == 15000
        assert transcript.get_nlm_config() is cfg

    def test_setter_updates_the_shared_singleton(self):
        """set_nlm_config should affect both modules because they share the singleton."""
        original = nlm_config.get_nlm_config()
        replacement = nlm_config.NLMConfig(
            notebook_batch_size=77,
            notebook_source_cap=88,
            notebook_source_materialization_timeout_s=99,
            max_sources_per_notebook=123,
            auth_check_interval=11.0,
            auth_max_calls_per_window=12,
            auth_cooldown=13.0,
            browser_profile_mode="persistent",
            browser_profile_name="notebooklm-test",
            browser_profile_seed_root="P:/__csf/.data/yt-is/notebooklm-browser-session-test",
            nlm_browser_mode="persistent",
            nlm_browser_profile_root=r"P:\packages\yt-is\.browser\notebooklm-test",
            nlm_browser_executable=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            nlm_browser_channel="chrome",
            nlm_browser_bootstrap_headless=False,
            nlm_browser_start_timeout_ms=30000,
            nlm_preflight_url_timeout_ms=60000,
            nlm_preflight_ui_timeout_ms=15000,
        )
        try:
            nlm_config.set_nlm_config(replacement)
            assert nlm_config.get_nlm_config() is replacement
            assert transcript.get_nlm_config() is replacement
        finally:
            nlm_config.set_nlm_config(original)
