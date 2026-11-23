"""平台能力探测（QQ 官方机器人专用）。

仅面向 QQ 官方机器人的 QQ 群 / 私聊（C2C）场景。
频道（guild）不在支持范围内。
"""

from __future__ import annotations

from astrbot.core.platform.astr_message_event import AstrMessageEvent

OFFICIAL_PLATFORMS: frozenset[str] = frozenset({"qq_official", "qq_official_webhook"})

# 官方文档：单聊被动消息每条可回复 4 次；群聊 5 次（群主/管理员 50 次）。
PRIVATE_REPLY_BUDGET = 4
GROUP_REPLY_BUDGET = 5

# 单条消息最多携带一个富媒体。
MAX_MEDIA_PER_MESSAGE = 1

# 卡片内最多展示的图片数量。
CARD_MAX_IMAGES = 9


def get_platform_name(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_platform_name() or "")
    except Exception:
        return ""


def is_official(event: AstrMessageEvent) -> bool:
    """是否运行在 QQ 官方机器人适配器上。"""
    return get_platform_name(event) in OFFICIAL_PLATFORMS


def is_guild_event(event: AstrMessageEvent) -> bool:
    """是否为频道（guild）事件，频道不在本插件支持范围。

    官方适配器会把 botpy 消息类包装为 ``PatchedMessage`` /
    ``PatchedDirectMessage``，因此按类名后缀判断，兼容原始类名与包装类名。

    按钮交互事件的原始对象是 ``Interaction``，群聊 / 单聊回调也会走这里，
    必须先按 ``scene`` 区分，不能把 ``Interaction`` 一律当作频道。
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    scene = getattr(raw, "scene", None)
    if scene is not None:
        return scene == "guild"

    name = type(raw).__name__
    if "GroupMessage" in name or "C2CMessage" in name:
        return False
    if name == "Interaction":
        return False
    return name.endswith("Message")


def reply_budget(event: AstrMessageEvent, override: int = 0) -> int:
    """本条被动消息允许的回复次数。"""
    if override and override > 0:
        return int(override)
    try:
        if event.is_private_chat():
            return PRIVATE_REPLY_BUDGET
    except Exception:
        pass
    return GROUP_REPLY_BUDGET
