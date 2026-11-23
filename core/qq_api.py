"""QQ 官方机器人直连 API（仅群聊 / 单聊）。

AstrBot 的 ``qq_official`` 适配器未封装以下能力，这里直接调用官方接口：

- 图文卡片消息（``msg_type=8``）
- 素材管理（``qqbot-media://``）

发送时会自动填充 ``msg_id`` / ``event_id`` / ``msg_seq``，避免被官方去重
（相同 ``msg_id + msg_seq`` 重复发送会失败）。
"""

from __future__ import annotations

import time
from typing import Any

_QQRoute: Any = None
_SEQ_STATE: dict[str, int] = {}
_SEQ_TS: dict[str, float] = {}
_SEQ_TTL = 60 * 60 * 2


def make_route(method: str, path: str, **params: Any):
    """构造指向 ``api.bot.qq.com`` 的 botpy Route。"""
    global _QQRoute
    if _QQRoute is None:
        from botpy.http import Route  # type: ignore

        class _QQRouteImpl(Route):  # type: ignore[misc, valid-type]
            DOMAIN = "api.bot.qq.com"

        _QQRoute = _QQRouteImpl
    return _QQRoute(method, path, **params)


def _raw_message(event: Any) -> Any:
    return getattr(getattr(event, "message_obj", None), "raw_message", None)


def get_scene(event: Any) -> str:
    """返回事件场景：group / c2c / friend / guild，未知返回空串。"""
    raw = _raw_message(event)
    scene = getattr(raw, "scene", None)
    if scene in ("group", "c2c", "guild", "friend"):
        return str(scene)

    name = type(raw).__name__
    if "GroupMessage" in name:
        return "group"
    if "C2CMessage" in name:
        return "c2c"
    if "DirectMessage" in name:
        return "friend"

    try:
        from astrbot.core.platform.message_type import MessageType

        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            return "group"
        return "c2c"
    except Exception:
        return ""


def resolve_target(event: Any) -> tuple[str, str] | None:
    """解析直发目标，返回 ``(kind, id)``；kind 为 ``group`` 或 ``c2c``。"""
    obj = getattr(event, "message_obj", None)
    scene = get_scene(event)

    if scene == "group":
        gid = getattr(obj, "group_id", None)
        return ("group", str(gid)) if gid else None

    if scene in ("c2c", "friend"):
        sender = getattr(obj, "sender", None)
        uid = getattr(sender, "user_id", None) or getattr(obj, "session_id", None)
        return ("c2c", str(uid)) if uid else None

    return None


def extract_ref_idx(raw: Any) -> str:
    """从收到的消息里取自身引用 ID（REFIDX_... / msg_idx）。"""
    data = getattr(raw, "raw_data", None)
    if not isinstance(data, dict):
        return ""
    scene = data.get("message_scene")
    if not isinstance(scene, dict):
        return ""
    for item in scene.get("ext") or []:
        text = str(item)
        if text.startswith("msg_idx="):
            value = text[len("msg_idx=") :].strip()
            if value:
                return value
    return ""


def _next_seq(key: str) -> int:
    now = time.time()
    for k, ts in list(_SEQ_TS.items()):
        if now - ts > _SEQ_TTL:
            _SEQ_TS.pop(k, None)
            _SEQ_STATE.pop(k, None)

    # AstrBot 适配器使用 1~10000 的随机 msg_seq，这里从 10001 起递增，
    # 避免与适配器的被动发送撞号（相同 msg_id + msg_seq 会被去重）。
    if key not in _SEQ_STATE:
        _SEQ_STATE[key] = 10000

    value = _SEQ_STATE[key] + 1
    if value > 100000:
        value = 10001
    _SEQ_STATE[key] = value
    _SEQ_TS[key] = now
    return value


def valid_https_url(url: Any, *, limit: int = 1024) -> str | None:
    if not isinstance(url, str):
        return None
    value = url.strip()
    if not value.startswith("https://") or len(value) > limit:
        return None
    return value


def valid_url(url: Any, *, limit: int = 1024) -> str | None:
    if not isinstance(url, str):
        return None
    value = url.strip()
    if not (value.startswith("http://") or value.startswith("https://")):
        return None
    if len(value) > limit:
        return None
    return value


