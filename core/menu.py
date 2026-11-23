"""QQ 官方机器人 Markdown 菜单：页面模型、键盘渲染与回调路由。

按钮统一定义为 :class:`MenuButton`，页面渲染为两部分：

- ``markdown()``：发送给用户看的 Markdown 正文；
- ``keyboard()``：官方内嵌键盘（回调数据经 AstrBot token 编码）。

发送优先走 ``QQActor`` 直连（可拿到消息 ID，便于翻页时撤回旧菜单），
不可用时退回 AstrBot 的 ``ActionRow`` / ``Button`` 组件。
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import ActionRow, Button, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.button_interaction import encode_button_callback

from .qq_api import QQActor

MAX_LABEL_CHARS = 10
MAX_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_ACTION_BYTES = 1024
MAX_TEXT_CHARS = 1800

MENU_TTL = 30 * 60
ACTION_TTL = 10 * 60
NAV_TTL = 3
MAX_ENTRIES = 2048
# 官方撤回限制 2 分钟，留出余量；只尝试撤回窗口内的旧菜单。
RECALL_TTL = 110
MAX_RECALL_IDS = 5

MENU_ACTION = "menu"

_STYLE_VALUE = {
    "default": 0,
    "primary": 1,
    "success": 1,
    "danger": 3,
}
_STYLE_PORTABLE = {
    "default": Button.Style.DEFAULT,
    "primary": Button.Style.PRIMARY,
    "success": Button.Style.SUCCESS,
    "danger": Button.Style.DANGER,
}

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")


@dataclass
class MenuButton:
    """一个菜单按钮。

    ``data`` 会随回调一并返回；``command`` 仅用于纯文本兜底提示。
    """

    label: str
    data: dict[str, Any] = field(default_factory=dict)
    style: str = "default"
    action_id: str = MENU_ACTION
    group: str | None = None
    confirm: str | None = None
    command: str | None = None
    id: str = ""


@dataclass
class MenuPage:
    """一屏菜单。``rows`` 中每行最多 5 个按钮，整页最多 5 行。"""

    id: str
    title: str
    body: str = ""
    rows: list[list[MenuButton]] = field(default_factory=list)
    note: str = ""

    def markdown(self) -> str:
        parts = [f"## {self.title.strip()}"] if self.title.strip() else []
        for block in (self.body, self.note):
            value = block.strip()
            if value:
                parts.append(value)
        return "\n\n".join(parts)[:MAX_TEXT_CHARS]

    def plain_text(self) -> str:
        text = _strip_markdown(self.markdown())
        hints = [
            f"{button.label}：{button.command}"
            for row in self.rows
            for button in row
            if button.command
        ]
        if hints:
            text = f"{text}\n\n也可以通过命令操作：\n" + "\n".join(hints)
        return text[:MAX_TEXT_CHARS]

    def _button_id(self, row_index: int, col_index: int, button: MenuButton) -> str:
        return button.id or f"m{row_index}{col_index}"

    def action_rows(self, owner: str = "") -> list[ActionRow]:
        rows: list[ActionRow] = []
        for row_index, row in enumerate(self.rows):
            buttons: list[Button] = []
            for col_index, button in enumerate(row):
                data = dict(button.data)
                data.setdefault("a", button.action_id)
                if owner:
                    data.setdefault("o", owner)
                buttons.append(
                    Button(
                        id=self._button_id(row_index, col_index, button),
                        label=button.label[:MAX_LABEL_CHARS],
                        style=_STYLE_PORTABLE.get(button.style, Button.Style.DEFAULT),
                        action=Button.CallbackAction(data=data),
                    )
                )
            if buttons:
                rows.append(
                    ActionRow(
                        buttons=buttons,
                        fallback_text="",
                        action_timeout=ACTION_TTL,
                    )
                )
        return rows

    def keyboard(self, owner: str = "") -> dict | None:
        if len(self.rows) > MAX_ROWS:
            raise ValueError("QQ 内嵌键盘最多 5 行")
        if not self.rows:
            return None

        ids: set[str] = set()
        rows: list[dict[str, Any]] = []
        expires_at = time.time() + ACTION_TTL
        for row_index, row in enumerate(self.rows):
            if len(row) > MAX_BUTTONS_PER_ROW:
                raise ValueError("QQ 内嵌键盘每行最多 5 个按钮")
            items: list[dict[str, Any]] = []
            for col_index, button in enumerate(row):
                if not button.label.strip():
                    raise ValueError("按钮文字不能为空")
                button_id = self._button_id(row_index, col_index, button)
                if button_id in ids:
                    raise ValueError(f"按钮 ID 重复: {button_id}")
                ids.add(button_id)

                data = dict(button.data)
                data.setdefault("a", button.action_id)
                if owner:
                    data.setdefault("o", owner)
                data.setdefault("n", secrets.token_hex(3))
                token = encode_button_callback(button_id, data, expires_at=expires_at)
                if len(token.encode("utf-8")) > MAX_ACTION_BYTES:
                    raise ValueError("按钮回调数据超过 1024 字节")

                label = button.label.strip()[:MAX_LABEL_CHARS]
                item: dict[str, Any] = {
                    "id": button_id,
                    "render_data": {
                        "label": label,
                        "visited_label": label,
                        "style": _STYLE_VALUE.get(button.style, 0),
                    },
                    "action": {
                        "type": 1,
                        "permission": {"type": 2},
                        "data": token,
                    },
                }
                if button.group:
                    item["group_id"] = button.group
                if button.confirm:
                    item["action"]["modal"] = {
                        "content": button.confirm[:40],
                        "confirm_text": "确认",
                        "cancel_text": "取消",
                    }
                items.append(item)
            rows.append({"buttons": items})
        return {"content": {"rows": rows}}


@dataclass
class PreferenceSection:
    platform: str
    display: str
    current: str
    options: tuple[str, ...]
    source: str = ""


class MenuStore:
    """菜单会话：记录最近一屏消息 ID，并对点击做短时去重。"""

    def __init__(
        self,
        *,
        menu_ttl: float = MENU_TTL,
        action_ttl: float = ACTION_TTL,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.menu_ttl = menu_ttl
        self.action_ttl = action_ttl
        self.max_entries = max_entries
        self._last: dict[tuple[str, str], list[tuple[str, float]]] = {}
        self._actions: dict[tuple[Any, ...], float] = {}

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _prune(self) -> None:
        now = self._now()
        for key, items in list(self._last.items()):
            fresh = [(mid, ts) for mid, ts in items if now - ts < self.menu_ttl]
            if fresh:
                self._last[key] = fresh
            else:
                self._last.pop(key, None)
        for key, deadline in list(self._actions.items()):
            if deadline <= now:
                self._actions.pop(key, None)
        while len(self._actions) > self.max_entries:
            oldest = min(self._actions, key=lambda key: self._actions[key])
            self._actions.pop(oldest, None)
        while len(self._last) > self.max_entries:
            oldest = min(self._last, key=lambda k: self._last[k][-1][1])
            self._last.pop(oldest, None)

    def claim_action(self, key: tuple[Any, ...], *, ttl: float = ACTION_TTL) -> bool:
        self._prune()
        now = self._now()
        deadline = self._actions.get(key)
        if deadline is not None and deadline > now:
            return False
        self._actions[key] = now + max(0.0, ttl)
        return True

    def release_action(self, key: tuple[Any, ...]) -> None:
        self._actions.pop(key, None)

    def set_last(self, umo: str, user: str, message_id: str) -> None:
        self._prune()
        if not message_id:
            return
        items = self._last.setdefault((str(umo), str(user)), [])
        items.append((str(message_id), self._now()))
        if len(items) > MAX_RECALL_IDS:
            del items[:-MAX_RECALL_IDS]

    def pop_last(self, umo: str, user: str) -> str | None:
        self._prune()
        key = (str(umo), str(user))
        items = self._last.get(key)
        if not items:
            return None
        message_id, _ = items.pop()
        if not items:
            self._last.pop(key, None)
        return message_id

    def get_last(self, umo: str, user: str) -> str | None:
        self._prune()
        items = self._last.get((str(umo), str(user)))
        return items[-1][0] if items else None

    def recallable(self, umo: str, user: str, *, ttl: float = RECALL_TTL) -> list[str]:
        """撤回窗口内的旧菜单消息 ID（旧到新）。"""
        self._prune()
        now = self._now()
        items = self._last.get((str(umo), str(user))) or []
        return [mid for mid, ts in items if now - ts < ttl]

    def drop_last(self, umo: str, user: str, message_id: str) -> None:
        key = (str(umo), str(user))
        items = self._last.get(key)
        if not items:
            return
        remaining = [(mid, ts) for mid, ts in items if mid != message_id]
        if remaining:
            self._last[key] = remaining
        else:
            self._last.pop(key, None)

    def clear(self) -> None:
        self._last.clear()
        self._actions.clear()


async def send_menu(
    event: AstrMessageEvent,
    page: MenuPage,
    store: MenuStore,
    *,
    owner: str = "",
    recall: bool = True,
) -> bool:
    """发送一屏菜单，返回是否至少成功发送一次。"""
    if not owner:
        try:
            owner = str(event.get_sender_id() or "")
        except Exception:
            owner = ""
    try:
        umo = str(event.unified_msg_origin)
    except Exception:
        umo = ""

    markdown = page.markdown()
    try:
        keyboard = page.keyboard(owner)
    except ValueError as exc:
        logger.warning(f"[菜单] 键盘构造失败，回退纯文本: {exc}")
        keyboard = None
        markdown = page.plain_text()

    actor = QQActor(event)
    if actor.available:
        previous = store.recallable(umo, owner) if recall else []
        payload: dict[str, Any] = {
            "msg_type": 2,
            "markdown": {"content": markdown or page.plain_text()},
        }
        if keyboard:
            payload["keyboard"] = keyboard
        try:
            response = await actor.send_payload(payload)
        except Exception as exc:
            logger.warning(f"[菜单] 发送失败，回退适配器: {exc}")
            response = None
        if response is not None:
            message_id = (
                str(response.get("id"))
                if isinstance(response, dict) and response.get("id")
                else ""
            )
            if message_id:
                store.set_last(umo, owner, message_id)
                for old in previous:
                    if old == message_id:
                        store.drop_last(umo, owner, old)
                        continue
                    if await actor.recall(old):
                        store.drop_last(umo, owner, old)
            return True

    rows = page.action_rows(owner)
    chain = [Plain(markdown or page.plain_text()), *rows]
    try:
        result = event.chain_result(chain)
        result.use_markdown(True)
        await event.send(result)
        return True
    except Exception as exc:
        logger.warning(f"[菜单] Markdown 发送失败，回退纯文本: {exc}")
    try:
        if rows:
            result = event.chain_result([Plain(page.plain_text()), *rows])
            result.use_markdown(False)
            await event.send(result)
        else:
            await event.send(event.plain_result(page.plain_text()))
        return True
    except Exception as exc:
        logger.error(f"[菜单] 纯文本回退也失败: {exc}")
    return False


def menu_key(*parts: Any) -> tuple[str, ...]:
    return tuple(str(part or "") for part in parts)


def _strip_markdown(text: str) -> str:
    text = _MD_LINK.sub(lambda match: match.group(1) or match.group(2), text)
    text = _MD_BOLD.sub(lambda match: match.group(1) or match.group(2), text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("`", "")
    return text.strip()


_VALUE_LABELS = {
    "best": "最好",
    "worst": "最差",
    "standard": "标准",
    "exhigh": "极高",
    "lossless": "无损",
    "hires": "Hi-Res",
}


def _display_value(value: str) -> str:
    text = value.lstrip("_")
    return _VALUE_LABELS.get(text, text)


def _value_with_source(current: str, source: str) -> str:
    value = _display_value(current)
    return f"{value}（{source}）" if source else value


def _button(
    label: str,
    payload: dict[str, Any],
    *,
    style: str = "default",
    command: str | None = None,
    confirm: str | None = None,
    group: str | None = None,
) -> MenuButton:
    data = {key: value for key, value in payload.items() if value not in (None, "")}
    return MenuButton(
        label=label,
        data=data,
        style=style,
        command=command,
        confirm=confirm,
        group=group,
    )


def _nav_row() -> list[MenuButton]:
    return [
        _button("返回", {"p": "home"}, command="/解析设置"),
        _button("帮助", {"p": "help"}, command="/解析帮助"),
    ]


def build_home_page(
    *,
    enabled: bool,
    quality_menu: bool,
    platforms: int,
    is_admin: bool,
) -> MenuPage:
    body = "\n".join(
        [
            f"- 解析：{'已开启' if enabled else '已关闭'}",
            f"- 画质菜单：{'开' if quality_menu else '关'}",
            f"- 已启用平台：{platforms} 个",
            "",
            "点击下方按钮，或直接使用 `/画质`、`/音质`、`/原图`；"
            "加 `查看` 查看当前设置，`默认` 清除设置。",
        ]
    )
    rows: list[list[MenuButton]] = [
        [
            _button(
                "画质设置",
                {"p": "quality", "k": "video"},
                style="primary",
                command="/画质",
            ),
            _button(
                "音质设置",
                {"p": "quality", "k": "audio"},
                style="primary",
                command="/音质",
            ),
            _button("原图设置", {"p": "image"}, command="/原图"),
        ],
        [
            _button(
                "关闭菜单" if quality_menu else "开启菜单",
                {"p": "toggle_menu"},
                command=f"/画质菜单 {'关' if quality_menu else '开'}",
            ),
            _button("帮助", {"p": "help"}, command="/解析帮助"),
        ],
    ]
    if is_admin:
        rows[1].append(
            _button(
                "关闭解析" if enabled else "开启解析",
                {"p": "toggle_parse"},
                style="danger" if enabled else "success",
                command="/关闭解析" if enabled else "/开启解析",
            )
        )
    return MenuPage(
        id="home",
        title="解析设置",
        body=body,
        rows=rows,
    )


def build_preference_overview(
    kind: str, sections: list[PreferenceSection], page: int = 0
) -> MenuPage:
    """偏好总览：按平台进入各自的档位页，避免多平台档位挤占按钮行。"""
    label = "画质" if kind == "video" else "音质"
    if sections:
        lines = [
            f"- {section.display}："
            f"{_value_with_source(section.current, section.source)}"
            for section in sections
        ]
    else:
        lines = ["当前没有启用支持该偏好的平台。"]

    per_page = (MAX_ROWS - 1) * MAX_BUTTONS_PER_ROW
    pages = max(1, (len(sections) + per_page - 1) // per_page)
    page = min(max(page, 0), pages - 1)
    chunk = sections[page * per_page : (page + 1) * per_page]

    rows: list[list[MenuButton]] = []
    for start in range(0, len(chunk), MAX_BUTTONS_PER_ROW):
        buttons = [
            _button(
                section.display,
                {"p": "quality", "k": kind, "plat": section.platform},
                style="primary",
                command=f"/{label}",
            )
            for section in chunk[start : start + MAX_BUTTONS_PER_ROW]
        ]
        if buttons:
            rows.append(buttons)

    nav: list[MenuButton] = []
    if sections:
        nav.append(
            _button(
                "清除设置",
                {"p": "clear", "k": kind},
                style="danger",
                command=f"/{label} 默认",
            )
        )
    if page > 0:
        nav.append(_button("上一页", {"p": "quality_page", "k": kind, "pg": page - 1}))
    if page < pages - 1:
        nav.append(_button("下一页", {"p": "quality_page", "k": kind, "pg": page + 1}))
    nav.extend(_nav_row())
    rows.append(nav)

    body = "\n".join(lines)
    if sections:
        body += f"\n\n选择平台进入档位设置；也可用 `/{label} <档位>`。"
    return MenuPage(
        id=f"pref:{kind}",
        title=f"{label}偏好",
        body=body,
        rows=rows[:MAX_ROWS],
    )


def build_platform_page(kind: str, section: PreferenceSection) -> MenuPage:
    """单个平台的档位页，独占整页按钮空间。"""
    label = "画质" if kind == "video" else "音质"
    options = list(section.options)
    rows: list[list[MenuButton]] = []
    for start in range(0, len(options), MAX_BUTTONS_PER_ROW):
        if len(rows) >= MAX_ROWS - 1:
            break
        buttons = [
            _button(
                _display_value(value),
                {"p": "set", "k": kind, "plat": section.platform, "v": value},
                style="primary" if value == section.current else "default",
                command=f"/{label} {value}",
                group=f"{kind}:{section.platform}",
            )
            for value in options[start : start + MAX_BUTTONS_PER_ROW]
        ]
        if buttons:
            rows.append(buttons)

    rows.append(
        [
            _button(
                "清除设置",
                {"p": "clear", "k": kind},
                style="danger",
                command=f"/{label} 默认",
            ),
            _button(
                "返回列表",
                {"p": "quality_page", "k": kind, "pg": 0},
                command=f"/{label}",
            ),
            _button("帮助", {"p": "help"}, command="/解析帮助"),
        ]
    )
    return MenuPage(
        id=f"pref:{kind}:{section.platform}",
        title=f"{label}偏好·{section.display}",
        body=(
            f"- 平台：{section.display}\n"
            f"- 当前：{_value_with_source(section.current, section.source)}\n\n"
            f"点击档位按钮设置；档位较多时可用 `/{label} <档位>`。"
        ),
        rows=rows[:MAX_ROWS],
    )


def build_image_page(current: str | None) -> MenuPage:
    label = {"original": "原图", "compressed": "压缩"}.get(current or "", "未设置")
    rows = [
        [
            _button(
                "原图",
                {"p": "image", "v": "original"},
                style="primary" if current == "original" else "default",
                command="/原图 开",
            ),
            _button(
                "压缩",
                {"p": "image", "v": "compressed"},
                style="primary" if current == "compressed" else "default",
                command="/原图 关",
            ),
            _button(
                "默认",
                {"p": "image", "v": "default"},
                command="/原图 默认",
            ),
        ],
        _nav_row(),
    ]
    return MenuPage(
        id="image",
        title="图片偏好",
        body=f"当前：**{label}**",
        rows=rows,
    )


def build_help_page() -> MenuPage:
    body = "\n".join(
        [
            "- `/解析设置`：打开本设置菜单",
            "- `/解析帮助`（`/帮助`）：查看本帮助",
            "- `/画质 [档位]`：查看或设置视频画质偏好；`默认` 清除",
            "- `/音质 [档位]`：查看或设置音频音质偏好；`默认` 清除",
            "- `/原图 开|关|默认`：设置或清除图片偏好",
            "- `/画质菜单 开|关|状态`：解析后是否追加画质/音质档位菜单",
            "- `/解析登录`：管理员登录 B站 / 网易云",
            "- `/开启解析`、`/关闭解析`：管理员开关当前会话解析",
            "- `/撤回 [消息ID]`：撤回机器人最近发送的消息",
        ]
    )
    return MenuPage(
        id="help",
        title="命令帮助",
        body=body,
        rows=[_nav_row()],
    )
