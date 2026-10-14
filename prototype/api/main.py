from __future__ import annotations

import asyncio
import json
import html
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from dotenv import load_dotenv

from prototype.agent.newsroom import run_newsroom_agent
from prototype.agent.media import acquire_story_media
from prototype.content.ai_writer import (
    review_video_text,
    translate_video_lines_en,
    validate_video_lines,
    write_video_lines,
)
from prototype.publishing import youtube as youtube_publishing
from prototype.review.queue import load_approved_stories
from prototype.scripts.drafts import render_drafts
from prototype.video.render import render_video
from prototype.video.retention import cleanup_expired_outputs


PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROTOTYPE_DIR.parent / ".env")
QUEUE_PATH = PROTOTYPE_DIR / "data" / "review_queue.json"
DRAFT_PATH = PROTOTYPE_DIR / "output" / "drafts.md"
GENERATED_DIR = PROTOTYPE_DIR / "output" / "generated"
VIDEO_DIR = GENERATED_DIR / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Premier League News Review API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "PATCH", "POST", "DELETE"],
    allow_headers=["*"],
)
_retention_task: asyncio.Task | None = None


async def _retention_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_expired_outputs, QUEUE_PATH, VIDEO_DIR)
        except Exception:
            logging.exception("Không dọn được media quá hạn.")
        # Keep expiry close to the two-hour limit while the UI/API is running.
        await asyncio.sleep(60)


@app.on_event("startup")
async def start_retention_loop() -> None:
    global _retention_task
    _retention_task = asyncio.create_task(_retention_loop())


@app.on_event("shutdown")
async def stop_retention_loop() -> None:
    if _retention_task:
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass


class StoryReviewUpdate(BaseModel):
    competition_confirmed: bool
    review_status: Literal["unreviewed", "supported", "uncertain", "rejected"]
    summary_vi: str = Field(default="", max_length=3000)
    evidence_notes: str = Field(default="", max_length=3000)


class ScriptReviewUpdate(BaseModel):
    script_text: str = Field(min_length=3, max_length=300)
    editor_approved: bool = False


class AgentRunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=10)


def read_queue() -> dict:
    if not QUEUE_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="Chưa có review_queue.json. Hãy chạy python -m prototype fetch trước.",
        )
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Không đọc được hàng đợi: {exc}") from exc


def write_queue(queue: dict) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=QUEUE_PATH.parent,
            prefix=".review_queue.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp.write(json.dumps(queue, ensure_ascii=False, indent=2) + "\n")
            temp_name = temp.name
        os.replace(temp_name, QUEUE_PATH)
    except OSError as exc:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
        raise HTTPException(status_code=500, detail=f"Không lưu được quyết định duyệt: {exc}") from exc


def _counts(stories: list[dict]):
    return {
        "all": len(stories),
        "unreviewed": sum(s.get("review_status") == "unreviewed" for s in stories),
        "supported": sum(s.get("review_status") == "supported" for s in stories),
        "uncertain": sum(s.get("review_status") == "uncertain" for s in stories),
        "rejected": sum(s.get("review_status") == "rejected" for s in stories),
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/agent/run")
def run_agent(update: AgentRunRequest):
    try:
        return run_newsroom_agent(limit=update.limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Newsroom agent gặp lỗi: {exc}") from exc


@app.get("/api/youtube/status")
def youtube_status():
    try:
        return youtube_publishing.status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không kiểm tra được kết nối YouTube: {exc}") from exc


@app.get("/api/youtube/oauth/start")
def youtube_oauth_start():
    if youtube_publishing.oauth_mode() == "desktop":
        try:
            youtube_publishing.authorize_desktop()
        except Exception as exc:
            detail = html.escape(str(exc))[:500]
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'><h2>Kết nối YouTube thất bại</h2>"
                f"<p>{detail}</p><script>if(window.opener)window.opener.postMessage(" 
                "{type:'youtube-oauth-error'},'*');setTimeout(()=>window.close(),1200);</script>",
                status_code=400,
            )
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><h2>Đã kết nối YouTube</h2>"
            "<p>Có thể đóng cửa sổ này.</p><script>if(window.opener)window.opener.postMessage(" 
            "{type:'youtube-connected'},'*');window.close();</script>"
        )
    try:
        return RedirectResponse(youtube_publishing.start_authorization())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không bắt đầu được OAuth YouTube: {exc}") from exc


