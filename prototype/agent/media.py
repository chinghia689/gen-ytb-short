from __future__ import annotations

import html
import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

from prototype.performance import count, stage


NCS_CATALOG_URL = "https://ncs.io/?display=list&page={page}"
NCS_TRACK_HOST = "ncsmusic.s3.eu-west-1.amazonaws.com"
NCS_POLICY_URL = "https://ncs.io/usage-policy"
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


class _NCSTrackParser(HTMLParser):
    """Extract official NCS download metadata from one music-library page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tracks: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.total_pages = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "a":
            href = html.unescape(values.get("href", ""))
            page_match = re.search(r"[?&]page=(\d+)", href)
            if page_match:
                self.total_pages = max(self.total_pages, int(page_match.group(1)))
        if tag.casefold() == "tr":
            self.current = {}
        if tag.casefold() == "a" and self.current is not None:
            classes = set(values.get("class", "").split())
            if "player-play" in classes and values.get("data-url"):
                self.current.update({
                    "download_url": html.unescape(values["data-url"]),
                    "file_title": html.unescape(values.get("data-track", "")).strip(),
                    "artist": html.unescape(values.get("data-artistraw", "")).strip(),
                    "track_id": values.get("data-tid", "").strip(),
                })
            elif href.startswith("/") and not href.startswith(("/artist/", "/music-search", "/genre/")):
                if not self.current.get("track_slug"):
                    self.current["track_slug"] = href.strip("/")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "tr" and self.current is not None:
            required = ("download_url", "file_title", "artist", "track_id", "track_slug")
            if all(self.current.get(key) for key in required):
                self.tracks.append(self.current)
            self.current = None


def _fetch_ncs_catalog_page(page: int) -> tuple[int, str]:
    response = httpx.get(
        NCS_CATALOG_URL.format(page=page),
        headers={"User-Agent": "YtbShortNewsPrototype/0.3 (NCS catalog reader)"},
        timeout=httpx.Timeout(25.0, connect=8.0),
        follow_redirects=False,
    )
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", "").casefold():
        raise ValueError("NCS trả về nội dung không phải HTML cho danh mục nhạc.")
    return page, response.text


def _ncs_catalog(media_dir: Path) -> list[dict[str, str]]:
    """Cache the complete NCS catalog and download only the randomly selected track."""
    cache_path = media_dir / "ncs_music_catalog.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            age_seconds = time.time() - float(cached.get("updated_at", 0))
            items = cached.get("tracks", [])
            if age_seconds < 24 * 60 * 60 and isinstance(items, list) and items:
                count("ncs_catalog_cache_hits")
                return items
        except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
            pass

    count("ncs_catalog_cache_misses")
    pages: dict[int, str] = {}
    try:
        _, first_page = _fetch_ncs_catalog_page(1)
        pages[1] = first_page
        first_parser = _NCSTrackParser()
        first_parser.feed(first_page)
        page_count = min(max(first_parser.total_pages, 1), 500)
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_fetch_ncs_catalog_page, page) for page in range(2, page_count + 1)]
            for future in as_completed(futures):
                try:
                    page_number, page_html = future.result()
                    pages[page_number] = page_html
                except (httpx.HTTPError, ValueError):
                    continue
    except (httpx.HTTPError, ValueError) as exc:
        if cache_path.is_file():
            try:
                stale = json.loads(cache_path.read_text(encoding="utf-8"))
                tracks = stale.get("tracks", [])
                if isinstance(tracks, list) and tracks:
                    return tracks
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        raise ValueError(f"Không tải được danh mục nhạc chính thức của NCS: {exc}") from exc

    if len(pages) != page_count:
        if cache_path.is_file():
            try:
                stale = json.loads(cache_path.read_text(encoding="utf-8"))
                tracks = stale.get("tracks", [])
                if isinstance(tracks, list) and tracks:
                    return tracks
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        raise ValueError(f"Danh mục NCS tải chưa đủ trang ({len(pages)}/{page_count}); hãy thử render lại.")

    tracks_by_id: dict[str, dict[str, str]] = {}
    for page_html in pages.values():
        parser = _NCSTrackParser()
        parser.feed(page_html)
        for item in parser.tracks:
            download_url = urlsplit(item["download_url"])
            if (download_url.scheme != "https" or download_url.hostname != NCS_TRACK_HOST
                    or not download_url.path.startswith("/tracks/")):
                continue
            slug = item["track_slug"]
            item.update({
                "provider": "NCS",
                "license": "NCS Usage Policy",
                "license_url": NCS_POLICY_URL,
                "file_page_url": f"https://ncs.io/{slug}",
                "music_attribution": (
                    f"Song: {item['artist']} - {item['file_title']}\n"
                    "Music provided by NoCopyrightSounds\n"
                    f"Free Download/Stream: http://ncs.io/{slug}\n"
                    f"Watch: http://ncs.lnk.to/{slug}AT/youtube"
                ),
            })
            tracks_by_id[item["track_id"]] = item
    tracks = list(tracks_by_id.values())
    if not tracks:
        raise ValueError("Không tìm thấy track có thể tải trong danh mục chính thức của NCS.")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps({"updated_at": time.time(), "tracks": tracks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(cache_path)
    return tracks


def _download_ncs_audio(url: str, path: Path) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != NCS_TRACK_HOST
            or parsed.username or parsed.password or parsed.port not in {None, 443}
            or not parsed.path.startswith("/tracks/")):
        raise ValueError("NCS trả về URL tải nhạc không hợp lệ.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    total = 0
    try:
        with httpx.stream(
            "GET", url,
            headers={"User-Agent": "YtbShortNewsPrototype/0.3 (NCS track download)"},
            timeout=30.0,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if not content_type.startswith("audio/"):
                raise ValueError(f"Track NCS không trả về audio: {content_type or 'không rõ'}.")
            with temp_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise ValueError("Track NCS vượt giới hạn tải 30 MB.")
                    output.write(chunk)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


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
        count("image_cache_misses")
        with stage("image_search", story_id=story["story_id"]):
            image = _publisher_image(story)
        image_ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[image["mime"]]
        image_path = media_dir / f"{story['story_id']}_image{image_ext}"
        with stage("image_save", story_id=story["story_id"]):
            _save_publisher_image(image, image_path)
        image.pop("bytes", None)
        image["provider"] = "news_source"
        image["artist"] = image["publisher"]
        image["license"] = "Theo điều khoản của nguồn tin"
        image["license_url"] = ""
        image["file_page_url"] = image["source_url"]
        image["local_path"] = str(image_path)
        image_meta_path.write_text(json.dumps(image, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        count("image_cache_hits")

    report("audio_search")
    selection_path = media_dir / "background_music_selection.json"
    last_track_id = ""
    if selection_path.exists():
        try:
            last_track_id = str(json.loads(selection_path.read_text(encoding="utf-8")).get("last_track_id", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    with stage("ncs_catalog", story_id=story["story_id"]):
        ncs_tracks = _ncs_catalog(media_dir)
    options = [item for item in ncs_tracks if item.get("track_id") != last_track_id] or ncs_tracks
    random.shuffle(options)
    music = None
    music_path = None
    for candidate in options:
        digest = hashlib.sha1(candidate["track_id"].encode("utf-8")).hexdigest()[:16]
        suffix = Path(urlsplit(candidate["download_url"]).path).suffix.casefold() or ".mp3"
        candidate_path = media_dir / f"ncs_music_{digest}{suffix}"
        try:
            if not candidate_path.is_file():
                count("music_cache_misses")
                with stage("music_download", story_id=story["story_id"], track_id=candidate["track_id"]):
                    _download_ncs_audio(candidate["download_url"], candidate_path)
            else:
                count("music_cache_hits")
        except (httpx.HTTPError, OSError, ValueError):
            continue
        music = candidate
        music_path = candidate_path
        break
    if music is None or music_path is None:
        raise ValueError("Không tải được track nào từ danh mục chính thức của NCS.")
    selection_path.write_text(
        json.dumps({"last_track_id": music.get("track_id", "")}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    return {
        "image_path": str(image_path),
        "image_credit": f"Ảnh: {image['artist']} · nguồn tin",
        "image_license_url": image["license_url"],
        "image_page_url": image["file_page_url"],
        "image_title": image["file_title"],
        "image_changes": "Ảnh từ bài viết nguồn, đã cắt theo tỷ lệ dọc và phủ chữ tóm tắt/credit.",
        "music_path": str(music_path),
        "music_provider": music["provider"],
        "music_credit": f"Nhạc: {music['artist']} · {music['file_title']} · NCS",
        "music_attribution": music["music_attribution"],
        "music_license_url": music["license_url"],
        "music_page_url": music["file_page_url"],
        "music_title": music["file_title"],
    }
