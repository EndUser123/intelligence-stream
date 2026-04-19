#!/usr/bin/env python3
"""Idempotent keyword-scoring subcategorizer for AI/ML channels.

Run after initial bulk categorization (categorize command). This script:
1. Scores unscored AI/ML channels (subcategory IS NULL) against keyword sets
2. Assigns subcategories within AI/ML OR relocates to other categories
3. Is fully idempotent — safe to re-run; only touches unscored rows

Usage:
    python csf/_categorize_remaining.py          # score + apply changes
    python csf/_categorize_remaining.py --dry  # preview without applying
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path("P:/__csf/.data/yt-is/batch_status.sqlite")

# ----------------------------------------------------------------------
# Keyword sets
# ----------------------------------------------------------------------
# title keywords: 2x weight | description keywords: 1x weight
# Covers both AI/ML subcategories and category relocations

TITLE_WEIGHT = 2
DESC_WEIGHT = 1
MIN_SUBCAT_SCORE = 2
MIN_CAT_SCORE = 3

SUBCAT_KEYWORDS = {
    "AI Coding & Tutorials": {
        "title": [
            "tutorial", "course", "learn", "python", "coding", "code", "programming",
            "pytorch", "tensorflow", "jupyter", "notebook", "fastai", "langchain",
            "openai", "api", "prompt engineering", "fine-tuning", "fine tuning",
            "llm", "transformer", "neural network", "machine learning", "mlops",
            "deploy", "rag", "vector database", "embeddings", "huggingface",
        ],
        "description": [
            "tutorial", "course", "learn to code", "programming tutorial",
            "machine learning tutorial", "deep learning tutorial", "ai course",
        ],
    },
    "Research & Papers": {
        "title": [
            "paper", "arxiv", "preprint", "research", "icml", "neurips", "acl",
            "cvpr", "iclr", "nature", "science", "journal", "conference",
            "study", "breakthrough", "deepmind research", "openai research",
            "paper review", "paper explained", "paper breakdown", "yoshua bengio",
        ],
        "description": [
            "research paper", "arxiv", "machine learning research",
            "paper review", "scientific", "conference paper",
        ],
    },
    "AI Tools & Reviews": {
        "title": [
            "review", "demo", "测评", "best ai", "top ", " vs ", "compare",
            "chatgpt", "claude", "gpt-", "gemini", "midjourney", "stable diffusion",
            "runway", "sora", "udio", "suno", "perplexity", "cursor", "windsurf",
            "copilot", "notion ai", "writesonic", "jasper", "copy.ai",
            "openai", "anthropic", "google ai", "microsoft ai", "amazon ai",
        ],
        "description": [
            "review", "ai tool", "ai assistant", "ai software", "测评",
            "artificial intelligence tool", "product review", "software review",
        ],
    },
    "AI Podcasts & Interviews": {
        "title": [
            "podcast", "interview", "conversation", "episode", "talk",
            "speech", "lecture", "keynote", "presentation",
            " Lex Fridman", "fridman", "dwarkesh", "micahl", "podcast episode",
        ],
        "description": [
            "podcast", "interview", "conversation with", "episode",
            "talk about ai", "ai podcast",
        ],
    },
    "AI Finance & Trading": {
        "title": [
            "trading", "finance", "financial", "investing", "invest",
            "stock", "crypto", "bitcoin", "trading strategy", "quant",
            "algorithmic", "options", "futures", "tradingview", "tastytrade",
            "backtest", "market analysis", "trader", "trading journal",
        ],
        "description": [
            "trading", "finance", "financial markets", "stock trading",
            "investing", "algorithmic trading", "quantitative finance",
        ],
    },
    "Tech & Coding": {
        "title": [
            "tech", "technology", "software", "developer", "devops", "cloud",
            "aws", "azure", "gcp", "docker", "kubernetes", "linux", "programming",
            "javascript", "typescript", "rust", "golang", "java", "backend",
            "frontend", "web dev", "app development", "mobile", "startup",
        ],
        "description": [
            "technology", "software development", "programming", "tech news",
            "developer", "cloud computing", "devops",
        ],
    },
}

CATEGORY_KEYWORDS = {
    "Technology": {
        "title": [
            "tech", "technology", "software", "developer", "devops", "cloud",
            "computer", "programming", "coding", "hacker", "ethical hacking",
            "cyber", "security", "network", "server", "linux", "windows",
            "apple", "android", "web development", "javascript",
            "frontend", "backend", "database", "sql", "nosql",
            "robotics", "hardware", "chip", "processor", "cpu", "gpu",
        ],
        "description": [
            "technology", "tech news", "software", "programming",
            "developer", "computer science", "tech review",
        ],
    },
    "Science": {
        "title": [
            "science", "scientific", "physics", "chemistry", "biology",
            "cosmology", "space", "astronomy", "astrophysics", "neuroscience",
            "psychology", "research", "experiment", "lab", "university",
            "quantum", "electromagnetic", "genetics",
        ],
        "description": [
            "science", "scientific", "research", "university",
            "physics", "biology", "chemistry", "space science",
        ],
    },
    "Education": {
        "title": [
            "education", "teaching", "teacher", "student", "course", "tutorial",
            "learn", "school", "university", "college", "lesson", "class",
            "study tips", "learning", "academic", "exam", "revision",
            "math", "maths", "statistics", "probability", "algebra", "calculus",
        ],
        "description": [
            "education", "teaching", "learning", "course", "tutorial",
            "student", "teacher", "school", "university",
        ],
    },
    "Entertainment": {
        "title": [
            "funny", "comedy", "entertainment", "vlog", "podcast",
            "gaming", "game", "twitch", "meme", "laugh", "prank",
            "reaction", "watch", "review", "movie", "film", "series",
            "anime", "cartoon", "animation", "funny moments",
        ],
        "description": [
            "entertainment", "comedy", "funny", "vlog", "gaming",
            "podcast", "reaction", "meme",
        ],
    },
    "Health": {
        "title": [
            "health", "fitness", "workout", "diet", "nutrition", "medical",
            "doctor", "medicine", "wellness", "health tips", "exercise",
            "gym", "running", "yoga", "mental health", "sleep", "weight loss",
            "keto", "carnivore", "dietary", "supplement", "biohacking",
        ],
        "description": [
            "health", "fitness", "nutrition", "medical", "wellness",
            "exercise", "diet", "health tips",
        ],
    },
    "Finance": {
        "title": [
            "finance", "financial", "investing", "invest", "stock market",
            "trading", "crypto", "banking", "money", "wealth", "passive income",
            "real estate", "retirement", "budgeting", "personal finance",
            "wealth building", "dividends", "index fund", "etf",
        ],
        "description": [
            "finance", "investing", "stock market", "personal finance",
            "wealth", "money management", "financial planning",
        ],
    },
    "Military": {
        "title": [
            "military", "army", "navy", "air force", "marine", "combat",
            "warfare", "tactical", "gun", "weapon", "soldier", "veteran",
            "defense", "defence", "national security", "geopolitics",
            "conflict", "war", "battle", "strategy", "military history",
        ],
        "description": [
            "military", "defense", "armed forces", "combat", "veteran",
            "warfare", "tactical", "national security",
        ],
    },
    "News": {
        "title": [
            "news", "journalism", "reporter", "breaking", "latest",
            "update", "world news", "politics", "election", "government",
            "policy", "economy", "market news", "current events",
        ],
        "description": [
            "news", "journalism", "current events", "breaking news",
            "world news", "politics", "reporter",
        ],
    },
}


# ----------------------------------------------------------------------
# Scoring logic
# ----------------------------------------------------------------------


def score_channel(title: str, description: str) -> tuple[str | None, str | None]:
    """Score a channel against keyword sets. Returns (category, subcategory).
    If subcategory is set, channel stays in AI/ML with that subcategory.
    If category is set (and subcategory None), channel is relocated.
    If both None, channel is unmatched and stays in AI/ML base.
    """
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()

    # Check AI/ML subcategories first
    best_subcat: str | None = None
    best_subcat_score = 0
    for subcat, kws in SUBCAT_KEYWORDS.items():
        score = sum(TITLE_WEIGHT for kw in kws.get("title", []) if kw.lower() in title_lower)
        score += sum(DESC_WEIGHT for kw in kws.get("description", []) if kw.lower() in desc_lower)
        if score >= MIN_SUBCAT_SCORE and score > best_subcat_score:
            best_subcat_score = score
            best_subcat = subcat

    if best_subcat:
        return ("AI/ML", best_subcat)

    # Check category relocations
    best_cat: str | None = None
    best_cat_score = 0
    for cat, kws in CATEGORY_KEYWORDS.items():
        score = sum(TITLE_WEIGHT for kw in kws.get("title", []) if kw.lower() in title_lower)
        score += sum(DESC_WEIGHT for kw in kws.get("description", []) if kw.lower() in desc_lower)
        if score >= MIN_CAT_SCORE and score > best_cat_score:
            best_cat_score = score
            best_cat = cat

    if best_cat:
        return (best_cat, None)

    return (None, None)


# ----------------------------------------------------------------------
# Main logic
# ----------------------------------------------------------------------


def run(dry: bool = False) -> dict:
    """Score and apply categorization. Returns stats dict."""
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")

    channels = conn.execute("""
        SELECT channel_url, channel_title, description
        FROM channel_metadata
        WHERE category = 'AI/ML' AND subcategory IS NULL
    """).fetchall()

    results: dict[str, list] = {"subcategorized": [], "relocated": [], "unmatched": []}

    for url, title, desc in channels:
        cat, subcat = score_channel(title, desc)
        entry = (url, title or "(untitled)", cat, subcat)
        if subcat:
            results["subcategorized"].append(entry)
        elif cat:
            results["relocated"].append(entry)
        else:
            results["unmatched"].append(entry)

    if not dry:
        for url, title, cat, subcat in results["subcategorized"]:
            conn.execute(
                "UPDATE channel_metadata SET subcategory = ? WHERE channel_url = ? AND category = 'AI/ML' AND subcategory IS NULL",
                (subcat, url),
            )
        for url, title, cat, _ in results["relocated"]:
            conn.execute(
                "UPDATE channel_metadata SET category = ?, subcategory = NULL WHERE channel_url = ? AND category = 'AI/ML'",
                (cat, url),
            )
        conn.commit()

    conn.close()

    subcat_total = len(results["subcategorized"])
    reloc_total = len(results["relocated"])
    unmatch_total = len(results["unmatched"])

    # Print summary
    print(f"{'[DRY RUN] ' if dry else ''}Scored {len(channels)} base AI/ML channels")
    print(f"  → Subcategorized (AI/ML): {subcat_total}")
    print(f"  → Relocated to other category: {reloc_total}")
    print(f"  → Unmatched (stay AI/ML base): {unmatch_total}")

    if subcat_total:
        print("\n  Subcategory breakdown:")
        by_subcat: dict[str, list] = {}
        for url, title, cat, subcat in results["subcategorized"]:
            by_subcat.setdefault(subcat, []).append(title)
        for subcat, titles in sorted(by_subcat.items(), key=lambda x: -len(x[1])):
            print(f"    AI/ML > {subcat}: {len(titles)}")

    if reloc_total:
        print("\n  Category relocation breakdown:")
        by_cat: dict[str, list] = {}
        for url, title, cat, _ in results["relocated"]:
            by_cat.setdefault(cat, []).append(title)
        for cat, titles in sorted(by_cat.items(), key=lambda x: -len(x[1])):
            print(f"    → {cat}: {len(titles)}")

    if unmatch_total:
        print(f"\n  {unmatch_total} channels scored 0 — need LLM or manual review")
        print("  Sample (first 5):")
        for _, title, _, _ in results["unmatched"][:5]:
            print(f"    - {title}")

    return {
        "total": len(channels),
        "subcategorized": subcat_total,
        "relocated": reloc_total,
        "unmatched": unmatch_total,
        "details": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Idempotent keyword subcategorizer for AI/ML channels")
    parser.add_argument("--dry", action="store_true", help="Preview changes without applying them")
    args = parser.parse_args()

    stats = run(dry=args.dry)

    if not args.dry:
        # Print final distribution
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
