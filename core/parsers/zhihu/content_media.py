from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from ...data import MediaContent, SendGroup, TextContent, VideoContent
from .common import BodyBlock, VideoEntry


class ZhihuMediaMixin:
    def _append_video_entry(
        self,
        video_entries: list[VideoEntry],
        candidate: VideoEntry | None,
    ) -> None:
        if not candidate:
            return

        url = self._normalize_media_url(candidate.get("url"))
        if not url or not self._looks_like_video_url(url):
            return

        entry: VideoEntry = {
            "url": url,
            "cover_url": self._normalize_media_url(candidate.get("cover_url")) or None,
            "title": self._normalize_text(str(candidate.get("title") or "")) or None,
        }
        key = self._media_key(url)
        if not key:
            return

        for existing in video_entries:
            if self._media_key(existing.get("url")) != key:
                continue
            if not existing.get("cover_url") and entry.get("cover_url"):
                existing["cover_url"] = entry["cover_url"]
            if not existing.get("title") and entry.get("title"):
                existing["title"] = entry["title"]
            return

        video_entries.append(entry)

    def _extract_image_url(self, tag: Tag, page_url: str) -> str | None:
        for key in (*self._MEDIA_ATTRS, "srcset"):
            if key not in tag.attrs:
                continue
            for raw in self._iter_attr_strings(tag.attrs.get(key)):
                candidates = re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw)
                if not candidates and raw:
                    candidates = [raw.split()[0]]
                for candidate in candidates:
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_image_url(normalized):
                        return normalized

        for raw in self._iter_attr_strings(tag.attrs):
            for candidate in re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw):
                normalized = self._normalize_media_url(candidate, page_url)
                if normalized and self._looks_like_image_url(normalized):
                    return normalized
        return None

    def _extract_video_entry_from_tag(
        self, tag: Tag, page_url: str
    ) -> VideoEntry | None:
        url: str | None = None

        for key in (*self._VIDEO_URL_KEYS, *self._MEDIA_ATTRS):
            if key not in tag.attrs:
                continue
            for raw in self._iter_attr_strings(tag.attrs.get(key)):
                candidates = re.findall(r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+", raw)
                if not candidates and raw:
                    candidates = [raw]
                for candidate in candidates:
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_video_url(normalized):
                        url = normalized
                        break
                if url:
                    break
            if url:
                break

        if not url:
            for raw in self._iter_attr_strings(tag.attrs):
                for candidate in re.findall(
                    r"https?://[^\s,'\"<>]+|//[^\s,'\"<>]+",
                    raw,
                ):
                    normalized = self._normalize_media_url(candidate, page_url)
                    if normalized and self._looks_like_video_url(normalized):
                        url = normalized
                        break
                if url:
                    break

        if not url:
            return None

        title = (
            self._normalize_text(
                str(tag.get("title") or tag.get("aria-label") or tag.get("alt") or "")
            )
            or None
        )
        return {
            "url": url,
            "cover_url": self._extract_cover_url(tag, page_url),
            "title": title,
        }

    def _extract_cover_url(self, tag: Tag, page_url: str) -> str | None:
        for node in (tag, tag.parent if isinstance(tag.parent, Tag) else None):
            if not isinstance(node, Tag):
                continue
            for key in (*self._VIDEO_COVER_KEYS, *self._MEDIA_ATTRS):
                if key not in node.attrs:
                    continue
                for raw in self._iter_attr_strings(node.attrs.get(key)):
                    normalized = self._normalize_media_url(raw, page_url)
                    if normalized and self._looks_like_image_url(normalized):
                        return normalized
        return None

    def _build_video_contents(
        self,
        video_entries: list[VideoEntry],
        *,
        request_headers: dict[str, str],
    ) -> list[VideoContent]:
        contents: list[VideoContent] = []
        for entry in video_entries:
            video_url = str(entry.get("url") or "").strip()
            if not video_url:
                continue
            contents.append(
                self._build_video_content_from_url(
                    video_url,
                    cover_url=entry.get("cover_url"),
                    request_headers=request_headers,
                )
            )
        return contents

    def _build_video_content_from_url(
        self,
        video_url: str,
        *,
        cover_url: str | None = None,
        request_headers: dict[str, str],
    ) -> VideoContent:
        if ".m3u8" in video_url.lower():
            task = self.downloader.ytdlp_download_video_relaxed(
                video_url,
                headers=request_headers,
                proxy=self.proxy,
            )
            return self.create_video_content_by_task(
                task,
                cover_url,
                headers=request_headers,
            )
        return self.create_video_content(
            video_url,
            cover_url,
            headers=request_headers,
        )

    def _extract_video_entries_from_state(
        self,
        initial_data: dict[str, Any],
        page_url: str,
    ) -> list[VideoEntry]:
        entries: list[VideoEntry] = []
        queue: list[Any] = [initial_data.get("initialState") or initial_data]
        seen: set[int] = set()

        while queue:
            value = queue.pop()
            if isinstance(value, dict):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                self._append_video_entry(
                    entries,
                    self._extract_video_entry_from_mapping(value, page_url),
                )
                queue.extend(value.values())
                continue

            if isinstance(value, list):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                queue.extend(value)

        return entries

    def _extract_video_entry_from_mapping(
        self,
        mapping: dict[str, Any],
        page_url: str,
    ) -> VideoEntry | None:
        lowered_keys = {str(key).lower() for key in mapping.keys()}
        if not lowered_keys:
            return None

        has_video_signal = any(
            token in key
            for key in lowered_keys
            for token in ("video", "play", "stream", "playlist", "cover", "poster")
        )
        if not has_video_signal:
            return None

        video_url = self._find_media_value(
            mapping,
            self._looks_like_video_url,
            self._VIDEO_URL_KEYS,
        )
        if not video_url:
            return None

        return {
            "url": self._normalize_media_url(video_url, page_url),
            "cover_url": self._normalize_media_url(
                self._find_media_value(
                    mapping,
                    self._looks_like_image_url,
                    self._VIDEO_COVER_KEYS,
                ),
                page_url,
            )
            or None,
            "title": self._find_text_value(mapping, self._VIDEO_TITLE_KEYS),
        }

    def _find_media_value(
        self,
        value: Any,
        predicate: Any,
        preferred_keys: tuple[str, ...] = (),
    ) -> str | None:
        seen: set[int] = set()
        preferred = {key.lower() for key in preferred_keys}

        def visit(current: Any) -> str | None:
            if isinstance(current, str):
                normalized = self._normalize_state_media_url(current)
                if normalized and predicate(normalized):
                    return normalized
                return None

            if isinstance(current, dict):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)

                for key in preferred_keys:
                    for current_key, nested in current.items():
                        if str(current_key).lower() == key.lower():
                            found = visit(nested)
                            if found:
                                return found

                for current_key, nested in current.items():
                    if str(current_key).lower() in preferred:
                        continue
                    found = visit(nested)
                    if found:
                        return found
                return None

            if isinstance(current, list):
                marker = id(current)
                if marker in seen:
                    return None
                seen.add(marker)
                for nested in current:
                    found = visit(nested)
                    if found:
                        return found
            return None

        return visit(value)
