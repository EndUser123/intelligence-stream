#!/usr/bin/env python3
"""Multi-topic AI/ML channel categorization pipeline.

Scoring strategy (best available signal wins):
  1. Video titles (fetched via yt-dlp) — primary signal, most accurate
  2. Channel title + description — fallback when video fetch unavailable

Output: weighted tag list per channel (channel_url → [(tag, weight), ...])
  Tags are "category/subcategory" strings or just "category".
  Weights sum to 1.0 per channel. Filtering by threshold or top-N gives
  different multi-channel audiences.

Fully idempotent — only updates rows where results differ.

Usage:
    python csf/_categorize.py --status        # show current state
    python csf/_categorize.py --score          # score unscored + update DB
    python csf/_categorize.py --score --dry   # preview without writing
    python csf/_categorize.py --export        # export unscored for subagents
    python csf/_categorize.py --apply T.json  # apply subagent results
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from csf.channel_filtering import (
    ENTERTAINMENT_TERMS,
    LEARNABLE_TERMS,
    PERFORMANCE_TERMS,
    PODCAST_TERMS,
    STORY_TERMS,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB = Path("P:\\\\\\.data/yt-is/batch_status.sqlite")
EXPORT_DIR = Path("P:\\\\\\packages/yt-is/csf/exports")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# Weight config — video titles are authoritative; title+desc is fallback
TITLE_WEIGHT = 2
DESC_WEIGHT = 1
VIDEO_TITLE_WEIGHT = 3  # video titles are best signal

MIN_SCORE = 2           # minimum raw score to earn a tag
AMBIGUOUS_GAP = 1       # if top-2 scores within this gap, flag as ambiguous
DEFAULT_LIMIT = 5      # top-N tags to store when no threshold earns

# ---------------------------------------------------------------------------
# Tag definitions
# ---------------------------------------------------------------------------

# Subcategories within AI/ML
SUBCATS = [
    "AI/ML/AI Coding & Tutorials",
    "AI/ML/AI Finance & Trading",
    "AI/ML/AI Podcasts & Interviews",
    "AI/ML/AI Tools & Reviews",
    "AI/ML/Company AI",
    "AI/ML/Platforms & Ecosystem",
    "AI/ML/Research & Papers",
    "AI/ML/Tech & Coding",
]

# Top-level categories (mutually exclusive with subcats)
CATEGORIES = [
    "Business",
    "Education",
    "Entertainment",
    "Finance",
    "Health",
    "History",
    "Mathematics",
    "Military",
    "News",
    "Robotics",
    "Science",
    "Technology",
]

# All valid tags
ALL_TAGS = SUBCATS + CATEGORIES

LEARNABLE_KEYWORDS = list(dict.fromkeys(LEARNABLE_TERMS))
CONSUMPTIVE_KEYWORDS = list(
    dict.fromkeys(
        (
            *STORY_TERMS,
            *PERFORMANCE_TERMS,
            *PODCAST_TERMS,
            *ENTERTAINMENT_TERMS,
        )
    )
)

# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

KEYWORD_SETS: dict[str, dict[str, list[str]]] = {
    # AI/ML subcategory keywords
    "AI/ML/AI Coding & Tutorials": {
        "title": [
            "tutorial", "course", "learn", "python", "coding", "code", "programming",
            "pytorch", "tensorflow", "jupyter", "notebook", "fastai", "langchain",
            "openai", "api", "prompt engineering", "fine-tuning", "llm",
            "transformer", "neural network", "machine learning", "mlops",
            "deploy", "rag", "vector database", "embeddings", "huggingface",
        ],
        "description": [
            "tutorial", "course", "learn to code", "programming tutorial",
            "machine learning tutorial", "deep learning tutorial", "ai course",
        ],
    },
    "AI/ML/Research & Papers": {
        "title": [
            "paper", "arxiv", "preprint", "research", "icml", "neurips", "acl",
            "cvpr", "iclr", "nature", "science", "journal", "conference",
            "study", "breakthrough", "deepmind", "openai research",
            "paper review", "paper explained", "paper breakdown",
        ],
        "description": [
            "research paper", "arxiv", "machine learning research",
            "paper review", "scientific", "conference paper",
        ],
    },
    "AI/ML/AI Tools & Reviews": {
        "title": [
            "review", "demo", "测评", "best ai", "top ", " vs ", "compare",
            "chatgpt", "claude", "gpt-", "gemini", "midjourney", "stable diffusion",
            "runway", "sora", "udio", "suno", "perplexity", "cursor", "windsurf",
            "copilot", "notion ai", "writesonic", "jasper",
            "openai", "anthropic", "google ai", "microsoft ai", "amazon ai",
        ],
        "description": [
            "review", "ai tool", "ai assistant", "测评",
            "artificial intelligence tool", "product review", "software review",
        ],
    },
    "AI/ML/AI Podcasts & Interviews": {
        "title": [
            "podcast", "interview", "conversation", "episode", "talk",
            "speech", "lecture", "keynote", "presentation",
            "lex fridman", "fridman", "dwarkesh", "micahl",
        ],
        "description": [
            "podcast", "interview", "conversation with", "episode",
            "talk about ai", "ai podcast",
        ],
    },
    "AI/ML/AI Finance & Trading": {
        "title": [
            "trading", "finance", "financial", "investing", "invest",
            "stock", "crypto", "bitcoin", "trading strategy", "quant",
            "algorithmic", "options", "futures", "tradingview", "tastytrade",
            "backtest", "market analysis", "trader",
        ],
        "description": [
            "trading", "finance", "financial markets", "stock trading",
            "investing", "algorithmic trading", "quantitative finance",
        ],
    },
    "AI/ML/Tech & Coding": {
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
    "AI/ML/Company AI": {
        "title": [
            "openai", "anthropic", "google ai", "deepmind", "meta ai",
            "microsoft ai", "amazon ai", "nvidia ai", "apple ai",
            " Stability AI", "midjourney", "character.ai",
        ],
        "description": [
            "ai company", "ai research", "ai safety", "ai assistant",
        ],
    },
    "AI/ML/Platforms & Ecosystem": {
        "title": [
            "kaggle", "langchain", "llamaindex", "huggingface", "ollama",
            "vectors", "pinecone", "weaviate", "chroma", "qdrant",
            "github", "gitlab", "vercel", "netlify", "replit",
        ],
        "description": [
            "ai platform", "ai ecosystem", "developer platform", "ml platform",
        ],
    },
    # Top-level category keywords
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
        ] + LEARNABLE_KEYWORDS,
        "description": [
            "education", "teaching", "learning", "course", "tutorial",
            "student", "teacher", "school", "university",
        ] + LEARNABLE_KEYWORDS,
    },
    "Entertainment": {
        "title": [
            "funny", "comedy", "entertainment", "vlog", "podcast",
            "gaming", "game", "twitch", "meme", "laugh", "prank",
            "reaction", "watch", "review", "movie", "film", "series",
            "anime", "cartoon", "animation", "funny moments",
        ] + CONSUMPTIVE_KEYWORDS,
        "description": [
            "entertainment", "comedy", "funny", "vlog", "gaming",
            "podcast", "reaction", "meme",
        ] + CONSUMPTIVE_KEYWORDS,
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
    "Robotics": {
        "title": [
            "robot", "robotics", "boston dynamics", "humanoid", "autonomous",
            "drone", "quadruped", "wave", "atlas", "spot", "cheetah",
        ],
        "description": [
            "robotics", "robot", "autonomous", "humanoid robot",
        ],
    },
    "Business": {
        "title": [
            "business", "startup", "entrepreneur", "founder", "ceo",
            "marketing", "sales", "product", "saas", "growth",
        ],
        "description": [
            "business", "startup", "entrepreneur", "marketing", "saas",
        ],
    },
    "Mathematics": {
        "title": [
            "math", "maths", "mathematics", "algebra", "calculus", "geometry",
            "number theory", "topology", "statistics", "probability",
        ],
        "description": [
            "mathematics", "math", "statistics", "algebra", "calculus",
        ],
    },
    "History": {
        "title": [
            "history", "historical", "war", "ww2", "wwii", "ancient",
            "medieval", "civilization", "empire",
        ],
        "description": [
            "history", "historical", "world history", "military history",
        ],
    },
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class ScoredTag:
    tag: str
    score: float
    ambiguous: bool = False


def score_text(texts: Sequence[str], tag: str) -> float:
    """Score a tag against a collection of text strings (title, descriptions, video titles).

    All strings are lowercased. Score = sum of matching keyword weights.
    """
    if not texts:
        return 0.0
    joined = " ".join(t.lower() for t in texts if t)
    kw_set = KEYWORD_SETS.get(tag)
    if not kw_set:
        return 0.0
    score = 0.0
    for kw in kw_set.get("title", []):
        score += _count_term_occurrences(joined, kw) * TITLE_WEIGHT
    for kw in kw_set.get("description", []):
        score += _count_term_occurrences(joined, kw) * DESC_WEIGHT
    return score


def _count_term_occurrences(text: str, term: str) -> int:
    """Count whole-term occurrences, avoiding substring overmatches."""
    if not text or not term:
        return 0
    pattern = re.compile(rf"\b{re.escape(term.lower())}\b")
    return len(pattern.findall(text))


def score_channel(
    channel_title: str,
    description: str,
    video_titles: list[str],
) -> list[ScoredTag]:
    """Score a channel against all tags. Returns weighted ScoredTag list.

    Prioritization: video titles >> (channel title + description).
    Ambiguous flag is set when top-2 scores are within AMBIGUOUS_GAP.
    """
    # Video titles as primary signal
    texts = [channel_title, description] if (channel_title or description) else []
    video_texts = video_titles if video_titles else []

    results: list[ScoredTag] = []
    raw_scores: dict[str, float] = {}

    for tag in ALL_TAGS:
        # Primary: video titles
        video_score = score_text(video_texts, tag) * VIDEO_TITLE_WEIGHT
        # Fallback: channel title + description
        text_score = score_text(texts, tag)
        total = video_score + text_score
        if total > 0:
            raw_scores[tag] = total

    if not raw_scores:
        return results

    # Normalize to weights (sum to 1.0)
    total_score = sum(raw_scores.values())
    if total_score > 0:
        for tag, raw in sorted(raw_scores.items(), key=lambda x: -x[1]):
            results.append(ScoredTag(tag=tag, score=raw / total_score))

    # Detect ambiguity: top-2 scores within gap (normalized)
    if len(results) >= 2:
        top2 = results[:2]
        if top2[0].score - top2[1].score <= AMBIGUOUS_GAP / total_score if total_score else 0:
            results[0].ambiguous = True
            results[1].ambiguous = True

    return results


# ---------------------------------------------------------------------------
# Video title fetching
# ---------------------------------------------------------------------------

def get_video_titles(channel_url: str, limit: int = 10) -> list[str]:
    """Fetch recent video titles from a channel using yt-dlp."""
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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_tags_table() -> None:
    """Create channel_tags table if not exists."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_tags (
                channel_url TEXT NOT NULL,
                tag TEXT NOT NULL,
                weight REAL NOT NULL,
                ambiguous INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'keyword',
                PRIMARY KEY (channel_url, tag)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_tags_url ON channel_tags(channel_url)
        """)
        # Migrate: add source column if missing
        try:
            conn.execute("SELECT source FROM channel_tags LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE channel_tags ADD COLUMN source TEXT DEFAULT 'keyword'")


def get_tags(channel_url: str) -> list[ScoredTag]:
    """Get current tags for a channel."""
    _ensure_tags_table()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT tag, weight, ambiguous FROM channel_tags WHERE channel_url = ? ORDER BY weight DESC",
            (channel_url,),
        ).fetchall()
    return [ScoredTag(tag=r[0], score=r[1], ambiguous=bool(r[2])) for r in rows]


def set_tags(channel_url: str, results: list[ScoredTag], source: str = "keyword") -> int:
    """Replace tags for a channel. Returns number of tags set."""
    _ensure_tags_table()
    conn = _conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Delete existing keyword tags for this channel
        conn.execute(
            "DELETE FROM channel_tags WHERE channel_url = ? AND source = ?",
            (channel_url, source),
        )
        for rt in results:
            conn.execute(
                "INSERT INTO channel_tags (channel_url, tag, weight, ambiguous, source) VALUES (?, ?, ?, ?, ?)",
                (channel_url, rt.tag, rt.score, int(rt.ambiguous), source),
            )
        conn.commit()
        return len(results)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_unscored_urls() -> list[tuple[str, str, str]]:
    """Return (url, title, desc) for channels with no keyword tags yet."""
    _ensure_tags_table()
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cm.channel_url, cm.channel_title, cm.description
            FROM channel_metadata cm
            LEFT JOIN channel_tags ct ON cm.channel_url = ct.channel_url AND ct.source = 'keyword'
            WHERE ct.channel_url IS NULL
            ORDER BY cm.channel_title
        """).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def run_scoring(dry: bool = False, limit: int | None = None) -> dict:
    """Score all unscored channels. Returns stats."""
    channels = get_unscored_urls()
    if limit:
        channels = channels[:limit]

    print(f"{'[DRY RUN] ' if dry else ''}Scoring {len(channels)} channels...")

    scored = 0
    no_signal = 0
    ambiguous_count = 0
    by_tag: dict[str, int] = {}

    for i, (url, title, desc) in enumerate(channels):
        titles = get_video_titles(url)
        results = score_channel(title or "", desc or "", titles)

        # Filter to tags that meet minimum score threshold
        thresholded = [r for r in results if r.score >= MIN_SCORE / 100]
        # Always keep top-N even if below threshold
        final = thresholded if thresholded else results[:DEFAULT_LIMIT]

        if not final:
            no_signal += 1
            continue

        amb = any(r.ambiguous for r in final)
        if amb:
            ambiguous_count += 1

        if not dry:
            set_tags(url, final)

        for r in final:
            by_tag[r.tag] = by_tag.get(r.tag, 0) + 1
        scored += 1

        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(channels)}")

    print(f"\n  Scored: {scored}")
    if no_signal:
        print(f"  No signal: {no_signal}")
    if ambiguous_count:
        print(f"  Ambiguous (top-2 within gap): {ambiguous_count}")

    if by_tag:
        print("\n  Tag distribution:")
        for tag, cnt in sorted(by_tag.items(), key=lambda x: -x[1]):
            print(f"    {tag}: {cnt}")

    return {"total": len(channels), "scored": scored, "no_signal": no_signal, "ambiguous": ambiguous_count}


