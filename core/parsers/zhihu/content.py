from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from ...data import MediaContent, SendGroup, TextContent, VideoContent
from .common import BodyBlock, VideoEntry
from .content_blocks import ZhihuBlocksMixin
from .content_media import ZhihuMediaMixin
from .content_text import ZhihuTextMixin


class ZhihuContentMixin(
    ZhihuBlocksMixin,
    ZhihuMediaMixin,
    ZhihuTextMixin,
):
    def _build_contents_and_groups(
        self,
        header_text: str,
        body_blocks: list[BodyBlock],
        video_entries: list[VideoEntry],
        *,
        request_headers: dict[str, str],
    ) -> tuple[list[MediaContent], list[SendGroup]]:
        video_contents = self._build_video_contents(
            video_entries,
            request_headers=request_headers,
        )

        contents: list[MediaContent] = []
        primary_contents: list[MediaContent] = []
        image_urls = [
            block["value"]
            for block in body_blocks
            if block.get("kind") == "image" and block.get("value")
        ]
        image_iter = iter(
            self.create_image_contents(image_urls, headers=request_headers)
        )
        pending_text_parts: list[str] = []

        def append_text_part(text: str) -> None:
            value = text.strip()
            if value:
                pending_text_parts.append(value)

        def flush_text_parts() -> None:
            if not pending_text_parts:
                return
            text_content = TextContent(self._join_sections(pending_text_parts))
            contents.append(text_content)
            primary_contents.append(text_content)
            pending_text_parts.clear()

        append_text_part(header_text)
        for block in body_blocks:
            kind = block.get("kind")
            value = str(block.get("value") or "").strip()
            if not value:
                continue
            if kind == "text":
                append_text_part(value)
                continue
            if kind != "image":
                continue
            flush_text_parts()
            image_content = next(image_iter, None)
            if image_content is None:
                continue
            contents.append(image_content)
            primary_contents.append(image_content)

        flush_text_parts()
        contents.extend(video_contents)

        send_groups: list[SendGroup] = []
        if primary_contents:
            send_groups.append(
                SendGroup(
                    contents=primary_contents,
                    force_merge=False,
                    render_card=True,
                )
            )
        for video in video_contents:
            send_groups.append(SendGroup(contents=[video], force_merge=False))
        return contents, send_groups

    async def _extract_content(
        self,
        html_text: str,
        initial_data: dict[str, Any],
        *,
        page_url: str,
        include_state_videos: bool = True,
    ) -> tuple[str, list[BodyBlock], list[VideoEntry]]:
        body_text = self._html_to_text(html_text, keep_newlines=True)
        body_blocks: list[BodyBlock] = []
        video_entries: list[VideoEntry] = []

        if html_text.strip():
            media_soup = BeautifulSoup(html_text, "html.parser")
            body_blocks = self._extract_ordered_body_blocks(
                media_soup, page_url=page_url
            )
            for node in media_soup.find_all(["video", "source", "iframe"]):
                self._append_video_entry(
                    video_entries,
                    self._extract_video_entry_from_tag(node, page_url),
                )

        if include_state_videos:
            video_entries = self._merge_unique_video_entries(
                video_entries,
                self._extract_video_entries_from_state(initial_data, page_url),
            )

        return body_text, body_blocks, video_entries