def build_keyboard(
    url: str,
    *,
    open_link: bool = True,
    reparse: bool = True,
) -> dict | None:
    """构造官方内嵌键盘（用于直发 msg_type=8 卡片时携带）。

    返回结构与该适配器 ``_parse_keyboard`` 一致。
    """
    if not valid_url(url):
        return None

    buttons: list[dict[str, Any]] = []
    if open_link:
        buttons.append(
            {
                "id": "open_link",
                "render_data": {
                    "label": "打开原链接",
                    "visited_label": "打开原链接",
                    "style": 1,
                },
                "action": {
                    "type": 0,
                    "permission": {"type": 2},
                    "data": url,
                },
            }
        )
    if reparse:
        try:
            from astrbot.core.platform.button_interaction import (
                encode_button_callback,
            )

            token = encode_button_callback("reparse", {"a": "reparse", "u": url})
            buttons.append(
                {
                    "id": "reparse",
                    "render_data": {
                        "label": "重新解析",
                        "visited_label": "重新解析",
                        "style": 1,
                    },
                    "action": {
                        "type": 1,
                        "permission": {"type": 2},
                        "data": token,
                    },
                }
            )
        except Exception:
            pass

    if not buttons:
        return None
    return {"content": {"rows": [{"buttons": buttons}]}}


class QQActor:
    """绑定到一条事件的最小直连发送器。"""

    def __init__(self, event: Any, session: Any = None) -> None:
        self.event = event
        self.bot = getattr(event, "bot", None)
        self.session = session
        self.raw = _raw_message(event)
        self.target = resolve_target(event)
        self.msg_id = str(
            getattr(getattr(event, "message_obj", None), "message_id", "") or ""
        )

    @property
    def available(self) -> bool:
        return self.bot is not None and self.target is not None

    def _is_interaction(self) -> bool:
        return type(self.raw).__name__ == "Interaction"

    def _seq(self) -> int:
        kind, tid = self.target  # type: ignore[misc]
        return _next_seq(f"{kind}:{tid}:{self.msg_id}")

    def _message_route(self) -> tuple[Any, tuple[str, str]]:
        kind, tid = self.target  # type: ignore[misc]
        if kind == "group":
            route = make_route(
                "POST",
                "/v2/groups/{group_openid}/messages",
                group_openid=tid,
            )
        else:
            route = make_route("POST", "/v2/users/{openid}/messages", openid=tid)
        return route, (kind, tid)

    async def send_payload(
        self, payload: dict[str, Any], *, proactive: bool = False
    ) -> dict | None:
        """直发一条消息，返回官方响应（失败返回 None）。

        ``proactive=True`` 时不携带 ``msg_id`` / ``event_id``，
        即作为主动消息发送（受主动消息频控与用户开关限制）。
        """
        if not self.available:
            return None

        route, _ = self._message_route()
        body = dict(payload)

        if proactive:
            body.pop("msg_id", None)
            body.pop("event_id", None)
            body.pop("msg_seq", None)
        elif self._is_interaction():
            event_id = getattr(self.raw, "event_id", None)
            if event_id:
                body.setdefault("event_id", str(event_id))
        else:
            body.setdefault("msg_seq", self._seq())
            if self.msg_id:
                body.setdefault("msg_id", self.msg_id)

        return await self.bot.api._http.request(route, json=body)

    async def recall(self, message_id: str) -> bool:
        """撤回机器人自己发送的消息（官方限制 2 分钟内）。"""
        if not self.available or not message_id:
            return False

        kind, tid = self.target  # type: ignore[misc]
        if kind == "group":
            route = make_route(
                "DELETE",
                "/v2/groups/{group_openid}/messages/{message_id}",
                group_openid=tid,
                message_id=message_id,
            )
        else:
            route = make_route(
                "DELETE",
                "/v2/users/{openid}/messages/{message_id}",
                openid=tid,
                message_id=message_id,
            )

        try:
            await self.bot.api._http.request(route)
            return True
        except Exception:
            return False