def export_for_subagents(output_path: Path, limit: int | None = None) -> int:
    """Export unscored channels with video titles for LLM subagent processing."""
    channels = get_unscored_urls()
    if limit:
        channels = channels[:limit]

    print(f"Fetching video titles for {len(channels)} channels...")
    results = []
    for i, (url, title, desc) in enumerate(channels):
        titles = get_video_titles(url)
        results.append({"url": url, "title": title or "", "desc": desc or "", "video_titles": titles})
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(channels)}")

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"Exported {len(results)} channels to {output_path}")
    return len(results)


def apply_subagent_results(json_path: Path, dry: bool = False) -> dict:
    """Apply results from subagent JSON. Supports multi-tag output.

    JSON format: list of {
        "url": str,
        "tags": [{"tag": str, "weight": float, "ambiguous": bool}, ...]
    }
    or legacy single-tag: {"url": str, "subcategory": str, "category": str}
    """
    with open(json_path, "r", encoding="utf-8") as f:
        items = json.load(f)

    applied = 0
    skipped = 0

    if not dry:
        for item in items:
            url = item["url"]
            tags = item.get("tags", [])

            # Legacy format support
            if not tags and item.get("subcategory"):
                tags = [
                    {
                        "tag": f"AI/ML/{item['subcategory']}" if item.get("category") == "AI/ML" else item["category"],
                        "weight": 1.0,
                        "ambiguous": False,
                    }
                ]

            results = [ScoredTag(tag=t["tag"], score=t["weight"], ambiguous=t.get("ambiguous", False)) for t in tags]
            if results:
                set_tags(url, results, source="subagent")
                applied += 1
            else:
                skipped += 1

    print(f"{'[DRY RUN] ' if dry else ''}Applied {applied} channels" + (f", skipped {skipped}" if skipped else ""))
    return {"applied": applied, "skipped": skipped}


