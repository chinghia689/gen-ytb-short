from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    preferred = FONT_CANDIDATES[1:2] + FONT_CANDIDATES[:1] if bold else FONT_CANDIDATES[2:] + FONT_CANDIDATES[:1]
    for candidate in preferred:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _wrap_pixels(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _credit_block(draw: ImageDraw.ImageDraw, story: dict[str, Any]) -> None:
    image_credit = str(story.get("image_credit", "Ảnh: nguồn tin"))
    music_credit = str(story.get("music_credit", "Nhạc: Wikimedia Commons"))
    font = _font(20)
    for index, line in enumerate((image_credit, music_credit)):
        line = textwrap.shorten(line, width=88, placeholder="…")
        draw.text((68, 1752 + index * 38), line, font=font, fill="#f0f4f6", stroke_width=2, stroke_fill="#102026")


def _make_card(story: dict[str, Any], card_path: Path) -> None:
    source_image = Path(str(story.get("image_path", "")))
    if not source_image.is_file():
        raise ValueError("Agent chưa tải được ảnh có license để dựng video.")
    with Image.open(source_image) as opened:
        image = ImageOps.fit(opened.convert("RGB"), (1080, 1920), method=Image.Resampling.LANCZOS)
    image = image.convert("RGBA")
    shade = Image.new("RGBA", image.size, (4, 14, 18, 80))
    image = Image.alpha_composite(image, shade)
    draw = ImageDraw.Draw(image, "RGBA")

    draw.rounded_rectangle((58, 72, 1022, 190), radius=28, fill=(9, 28, 34, 205), outline=(94, 215, 174, 210), width=2)
    draw.text((94, 106), "TÓM TẮT BÓNG ĐÁ", font=_font(42, True), fill="#8fe2c4")

    lines = story.get("video_lines") or []
    if not isinstance(lines, list):
        lines = []
    lines = [str(line).strip() for line in lines if str(line).strip()][:3]
    if not lines:
        summary = str(story.get("summary_vi", "")).strip()
        lines = [textwrap.shorten(part.strip(), width=64, placeholder="…")
                 for part in summary.replace("?", ".").split(".") if part.strip()][:3]
    if not lines:
        raise ValueError("Agent chưa tạo được các dòng tóm tắt cho video.")

    y = 530 if len(lines) == 3 else 650
    line_font = _font(54, True)
    for line in lines:
        wrapped = _wrap_pixels(draw, line, line_font, 820)[:2]
        height = 180 if len(wrapped) > 1 else 142
        draw.rounded_rectangle((62, y, 1018, y + height), radius=26, fill=(5, 19, 24, 196), outline=(255, 255, 255, 90), width=2)
        text_y = y + (24 if len(wrapped) > 1 else 42)
        for part in wrapped:
            draw.text((104, text_y), part, font=line_font, fill="#ffffff", stroke_width=1, stroke_fill="#102026")
            text_y += 68
        y += height + 30

    draw.rounded_rectangle((52, 1718, 1028, 1870), radius=24, fill=(6, 18, 23, 190))
    _credit_block(draw, story)
    image.convert("RGB").save(card_path, quality=94)


def _write_credits(story: dict[str, Any], path: Path) -> None:
    credits = [
        str(story.get("image_credit", "")),
        f"Tên ảnh: {story.get('image_title', '')}",
        f"Ảnh: {story.get('image_page_url', '')}",
        *([f"Giấy phép ảnh: {story['image_license_url']}"] if story.get("image_license_url") else []),
        str(story.get("image_changes", "")),
        str(story.get("music_credit", "")),
        f"Tên nhạc: {story.get('music_title', '')}",
        f"Nhạc: {story.get('music_page_url', '')}",
        f"Giấy phép nhạc: {story.get('music_license_url', '')}",
    ]
    path.write_text("\n".join(line for line in credits if line.strip()) + "\n", encoding="utf-8")


def render_video(story: dict[str, Any], output_dir: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("Không tìm thấy ffmpeg. Cài FFmpeg rồi khởi động lại FastAPI.")
    image_path = Path(str(story.get("image_path", "")))
    music_path = Path(str(story.get("music_path", "")))
    if not image_path.is_file():
        raise ValueError("Thiếu ảnh có license cho story.")
    if not music_path.is_file():
        raise ValueError("Thiếu nhạc nền có giấy phép cho video.")
    if not story.get("video_lines") and not story.get("summary_vi"):
        raise ValueError("Story chưa có các dòng tóm tắt để hiển thị.")

    output_dir.mkdir(parents=True, exist_ok=True)
    story_id = str(story["story_id"])
    card_path = output_dir / f"{story_id}.png"
    video_path = output_dir / f"{story_id}.mp4"
    credits_path = output_dir / f"{story_id}_credits.txt"
    _make_card(story, card_path)
    _write_credits(story, credits_path)

    command = [
        ffmpeg, "-y", "-loop", "1", "-framerate", "30", "-i", str(card_path),
        "-stream_loop", "-1", "-i", str(music_path), "-map", "0:v:0", "-map", "1:a:0",
        "-vf", "scale=1080:1920,format=yuv420p", "-af", "volume=0.20,afade=t=out:st=18:d=2",
        "-t", "20", "-r", "30", "-c:v", "libx264", "-preset", "medium", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(video_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()[-1600:]
        raise RuntimeError(f"FFmpeg không dựng được video: {detail}") from exc
    return video_path
