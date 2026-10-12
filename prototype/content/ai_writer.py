from __future__ import annotations

import json
import os
from typing import Any

import httpx


def _ollama_chat(story: dict[str, Any], instruction: str, *, json_output: bool = False) -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("QWEN_MODEL", "qwen3:8b").strip()
    if not model:
        raise ValueError("Thiếu QWEN_MODEL trong .env.")
    input_data = {
        "title_metadata": story.get("title", ""),
        "editor_summary_vi": story.get("summary_vi", ""),
        "editor_evidence_notes": story.get("evidence_notes", ""),
        "editor_status": story.get("review_status", ""),
        "competition_confirmed": story.get("competition_confirmed") is True,
        "script_to_review": story.get("script_to_review", ""),
        "script_revision_notes": story.get("script_revision_notes", ""),
        "video_lines": story.get("video_lines", []),
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4},
    }
    if json_output:
        payload["format"] = "json"
    try:
        response = httpx.post(f"{base_url}/api/chat", json=payload, timeout=240)
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError as exc:
        raise ValueError(
            "Không kết nối được Ollama. Hãy mở Ollama và tải model bằng `ollama pull qwen3:8b`."
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise ValueError(f"Ollama báo lỗi ({exc.response.status_code}): {detail}") from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Lỗi khi gọi Ollama: {exc}") from exc
    content = result.get("message", {}).get("content", "").strip()
    if not content:
        raise ValueError("Qwen3 không trả về nội dung.")
    return content


def write_video_lines(story: dict[str, Any], revision_notes: str = "") -> list[str]:
    if not story.get("summary_vi", "").strip():
        raise ValueError("Thiếu tóm tắt để tạo chữ trên video.")
    instruction = (
        "Tạo đúng 3 dòng tóm tắt tiếng Việt ngắn để hiển thị trên một ảnh dọc. "
        "Mỗi dòng tối đa 48 ký tự, mỗi dòng nêu một ý khác nhau. Chỉ dùng dữ kiện trong summary và evidence notes; "
        "không giật tít, không thêm thông tin. Nếu editor_status là uncertain, diễn đạt mọi chi tiết như thông tin từ nguồn "
        "và ít nhất một dòng phải nói rõ tin chưa được xác nhận. "
        "Trả JSON đúng dạng {\"video_lines\":[\"...\",\"...\",\"...\"]}."
    )
    if revision_notes.strip():
        instruction += " Sửa theo các ghi chú rà soát sau, không thêm dữ kiện: " + revision_notes[:800]
    raw = _ollama_chat(story, instruction, json_output=True)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Qwen không trả JSON hợp lệ cho các dòng video.") from exc
    lines = parsed.get("video_lines") if isinstance(parsed, dict) else None
    if not isinstance(lines, list):
        raise ValueError("Qwen không trả danh sách video_lines.")
    lines = [str(line).strip()[:48] for line in lines if str(line).strip()][:3]
    if len(lines) != 3:
        raise ValueError("Qwen cần tạo đúng 3 dòng tóm tắt video.")
    return lines


def review_video_text(story: dict[str, Any], video_text: str) -> dict[str, str]:
    review_story = {**story, "script_to_review": video_text}
    instruction = (
        "Bạn là lượt rà soát biên tập. JSON đầu vào có tóm tắt và ba dòng chữ đặt trên video. "
        "Đối chiếu từng dòng chữ với phần tóm tắt/ghi chú do quy trình newsroom cung cấp; không truy cập internet "
        "và không dùng tiêu đề RSS làm bằng chứng. Đánh dấu needs_review nếu có chi tiết không căn cứ, "
        "mâu thuẫn hoặc khẳng định quá chắc với trạng thái uncertain. Chỉ được nêu lỗi nếu cụm chữ đó thực sự có trong "
        "script_to_review; khi báo lỗi phải trích nguyên văn một dòng hiện có. Nếu cụm bị cho là lỗi không xuất hiện "
        "trong ba dòng hiện tại thì không tính là lỗi. "
        'Chỉ trả JSON đúng schema: {"status":"pass" hoặc "needs_review", "notes":"lý do ngắn tiếng Việt"}. '
        "pass chỉ có nghĩa là không phát hiện lỗi rõ trong văn bản, không xác nhận sự thật."
    )
    raw = _ollama_chat(review_story, instruction, json_output=True)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "needs_review", "notes": "Không đọc được JSON rà soát; cần tự kiểm tra."}
    status = result.get("status")
    if status not in {"pass", "needs_review"}:
        status = "needs_review"
    return {"status": status, "notes": str(result.get("notes", "")).strip()[:1200]}
