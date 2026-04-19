#!/usr/bin/env python3
"""Few-shot LLM subcategorizer for unscored AI/ML channels.

Uses Gemini CLI with few-shot examples to assign subcategories to AI/ML channels
that scored 0 in keyword matching. Fully idempotent — only touches rows where
subcategory IS NULL.

Usage:
    python csf/_categorize_llm.py --dry        # preview without applying
    python csf/_categorize_llm.py --limit 20   # small batch for testing
    python csf/_categorize_llm.py              # full run (279 channels)
    python csf/_categorize_llm.py --limit 20 --dry  # test with preview
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Literal

DB = Path("P:/__csf/.data/yt-is/batch_status.sqlite")

Subcategory = Literal[
    "AI Coding & Tutorials",
    "Research & Papers",
    "AI Tools & Reviews",
    "AI Podcasts & Interviews",
    "AI Finance & Trading",
    "Tech & Coding",
    "Company AI",
    "Platforms & Ecosystem",
]

SUBCATEGORIES: list[str] = [
    "AI Coding & Tutorials",
    "Research & Papers",
    "AI Tools & Reviews",
    "AI Podcasts & Interviews",
    "AI Finance & Trading",
    "Tech & Coding",
    "Company AI",
    "Platforms & Ecosystem",
]

SUBCATEGORY_LIST = ", ".join(SUBCATEGORIES)

# Few-shot examples extracted from existing subcategorized channels
FEW_SHOT_EXAMPLES = """\
Example 1:
Title: Tech With Tim
Description: I'm Tim, a self-taught developer & entrepreneur who brings you educational tech content without the fluff and noise.
Category: AI/ML  →  Subcategory: AI Coding & Tutorials

Example 2:
Title: Liam Ottley
Description: I'm an AI entrepreneur from New Zealand. I created the 'AI Automation Agency' model based on my learnings at my own agency Morningside AI.
Category: AI/ML  →  Subcategory: AI Coding & Tutorials

Example 3:
Title: Yannic Kilcher
Description: I make videos about machine learning research papers, programming, and issues of the AI community.
Category: AI/ML  →  Subcategory: Research & Papers

Example 4:
Title: Two Minute Papers
Description: What a time to be alive - with Dr. Károly Zsolnai-Fehér.
Category: AI/ML  →  Subcategory: Research & Papers

Example 5:
Title: AI Explained
Description: AI Explained is a YouTube channel focused on exploring the world of artificial intelligence.
Category: AI/ML  →  Subcategory: AI Tools & Reviews

Example 6:
Title: Matthew Berman
Description: My mission is simple: to make the benefits of AI and emerging technology accessible to everyone, everywhere.
Category: AI/ML  →  Subcategory: AI Tools & Reviews

Example 7:
Title: The Diary Of A CEO
Description: 64% of our viewers don't realise they don't subscribe, please double check, thank you!!
Category: AI/ML  →  Subcategory: AI Podcasts & Interviews

Example 8:
Title: Lex Fridman
Description: Lex Fridman Podcast and other videos.
Category: AI/ML  →  Subcategory: AI Podcasts & Interviews

Example 9:
Title: Mike Jones Investing
Description: My name is Mike Jones and I created this channel to share financial and entertaining information about saving, investing and retiring.
Category: AI/ML  →  Subcategory: AI Finance & Trading

Example 10:
Title: StockedUp
Description: Informative and entertaining stock market videos for every trading day of the week.
Category: AI/ML  →  Subcategory: AI Finance & Trading

Example 11:
Title: Rob Braxman Tech
Description: Alt-Tech. The Internet Privacy Guy. Public interest hacker and technologist.
Category: AI/ML  →  Subcategory: Tech & Coding

Example 12:
Title: ArjanCodes
Description: On this channel, I post videos about programming and software design to help you take your coding skills to the next level.
Category: AI/ML  →  Subcategory: Tech & Coding

