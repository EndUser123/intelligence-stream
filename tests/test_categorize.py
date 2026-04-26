"""Tests for shared channel categorization vocabulary."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(r"P:\\packages\\yt-is").absolute()))

from csf._categorize import score_channel, score_text


def test_score_text_does_not_match_cover_inside_discover():
    assert score_text(["discover ai"], "Entertainment") == 0.0


def test_learnable_channel_scores_education():
    tags = score_channel(
        "Learn Meta-Analysis",
        "Tutorial and guide for researchers.",
        ["Lesson breakdown for beginners"],
    )
    assert tags
    assert tags[0].tag == "Education"


def test_consumptive_music_channel_scores_entertainment():
    tags = score_channel(
        "BASS BOOSTED SONGS",
        "Official audio live set uploads",
        ["Cover song remix"],
    )
    assert tags
    assert tags[0].tag == "Entertainment"
