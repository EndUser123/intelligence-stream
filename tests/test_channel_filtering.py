"""Tests for conservative channel filtering heuristics."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\\\\\\packages\\yt-is").absolute()))

from csf.channel_filtering import analyze_channel_filter_signals


def test_learnable_channel_is_keep():
    signals = analyze_channel_filter_signals(
        channel_title="Learn Meta-Analysis",
        description="A tutorial and guide for researchers.",
        keywords="education, explainer, analysis",
        video_count=128,
    )
    assert signals.recommendation == "keep"
    assert "tutorial" in signals.learnable
    assert not signals.story
    assert not signals.performance


def test_story_channel_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="Best HFY Stories",
        description="Story archive",
        keywords="hfy, fiction, lore",
        video_count=96,
    )
    assert signals.recommendation == "block"
    assert "hfy" in signals.story
    assert "fiction" in signals.story


def test_music_channel_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="BASS BOOSTED SONGS",
        description="Official audio and live set uploads",
        keywords="music, song, performance",
        video_count=947,
    )
    assert signals.recommendation == "block"
    assert "music" in signals.performance
    assert "official audio" in signals.performance


def test_generic_brand_title_without_learnable_signal_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="Rayen",
        description="A channel about things.",
        keywords="",
        video_count=50,
    )
    assert signals.recommendation == "block"


def test_out_of_context_archive_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="Out of Context AI",
        description="Archive of clips and highlights",
        keywords="",
        video_count=48,
    )
    assert signals.recommendation == "block"
    assert "out of context" in signals.consumptive_context


def test_podcast_without_clear_learning_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="The Visser Podcast",
        description="Podcast and discussion",
        keywords="podcast, episode",
        video_count=215,
    )
    assert signals.recommendation == "block"
    assert "podcast" in signals.podcast


def test_institutional_core_without_learnable_is_block():
    signals = analyze_channel_filter_signals(
        channel_title="CNN",
        description="Breaking news network coverage",
        keywords="news, breaking, official",
        video_count=182077,
    )
    assert signals.recommendation == "block"
    assert "keywords/description contains 'breaking'" in signals.institutional_core
    assert "description contains 'news'" in signals.institutional_core
