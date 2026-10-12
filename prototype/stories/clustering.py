from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "have", "in", "is", "it", "of", "on", "or", "said", "says", "the", "to",
    "with", "after", "before", "over", "under", "will", "could", "would",
}


def _tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.casefold())
    return {word for word in words if word not in STOP_WORDS and len(word) > 1}


def title_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, " ".join(sorted(left_tokens)), " ".join(sorted(right_tokens))).ratio()
    return max(overlap, sequence)


def _same_story(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, float, str]:
    if left["source_url"].rstrip("/").casefold() == right["source_url"].rstrip("/").casefold():
        return True, 1.0, "same_url"
    if left.get("publisher", left["source_name"]) == right.get(
        "publisher", right["source_name"]
    ):
        return False, 0.0, "same_publisher"
    similarity = title_similarity(left["title"], right["title"])
    if similarity >= 0.74:
        return True, similarity, "similar_headline"
    return False, similarity, ""


def _story_id(titles: list[str]) -> str:
    normalized = min(" ".join(_tokens(title)) for title in titles)
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"story-{digest}"


def cluster_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    match_details: list[list[tuple[float, str]]] = []

    for article in articles:
        found = None
        match = (0.0, "")
        for index, group in enumerate(groups):
            comparisons = [_same_story(article, existing) for existing in group]
            accepted = [result for result in comparisons if result[0]]
            if accepted:
                found = index
                best = max(accepted, key=lambda result: result[1])
                match = (best[1], best[2])
                break
        if found is None:
            groups.append([article])
            match_details.append([])
        else:
            groups[found].append(article)
            match_details[found].append(match)

    stories = []
    for group, details in zip(groups, match_details):
        latest = max(group, key=lambda item: item.get("published_at", ""))
        in_scope = [item for item in group if item.get("competition") == "Premier League"]
        title_item = max(in_scope or group, key=lambda item: item.get("published_at", ""))
        sources = []
        seen_sources = set()
        for item in group:
            key = (item["source_name"], item["source_url"])
            if key in seen_sources:
                continue
            seen_sources.add(key)
            sources.append({
                "source_name": item["source_name"],
                "publisher": item.get("publisher", item["source_name"]),
                "source_url": item["source_url"],
                "published_at": item["published_at"],
            })

        titles = [item["title"] for item in group]
        stories.append({
            "story_id": _story_id(titles),
            "title": title_item["title"],
            "competition_candidate": "Premier League" if in_scope else "unknown",
            "scope_status": (
                "feed_or_entity_match_needs_confirmation" if in_scope else "needs_scope_review"
            ),
            "latest_published_at": latest["published_at"],
            "source_count": len({source["publisher"] for source in sources}),
            "article_count": len(sources),
            "sources": sources,
            "cluster_method": "url_or_headline_similarity",
            "cluster_similarity": max((value for value, _ in details), default=0.0),
        })

    stories.sort(key=lambda story: story["latest_published_at"], reverse=True)
    return stories


def load_articles(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("File tin đầu vào phải là JSON array.")
    return data


def save_stories(stories: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stories, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
