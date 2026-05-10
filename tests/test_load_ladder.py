"""Tests for the NotebookLM load ladder helpers."""

from pathlib import Path

from csf.load_ladder import (
    build_fallback_benchmark_command,
    default_load_ladder_scenarios,
    scenario_by_name,
)


class TestLoadLadderHelpers:
    """The ladder should define stable scenarios and commands."""

    def test_default_scenarios_have_the_expected_order(self):
        """Scenario order should stay fixed so benchmark comparisons remain stable."""
        names = [scenario.name for scenario in default_load_ladder_scenarios()]
        assert names == [
            "baseline",
            "fullness_25",
            "fresh_state",
            "reuse_state",
            "staggered_off",
            "staggered_on",
            "rotation_75",
            "route_no_captions_to_fallback",
        ]

    def test_scenario_lookup_returns_the_expected_metadata(self):
        """The helper should return the named scenario with its intent metadata."""
        scenario = scenario_by_name("staggered_on")
        assert scenario.name == "staggered_on"
        assert scenario.preserve_worker_state_root is True
        assert scenario.env_overrides == {}

    def test_route_scenario_sets_the_no_caption_fallback_toggle(self):
        """The route split scenario should enable the no-caption fallback flag."""
        scenario = scenario_by_name("route_no_captions_to_fallback")
        assert scenario.name == "route_no_captions_to_fallback"
        assert scenario.preserve_worker_state_root is False
        assert scenario.env_overrides == {"YTIS_ROUTE_NO_CAPTIONS_TO_FALLBACK": "true"}

    def test_command_builder_includes_shared_worker_state_root(self, tmp_path):
        """The fallback benchmark command should carry the shared worker-state root."""
        command = build_fallback_benchmark_command(
            python_executable="python",
            fallback_benchmark_script=Path("P:\\\\\\packages/yt-is/bin/csf-fallback-crossover-benchmark"),
            trace_root=Path("P:\\\\\\packages/yt-is/.logs/worker_count_trials"),
            cohort_json=Path("P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/cohort.json"),
            output_root=Path("P:\\\\\\packages/yt-is/.logs/load_ladder_benchmark/baseline"),
            source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
            workers=2,
            limit=10,
            batch_size=10,
            policy="notebooklm_only_30s",
            cohort_shape="captioned",
            sample_label="caption_rich",
            worker_state_root=tmp_path / "worker_states",
            preserve_worker_state_root=True,
        )
        assert "--cohort-shape" in command
        assert "captioned" in command
        assert "--sample-label" in command
        assert "caption_rich" in command
        assert "--worker-state-root" in command
        assert str(tmp_path / "worker_states") in command
        assert "--preserve-worker-state-root" in command

    def test_command_builder_includes_manifest_selection(self, tmp_path):
        """The fallback benchmark command should carry manifest filters when provided."""
        command = build_fallback_benchmark_command(
            python_executable="python",
            fallback_benchmark_script=Path("P:\\\\\\packages/yt-is/bin/csf-fallback-crossover-benchmark"),
            trace_root=Path("P:\\\\\\packages/yt-is/.logs/worker_count_trials"),
            cohort_json=Path("P:\\\\\\packages/yt-is/.logs/breadth_series/cohort.json"),
            output_root=Path("P:\\\\\\packages/yt-is/.logs/breadth_series/broad"),
            source_url="https://www.youtube.com/channel/UCYTISFALLBACKBMK",
            workers=2,
            limit=400,
            batch_size=200,
            policy="notebooklm_only_30s",
            cohort_shape="manifest",
            sample_label="breadth_broad",
            manifest_json=Path("P:\\\\\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json"),
            manifest_families="routing,hot_path_control",
            worker_state_root=tmp_path / "worker_states",
            preserve_worker_state_root=False,
        )
        assert "--manifest-json" in command
        assert str(Path("P:\\\\\\packages/yt-is/tests/fixtures/shared_benchmark_manifest.json")) in command
        assert "--manifest-families" in command
        assert "routing,hot_path_control" in command
