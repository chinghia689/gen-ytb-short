from __future__ import annotations

import html
import hashlib
import json
import os
import random
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urljoin, urlsplit

import httpx
from PIL import Image


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MAX_MEDIA_BYTES = 30_000_000
SOURCE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "sources.json"


class _ArticleImageParser(HTMLParser):
    """Collect publisher-declared lead images, preferring social preview metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                self.candidates.append(values.get("content", ""))
        elif tag.casefold() == "img":
            self.candidates.append(values.get("src") or values.get("data-src") or "")


def _source_hosts() -> tuple[set[str], set[str]]:
    try:
        sources = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8")).get("sources", [])
    except (OSError, json.JSONDecodeError):
        sources = []
    article_hosts: set[str] = set()
    image_hosts: set[str] = set()
    for source in sources:
        if not source.get("enabled"):
            continue
        article_hosts.update(str(host).casefold().rstrip(".") for host in source.get("article_hosts", []))
        image_hosts.update(str(host).casefold().rstrip(".") for host in source.get("image_hosts", []))
    return article_hosts, article_hosts | image_hosts


def _https_url(url: str, allowed_hosts: set[str]) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme.casefold() != "https" or host not in allowed_hosts:
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
        return parsed.geturl()
    except ValueError:
        return None


def _publisher_image(story: dict[str, Any]) -> dict[str, Any]:
    article_hosts, media_hosts = _source_hosts()
    headers = {"User-Agent": "YtbShortNewsPrototype/0.3 (publisher image reader)", "Accept": "text/html,application/xhtml+xml"}
    for source in story.get("sources", []):
        article_url = _https_url(str(source.get("source_url", "")), article_hosts)
        if not article_url:
            continue
        try:
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(18.0, connect=8.0)) as client:
                current_url = article_url
                response = None
                for attempt in range(6):
                    response = client.get(current_url, headers=headers)
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        response.raise_for_status()
                        break
                    location = response.headers.get("location", "")
                    current_url = _https_url(urljoin(current_url, location), article_hosts) or ""
                    if not current_url or attempt == 5:
                        response = None
                        break
                if response is None or "html" not in response.headers.get("content-type", "").casefold():
                    continue
                parser = _ArticleImageParser()
                parser.feed(response.text[:2_000_000])
                for candidate in parser.candidates:
                    image_url = _https_url(urljoin(current_url, candidate), media_hosts)
                    if not image_url:
                        continue
                    mime = "image/jpeg"
                    try:
                        with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(25.0, connect=8.0)) as client:
                            image_response = client.get(image_url, headers={"User-Agent": headers["User-Agent"]})
                            if image_response.status_code in {301, 302, 303, 307, 308}:
                                redirected = _https_url(urljoin(image_url, image_response.headers.get("location", "")), media_hosts)
                                if not redirected:
                                    continue
                                image_response = client.get(redirected, headers={"User-Agent": headers["User-Agent"]})
                            image_response.raise_for_status()
                            mime = image_response.headers.get("content-type", "").split(";", 1)[0].casefold()
                            if mime not in {"image/jpeg", "image/png", "image/webp"} or len(image_response.content) > MAX_MEDIA_BYTES:
                                continue
                            return {
                                "bytes": image_response.content,
                                "mime": mime,
                                "download_url": image_url,
                                "source_url": article_url,
                                "publisher": source.get("publisher") or source.get("source_name") or "nguồn tin",
                                "file_title": f"Ảnh bài viết - {source.get('publisher') or source.get('source_name') or 'nguồn tin'}",
                            }
                    except httpx.HTTPError:
                        continue
        except (httpx.HTTPError, ValueError):
            continue
    raise ValueError("Không tìm thấy ảnh trong các bài nguồn tin của story.")


def _save_publisher_image(image: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_bytes(image["bytes"])
        with Image.open(temp_path) as opened:
            opened.verify()
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _user_agent() -> str:
    contact = os.getenv("WIKIMEDIA_CONTACT", "").strip()
    if not contact:
        raise ValueError(
            "Thiếu WIKIMEDIA_CONTACT. Hãy thêm email hoặc URL liên hệ của bạn vào file .env "
            "rồi khởi động lại backend trước khi tìm ảnh/nhạc Wikimedia."
        )
    return f"FootballNewsroomAgent/0.6 ({contact}) httpx/{httpx.__version__}"


def _plain(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(html.unescape(text).split())


def _license_ok(short_name: str, url: str) -> bool:
    name = (short_name + " " + url).casefold()
    if any(term in name for term in ("noncommercial", "non-commercial", "no derivatives", "no-derivatives", "nc-", "nd-", "by-nc", "by-nd")):
        return False
    if "public domain" in name or "creativecommons.org/publicdomain" in name or "cc0" in name:
        return True
    is_by = "cc by" in name or "creativecommons.org/licenses/by/" in name
    is_sharealike = "share alike" in name or "share-alike" in name or "by-sa" in name
    return is_by and not is_sharealike


def _search_commons(query: str, file_type: str, limit: int = 50) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": f"filetype:{file_type} {query}",
        "gsrlimit": min(limit, 50),
        "prop": "imageinfo",
        "iiprop": "url|mime|extmetadata|mediatype",
        "iiurlwidth": 1600,
        "format": "json",
        "formatversion": 2,
    }
    response = httpx.get(
        COMMONS_API,
        params=params,
        headers={"User-Agent": _user_agent(), "Api-User-Agent": _user_agent(), "Accept": "application/json"},
        timeout=25.0,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if response.status_code == 403:
            detail = _plain(response.text)[:400]
            raise ValueError(
                "Wikimedia Commons từ chối API (403). Xác nhận WIKIMEDIA_CONTACT trong .env là email "
                "hoặc URL liên hệ hợp lệ và backend đã được khởi động lại. Nếu đã đúng, kiểm tra giới hạn "
                f"truy cập hoặc firewall/IP mạng. Phản hồi: {detail or 'không có nội dung'}"
            ) from exc
        raise
    pages = response.json().get("query", {}).get("pages", [])
    if isinstance(pages, dict):
        pages = list(pages.values())
    return pages if isinstance(pages, list) else []


def _search_music(query: str, limit: int = 50) -> list[dict[str, Any]]:
    return _search_commons(query, "audio", limit)


def _music_metadata(page: dict[str, Any]) -> dict[str, Any] | None:
    info_list = page.get("imageinfo") or []
    if not info_list:
        return None
    info = info_list[0]
    ext = info.get("extmetadata") or {}
    license_name = _plain((ext.get("LicenseShortName") or {}).get("value"))
    license_url = _plain((ext.get("LicenseUrl") or {}).get("value"))
    artist = _plain((ext.get("Artist") or {}).get("value")) or _plain((ext.get("Credit") or {}).get("value"))
    mime = str(info.get("mime", ""))
    if (mime not in {"audio/mpeg", "audio/ogg", "audio/wav", "audio/x-wav", "audio/flac"}
            or not _license_ok(license_name, license_url) or not license_url or not artist):
        return None
    return {
        "file_title": page.get("title", ""),
        "file_page_url": "https://commons.wikimedia.org/wiki/" + str(page.get("title", "")).replace(" ", "_"),
        "download_url": info.get("url", ""),
        "mime": mime,
        "artist": artist,
        "license": license_name,
        "license_url": license_url,
    }


def _image_metadata(page: dict[str, Any]) -> dict[str, Any] | None:
    info_list = page.get("imageinfo") or []
    if not info_list:
        return None
    info = info_list[0]
    ext = info.get("extmetadata") or {}
    license_name = _plain((ext.get("LicenseShortName") or {}).get("value"))
    license_url = _plain((ext.get("LicenseUrl") or {}).get("value"))
    artist = _plain((ext.get("Artist") or {}).get("value")) or _plain((ext.get("Credit") or {}).get("value"))
    download_url = _commons_media_url(info.get("thumburl")) or _commons_media_url(info.get("url"))
    mime = str(info.get("thumbmime") or info.get("mime", "")).casefold()
    if info.get("thumburl") and not info.get("thumbmime"):
        suffix = Path(urlsplit(str(download_url)).path).suffix.casefold()
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix, mime)
    if (mime not in {"image/jpeg", "image/png", "image/webp"}
            or not _license_ok(license_name, license_url) or not license_url or not artist or not download_url):
        return None
    title = str(page.get("title", ""))
    return {
        "file_title": title,
        "file_page_url": "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":/_"),
        "download_url": str(download_url),
        "mime": mime,
        "artist": artist,
        "license": license_name,
        "license_url": license_url,
    }


IMAGE_STOPWORDS = {
    "about", "after", "against", "before", "being", "could", "first", "from", "have",
    "into", "league", "more", "over", "premier", "record", "records", "said", "says", "their", "there",
    "these", "they", "this", "three", "through", "under", "what", "when", "where", "which", "while",
    "with", "wins", "world", "would", "the", "and", "for", "was", "were", "has", "had", "his", "her",
    "new", "all", "one", "two", "top", "man", "men", "women", "teenager", "football", "soccer", "club", "team",
    "tin", "anh", "đội", "bóng", "người", "mới", "nhất", "được", "với", "của", "trong", "khi",
}


def _image_query_terms(story: dict[str, Any]) -> tuple[list[str], list[str]]:
    title_tokens = re.findall(r"[^\W_]{3,}", str(story.get("title", "")).casefold(), flags=re.UNICODE)
    text_tokens = re.findall(
        r"[^\W_]{3,}",
        " ".join((str(story.get("summary_vi", "")), str(story.get("evidence_notes", "")))).casefold(),
        flags=re.UNICODE,
    )
    distinctive = list(dict.fromkeys(token for token in title_tokens if token not in IMAGE_STOPWORDS))
    weighted = list(dict.fromkeys(token for token in title_tokens + text_tokens if token not in IMAGE_STOPWORDS))
    return weighted, distinctive


def _pick_story_image(story: dict[str, Any]) -> dict[str, Any]:
    terms, distinctive = _image_query_terms(story)
    if not terms:
        raise ValueError("Story chưa có từ khóa để tìm ảnh liên quan.")
    queries: list[str] = []
    first_title_term = next(iter(distinctive), "")
    for candidate in (terms[:5], terms[:3], terms[:2], [first_title_term] if first_title_term else []):
        query = " ".join(candidate).strip()
        if query and query not in queries:
            queries.append(query)

    tried: set[str] = set()
    for query in queries:
        candidates = []
        for page in _search_commons(query, "bitmap"):
            title = str(page.get("title", ""))
            if title in tried:
                continue
            tried.add(title)
            asset = _image_metadata(page)
            if not asset:
                continue
            searchable = (title + " " + _plain((page.get("imageinfo") or [{}])[0].get("extmetadata", {}).get("ImageDescription", {}).get("value", ""))).casefold()
            hits = [term for term in terms if term in searchable]
            distinct_hits = [term for term in distinctive if term in searchable]
            if not hits or (distinctive and not distinct_hits):
                continue
            candidates.append((len(hits) + 2 * len(distinct_hits), asset))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
    raise ValueError(
        "Không tìm được ảnh Commons đủ liên quan có license Public Domain, CC0 hoặc CC BY và có tác giả."
    )


def _music_pool(media_dir: Path, target_count: int = 8) -> list[dict[str, Any]]:
    """Maintain a local pool of reusable, attributed, commercially usable tracks."""
    media_dir = media_dir.resolve()
    media_dir.mkdir(parents=True, exist_ok=True)
    pool_path = media_dir / "background_music_pool.json"
    pool: list[dict[str, Any]] = []
    if pool_path.exists():
        try:
            cached = json.loads(pool_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                for item in cached:
                    if not isinstance(item, dict) or not _license_ok(
                        str(item.get("license", "")), str(item.get("license_url", ""))
                    ):
                        continue
                    local_path = Path(str(item.get("local_path", "")))
                    if not local_path.is_absolute():
                        local_path = local_path.resolve()
                        if not local_path.is_file():
                            local_path = media_dir / Path(str(item.get("local_path", ""))).name
                    if local_path.is_file():
                        item["local_path"] = str(local_path)
                        pool.append(item)
        except (OSError, json.JSONDecodeError):
            pool = []

    # Reuse a previously downloaded single-track cache as the first pool item.
    legacy_path = media_dir / "background_music.json"
    if legacy_path.exists():
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            legacy_path_value = Path(str(legacy.get("local_path", ""))) if isinstance(legacy, dict) else Path()
            if isinstance(legacy, dict) and not legacy_path_value.is_absolute():
                legacy_path_value = legacy_path_value.resolve()
                if not legacy_path_value.is_file():
                    legacy_path_value = media_dir / legacy_path_value.name
            if (isinstance(legacy, dict) and legacy_path_value.is_file()
                    and _license_ok(str(legacy.get("license", "")), str(legacy.get("license_url", "")))
                    and all(item.get("file_title") != legacy.get("file_title") for item in pool)):
                legacy["local_path"] = str(legacy_path_value)
                pool.append(legacy)
        except (OSError, json.JSONDecodeError):
            pass

    seen = {str(item.get("file_title", "")) for item in pool}
    queries = (
        "instrumental background music upbeat sports",
        "royalty free instrumental music",
        "ambient instrumental music",
        "upbeat instrumental music",
        "background music instrumental",
    )
    for query in queries:
        if len(pool) >= target_count:
            break
        for page in _search_music(query):
            title = str(page.get("title", ""))
            if not title or title in seen:
                continue
            seen.add(title)
            asset = _music_metadata(page)
            if not asset:
                continue
            suffix = Path(urlsplit(str(asset["download_url"])).path).suffix.casefold()
            if suffix not in {".ogg", ".oga", ".mp3", ".wav", ".flac"}:
                suffix = {"audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac"}.get(asset["mime"], ".ogg")
            digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
            local_path = media_dir / f"background_music_{digest}{suffix}"
            try:
                _download_audio(asset["download_url"], local_path)
            except (httpx.HTTPError, OSError, ValueError):
                continue
            asset["local_path"] = str(local_path)
            pool.append(asset)
            if len(pool) >= target_count:
                break

    if pool:
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return pool
    raise ValueError("Không tìm được nhạc nền Public Domain, CC0 hoặc CC BY có tác giả trên Wikimedia Commons.")


def _commons_media_url(value: Any) -> str | None:
    """Normalize Wikimedia media URLs and reject hosts outside Wikimedia."""
    url = str(value or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host not in {"upload.wikimedia.org", "commons.wikimedia.org"}:
            return None
        if parsed.scheme.casefold() == "http":
            parsed = parsed._replace(scheme="https")
        elif parsed.scheme.casefold() != "https":
            return None
        if parsed.username or parsed.password or parsed.port not in {None, 443}:
            return None
        return parsed.geturl()
    except ValueError:
        return None


def _download_audio(url: str, path: Path) -> None:
    url = _commons_media_url(url) or ""
    if not url:
        raise ValueError("Wikimedia trả về địa chỉ tải nhạc không hợp lệ.")
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with httpx.stream(
        "GET", url,
        headers={"User-Agent": _user_agent(), "Api-User-Agent": _user_agent()},
        timeout=30.0,
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").casefold()
        if not content_type.startswith("audio/"):
            raise ValueError(f"Media không phải file nhạc: {content_type or 'không rõ'}.")
        with path.open("wb") as output:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise ValueError("File nhạc vượt giới hạn tải 30 MB.")
                output.write(chunk)


def _download_image(url: str, path: Path, expected_mime: str) -> None:
    url = _commons_media_url(url) or ""
    if not url:
        raise ValueError("Wikimedia trả về URL ảnh không hợp lệ; chỉ chấp nhận HTTPS từ Commons hoặc upload.wikimedia.org.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    total = 0
    try:
        with httpx.stream(
            "GET", url,
            headers={"User-Agent": _user_agent(), "Api-User-Agent": _user_agent()},
            timeout=30.0,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if content_type != expected_mime:
                raise ValueError(f"Wikimedia trả media không phải ảnh {expected_mime}: {content_type or 'không rõ'}.")
            with temp_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise ValueError("Ảnh vượt giới hạn tải 30 MB.")
                    output.write(chunk)
        with Image.open(temp_path) as image:
            image.verify()
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def acquire_story_media(
    story: dict[str, Any],
    output_dir: Path,
    *,
    on_step: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def report(step: str) -> None:
        if on_step:
            on_step(step)

    media_dir = output_dir / "media"
    image_meta_path = media_dir / f"{story['story_id']}_image.json"
    image: dict[str, Any] = {}
    image_path = Path()
    if image_meta_path.exists():
        try:
            image = json.loads(image_meta_path.read_text(encoding="utf-8"))
            image_path = Path(str(image.get("local_path", "")))
        except (OSError, json.JSONDecodeError):
            image = {}
    image_is_valid = image.get("provider") == "news_source"
    if not image_path.is_file() or not image_is_valid:
        report("image_search")
        image = _publisher_image(story)
        image_ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[image["mime"]]
        image_path = media_dir / f"{story['story_id']}_image{image_ext}"
        _save_publisher_image(image, image_path)
        image.pop("bytes", None)
        image["provider"] = "news_source"
        image["artist"] = image["publisher"]
        image["license"] = "Theo điều khoản của nguồn tin"
        image["license_url"] = ""
        image["file_page_url"] = image["source_url"]
        image["local_path"] = str(image_path)
        image_meta_path.write_text(json.dumps(image, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report("audio_search")
    music_pool = _music_pool(media_dir)
    selection_path = media_dir / "background_music_selection.json"
    last_title = ""
    if selection_path.exists():
        try:
            last_title = str(json.loads(selection_path.read_text(encoding="utf-8")).get("last_file_title", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    options = [item for item in music_pool if item.get("file_title") != last_title] or music_pool
    music = random.choice(options)
    selection_path.write_text(
        json.dumps({"last_file_title": music.get("file_title", "")}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    music_path = Path(str(music["local_path"]))

    return {
        "image_path": str(image_path),
        "image_credit": f"Ảnh: {image['artist']} · nguồn tin",
        "image_license_url": image["license_url"],
        "image_page_url": image["file_page_url"],
        "image_title": image["file_title"],
        "image_changes": "Ảnh từ bài viết nguồn, đã cắt theo tỷ lệ dọc và phủ chữ tóm tắt/credit.",
        "music_path": str(music_path),
        "music_credit": f"Nhạc: {music['artist']} · {music['license']}",
        "music_license_url": music["license_url"],
        "music_page_url": music["file_page_url"],
        "music_title": music["file_title"],
    }
