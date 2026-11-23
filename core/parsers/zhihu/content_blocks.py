from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from ...data import MediaContent, SendGroup, TextContent, VideoContent
from .common import BodyBlock, VideoEntry


class ZhihuBlocksMixin:
    def _build_section_blocks(
        self,
        title: str | None,
        body_blocks: list[BodyBlock],
        fallback_text: str,
    ) -> list[BodyBlock]:
        normalized_fallback = self._normalize_text(
            fallback_text,
            keep_newlines=True,
        )
        if not body_blocks and not normalized_fallback:
            return []

        section_blocks: list[BodyBlock] = []
        if title:
            section_blocks.append(self._make_text_block(title))
        if body_blocks:
            section_blocks.extend(body_blocks)
            return section_blocks

        section_blocks.append(self._make_text_block(normalized_fallback))
        return section_blocks

    def _extract_ordered_body_blocks(
        self,
        root: Tag | BeautifulSoup,
        *,
        page_url: str,
    ) -> list[BodyBlock]:
        body_blocks: list[BodyBlock] = []
        text_blocks: list[str] = []
        seen_images: set[str] = set()
        self._append_ordered_body_container(
            root,
            body_blocks,
            text_blocks,
            seen_images,
            page_url,
        )
        self._flush_body_text_blocks(body_blocks, text_blocks)
        return self._merge_adjacent_body_text_blocks(body_blocks)

    def _append_ordered_body_container(
        self,
        node: Tag | BeautifulSoup,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
        seen_images: set[str],
        page_url: str,
    ) -> None:
        for child in node.children:
            if isinstance(child, (Tag, NavigableString)):
                self._append_ordered_body_node(
                    child,
                    body_blocks,
                    text_blocks,
                    seen_images,
                    page_url,
                )

    def _append_ordered_body_node(
        self,
        node: Tag | NavigableString,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
        seen_images: set[str],
        page_url: str,
    ) -> None:
        if isinstance(node, NavigableString):
            text = self._normalize_text(str(node))
            if text:
                self._append_text_block(text_blocks, text)
            return

        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in {
            "script",
            "style",
            "noscript",
            "video",
            "source",
            "iframe",
            "audio",
            "svg",
        }:
            return

        if name == "img":
            self._flush_body_text_blocks(body_blocks, text_blocks)
            self._append_body_image_block(
                body_blocks,
                seen_images,
                self._extract_image_url(node, page_url),
            )
            return

        if name == "br":
            if text_blocks:
                text_blocks[-1] = text_blocks[-1].rstrip() + "\n"
            return

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "blockquote":
            self._append_text_block(
                text_blocks,
                self._format_blockquote_text(self._node_text(node, keep_newlines=True)),
            )
            return

        if name in {"ul", "ol"}:
            self._append_text_block(
                text_blocks,
                self._format_list_text(
                    self._collect_list_items(node),
                    ordered=name == "ol",
                ),
            )
            return

        if name == "li":
            item_text = self._node_text(node, keep_newlines=True)
            if item_text:
                self._append_text_block(
                    text_blocks,
                    self._format_list_text([item_text], ordered=False),
                )
            return

        if name == "pre":
            self._append_text_block(
                text_blocks,
                self._format_code_block(
                    self._extract_code_block(node),
                    self._extract_code_language(node),
                ),
            )
            return

        if name == "code":
            parent = node.parent
            if isinstance(parent, Tag) and (parent.name or "").lower() == "pre":
                return
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "hr":
            self._append_text_block(text_blocks, "---")
            return

        block_tags = {
            "p",
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "aside",
            "figure",
            "figcaption",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
        }
        has_block_child = any(
            isinstance(child, Tag)
            and (child.name or "").lower()
            in {
                "p",
                "div",
                "section",
                "article",
                "blockquote",
                "ul",
                "ol",
                "pre",
                "figure",
                "table",
                "hr",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            }
            for child in node.children
        )
        if name in {"figure", "figcaption"} or (
            name in block_tags and self._has_media_child(node)
        ):
            self._append_ordered_body_container(
                node,
                body_blocks,
                text_blocks,
                seen_images,
                page_url,
            )
            return

        if name in block_tags and has_block_child:
            self._append_ordered_body_container(
                node,
                body_blocks,
                text_blocks,
                seen_images,
                page_url,
            )
            return

        if name in block_tags:
            text = self._node_text(node, keep_newlines=True)
            if text:
                self._append_text_block(text_blocks, text)
            return

        text = self._node_text(node, keep_newlines=True)
        if text:
            self._append_text_block(text_blocks, text)
            return

        self._append_ordered_body_container(
            node,
            body_blocks,
            text_blocks,
            seen_images,
            page_url,
        )

    def _flush_body_text_blocks(
        self,
        body_blocks: list[BodyBlock],
        text_blocks: list[str],
    ) -> None:
        if not text_blocks:
            return
        text = self._compact_text_blocks(text_blocks)
        text_blocks.clear()
        if text:
            body_blocks.append(self._make_text_block(text))

    def _append_body_image_block(
        self,
        body_blocks: list[BodyBlock],
        seen_images: set[str],
        candidate: str | None,
    ) -> None:
        normalized = self._normalize_media_url(candidate)
        if not normalized or not self._looks_like_image_url(normalized):
            return
        key = self._media_key(normalized)
        if not key or key in seen_images:
            return
        seen_images.add(key)
        body_blocks.append(self._make_image_block(normalized))

    def _merge_adjacent_body_text_blocks(
        self,
        body_blocks: list[BodyBlock],
    ) -> list[BodyBlock]:
        merged: list[BodyBlock] = []
        pending_texts: list[str] = []

        def flush_texts() -> None:
            if not pending_texts:
                return
            merged.append(self._make_text_block(self._join_sections(pending_texts)))
            pending_texts.clear()

        for block in body_blocks:
            kind = block.get("kind")
            value = str(block.get("value") or "").strip()
            if not value:
                continue
            if kind == "text":
                pending_texts.append(value)
                continue
            flush_texts()
            if kind == "image":
                merged.append(block)

        flush_texts()
        return merged

    @staticmethod
    def _make_text_block(text: str) -> BodyBlock:
        return {"kind": "text", "value": text}

    @staticmethod
    def _make_image_block(url: str) -> BodyBlock:
        return {"kind": "image", "value": url}

    def _append_node_content(
        self,
        node: Tag | NavigableString,
        text_blocks: list[str],
    ) -> None:
        if isinstance(node, NavigableString):
            text = self._normalize_text(str(node))
            if text:
                self._append_text_block(text_blocks, text)
            return

        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in {
            "script",
            "style",
            "noscript",
            "img",
            "video",
            "source",
            "iframe",
            "audio",
            "svg",
        }:
            return

        if name == "br":
            if text_blocks:
                text_blocks[-1] = text_blocks[-1].rstrip() + "\n"
            return

        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "blockquote":
            self._append_text_block(
                text_blocks,
                self._format_blockquote_text(self._node_text(node, keep_newlines=True)),
            )
            return

        if name in {"ul", "ol"}:
            self._append_text_block(
                text_blocks,
                self._format_list_text(
                    self._collect_list_items(node),
                    ordered=name == "ol",
                ),
            )
            return

        if name == "li":
            item_text = self._node_text(node, keep_newlines=True)
            if item_text:
                self._append_text_block(
                    text_blocks,
                    self._format_list_text([item_text], ordered=False),
                )
            return

        if name == "pre":
            self._append_text_block(
                text_blocks,
                self._format_code_block(
                    self._extract_code_block(node),
                    self._extract_code_language(node),
                ),
            )
            return

        if name == "code":
            parent = node.parent
            if isinstance(parent, Tag) and (parent.name or "").lower() == "pre":
                return
            self._append_text_block(text_blocks, self._node_text(node))
            return

        if name == "hr":
            self._append_text_block(text_blocks, "---")
            return

        block_tags = {
            "p",
            "div",
            "section",
            "article",
            "main",
            "header",
            "footer",
            "aside",
            "figure",
            "figcaption",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
        }
        has_block_child = any(
            isinstance(child, Tag)
            and (child.name or "").lower()
            in {
                "p",
                "div",
                "section",
                "article",
                "blockquote",
                "ul",
                "ol",
                "pre",
                "figure",
                "table",
                "hr",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            }
            for child in node.children
        )
        if name in block_tags and has_block_child:
            self._append_container_content(node, text_blocks)
            return

        if name in block_tags:
            if self._has_media_child(node) and not self._node_text(
                node, keep_newlines=True
            ):
                return
            self._append_text_block(
                text_blocks,
                self._node_text(node, keep_newlines=True),
            )
            return

        text = self._node_text(node, keep_newlines=True)
        if text:
            self._append_text_block(text_blocks, text)
            return

        self._append_container_content(node, text_blocks)

    def _append_container_content(
        self,
        node: Tag | BeautifulSoup,
        text_blocks: list[str],
    ) -> None:
        for child in node.children:
            if isinstance(child, (Tag, NavigableString)):
                self._append_node_content(child, text_blocks)

    def _append_text_block(self, text_blocks: list[str], text: str) -> None:
        value = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not value:
            return
        if value.startswith("```"):
            text_blocks.append(value)
            return
        text_blocks.append(self._normalize_text(value, keep_newlines=True))

    def _compact_text_blocks(self, text_blocks: list[str]) -> str:
        compacted: list[str] = []

        def is_special(block: str) -> bool:
            stripped = block.lstrip()
            return (
                not stripped
                or stripped.startswith("```")
                or stripped.startswith(">")
                or stripped.startswith("- ")
                or stripped == "---"
                or bool(re.match(r"^\d+\.\s", stripped))
                or "\n" in stripped
            )

        for block in text_blocks:
            value = block.strip()
            if not value:
                continue
            if compacted and not is_special(compacted[-1]) and not is_special(value):
                compacted[-1] = self._normalize_text(f"{compacted[-1]} {value}")
                continue
            compacted.append(value)
        return "\n\n".join(compacted).strip()

    def _format_blockquote_text(self, text: str) -> str:
        value = self._normalize_text(text, keep_newlines=True)
        if not value:
            return ""
        return "\n".join(
            f"> {line}" if line else ">" for line in value.splitlines()
        ).strip()

    def _format_list_text(self, items: list[str], *, ordered: bool) -> str:
        lines: list[str] = []
        for index, item in enumerate(items, start=1):
            value = self._normalize_text(item, keep_newlines=True)
            if not value:
                continue
            prefix = f"{index}. " if ordered else "- "
            parts = value.splitlines() or [value]
            lines.append(prefix + parts[0])
            indent = " " * len(prefix)
            for line in parts[1:]:
                lines.append(indent + line)
        return "\n".join(lines).strip()

    def _format_code_block(self, code: str, language: str | None = None) -> str:
        value = code.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not value.strip():
            return ""
        lang = re.sub(r"[^A-Za-z0-9_+#.-]+", "", language or "")
        if lang:
            return f"```{lang}\n{value}\n```"
        return f"```\n{value}\n```"

    def _has_media_child(self, node: Tag) -> bool:
        return (
            node.find(["img", "video", "source", "iframe"], recursive=True) is not None
        )

    def _collect_list_items(self, list_node: Tag) -> list[str]:
        items: list[str] = []
        for li in list_node.find_all("li", recursive=False):
            li_copy_soup = BeautifulSoup(str(li), "html.parser")
            li_copy = li_copy_soup.find("li")
            if li_copy is None:
                continue

            nested_blocks: list[str] = []
            for nested in li.find_all(["ul", "ol"], recursive=False):
                nested_text = self._format_list_text(
                    self._collect_list_items(nested),
                    ordered=(nested.name or "").lower() == "ol",
                )
                if nested_text:
                    nested_blocks.append(
                        "\n".join(f"  {line}" for line in nested_text.splitlines())
                    )

            for nested in li_copy.find_all(["ul", "ol"]):
                nested.decompose()

            base_text = self._node_text(li_copy, keep_newlines=True)
            combined = "\n".join(
                part for part in [base_text, *nested_blocks] if part
            ).strip()
            if combined:
                items.append(combined)
        return items

    def _extract_code_block(self, pre_tag: Tag) -> str:
        code_tag = pre_tag.find("code")
        source = code_tag if isinstance(code_tag, Tag) else pre_tag
        return html.unescape(source.get_text("", strip=False))

    def _extract_code_language(self, pre_tag: Tag) -> str | None:
        for tag in (pre_tag, pre_tag.find("code")):
            if not isinstance(tag, Tag):
                continue
            for key in ("data-language", "data-lang", "lang"):
                value = tag.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for item in self._iter_attr_strings(tag.get("class")):
                matched = re.search(
                    r"(?:lang|language)[-_]?([A-Za-z0-9_+#.-]+)",
                    item,
                    re.I,
                )
                if matched:
                    return matched.group(1)
        return None
