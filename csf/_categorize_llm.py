#!/usr/bin/env python3
"""Keyword-based subcategorizer for unscored AI/ML channels.

Runs keyword scoring on AI/ML channels with subcategory IS NULL.
Fully idempotent — safe to re-run; only touches unscored rows.

Usage:
    python csf/_categorize_llm.py          # score + apply changes
    python csf/_categorize_llm.py --dry   # preview without applying
    python csf/_categorize_llm.py --dry --limit 20  # test on 20 channels
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DB = Path("P:/__csf/.data/yt-is/batch_status.sqlite")

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
    "Company AI": {
        "title": [
            "openai", "anthropic", "google ai", "deepmind", "meta ai",
            "microsoft ai", "amazon ai", "nvidia ai", "nvidia", "apple ai",
            "ibm watson", " Stability AI", "midjourney", "character.ai",
        ],
        "description": [
            "ai company", "ai research", "ai safety", "ai assistant",
        ],
    },
    "Platforms & Ecosystem": {
        "title": [
            "kaggle", "langchain", "llamaindex", "huggingface", "ollama",
            "vectors", "pinecone", "weaviate", "chroma", "qdrant",
            "github", "gitlab", "vercel", "netlify", "replit",
        ],
        "description": [
            "ai platform", "ai ecosystem", "developer platform", "ml platform",
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


def score_channel(title: str, description: str) -> tuple[str | None, str | None]:
    """Score a channel against keyword sets. Returns (category, subcategory)."""
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()

    best_subcat: str | None = None
    best_subcat_score = 0
    for subcat, kws in SUBCAT_KEYWORDS.items():
        score = sum(
            TITLE_WEIGHT for kw in kws.get("title", []) if kw.lower() in title_lower
        )
        score += sum(
            DESC_WEIGHT for kw in kws.get("description", []) if kw.lower() in desc_lower
        )
        if score >= MIN_SUBCAT_SCORE and score > best_subcat_score:
            best_subcat_score = score
            best_subcat = subcat

    if best_subcat:
        return ("AI/ML", best_subcat)

    best_cat: str | None = None
    best_cat_score = 0
    for cat, kws in CATEGORY_KEYWORDS.items():
        score = sum(
            TITLE_WEIGHT for kw in kws.get("title", []) if kw.lower() in title_lower
        )
        score += sum(
            DESC_WEIGHT for kw in kws.get("description", []) if kw.lower() in desc_lower
        )
        if score >= MIN_CAT_SCORE and score > best_cat_score:
            best_cat_score = score
            best_cat = cat

    if best_cat:
        return (best_cat, None)

    return (None, None)


def get_video_titles(channel_url: str, limit: int = 10) -> list[str]:
    """Fetch recent video titles from a channel using yt-dlp.

    Args:
        channel_url: YouTube channel URL
        limit: Max number of video titles to return

    Returns:
        List of video titles, empty if fetch failed.
    """
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--flat-playlist", "--playlist-end", str(limit), channel_url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        titles = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    entry = json.loads(line)
                    if entry.get("title"):
                        titles.append(entry["title"])
                except json.JSONDecodeError:
                    continue
        return titles
    except Exception:
        return []


def export_hardtail_with_videos(output_path: Path, limit: int | None = None) -> list[dict]:
    """Export AI/ML channels with subcategory IS NULL, fetching video titles for each.

    Args:
        output_path: Where to write the JSON
        limit: Optional limit on number of channels

    Returns:
        List of channel dicts with url, title, desc, video_titles
    """
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

    print(f"Fetching video titles for {len(channels)} channels...")
    results = []
    for i, (url, title, desc) in enumerate(channels):
        video_titles = get_video_titles(url)
        results.append({
            "url": url,
            "title": title or "",
            "desc": desc or "",
            "video_titles": video_titles,
        })
        if (i + 1) % 10 == 0:
            print(f"  Processed {i + 1}/{len(channels)}")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(results)} channels to {output_path}")
    return results


def run(dry: bool = False, limit: int | None = None) -> dict:
    """Score and apply categorization. Returns stats dict."""
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
        "relocated": [],
        "unmatched": [],
    }

    for url, title, desc in channels:
        cat, subcat = score_channel(title or "", desc or "")
        if subcat:
            results["subcategorized"].append((url, title or "(untitled)", subcat))
        elif cat:
            results["relocated"].append((url, title or "(untitled)", cat))
        else:
            results["unmatched"].append((url, title or "(untitled)"))

    if not dry:
        conn = sqlite3.connect(DB)
        conn.execute("PRAGMA journal_mode=WAL")
        for url, title, subcat in results["subcategorized"]:
            conn.execute(
                "UPDATE channel_metadata SET subcategory = ? WHERE channel_url = ? AND category = 'AI/ML' AND subcategory IS NULL",
                (subcat, url),
            )
        for url, title, cat in results["relocated"]:
            conn.execute(
                "UPDATE channel_metadata SET category = ?, subcategory = NULL WHERE channel_url = ? AND category = 'AI/ML'",
                (cat, url),
            )
        conn.commit()
        conn.close()

    subcat_total = len(results["subcategorized"])
    reloc_total = len(results["relocated"])
    unmatch_total = len(results["unmatched"])

    print(f"{'[DRY RUN] ' if dry else ''}Scored {len(channels)} channels")
    print(f"  → Subcategorized (AI/ML): {subcat_total}")
    print(f"  → Relocated to other category: {reloc_total}")
    print(f"  → Unmatched (stay AI/ML base): {unmatch_total}")

    if subcat_total:
        print("\n  Subcategory breakdown:")
        by_subcat: dict[str, list] = {}
        for _, title, subcat in results["subcategorized"]:
            by_subcat.setdefault(subcat, []).append(title)
        for subcat, titles in sorted(by_subcat.items(), key=lambda x: -len(x[1])):
            print(f"    AI/ML > {subcat}: {len(titles)}")

    if reloc_total:
        print("\n  Category relocation breakdown:")
        by_cat: dict[str, list] = {}
        for _, title, cat in results["relocated"]:
            by_cat.setdefault(cat, []).append(title)
        for cat, titles in sorted(by_cat.items(), key=lambda x: -len(x[1])):
            print(f"    → {cat}: {len(titles)}")

    if unmatch_total:
        print(f"\n  {unmatch_total} channels scored 0")

    return {
        "total": len(channels),
        "subcategorized": subcat_total,
        "relocated": reloc_total,
        "unmatched": unmatch_total,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keyword-based subcategorizer for AI/ML channels"
    )
    parser.add_argument("--dry", action="store_true", help="Preview without applying")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N channels")
    parser.add_argument(
        "--export-videos",
        action="store_true",
        help="Export hardtail channels with video titles (for LLM subagents)",
    )
    parser.add_argument(
        "--export-output",
        type=Path,
        default=Path("P:/packages/yt-is/csf/hardtail_channels.json"),
        help="Output path for export",
    )
    args = parser.parse_args()

    if args.export_videos:
        export_hardtail_with_videos(args.export_output, args.limit)
        sys.exit(0)

    stats = run(dry=args.dry, limit=args.limit)

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
