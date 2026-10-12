from __future__ import annotations

from pathlib import Path
from typing import Any


def render_script(story: dict[str, Any], index: int) -> str:
    title = story["title"].strip().rstrip(".!?")
    summary = story["summary_vi"].strip()
    status = story["review_status"]
    video_lines = story.get("video_lines") or [part.strip() for part in summary.split(".") if part.strip()][:3]
    overlay = "\n".join(f"- {line}" for line in video_lines)

    sources = "\n".join(
        f"- [{source['source_name']}]({source['source_url']})"
        for source in story["sources"]
    )
    return (
        f"## Bản tin {index}: {title}\n\n"
        f"**Trạng thái biên tập:** {status}  "
        f"**Nguồn đối chiếu:** {story['source_count']}\n\n"
        f"### Chữ trên video\n\n{overlay or summary}\n\n"
        f"### Ghi chú xác minh\n\n"
        f"{story.get('evidence_notes', '').strip() or 'Chưa có ghi chú.'}\n\n"
        f"### Nguồn\n\n{sources}\n"
    )


def render_drafts(stories: list[dict[str, Any]], output_path: Path) -> None:
    if stories:
        content = (
            "# Bản nháp bản tin Ngoại hạng Anh\n\n"
            "> Chỉ gồm story đã được duyệt. Kiểm tra lại chữ trên video, ảnh/nhạc được chọn và nguồn trước khi phát hành.\n\n"
            + "\n---\n\n".join(
                render_script(story, index)
                for index, story in enumerate(stories, start=1)
            )
        )
    else:
        content = (
            "# Chưa có bản nháp\n\n"
            "Hãy xác nhận phạm vi giải đấu, điền summary_vi và duyệt story trước."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content + "\n", encoding="utf-8")
