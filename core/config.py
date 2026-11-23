from __future__ import annotations

import json
import os
import zoneinfo
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)

_PLUGIN_NAME = "astrbot_plugin_parser_qqofficial"

# 旧版扁平配置项 -> 新版分组。插件模块会在 AstrBot 读取配置前执行迁移，
# 否则 AstrBot 会按新 schema 删除这些旧顶层键，导致用户设置丢失。
_LEGACY_CONFIG_GROUPS: dict[str, str] = {
    "whitelist": "basic",
    "blacklist": "basic",
    "require_at_in_group": "basic",
    "enable_reply_parse": "basic",
    "debounce_interval": "basic",
    "source_max_size": "media",
    "source_max_minute": "media",
    "audio_to_file": "media",
    "single_heavy_render_card": "media",
    "forward_threshold": "media",
    "show_download_fail_tip": "media",
    "download_timeout": "media",
    "download_retry_times": "media",
    "common_timeout": "media",
    "qq_gallery_render_card": "qq_send",
    "qq_card_max_images": "qq_send",
    "qq_reply_budget_override": "qq_send",
    "qq_image_format": "qq_send",
    "qq_native_card": "qq_send",
    "qq_quote_reply": "qq_send",
    "qq_keyboard": "qq_send",
    "qq_keyboard_open_link": "qq_send",
    "qq_keyboard_reparse": "qq_send",
    "qq_markdown": "qq_send",
    "qq_material_upload": "qq_send",
    "qq_overflow_mode": "qq_send",
    "qq_overflow_limit": "qq_send",
    "proxy": "network",
    "clean_cron": "network",
}


def _migrate_legacy_config() -> None:
    """把旧版顶层配置项迁移到分组，保留用户已有设置。"""
    path = Path(get_astrbot_config_path()) / f"{_PLUGIN_NAME}_config.json"
    if not path.is_file():
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.warning(f"[config] 读取旧配置失败，跳过迁移: {exc}")
        return

    if not isinstance(data, dict):
        return

    changed = False

    # 兼容曾经被归入 parsers 分组的过渡版本配置。
    parsers_group = data.get("parsers")
    if isinstance(parsers_group, dict) and "parsers_template" in parsers_group:
        data.setdefault("parsers_template", parsers_group.pop("parsers_template"))
        if not parsers_group:
            data.pop("parsers")
        changed = True

    legacy_keys = [key for key in _LEGACY_CONFIG_GROUPS if key in data]
    for key in legacy_keys:
        group_name = _LEGACY_CONFIG_GROUPS[key]
        group = data.get(group_name)
        if not isinstance(group, dict):
            group = {}
            data[group_name] = group
        group.setdefault(key, data.pop(key))
        changed = True

    if not changed:
        return

    try:
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except Exception as exc:
        logger.warning(f"[config] 写入迁移后的配置失败: {exc}")
        return

    logger.info("[config] 已将旧版扁平配置迁移到分组结构")


_migrate_legacy_config()


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    cookies: str | None
    audio_quality: str | None
    audio_quality_fallback: str | None
    embed_metadata: bool | None
    embed_lyrics: bool | None
    show_body_text: bool | None
    use_original_image: bool | None
    mixed_layout: bool | None
    show_work_content: bool | None
    text_max_length: int | None
    video_send_mode: str | None
    video_codec_list: list | None
    video_quality: str | None
    video_codec: str | None
    video_format: str | None
    block_pcdn: bool | None
    metube_url: str | None
    wait_timeout: int | None
    delete_after_fetch: bool | None
    nsfw: str | None
    max_page: int | None
    image_send_mode: str | None
    send_blue_links: bool | None
    qzone_credential_source: str | None
    snowluma_http_url: str | None
    snowluma_access_token: str | None
    snowluma_credential_cache_seconds: int | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    allcpp: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    metube: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem
    shipinhao: ParserItem
    qzone: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class BasicConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]
    require_at_in_group: bool
    enable_reply_parse: bool
    debounce_interval: int


class MediaConfig(ConfigNode):
    source_max_size: int
    source_max_minute: int
    audio_to_file: bool
    single_heavy_render_card: bool
    forward_threshold: int
    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int


class QQSendConfig(ConfigNode):
    qq_gallery_render_card: bool
    qq_card_max_images: int
    qq_reply_budget_override: int
    qq_image_format: str
    qq_native_card: bool
    qq_quote_reply: bool
    qq_keyboard: bool
    qq_keyboard_open_link: bool
    qq_keyboard_reparse: bool
    qq_markdown: str
    qq_material_upload: bool
    qq_overflow_mode: str
    qq_overflow_limit: int


class NetworkConfig(ConfigNode):
    proxy: str | None
    clean_cron: str


class PluginConfig(ConfigNode):
    basic: BasicConfig
    media: MediaConfig
    qq_send: QQSendConfig
    network: NetworkConfig
    parsers_template: list[dict[str, Any]]

    _plugin_name = _PLUGIN_NAME

    # 保持原有平铺访问方式，内部读写对应的分组字段。
    _ALIASES = {key: (group, key) for key, group in _LEGACY_CONFIG_GROUPS.items()}

    def __getattr__(self, key: str) -> Any:
        alias = type(self)._ALIASES.get(key)
        if alias is not None:
            group, field = alias
            return getattr(getattr(self, group), field)
        return super().__getattr__(key)

    def __setattr__(self, key: str, value: Any) -> None:
        alias = type(self)._ALIASES.get(key)
        if alias is not None:
            group, field = alias
            setattr(getattr(self, group), field, value)
            return
        super().__setattr__(key, value)

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.default_template_file = self.plugin_dir / "default_template.json"

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
