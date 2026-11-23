"""QQ 空间分享页中的 JS 对象/JSON 提取工具。

从 ``qzone.py`` 拆出：这些函数只负责从 HTML/JS 源码中定位并解析
``FrontPage`` 对象，与解析器实例状态无关。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from bs4 import BeautifulSoup


@dataclass(slots=True)
class _QZoneVideo:
    url: str | None = None
    cover_url: str | None = None
    duration: float = 0
    media_indices: set[str] = field(default_factory=set)
    unavailable: bool = False
    topic_id: str | None = None
    pic_key: str | None = None


@dataclass(slots=True)
class _QZoneImageRef:
    index: str
    url: str
    fallback_url: str | None = None
    topic_id: str | None = None
    pic_key: str | None = None


def _balanced_object(source: str, start: int) -> str | None:
    if start < 0 or start >= len(source) or source[start] != "{":
        return None

    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    for index in range(start, len(source)):
        character = source[index]
        next_character = source[index + 1 : index + 2]
        if line_comment:
            if character in "\r\n":
                line_comment = False
            continue
        if block_comment:
            if character == "*" and next_character == "/":
                block_comment = False
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue

        if character in {'"', "'", "`"}:
            quote = character
        elif character == "/" and next_character == "/":
            line_comment = True
        elif character == "/" and next_character == "*":
            block_comment = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
            if depth < 0:
                return None
    return None


def _frontpage_object_starts(source: str):
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(source):
        character = source[index]
        next_character = source[index + 1 : index + 2]
        if line_comment:
            if character in "\r\n":
                line_comment = False
        elif block_comment:
            if character == "*" and next_character == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
        elif character in {'"', "'", "`"}:
            quote = character
        elif character == "/" and next_character == "/":
            line_comment = True
            index += 1
        elif character == "/" and next_character == "*":
            block_comment = True
            index += 1
        elif source.startswith("FrontPage", index):
            before = source[index - 1] if index else ""
            end = index + len("FrontPage")
            if not before or not (before.isalnum() or before in "_$"):
                cursor = end
                while cursor < len(source) and source[cursor].isspace():
                    cursor += 1
                if cursor < len(source) and source[cursor] == "=":
                    cursor += 1
                    while cursor < len(source) and source[cursor].isspace():
                        cursor += 1
                    if cursor < len(source) and source[cursor] == "{":
                        yield cursor
        index += 1


def _extract_frontpage_data(html: str) -> dict[str, object] | None:
    scripts = BeautifulSoup(html, "html.parser").find_all("script")
    sources = [script.get_text() for script in scripts] if scripts else [html]
    for source in sources:
        if (data := _extract_frontpage_source(source)) is not None:
            return data
    return None


def _extract_frontpage_source(source: str) -> dict[str, object] | None:
    for outer_start in _frontpage_object_starts(source):
        outer = _balanced_object(source, outer_start)
        if outer is not None and (data := _extract_top_level_data(outer)) is not None:
            return data
    return None


def _extract_top_level_data(outer: str) -> dict[str, object] | None:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(outer):
        character = outer[index]
        next_character = outer[index + 1 : index + 2]
        if line_comment:
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and next_character == "/":
                block_comment = False
                index += 1
            index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue

        key_end: int | None = None
        if depth == 1 and outer.startswith(('"data"', "'data'"), index):
            key_end = index + len('"data"')
        elif depth == 1 and outer.startswith("data", index):
            before = outer[index - 1] if index else ""
            after = outer[index + 4 : index + 5]
            if not (before.isalnum() or before in "_$") and not (
                after.isalnum() or after in "_$"
            ):
                key_end = index + len("data")

        if key_end is not None:
            value_start = key_end
            while value_start < len(outer) and outer[value_start].isspace():
                value_start += 1
            if value_start < len(outer) and outer[value_start] == ":":
                value_start += 1
                while value_start < len(outer) and outer[value_start].isspace():
                    value_start += 1
                value = _balanced_object(outer, value_start)
                if value is None:
                    return None
                try:
                    decoded = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return None
                return decoded if isinstance(decoded, dict) else None

        if character in {'"', "'", "`"}:
            quote = character
        elif character == "/" and next_character == "/":
            line_comment = True
            index += 1
        elif character == "/" and next_character == "*":
            block_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        index += 1
    return None