Example 13:
Title: Anthropic
Description: We're an AI safety and research company. Talk to our AI assistant Claude on claude.com.
Category: AI/ML  →  Subcategory: Company AI

Example 14:
Title: Kaggle
Description: Kaggle's global community of practitioners, researchers, and enthusiasts collaborate to shape the frontier of AI.
Category: AI/ML  →  Subcategory: Platforms & Ecosystem

Example 15:
Title: LangChain
Description: Learn more about how to build agents with LangChain products.
Category: AI/ML  →  Subcategory: Platforms & Ecosystem
"""


def build_prompt(channel_title: str, channel_description: str) -> str:
    return (
        f"You are an AI/ML subcategory classifier. Given a YouTube channel's title and description,\n"
        f"choose EXACTLY ONE subcategory from this list:\n"
        f"{SUBCATEGORY_LIST}\n\n"
        "Rules:\n"
        "- Only use subcategories from the list above\n"
        "- If the channel is NOT primarily about AI/ML (e.g., it's primarily about general tech, news, entertainment), return 'RECLASSIFY'\n"
        "- If genuinely uncertain between multiple subcategories, pick the best fit\n"
        "- Return ONLY the subcategory name or 'RECLASSIFY', nothing else\n\n"
        f"{FEW_SHOT_EXAMPLES}\n\n"
        f"Channel title: {channel_title}\n"
        f"Channel description: {channel_description or '(none)'}\n\n"
        "Return ONLY the subcategory name or RECLASSIFY."
    )


def classify_channel(
    channel_title: str,
    channel_description: str,
    timeout: float = 30.0,
) -> str | None:
    """Classify a channel using Gemini CLI few-shot prompting.

    Returns a subcategory name, 'RECLASSIFY', or None on failure.
    """
    if not channel_title:
        return None

    prompt = build_prompt(channel_title, channel_description)

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
        if "Quota" in result.stderr or "quota" in result.stderr:
            print(f"[LLM QUOTA EXHAUSTED] Gemini rate limit hit. gemini stderr: {result.stderr[:200]}", file=sys.stderr)
        return None

    text = result.stdout.strip()
    if text and not text.startswith("{"):
        raw = text.split("\n")[0].strip().strip('"')
        if raw.upper() == "RECLASSIFY":
            return "RECLASSIFY"
        if raw in SUBCATEGORIES:
            return raw
        for subcat in SUBCATEGORIES:
            if subcat.lower() in raw.lower():
                return subcat
        return None
    try:
        parsed = json.loads(text)
        raw = (
            parsed.get("response", "")
            or parsed.get("category", "")
            or parsed.get("text", "")
            or ""
        )
    except json.JSONDecodeError:
        cleaned = re.sub(r"```(?:json)?\n?|```", "", text).strip()
        raw = cleaned.split("\n")[0].strip().strip('"')

    raw = raw.strip()
    if raw.upper() == "RECLASSIFY":
        return "RECLASSIFY"
    if raw in SUBCATEGORIES:
        return raw
    for subcat in SUBCATEGORIES:
        if subcat.lower() in raw.lower():
            return subcat
    return None


def get_seed_examples(conn: sqlite3.Connection, n: int = 3) -> dict[str, list[tuple[str, str]]]:
    """Fetch n seed examples per subcategory from already-categorized channels."""
    rows = conn.execute("""
        SELECT channel_title, description, subcategory
        FROM channel_metadata
        WHERE category = 'AI/ML' AND subcategory IS NOT NULL
          AND description IS NOT NULL AND LENGTH(description) > 40
        ORDER BY RANDOM()
        LIMIT ?
    """, (n * len(SUBCATEGORIES),)).fetchall()
    by_sub: dict[str, list[tuple[str, str]]] = {}
    for title, desc, sub in rows:
        by_sub.setdefault(sub, []).append((title, (desc or "")[:250]))
    return by_sub


def run(dry: bool = False, limit: int | None = None, workers: int = 1) -> dict:
    """Run few-shot LLM subcategorization. Returns stats dict."""
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")

    query = """
        SELECT channel_url, channel_title, description
        FROM channel_metadata
        WHERE category = 'AI/ML' AND subcategory IS NULL
    """
    if limit:
        query += f" LIMIT {limit}"
    channels = conn.execute(query).fetchall()
    conn.close()

    results: dict[str, list] = {
        "subcategorized": [],
        "reclassify": [],
        "failed": [],
    }

    for url, title, desc in channels:
        subcat = classify_channel(title or "", desc or "")
        if subcat == "RECLASSIFY":
            results["reclassify"].append((url, title or "(untitled)"))
        elif subcat:
            results["subcategorized"].append((url, title or "(untitled)", subcat))
        else:
            results["failed"].append((url, title or "(untitled)"))

    if not dry:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode=WAL")
        for url, title, subcat in results["subcategorized"]:
            conn.execute(
                "UPDATE channel_metadata SET subcategory = ? WHERE channel_url = ? AND category = 'AI/ML' AND subcategory IS NULL",
                (subcat, url),
            )
        for url, title in results["reclassify"]:
            # These need manual relocation - flag by leaving subcategory NULL
            # but mark them as needing review (could set a temp flag column)
            pass
        conn.commit()
        conn.close()

    # Print summary
    subcat_total = len(results["subcategorized"])
    reclass_total = len(results["reclassify"])
    failed_total = len(results["failed"])

    print(f"{'[DRY RUN] ' if dry else ''}Processed {len(channels)} channels")
    print(f"  → Subcategorized (AI/ML): {subcat_total}")
    print(f"  → Flagged for relocation (RECLASSIFY): {reclass_total}")
    print(f"  → Failed (LLM error): {failed_total}")

    if subcat_total:
        print("\n  Subcategory breakdown:")
        by_subcat: dict[str, list] = {}
        for _, title, subcat in results["subcategorized"]:
            by_subcat.setdefault(subcat, []).append(title)
        for subcat, titles in sorted(by_subcat.items(), key=lambda x: -len(x[1])):
            print(f"    AI/ML > {subcat}: {len(titles)}")
            for t in titles[:3]:
                print(f"      - {t}")
            if len(titles) > 3:
                print(f"      ... and {len(titles) - 3} more")

    if reclass_total:
        print(f"\n  {reclass_total} channels flagged for relocation:")
        for _, title in results["reclassify"][:10]:
            print(f"    - {title}")
        if reclass_total > 10:
            print(f"    ... and {reclass_total - 10} more")

    if failed_total:
        print(f"\n  {failed_total} channels failed LLM classification:")
        for _, title in results["failed"][:5]:
            print(f"    - {title}")

    return {
        "total": len(channels),
        "subcategorized": subcat_total,
        "reclassify": reclass_total,
        "failed": failed_total,
        "details": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Few-shot LLM subcategorizer for AI/ML channels"
    )
    parser.add_argument("--dry", action="store_true", help="Preview without applying")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit to N channels (for testing)"
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="Number of parallel workers (default 1; Gemini CLI is CPU-bound)"
    )
    args = parser.parse_args()

    stats = run(dry=args.dry, limit=args.limit, workers=args.workers)

    if not args.dry:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute("""
            SELECT category, subcategory, COUNT(*) as cnt
            FROM channel_metadata
            GROUP BY category, subcategory
            ORDER BY CASE WHEN category IS NULL THEN 1 ELSE 0 END, category,
                     CASE WHEN subcategory IS NULL THEN 1 ELSE 0 END, subcategory
        """).fetchall()
        conn.close()

        print("\n--- Distribution ---")
        for cat, sub, cnt in rows:
            label = f"{cat} > {sub}" if sub else str(cat)
            print(f"  {label}: {cnt}")
        print(f"\nTotal: {sum(r[2] for r in rows)}")

    sys.exit(0)


if __name__ == "__main__":
    main()
