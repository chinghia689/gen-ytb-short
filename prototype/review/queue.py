from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REVIEW_STATUSES = {"unreviewed", "supported", "uncertain", "rejected"}


def create_review_queue(stories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "instructions": (
            "Mỗi story: xác nhận thuộc Ngoại hạng Anh, tự viết summary_vi dựa trên "
            "các nguồn, rồi chọn supported/uncertain/rejected. Chỉ dùng supported hoặc "
            "uncertain khi competition_confirmed=true và đã kiểm tra nguồn."
        ),
        "stories": [
            {
                "story_id": story["story_id"],
                "title": story["title"],
                "competition_candidate": story["competition_candidate"],
                "scope_status": story["scope_status"],
                "competition_confirmed": False,
                "review_status": "unreviewed",
                "summary_vi": "",
                "evidence_notes": "",
                "sources": story["sources"],
                "source_count": story["source_count"],
                "article_count": story["article_count"],
                "cluster_method": story["cluster_method"],
                "cluster_similarity": story["cluster_similarity"],
            }
            for story in stories
        ],
    }


def save_review_queue(queue: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_approved_stories(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    queue = json.loads(path.read_text(encoding="utf-8"))
    stories = queue.get("stories", [])
    approved = []
    skipped = []

    for story in stories:
        status = story.get("review_status")
        if status not in REVIEW_STATUSES:
            skipped.append(f"{story.get('story_id')}: trạng thái không hợp lệ")
            continue
        if status == "rejected":
            continue
        if status != "supported" and status != "uncertain":
            skipped.append(f"{story.get('story_id')}: chưa duyệt")
            continue
        if story.get("competition_confirmed") is not True:
            skipped.append(f"{story.get('story_id')}: chưa xác nhận Ngoại hạng Anh")
            continue
        if not story.get("summary_vi", "").strip():
            skipped.append(f"{story.get('story_id')}: thiếu summary_vi")
            continue
        if not story.get("sources"):
            skipped.append(f"{story.get('story_id')}: không có nguồn")
            continue
        approved.append(story)
    return approved, skipped