@app.get("/api/youtube/oauth/callback", response_class=HTMLResponse)
def youtube_oauth_callback(request: FastAPIRequest, code: str | None = None,
                           state: str | None = None, error: str | None = None):
    if error:
        return HTMLResponse(
            "<h2>Không kết nối được YouTube</h2><p>Đóng cửa sổ này và thử kết nối lại.</p>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse("<h2>Thiếu thông tin OAuth.</h2>", status_code=400)
    try:
        youtube_publishing.finish_authorization(str(request.url), state)
    except Exception as exc:
        detail = str(exc).replace("<", "&lt;").replace(">", "&gt;")[:500]
        return HTMLResponse(
            f"<h2>Kết nối YouTube thất bại</h2><p>{detail}</p><p>Đóng cửa sổ này và thử lại.</p>",
            status_code=400,
        )
    return HTMLResponse(
        "<!doctype html><html><meta charset='utf-8'><title>YouTube đã kết nối</title>"
        "<body><h2>YouTube đã kết nối</h2><p>Có thể đóng cửa sổ này.</p>"
        "<script>if(window.opener)window.opener.postMessage({type:'youtube-connected'},'*');window.close();</script>"
        "</body></html>"
    )


@app.get("/api/stories")
def list_stories(
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    cleanup_expired_outputs(QUEUE_PATH, VIDEO_DIR)
    queue = read_queue()
    all_stories = queue.get("stories", [])
    stories = all_stories
    if status and status != "all":
        stories = [story for story in stories if story.get("review_status") == status]
    if search:
        needle = search.casefold().strip()
        stories = [
            story for story in stories
            if needle in story.get("title", "").casefold()
            or any(
                needle in source.get("source_name", "").casefold()
                for source in story.get("sources", [])
            )
        ]
    return {"stories": stories, "counts": _counts(all_stories)}


@app.get("/api/videos/archive")
def list_video_archive():
    """List rendered videos that are still present on disk."""
    cleanup_expired_outputs(QUEUE_PATH, VIDEO_DIR)
    queue = read_queue()
    videos = []
    for story in queue.get("stories", []):
        raw_path = story.get("video_path")
        if not raw_path:
            continue
        path = Path(str(raw_path)).resolve()
        try:
            path.relative_to(VIDEO_DIR.resolve())
        except ValueError:
            continue
        if path.is_file() and path.suffix.lower() == ".mp4":
            videos.append(story)
    videos.sort(key=lambda story: story.get("video_created_at", ""), reverse=True)
    return {"videos": videos, "count": len(videos)}


@app.delete("/api/stories/{story_id}/video")
def delete_story_video(story_id: str):
    """Delete local rendered files while keeping the editorial story record."""
    queue = read_queue()
    story = _find_story(queue, story_id)
    raw_path = story.get("video_path")
    if not raw_path:
        raise HTTPException(status_code=404, detail="Story này không có video lưu trên máy.")
    video_path = Path(str(raw_path)).resolve()
    try:
        video_path.relative_to(VIDEO_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Đường dẫn video nằm ngoài thư mục lưu trữ.") from exc
    if video_path.suffix.lower() != ".mp4":
        raise HTTPException(status_code=422, detail="Chỉ có thể xóa file MP4 đã render.")
    media_dir = VIDEO_DIR / "media"
    related_paths = [
        video_path,
        VIDEO_DIR / f"{story_id}.png",
        VIDEO_DIR / f"{story_id}_credits.txt",
        media_dir / f"{story_id}_image.json",
        *media_dir.glob(f"{story_id}_image.*"),
    ]
    raw_image_path = story.get("image_path")
    if raw_image_path:
        related_paths.append(Path(str(raw_image_path)))

    removed = []
    seen_paths: set[Path] = set()
    for path in related_paths:
        try:
            resolved = path.resolve()
            resolved.relative_to(VIDEO_DIR.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            if resolved.is_file():
                resolved.unlink()
                removed.append(resolved.name)
        except (OSError, ValueError):
            continue
    for key in (
        "video_path", "video_url", "credits_url", "video_created_at",
        "image_path", "image_credit", "image_license_url", "image_page_url",
        "image_title", "image_changes",
    ):
        story.pop(key, None)
    story["video_deleted_at"] = datetime.now(timezone.utc).isoformat()
    story["agent_status"] = "video_deleted"
    story["agent_step"] = "video_deleted"
    write_queue(queue)
    return {"deleted": removed, "story_id": story_id}


@app.patch("/api/stories/{story_id}")
def update_story(story_id: str, update: StoryReviewUpdate):
    if update.review_status in {"supported", "uncertain"}:
        if not update.competition_confirmed:
            raise HTTPException(status_code=422, detail="Hãy xác nhận story thuộc Ngoại hạng Anh.")
        if not update.summary_vi.strip():
            raise HTTPException(status_code=422, detail="Cần nhập tóm tắt tiếng Việt trước khi duyệt.")
        if not update.evidence_notes.strip():
            raise HTTPException(status_code=422, detail="Cần ghi căn cứ đã đối chiếu để AI rà soát nội dung video.")

    queue = read_queue()
    target = next(
        (story for story in queue.get("stories", []) if story.get("story_id") == story_id),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy story.")

    target.update({
        "competition_confirmed": update.competition_confirmed,
        "review_status": update.review_status,
        "summary_vi": update.summary_vi.strip(),
        "evidence_notes": update.evidence_notes.strip(),
        "last_reviewed_at": datetime.now(timezone.utc).isoformat(),
    })
    target["generated_script"] = ""
    target["video_lines"] = []
    target.pop("ai_review_status", None)
    target.pop("ai_review_notes", None)
    target.pop("script_generated_at", None)
    target["script_approved"] = False
    target.pop("script_approval_method", None)
    target.pop("script_approved_at", None)
    target.pop("script_reviewed_at", None)
    target.pop("video_path", None)
    target.pop("video_url", None)
    target.pop("subtitle_url", None)
    target.pop("video_created_at", None)
    target.pop("credits_url", None)
    for key in ("image_path", "music_path", "image_credit", "image_license_url", "image_page_url",
                "image_title", "image_changes", "music_provider", "music_credit", "music_attribution",
                "music_license_url", "music_page_url", "music_title"):
        target.pop(key, None)
    write_queue(queue)
    return {"story": target, "counts": _counts(queue.get("stories", []))}


def _find_story(queue: dict, story_id: str) -> dict:
    story = next(
        (item for item in queue.get("stories", []) if item.get("story_id") == story_id),
        None,
    )
    if story is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy story.")
    return story


def _require_editor_approved(story: dict) -> None:
    if story.get("review_status") not in {"supported", "uncertain"}:
        raise HTTPException(status_code=422, detail="Story cần được duyệt trước khi tạo nội dung.")
    if story.get("competition_confirmed") is not True or not story.get("summary_vi", "").strip():
        raise HTTPException(status_code=422, detail="Cần xác nhận Ngoại hạng Anh và lưu tóm tắt trước.")
    if not story.get("evidence_notes", "").strip():
        raise HTTPException(status_code=422, detail="Cần lưu ghi chú căn cứ đã đối chiếu trước khi tạo chữ trên video.")
    if not story.get("sources"):
        raise HTTPException(status_code=422, detail="Story chưa có nguồn tham khảo.")


@app.post("/api/stories/{story_id}/generate-script")
def generate_story_script(story_id: str):
    queue = read_queue()
    story = _find_story(queue, story_id)
    _require_editor_approved(story)
    try:
        lines = write_video_lines(story)
        script = "\n".join(lines)
        issues = validate_video_lines(lines, story.get("review_status", ""))
        ai_review = (
            {"status": "needs_review", "notes": " ".join(issues)}
            if issues else review_video_text(story, script)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Không gọi được dịch vụ AI: {exc}") from exc
    approved = ai_review["status"] == "pass"
    now = datetime.now(timezone.utc).isoformat()
    story.update({
        "video_lines": lines,
        "generated_script": script,
        "ai_review_status": ai_review["status"],
        "ai_review_notes": ai_review["notes"],
        "script_approved": approved,
        "script_approval_method": "llm" if approved else None,
        "script_generated_at": now,
        "agent_step": "script_review",
    })
    for key in ("video_path", "video_url", "video_created_at", "credits_url"):
        story.pop(key, None)
    if approved:
        story["script_approved_at"] = now
    else:
        story.pop("script_approved_at", None)
    write_queue(queue)
    return {"story": story}


@app.patch("/api/stories/{story_id}/script")
def review_story_script(story_id: str, update: ScriptReviewUpdate):
    queue = read_queue()
    story = _find_story(queue, story_id)
    _require_editor_approved(story)
    if not story.get("generated_script"):
        raise HTTPException(status_code=422, detail="Hãy tạo ba dòng chữ trước khi gửi AI rà soát.")
    script = update.script_text.strip()
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    if len(lines) != 3 or any(len(line) > 48 for line in lines):
        raise HTTPException(status_code=422, detail="Cần đúng 3 dòng chữ, mỗi dòng tối đa 48 ký tự.")
    if update.editor_approved:
        ai_review = {"status": "editor_approved", "notes": "Người biên tập đã xác nhận ba dòng chữ."}
        approved = True
        approval_method = "editor"
    else:
        issues = validate_video_lines(lines, story.get("review_status", ""))
        if issues:
            ai_review = {"status": "needs_review", "notes": " ".join(issues)}
        else:
            try:
                ai_review = review_video_text(story, script)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Không gọi được dịch vụ AI: {exc}") from exc
        approved = ai_review["status"] == "pass"
        approval_method = "llm" if approved else None
    now = datetime.now(timezone.utc).isoformat()
    story.update({
        "video_lines": lines,
        "generated_script": "\n".join(lines),
        "ai_review_status": ai_review["status"],
        "ai_review_notes": ai_review["notes"],
        "script_approved": approved,
        "script_approval_method": approval_method,
        "script_reviewed_at": now,
        "agent_step": "script_review",
    })
    if approved:
        story["script_approved_at"] = now
    else:
        story.pop("script_approved_at", None)
    story.pop("video_path", None)
    story.pop("video_url", None)
    story.pop("subtitle_url", None)
    story.pop("video_created_at", None)
    story.pop("credits_url", None)
    write_queue(queue)
    return {"story": story}


@app.post("/api/stories/{story_id}/render-video")
def create_story_video(story_id: str):
    queue = read_queue()
    story = _find_story(queue, story_id)
    _require_editor_approved(story)
    if not story.get("script_approved"):
        raise HTTPException(status_code=422, detail="AI chưa duyệt chữ trên video; hãy gửi rà soát trước khi dựng.")
    lines = story.get("video_lines")
    if (not isinstance(lines, list) or len(lines) != 3
            or any(not isinstance(line, str) or not line.strip() or len(line) > 48 for line in lines)
            or story.get("generated_script", "").strip() != "\n".join(lines).strip()):
        raise HTTPException(status_code=422, detail="Story chưa có ba dòng chữ đã được AI rà soát; hãy tạo và rà soát lại.")
    try:
        def set_media_step(step: str) -> None:
            story["agent_step"] = step

        story["video_lines_en"] = translate_video_lines_en(story, lines)
        story.update(acquire_story_media(story, VIDEO_DIR, on_step=set_media_step))
        story["agent_step"] = "video_render"
        video_path = render_video(story, VIDEO_DIR)
    except ValueError as exc:
        story["agent_status"] = "error"
        story["agent_error"] = str(exc)[:1000]
        write_queue(queue)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        story["agent_status"] = "error"
        story["agent_step"] = story.get("agent_step") or "video_render"
        story["agent_error"] = str(exc)[:1000]
        write_queue(queue)
        raise HTTPException(status_code=502, detail=f"Không tạo được video: {exc}") from exc
    story["video_path"] = str(video_path)
    story["video_url"] = f"/api/videos/{video_path.name}"
    story.pop("subtitle_url", None)
    story["credits_url"] = f"/api/videos/{story['story_id']}_credits.txt"
    story["video_created_at"] = datetime.now(timezone.utc).isoformat()
    story["agent_status"] = "complete"
    story["agent_step"] = "complete"
    story.pop("agent_error", None)
    write_queue(queue)
    return {"story": story}


@app.post("/api/stories/{story_id}/youtube-upload")
def publish_story_to_youtube(story_id: str):
    queue = read_queue()
    story = _find_story(queue, story_id)
    _require_editor_approved(story)
    lines = story.get("video_lines")
    if (not story.get("script_approved") or not isinstance(lines, list) or len(lines) != 3
            or story.get("generated_script", "").strip() != "\n".join(lines).strip()):
        raise HTTPException(status_code=422, detail="Cần có ba dòng chữ đã được AI rà soát trước khi đăng.")
    if story.get("youtube_video_id"):
        raise HTTPException(status_code=409, detail="Video này đã được đăng lên YouTube.")
    raw_video_path = story.get("video_path")
    if not raw_video_path:
        raise HTTPException(status_code=422, detail="Hãy render và xem video trước khi đăng.")
    video_path = Path(str(raw_video_path)).resolve()
    try:
        video_path.relative_to(VIDEO_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Đường dẫn video nằm ngoài thư mục MP4 của prototype.") from exc
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise HTTPException(status_code=422, detail="Không tìm thấy file MP4 hợp lệ để đăng.")
    try:
        result = youtube_publishing.upload_video(video_path, story)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        story["youtube_upload_error"] = str(exc)[:1200]
        write_queue(queue)
        raise HTTPException(status_code=502, detail=f"YouTube chưa nhận được video: {exc}") from exc
    story.update({
        "youtube_video_id": result["video_id"],
        "youtube_url": result["url"],
        "youtube_title": result["title"],
        "youtube_privacy_status": result["privacy_status"],
        "youtube_uploaded_at": datetime.now(timezone.utc).isoformat(),
        "youtube_upload_error": "",
    })
    write_queue(queue)
    return {"story": story}


@app.post("/api/drafts")
def create_drafts():
    queue = read_queue()
    approved, skipped = load_approved_stories(QUEUE_PATH)
    render_drafts(approved, DRAFT_PATH)
    return {
        "created": len(approved),
        "skipped": len(skipped),
        "output_path": str(DRAFT_PATH),
        "content": DRAFT_PATH.read_text(encoding="utf-8"),
    }


@app.get("/api/drafts")
def get_drafts():
    if not DRAFT_PATH.exists():
        return {"content": "", "output_path": str(DRAFT_PATH)}
    return {
        "content": DRAFT_PATH.read_text(encoding="utf-8"),
        "output_path": str(DRAFT_PATH),
    }


# Register the static file mount after API routes so `/api/videos/archive`
# resolves to the JSON endpoint instead of being treated as a filename.
app.mount("/api/videos", StaticFiles(directory=VIDEO_DIR), name="generated-videos")
