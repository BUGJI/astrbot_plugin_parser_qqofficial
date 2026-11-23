from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from ...data import MediaContent, SendGroup, TextContent, VideoContent
from .common import BodyBlock, VideoEntry


class ZhihuTextMixin:
    def _html_to_text(self, html_text: str, *, keep_newlines: bool = False) -> str:
        if not html_text.strip():
            return ""

        soup = BeautifulSoup(html_text, "html.parser")
        for node in soup.find_all(
            [
                "script",
                "style",
                "noscript",
                "img",
                "video",
                "source",
                "iframe",
                "audio",
                "svg",
            ]
        ):
            node.decompose()

        text_blocks: list[str] = []
        self._append_container_content(soup, text_blocks)
        text = self._compact_text_blocks(text_blocks)
        if keep_newlines:
            return text
        return self._normalize_text(text)

    def _normalize_text(self, text: str, *, keep_newlines: bool = False) -> str:
        if not text:
            return ""
        value = html.unescape(text)
        value = value.replace("\xa0", " ").replace("\u3000", " ")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t\f\v]+", " ", value)
        if keep_newlines:
            value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
            value = re.sub(r"\n{3,}", "\n\n", value)
        else:
            value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _node_text(
        self,
        node: Tag | NavigableString,
        *,
        keep_newlines: bool = False,
    ) -> str:
        if isinstance(node, NavigableString):
            return self._normalize_text(str(node), keep_newlines=keep_newlines)
        if not isinstance(node, Tag):
            return ""
        separator = "\n" if keep_newlines else " "
        return self._normalize_text(
            node.get_text(separator=separator, strip=False),
            keep_newlines=keep_newlines,
        )

    def _find_text_value(
        self,
        value: Any,
        preferred_keys: tuple[str, ...] = (),
    ) -> str | None:
        seen: set[int] = set()
        preferred = {key.lower() for key in preferred_keys}

        def pick_text(raw: str) -> str | None:
            text = self._normalize_text(raw)
            if not text:
                return None
            lowered = text.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                return None
            return text

        def visit(current: Any) -> str | None:
            if isinstance(current, str):
                return pick_text(current)

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

    def _iter_attr_strings(self, value: Any):
        if isinstance(value, str):
            yield value
            return

        if isinstance(value, dict):
            for item in value.values():
                yield from self._iter_attr_strings(item)
            return

        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from self._iter_attr_strings(item)

    def _normalize_media_url(
        self,
        url: str | None,
        page_url: str | None = None,
    ) -> str:
        if not url:
            return ""
        value = html.unescape(str(url)).strip().strip("\"'")
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        value = value.replace("&amp;", "&")
        value = value.rstrip(".,);")
        if not value or value.startswith(("data:", "blob:")):
            return ""
        if value.startswith("//"):
            value = "https:" + value
        elif page_url and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
            value = urljoin(page_url, value)
        if not value.startswith(("http://", "https://")):
            return ""
        return value

    def _normalize_state_media_url(self, url: str | None) -> str:
        if not url:
            return ""
        value = html.unescape(str(url)).strip().strip("\"'")
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        matched = re.search(r"https?://[^\s'\"<>]+|//[^\s'\"<>]+", value)
        if matched:
            value = matched.group(0)
        return self._normalize_media_url(value)

    def _looks_like_video_url(self, url: str | None) -> bool:
        if not url:
            return False
        value = url.lower()
        if not value.startswith(("http://", "https://")):
            return False
        if self._looks_like_image_url(value):
            return False
        if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", value):
            return True
        return any(
            marker in value
            for marker in (
                "video.zhihu.com",
                "/playlist.m3u8",
                "/playback/",
                "/stream/",
                ".vod.",
            )
        )

    def _looks_like_image_url(self, url: str | None) -> bool:
        if not url:
            return False
        value = url.lower()
        if not value.startswith(("http://", "https://")):
            return False
        if re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", value):
            return False
        if re.search(r"\.(jpg|jpeg|png|webp|gif|bmp|avif)(?:$|[?#])", value):
            return True
        return any(
            host in value
            for host in (
                "picx.zhimg.com",
                "pic1.zhimg.com",
                "pic2.zhimg.com",
                "zhimg.com",
            )
        )

    def _media_key(self, url: str | None) -> str:
        normalized = self._normalize_media_url(url)
        if not normalized:
            return ""
        value = normalized.split("#", 1)[0]
        if self._looks_like_image_url(value) or self._looks_like_video_url(value):
            value = value.split("?", 1)[0]
        return value.replace("http://", "https://")

    def _merge_unique_urls(self, *groups: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for url in group:
                key = self._media_key(url)
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(url)
        return merged

    def _merge_unique_video_entries(
        self,
        *groups: list[VideoEntry],
    ) -> list[VideoEntry]:
        merged: dict[str, VideoEntry] = {}
        order: list[str] = []

        for group in groups:
            for entry in group:
                url = self._normalize_media_url(entry.get("url"))
                key = self._media_key(url)
                if not key:
                    continue
                normalized_entry: VideoEntry = {
                    "url": url,
                    "cover_url": self._normalize_media_url(entry.get("cover_url"))
                    or None,
                    "title": self._normalize_text(str(entry.get("title") or ""))
                    or None,
                }
                if key not in merged:
                    merged[key] = normalized_entry
                    order.append(key)
                    continue
                if not merged[key].get("cover_url") and normalized_entry.get(
                    "cover_url"
                ):
                    merged[key]["cover_url"] = normalized_entry["cover_url"]
                if not merged[key].get("title") and normalized_entry.get("title"):
                    merged[key]["title"] = normalized_entry["title"]

        return [merged[key] for key in order]

    def _append_image_url(self, image_urls: list[str], candidate: str | None) -> None:
        normalized = self._normalize_media_url(candidate)
        if not normalized or not self._looks_like_image_url(normalized):
            return
        key = self._media_key(normalized)
        if not key:
            return
        if any(self._media_key(item) == key for item in image_urls):
            return
        image_urls.append(normalized)

    @staticmethod
    def _pin_content_html(pin: dict[str, Any]) -> str:
        return str(pin.get("content_html") or pin.get("contentHtml") or "").strip()

    def _pin_plain_text(self, pin: dict[str, Any]) -> str:
        content = pin.get("content")
        if isinstance(content, str):
            return self._normalize_text(content, keep_newlines=True)
        if content is None:
            return ""
        if isinstance(content, (dict, list)):
            value = self._find_text_value(
                content,
                (
                    "content",
                    "text",
                    "description",
                    "title",
                ),
            )
            return self._normalize_text(value or "", keep_newlines=True)
        return self._normalize_text(str(content), keep_newlines=True)

    def _pin_timestamp(self, pin: dict[str, Any]) -> int | None:
        return self._safe_int(pin.get("created_time") or pin.get("updated_time"))
