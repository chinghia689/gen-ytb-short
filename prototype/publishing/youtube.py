from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_PATH = Path(__file__).resolve().parents[1] / "data" / "youtube_oauth_token.json"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

_pending_flow: Flow | None = None
_pending_state: str | None = None


def _client_configured() -> bool:
    return bool(os.getenv("YOUTUBE_CLIENT_ID", "").strip() and os.getenv("YOUTUBE_CLIENT_SECRET", "").strip())


def _redirect_uri() -> str:
    return os.getenv(
        "YOUTUBE_REDIRECT_URI",
        "http://127.0.0.1:8000/api/youtube/oauth/callback",
    ).strip()


def oauth_mode() -> str:
    mode = os.getenv("YOUTUBE_OAUTH_FLOW", "desktop").strip().casefold()
    return mode if mode in {"desktop", "web"} else "desktop"


def status() -> dict[str, Any]:
    configured = _client_configured()
    connection_error = ""
    try:
        credentials = load_credentials() if configured else None
    except Exception as exc:
        credentials = None
        connection_error = str(exc)[:500]
    return {
        "configured": configured,
        "connected": bool(credentials and credentials.valid),
        "oauth_flow": oauth_mode(),
        "redirect_uri": _redirect_uri() if oauth_mode() == "web" else "",
        "connection_error": connection_error,
    }


def load_credentials() -> Credentials | None:
    if not _client_configured():
        return None
    credentials: Credentials | None = None
    if TOKEN_PATH.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), [YOUTUBE_UPLOAD_SCOPE])
        except (OSError, ValueError):
            credentials = None
    if not credentials or not credentials.refresh_token:
        refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip()
        if refresh_token:
            credentials = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=GOOGLE_TOKEN_URI,
                client_id=os.getenv("YOUTUBE_CLIENT_ID", "").strip(),
                client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", "").strip(),
                scopes=[YOUTUBE_UPLOAD_SCOPE],
            )
    if credentials and not credentials.valid and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(credentials)
    return credentials if credentials and credentials.valid else None


def start_authorization() -> str:
    global _pending_flow, _pending_state
    if oauth_mode() != "web":
        raise ValueError("Desktop OAuth sử dụng callback localhost tự chọn; gọi authorize_desktop().")
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("Thiếu YOUTUBE_CLIENT_ID hoặc YOUTUBE_CLIENT_SECRET trong .env.")
    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [_redirect_uri()],
        }
    }
    _pending_flow = Flow.from_client_config(config, scopes=[YOUTUBE_UPLOAD_SCOPE])
    _pending_flow.redirect_uri = _redirect_uri()
    authorization_url, _pending_state = _pending_flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return authorization_url


def authorize_desktop() -> None:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("Thiếu YOUTUBE_CLIENT_ID hoặc YOUTUBE_CLIENT_SECRET trong .env.")
    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
        }
    }
    flow = InstalledAppFlow.from_client_config(config, scopes=[YOUTUBE_UPLOAD_SCOPE])
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        access_type="offline",
        prompt="consent",
    )
    save_credentials(credentials)


def finish_authorization(authorization_response: str, state: str) -> None:
    global _pending_flow, _pending_state
    if not _pending_flow or not _pending_state or not secrets.compare_digest(state, _pending_state):
        raise ValueError("Yêu cầu OAuth không khớp hoặc đã hết hạn. Hãy kết nối lại YouTube.")
    flow = _pending_flow
    _pending_flow = None
    _pending_state = None
    flow.fetch_token(authorization_response=authorization_response)
    save_credentials(flow.credentials)


def save_credentials(credentials: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=TOKEN_PATH.parent, prefix=".youtube-token.", suffix=".tmp", delete=False
    ) as temp:
        temp.write(credentials.to_json())
        temp_path = Path(temp.name)
    os.chmod(temp_path, 0o600)
    temp_path.replace(TOKEN_PATH)


def upload_video(video_path: Path, story: dict[str, Any]) -> dict[str, str]:
    credentials = load_credentials()
    if not credentials:
        raise ValueError("Chưa kết nối YouTube. Hãy kết nối tài khoản trước khi đăng.")
    if not video_path.is_file():
        raise ValueError("Không tìm thấy file MP4 cần đăng.")

    lines = story.get("video_lines") or []
    title = str(lines[0] if lines else story.get("title", "Bản tin bóng đá"))[:100]
    summary = str(story.get("summary_vi", "")).strip()
    sources = [
        f"- {source.get('publisher') or source.get('source_name')}: {source.get('source_url')}"
        for source in story.get("sources", [])
    ]
    credits = [
        str(story.get("image_credit", "")),
        f"Nguồn ảnh: {story.get('image_page_url', '')}",
        *([f"Giấy phép ảnh: {story['image_license_url']}"] if story.get("image_license_url") else []),
        str(story.get("image_changes", "")),
        str(story.get("music_credit", "")),
        f"Nguồn nhạc: {story.get('music_page_url', '')}",
        f"Giấy phép nhạc: {story.get('music_license_url', '')}",
    ]
    sections = [summary]
    if story.get("review_status") == "uncertain":
        sections.insert(0, "Lưu ý: thông tin trong video chưa được xác nhận đầy đủ.")
    if sources:
        sections.append("Nguồn tham khảo:\n" + "\n".join(sources))
    if any(credits):
        sections.append("Tư liệu:\n" + "\n".join(item for item in credits if item))
    sections.append("#Shorts #BóngĐá #PremierLeague")
    description = "\n\n".join(part for part in sections if part).strip()[:5000]

    service = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    request = service.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": ["Shorts", "Bóng đá", "Premier League"],
                "categoryId": "17",
                "defaultLanguage": "vi",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        },
        media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True),
    )
    response = None
    while response is None:
        _progress, response = request.next_chunk()
    video_id = str(response.get("id", ""))
    if not video_id:
        raise RuntimeError("YouTube không trả về video ID sau khi upload.")
    privacy_status = str((response.get("status") or {}).get("privacyStatus", "unknown"))
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/shorts/{video_id}",
        "privacy_status": privacy_status,
        "title": title,
    }
