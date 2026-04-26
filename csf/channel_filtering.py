"""Shared heuristics for conservative channel filtering decisions.

The goal is to keep the policy simple:
- keep when a channel is clearly useful to learn from
- block when it is clearly consumption-first
- review when the signal is mixed
"""

from __future__ import annotations

from dataclasses import dataclass
import re


LEARNABLE_TERMS = (
    "tutorial",
    "guide",
    "lesson",
    "course",
    "learn",
    "learning",
    "learner",
    "learns",
    "explainer",
    "explained",
    "breakdown",
    "analysis",
    "review",
    "walkthrough",
    "demo",
    "education",
    "educational",
    "science",
    "research",
    "history",
    "code",
    "coding",
    "programming",
    "build",
    "builder",
    "development",
    "developer",
    "engineering",
    "engineer",
    "strategy",
    "workflow",
    "how to",
    "teach",
    "teaching",
    "exploring",
)

STORY_TERMS = (
    "story",
    "stories",
    "storytelling",
    "lore",
    "fiction",
    "narrative",
    "hfy",
)

PERFORMANCE_TERMS = (
    "music",
    "dance",
    "performance",
    "performer",
    "performing",
    "cover",
    "covers",
    "song",
    "songs",
    "album",
    "concert",
    "live set",
    "official audio",
    "remix",
    "choreography",
    "recital",
)

ENTERTAINMENT_TERMS = (
    "vlog",
    "reaction",
    "meme",
    "comedy",
    "funny",
    "entertainment",
)

CONSUMPTIVE_CONTEXT_TERMS = (
    "out of context",
    "vault",
    "clips",
    "highlights",
    "archive",
)

PODCAST_TERMS = (
    "podcast",
    "episode",
    "interview",
    "conversation",
    "talk",
    "discussion",
    "show",
)

INSTITUTIONAL_CORE_TERMS = (
    ("sports", "keywords contains 'sports'"),
    ("breaking", "keywords/description contains 'breaking'"),
    ("official", "keywords contains 'official'"),
    ("news", "description contains 'news'"),
    ("network", "description contains 'network'"),
)


@dataclass(frozen=True)
class ChannelFilterSignals:
    learnable: tuple[str, ...]
    story: tuple[str, ...]
    performance: tuple[str, ...]
    entertainment: tuple[str, ...]
    consumptive_context: tuple[str, ...]
    podcast: tuple[str, ...]
    institutional_core: tuple[str, ...]
    institutional_light: tuple[str, ...]
    caution: tuple[str, ...]
    recommendation: str


def _collect_hits(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    hits: list[str] = []
    for term in terms:
        pattern = re.compile(rf"\b{re.escape(term)}\b")
        if pattern.search(text) and term not in hits:
            hits.append(term)
    return tuple(hits)


def _collect_pairs(text: str, pairs: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    hits: list[str] = []
    for term, label in pairs:
        pattern = re.compile(rf"\b{re.escape(term)}\b")
        if pattern.search(text) and label not in hits:
            hits.append(label)
    return tuple(hits)


def _looks_like_generic_brand_title(channel_title: str | None) -> bool:
    if not channel_title:
        return False
    tokens = re.findall(r"[A-Za-z0-9']+", channel_title)
    return len(tokens) == 1 and len(tokens[0]) >= 3


def analyze_channel_filter_signals(
    *,
    channel_title: str | None = None,
    description: str | None = None,
    keywords: str | None = None,
    custom_url: str | None = None,
    video_count: int | None = None,
) -> ChannelFilterSignals:
    """Return conservative keep/review/block cues for a channel.

    The rule is intentionally simple:
    - strong learnable signal and no consumptive signal => keep
    - consumption-first without learnable signal => block
    - institutional/news/broadcast core without learnable signal => block
    - podcast-first or mixed signals => review
    """
    parts = " ".join(
        part for part in (channel_title, description, keywords, custom_url) if part
    ).lower()

    learnable = _collect_hits(parts, LEARNABLE_TERMS)
    story = _collect_hits(parts, STORY_TERMS)
    performance = _collect_hits(parts, PERFORMANCE_TERMS)
    entertainment = _collect_hits(parts, ENTERTAINMENT_TERMS)
    consumptive_context = _collect_hits(parts, CONSUMPTIVE_CONTEXT_TERMS)
    podcast = _collect_hits(parts, PODCAST_TERMS)
    institutional_core = _collect_pairs(parts, INSTITUTIONAL_CORE_TERMS)
    institutional_light: list[str] = []
    if channel_title and "news" in channel_title.lower():
        institutional_light.append("title contains 'news'")
    if channel_title and "broadcast" in channel_title.lower():
        institutional_light.append("title contains 'broadcast'")
    if channel_title and "network" in channel_title.lower():
        institutional_light.append("title contains 'network'")

    caution: list[str] = []
    if video_count is not None and video_count < 5:
        caution.append("video_count < 5")
    if video_count is not None and video_count <= 15:
        caution.append("video_count <= 15")
    if custom_url and custom_url.startswith("/@"):
        caution.append("custom_url looks like a creator handle")
    elif custom_url is None:
        caution.append("no custom_url")

    has_consumptive = bool(story or performance or entertainment)
    has_consumptive_context = bool(consumptive_context)
    has_learnable = bool(learnable)
    has_institutional_core = bool(institutional_core)
    generic_brand_title = _looks_like_generic_brand_title(channel_title)

    if has_learnable and not has_consumptive and not has_consumptive_context and not has_institutional_core:
        recommendation = "keep"
    elif (
        (has_consumptive or has_consumptive_context)
        and not has_learnable
    ) or (has_institutional_core and not has_learnable) or (generic_brand_title and not has_learnable):
        recommendation = "block"
    elif podcast and not has_learnable:
        recommendation = "block"
    else:
        recommendation = "review"

    return ChannelFilterSignals(
        learnable=learnable,
        story=story,
        performance=performance,
        entertainment=entertainment,
        consumptive_context=consumptive_context,
        podcast=podcast,
        institutional_core=institutional_core,
        institutional_light=tuple(institutional_light),
        caution=tuple(caution),
        recommendation=recommendation,
    )
