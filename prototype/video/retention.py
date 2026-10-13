from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RETENTION = timedelta(hours=2)


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _file_time(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _unlink_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
        if resolved.is_file():
            resolved.unlink()
            return True
    except (OSError, ValueError):
        pass
    return False


def _write_queue(path: Path, queue: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".retention-queue.", suffix=".tmp", delete=False
        ) as temp:
            json.dump(queue, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp_path = Path(temp.name)
        os.replace(temp_path, path)
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def cleanup_expired_outputs(queue_path: Path, output_dir: Path, *, now: datetime | None = None) -> dict[str, int]:
    """Remove rendered videos and downloaded media older than two hours."""
    current = now or datetime.now(timezone.utc)
    cutoff = current - RETENTION
    root = output_dir.resolve()
    media_dir = root / "media"
    counts = {"videos": 0, "files": 0, "queue_entries": 0}
    queue: dict[str, Any] = {"stories": []}
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    stories = queue.get("stories", [])
    active_paths: set[Path] = set()
    queue_changed = False

    for story in stories:
        raw_video = story.get("video_path")
        video = Path(str(raw_video)) if raw_video else None
        created = _timestamp(story.get("video_created_at")) or (_file_time(video) if video else None)
        if not video or not created:
            continue
        if created > cutoff and video.is_file():
            active_paths.add(video.resolve())
            for key in ("image_path", "music_path"):
                raw_media = story.get(key)
                if raw_media:
                    media_path = Path(str(raw_media))
                    if media_path.is_file():
                        active_paths.add(media_path.resolve())
            continue

        removed = _unlink_inside(video, root)
        if removed:
            counts["videos"] += 1
            counts["files"] += 1
        story_id = str(story.get("story_id", ""))
        related = [
            root / f"{story_id}.png",
            root / f"{story_id}_credits.txt",
            media_dir / f"{story_id}_image.json",
        ]
        related.extend(media_dir.glob(f"{story_id}_image.*"))
        for path in related:
            if _unlink_inside(path, root):
                counts["files"] += 1
        for key in ("video_path", "video_url", "credits_url", "video_created_at", "image_path", "music_path"):
            story.pop(key, None)
        story["video_expired_at"] = current.isoformat()
        story["agent_status"] = "expired"
        story["agent_step"] = "expired"
        queue_changed = True
        counts["queue_entries"] += 1

    for path in root.rglob("*"):
        if not path.is_file() or path.resolve() in active_paths:
            continue
        if path.name in {"ncs_music_catalog.json", "background_music_selection.json"}:
            continue
        modified = _file_time(path)
        if modified and modified <= cutoff and _unlink_inside(path, root):
            counts["files"] += 1

    for story in stories:
        for key in ("image_path", "music_path"):
            raw_media = story.get(key)
            if raw_media and not Path(str(raw_media)).is_file():
                story.pop(key, None)
                queue_changed = True

    if queue_changed and queue_path.exists():
        _write_queue(queue_path, queue)
    return counts
