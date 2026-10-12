from __future__ import annotations

import html
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join(part.strip() for part in element.itertext() if part.strip())


def _link(entry: ET.Element) -> str:
    for child in entry:
        if _tag_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if href and child.attrib.get("rel", "alternate") in {"alternate", ""}:
            return href
        value = _text(child)
        if value:
            return value
    return ""


def _published_at(entry: ET.Element) -> tuple[str, bool]:
    for child in entry:
        if _tag_name(child.tag) not in {"pubdate", "published", "updated", "date"}:
            continue
        value = _text(child)
        if not value:
            continue
        try:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(), False
        except (TypeError, ValueError, OverflowError):
            continue
    return datetime.now(timezone.utc).isoformat(), True


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit((
        parts.scheme.casefold(),
        parts.netloc.casefold(),
        parts.path.rstrip("/"),
        parts.query,
        "",
    ))


def _classify(title: str, categories: list[str], source: dict[str, Any], config: dict[str, Any]):
    if source.get("scope") == "premier_league":
        return "Premier League", "feed_dedicated_to_league"

    searchable = _normalized(" ".join([title, *categories]))
    if any(term in searchable for term in ("premier league", "english premier league", "epl")):
        return "Premier League", "explicit_league_reference"

    for entity in config.get("epl_entities", []):
        if _normalized(str(entity)) in searchable:
            return "Premier League", "team_name_match_needs_review"
    return "unknown", "needs_scope_review"


def parse_feed(xml_data: bytes, source: dict[str, Any], config: dict[str, Any]):
    root = ET.fromstring(xml_data)
    entries = [node for node in root.iter() if _tag_name(node.tag) in {"item", "entry"}]
    collected_at = datetime.now(timezone.utc).isoformat()
    records = []

    for entry in entries:
        title_node = next((child for child in entry if _tag_name(child.tag) == "title"), None)
        title = html.unescape(_text(title_node)).strip()
        link = _link(entry)
        if not title or not link:
            continue

        categories = [
            _text(child) for child in entry if _tag_name(child.tag) == "category"
        ]
        competition, scope_status = _classify(title, categories, source, config)
        published_at, estimated = _published_at(entry)
        records.append({
            "title": title,
            "competition": competition,
            "scope_status": scope_status,
            "source_name": source["name"],
            "publisher": source.get("publisher", source["name"]),
            "source_url": link,
            "published_at": published_at,
            "published_at_is_estimated": estimated,
            "collected_at": collected_at,
            "language": source.get("language", "en"),
        })
    return records


def fetch_sources(config_path: Path):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    sources = [item for item in config.get("sources", []) if item.get("enabled")]
    records: list[dict[str, Any]] = []
    errors: list[str] = []

    for index, source in enumerate(sources):
        if index:
            time.sleep(1)
        request = Request(
            source["url"],
            headers={"User-Agent": "YtbShortNewsPrototype/0.2 (RSS metadata reader)"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                items = parse_feed(response.read(), source, config)
            records.extend(items)
            print(f"{source['name']}: nhận {len(items)} mục RSS")
        except (URLError, TimeoutError, ET.ParseError, OSError, KeyError, ValueError) as exc:
            errors.append(f"{source.get('name', source.get('url'))}: {exc}")

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = _normalized_url(record["source_url"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique, errors


def save_records(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