# ---------------------------------------------------------------------------
# Status & display
# ---------------------------------------------------------------------------

def print_distribution() -> None:
    _ensure_tags_table()
    with _conn() as conn:
        rows = conn.execute("""
            SELECT tag, COUNT(*) as cnt
            FROM channel_tags
            GROUP BY tag
            ORDER BY cnt DESC
        """).fetchall()
        total_channels = conn.execute("SELECT COUNT(DISTINCT channel_url) FROM channel_tags").fetchone()[0]

    print(f"\n--- Tag Distribution ({total_channels} tagged channels) ---")
    for tag, cnt in rows:
        print(f"  {tag}: {cnt}")
    print(f"\nTotal: {sum(r[1] for r in rows)} tag assignments")


def print_status() -> None:
    _ensure_tags_table()
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM channel_metadata").fetchone()[0]
        tagged = conn.execute("SELECT COUNT(DISTINCT channel_url) FROM channel_tags").fetchone()[0]
        unscored = conn.execute("""
            SELECT COUNT(*) FROM channel_metadata cm
            LEFT JOIN channel_tags ct ON cm.channel_url = ct.channel_url AND ct.source = 'keyword'
            WHERE ct.channel_url IS NULL
        """).fetchone()[0]

    print(f"\n{'='*50}")
    print(f"  Total channels in DB: {total}")
    print(f"  Tagged: {tagged}")
    print(f"  Unscored: {unscored}")
    if unscored > 0:
        print()
        print(f"  Next: python csf/_categorize.py --score")
        print(f"  Or:   python csf/_categorize.py --export  (prepare for subagents)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-topic channel categorization")
    parser.add_argument("--dry", action="store_true", help="Preview without applying")
    parser.add_argument("--limit", type=int, default=None, help="Limit to N channels")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--score", action="store_true", help="Score unscored channels")
    parser.add_argument("--export", action="store_true", help="Export unscored with video titles")
    parser.add_argument("--apply", type=Path, default=None, help="Apply subagent results JSON")
    parser.add_argument("--output", type=Path, default=None, help="Output path for --export")
    args = parser.parse_args()

    has_action = any([args.status, args.score, args.export, args.apply])
    if not has_action:
        print_status()
        sys.exit(0)

    if args.status:
        print_status()
        print_distribution()
        sys.exit(0)

    if args.apply:
        apply_subagent_results(args.apply, dry=args.dry)
        sys.exit(0)

    if args.export:
        output = args.output or EXPORT_DIR / "llm_batch.json"
        export_for_subagents(output, limit=args.limit)
        sys.exit(0)

    if args.score:
        run_scoring(dry=args.dry, limit=args.limit)
        if not args.dry:
            print_distribution()
        sys.exit(0)


if __name__ == "__main__":
    main()
