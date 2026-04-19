"""LLM channel categorization using Gemini CLI.

Reads channel title + description, assigns a category from a fixed set.
Zero external API cost — uses the Gemini CLI already in the environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CATEGORIES = [
    "AI/ML",
    "Robotics",
    "Physics",
    "Mathematics",
    "Business",
    "Entertainment",
    "Education",
    "Science",
    "Technology",
    "Gaming",
    "Music",
    "News",
    "Finance",
    "Health",
    "Sports",
]

_CATEGORY_LIST = ", ".join(CATEGORIES)

# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def categorize_channel(
    channel_title: str,
    channel_description: str,
    timeout: float = 30.0,
) -> str | None:
    """Categorize a channel using Gemini CLI.

    Args:
        channel_title: The channel's title.
        channel_description: The channel's description (can be empty).
        timeout: Subprocess timeout in seconds.

    Returns:
        A category string from CATEGORIES, or None on failure.
    """
    if not channel_title:
        return None

    prompt = (
        "You are a channel classifier. Given a YouTube channel's title and description,\n"
        "choose exactly ONE category from this list:\n"
        f"{_CATEGORY_LIST}\n\n"
        "Rules:\n"
        "- Pick the most specific category that fits\n"
        "- Return ONLY the category name, nothing else\n"
        "- If you cannot determine, return 'Other'\n\n"
        f"Channel title: {channel_title}\n"
        f"Channel description: {channel_description or '(none)'}\n\n"
        "Return ONLY the category name."
    )

    gemini_path = shutil.which("gemini")
    if not gemini_path:
        return None

    try:
        result = subprocess.run(
            [gemini_path, "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    text = result.stdout.strip()
    # Try to extract JSON or raw text
    try:
        parsed = json.loads(text)
        category = parsed.get("response", "") or parsed.get("category", "") or parsed.get("text", "") or ""
    except json.JSONDecodeError:
        # Strip markdown and extract first non-empty line
        cleaned = re.sub(r"```(?:json)?\n?|```", "", text).strip()
        category = cleaned.split("\n")[0].strip().strip('"')

    # Validate against known categories
    if category in CATEGORIES:
        return category
    # Fallback: fuzzy match on first word
    for cat in CATEGORIES:
        if cat.lower() in category.lower():
            return cat
    return None