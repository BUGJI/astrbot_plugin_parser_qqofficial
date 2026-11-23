# main.py

import asyncio
import re

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType

from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.data import AudioContent, VideoContent
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.login import (
    BilibiliLoginProvider,
    LoginQRCode,
    LoginRegistry,
    NeteaseLoginProvider,
)
from .core.menu import (
    NAV_TTL,
    MenuStore,
    PreferenceSection,
    build_help_page,
    build_home_page,
    build_image_page,
    build_platform_page,
    build_preference_overview,
    menu_key,
    send_menu,
)
from .core.parsers import BaseParser, NCMParser
from .core.parsers.base import QualityRequest, quality_context
from .core.platform import is_guild_event
from .core.preferences import (
    IMAGE_VALUES,
    QUALITY_KINDS,
    PreferenceStore,
    resolve_preference,
)
from .core.qq_api import QQActor
from .core.quality import QualityMenuRegistry
from .core.render import Renderer
from .core.sender import MessageSender
from .core.utils import (
    extract_card_url,
    extract_json_url,
    extract_qqbot_mentions,
)

LOGIN_COMMAND = "解析登录"
LOGIN_HELP_ALIASES = {"帮助", "help", "用法", "-h", "--help"}
LOGIN_STATUS_ALIASES = {"状态", "status"}


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 画质菜单去重
        self.menu_registry = QualityMenuRegistry()
        # 通用菜单会话（最近一屏消息 + 点击去重）
        self.menu_store = MenuStore()
        # 画质菜单开关缓存
        self._quality_pref_cache: dict[str, bool] = {}
        # 用户媒体偏好（画质/音质/原图）
        self.preferences = PreferenceStore(self.cfg.data_dir / "preferences.json")
        # 消息发送器
        self.sender = MessageSender(
            self.cfg,
            self.renderer,
            self.downloader,
            menu_registry=self.menu_registry,
            menu_store=self.menu_store,
        )
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 平台名 -> Parser 实例
        self.parsers: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()
        # 清理菜单状态
        self.menu_registry.clear()
        self.menu_store.clear()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        self.parser_map.clear()
        self.parsers.clear()
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())
        registered_platforms = {cls.platform.name for cls in all_subclass}

        # 配置里启用、但没有对应实现（未导入或已移除）的平台不再被静默忽略
        missing_platforms = enabled_platforms - registered_platforms
        if missing_platforms:
            logger.warning(
                "[parser] 以下平台已启用但没有对应实现（未导入或已移除），已跳过: "
                f"{'、'.join(sorted(missing_platforms))}"
            )

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        keyword_owner: dict[str, str] = {}
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)
            self.parsers[platform_name] = parser

            # 关键词 → parser（同关键词后注册者覆盖，需显式告警）
            for keyword, _ in cls._key_patterns:
                previous = keyword_owner.get(keyword)
                if previous is not None and previous != platform_name:
                    logger.warning(
                        f"[parser] 关键词 {keyword!r} 同时被 {previous} 与 "
                        f"{platform_name} 注册，实际使用后者（请检查是否重复）"
                    )
                keyword_owner[keyword] = platform_name
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 频道（guild）不在支持范围
        if is_guild_event(event):
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        text = event.message_str
        plain_text = "".join(
            seg.text for seg in chain if isinstance(seg, Plain) and seg.text
        )
        combined_text = "\n".join(part for part in (text, plain_text) if part)

        # 指定机制：专门@其他bot的消息不解析
        self_id = event.get_self_id()
        mentioned_ids: set[str] = set()
        for seg in chain:
            if isinstance(seg, At) and seg.qq is not None:
                mentioned_ids.add(str(seg.qq))
        mentioned_ids |= extract_qqbot_mentions(combined_text)
        # 官方适配器会用 "qq_official" 作为自身占位标记，不能当作真实 @ 目标
        mentioned_ids.discard("qq_official")
        mentioned_ids.discard("unknown_selfid")

        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        # 官方群聊默认必须 @ 机器人
        if self.cfg.require_at_in_group and is_group and self_id not in mentioned_ids:
            return
        # 群聊里 @ 了其他机器人却没有 @ 本机器人时不解析
        if is_group and mentioned_ids and self_id not in mentioned_ids:
            return

        # 卡片解析：优先读取卡片文本 / 原始 ark_data 中的跳转链接
        raw_message = getattr(event.message_obj, "raw_message", None)
        raw_data = getattr(raw_message, "raw_data", None)
        raw_type = getattr(raw_message, "message_type", None)
        is_card = str(raw_type) == "3" or "卡片消息" in combined_text
        card_url = extract_card_url(combined_text, raw_data) if is_card else None
        if card_url:
            logger.debug(f"解析卡片链接: {card_url}")
            text = card_url
        else:
            # 兼容 Json 卡片组件（部分平台）
            json_url: str | None = None
            for seg in chain:
                if isinstance(seg, Json):
                    json_url = extract_json_url(seg.data)
                    logger.debug(f"解析Json组件: {json_url}")
                    if json_url:
                        break
            text = json_url or combined_text

        # 引用解析
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:
            reply_texts: list[str] = []
            reply_raw_data = None
            for seg in reply_seg.chain:
                if isinstance(seg, Plain):
                    reply_texts.append(seg.text)
                elif isinstance(seg, Json):
                    json_url = extract_json_url(seg.data)
                    if json_url:
                        reply_texts.append(json_url)
                    if reply_raw_data is None:
                        reply_raw_data = seg.data
            reply_text = "".join(t for t in reply_texts if t)
            if reply_text:
                # 引用的可能是卡片消息：优先提取 jump_url 等跳转链接
                reply_card_url = extract_card_url(reply_text, reply_raw_data)
                text = reply_card_url or reply_text

        if not text:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 基于link防抖
        link = searched.group(0)
        if self.debouncer.hit_link(umo, link):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析（注入用户偏好：用户设置优先于平台全局配置）
        parser = self.parser_map[keyword]
        parse_res = await self._parse_with_preferences(
            parser, keyword, searched, self._pref_user_id(event)
        )

        # 基于资源ID防抖
        resource_id = parse_res.get_resource_id()
        if self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 发送
        quality_menu = await self._quality_menu_enabled(umo, event.get_sender_id())
        await self.sender.send_parse_result(event, parse_res, quality_menu=quality_menu)

    @filter.button_action("reparse")
    async def on_reparse_button(self, event: AstrMessageEvent):
        """处理「重新解析」按钮回调。"""
        interaction = event.get_button_interaction()
        if interaction is None:
            return

        umo = event.unified_msg_origin
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return
        if is_guild_event(event):
            return

        data = interaction.data if isinstance(interaction.data, dict) else {}
        url = data.get("u")
        if not isinstance(url, str) or not url:
            return

        try:
            result = await self.parse_url(url, user_id=self._pref_user_id(event))
        except Exception as e:
            logger.warning(f"[按钮] 重新解析失败: {url}, error: {e}")
            return

        await self.sender.send_parse_result(event, result)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("撤回", alias={"撤回解析"})
    async def recall_message(self, event: AstrMessageEvent, message_id: str = ""):
        """撤回机器人自己发送的消息（官方限制 2 分钟内）。"""
        umo = event.unified_msg_origin
        target = message_id.strip() or self.sender.get_last_sent_id(umo)
        if not target:
            yield event.plain_result(
                "没有可撤回的消息（官方仅支持撤回机器人自己发送的消息）"
            )
            return

        actor = QQActor(event)
        if await actor.recall(target):
            self.sender.clear_last_sent_id(umo)
            yield event.plain_result("已撤回")
        else:
            yield event.plain_result("撤回失败（仅支持机器人自己 2 分钟内发送的消息）")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @staticmethod
    def _quality_menu_key(umo: str, sender_id: str) -> str:
        return f"quality_menu:{umo}:{sender_id}"

    async def _quality_menu_enabled(self, umo: str, sender_id: str) -> bool:
        if not sender_id:
            return False
        key = self._quality_menu_key(umo, sender_id)
        if key in self._quality_pref_cache:
            return self._quality_pref_cache[key]
        try:
            value = await self.get_kv_data(key, False)
        except Exception as e:
            logger.debug(f"[画质菜单] 读取开关失败: {e!r}")
            value = False
        enabled = bool(value)
        self._quality_pref_cache[key] = enabled
        return enabled

    @filter.command("画质菜单", alias={"画质面板", "音质菜单", "音质面板"})
    async def quality_menu_command(self, event: AstrMessageEvent, action: str = ""):
        """开关当前用户在当前会话的画质/音质菜单。"""
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法识别当前用户")
            return

        arg = (action or "").strip().casefold()
        if not arg:
            parts = (event.message_str or "").strip().lstrip("/").split()
            arg = parts[1].casefold() if len(parts) > 1 else ""

        enabled_values = {"开", "开启", "on", "enable", "1"}
        disabled_values = {"关", "关闭", "off", "disable", "0"}
        status_values = {"", "状态", "status"}

        if arg in status_values:
            enabled = await self._quality_menu_enabled(umo, sender_id)
            state = "已开启" if enabled else "已关闭（发送 /画质菜单 开 启用）"
            yield event.plain_result(f"画质菜单：{state}")
            return
        if arg not in enabled_values | disabled_values:
            yield event.plain_result("用法：/画质菜单 开 | 关 | 状态")
            return

        enabled = arg in enabled_values
        key = self._quality_menu_key(umo, sender_id)
        try:
            await self.put_kv_data(key, enabled)
        except Exception as e:
            logger.warning(f"[画质菜单] 保存开关失败: {e!r}")
            yield event.plain_result("保存画质菜单开关失败")
            return
        self._quality_pref_cache[key] = enabled
        yield event.plain_result(f"画质菜单已{'开启' if enabled else '关闭'}")

    @filter.button_action()
    async def on_quality_button(self, event: AstrMessageEvent):
        """处理画质/音质菜单按钮。"""
        interaction = event.get_button_interaction()
        if interaction is None:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        if data.get("a") != "parser_quality":
            return

        umo = event.unified_msg_origin
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return
        if is_guild_event(event):
            return

        owner = str(data.get("o") or "")
        clicker = event.get_sender_id()
        if not owner or clicker != owner:
            await event.send(event.plain_result("操作被拒绝"))
            return

        try:
            index = int(data.get("i") or 0)
        except (TypeError, ValueError):
            index = 0
        key = (str(umo), clicker, str(data.get("r") or ""), index)
        if not self.menu_registry.claim_action(key):
            await event.send(event.plain_result("该菜单已使用或已过期"))
            return

        platform = str(data.get("p") or "")
        url = str(data.get("u") or "")
        quality = str(data.get("q") or "")
        kind = str(data.get("k") or "audio")
        send_mode = str(data.get("m") or "") or None
        if send_mode not in ("file", "record"):
            send_mode = None

        parser = self.parsers.get(platform)
        if parser is None:
            self.menu_registry.release_action(key)
            await event.send(event.plain_result("该平台解析器已关闭"))
            return

        label = str(data.get("l") or quality)
        mode_label = (
            "文件" if send_mode == "file" else "语音" if send_mode == "record" else ""
        )
        if mode_label:
            label = f"{mode_label}·{label}"
        try:
            await event.send(event.plain_result(f"正在准备 {label}…"))
        except Exception:
            pass

        try:
            result = await parser.parse_with_quality(
                url, quality or None, kind=kind, strict=True
            )
        except Exception as e:
            self.menu_registry.release_action(key)
            logger.warning(f"[画质菜单] 重新解析失败: {url}, quality={quality}, {e!r}")
            await event.send(event.plain_result(f"切换失败：{e}"))
            return

        options = [opt for opt in result.quality_options if opt.kind == kind]
        if quality not in {opt.key for opt in options}:
            self.menu_registry.release_action(key)
            await event.send(event.plain_result("该档位不可用"))
            return

        expected_cls = AudioContent if kind == "audio" else VideoContent
        contents = result.audio_contents if kind == "audio" else result.video_contents
        content = None
        if 0 <= index < len(result.contents) and isinstance(
            result.contents[index], expected_cls
        ):
            content = result.contents[index]
        elif contents:
            content = contents[0]
        if content is None:
            self.menu_registry.release_action(key)
            await event.send(event.plain_result("未找到可发送的媒体"))
            return

        ok = await self.sender.send_media_only(event, content, send_mode=send_mode)
        if not ok:
            self.menu_registry.release_action(key)
            await event.send(event.plain_result("发送失败，请稍后重试"))
            return

        source_id = interaction.source_message_id
        if source_id:
            try:
                await QQActor(
                    event, self.downloader.client if self.downloader else None
                ).recall(source_id)
            except Exception:
                pass

    # ---------------- 通用菜单 ----------------

    @filter.command("解析设置")
    async def menu_command(self, event: AstrMessageEvent):
        """展示解析设置菜单（仅 QQ 官方群聊/单聊）。"""
        if is_guild_event(event):
            return
        umo = event.unified_msg_origin
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            yield event.plain_result("当前会话不在白名单中，无法使用解析设置")
            return
        page = await self._menu_home_page(event)
        await send_menu(event, page, self.menu_store, owner=event.get_sender_id())

    @filter.command("解析帮助", alias={"帮助"})
    async def help_command(self, event: AstrMessageEvent):
        """展示命令帮助页（仅 QQ 官方群聊/单聊）。"""
        if is_guild_event(event):
            return
        umo = event.unified_msg_origin
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            yield event.plain_result("当前会话不在白名单中，无法使用解析帮助")
            return
        await send_menu(
            event,
            build_help_page(),
            self.menu_store,
            owner=event.get_sender_id(),
        )

    @filter.button_action()
    async def on_menu_button(self, event: AstrMessageEvent):
        """处理通用菜单按钮回调。"""
        interaction = event.get_button_interaction()
        if interaction is None:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        if data.get("a") != "menu":
            return

        umo = event.unified_msg_origin
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return
        if is_guild_event(event):
            return

        owner = str(data.get("o") or "")
        clicker = str(event.get_sender_id() or "")
        if owner and clicker != owner:
            await event.send(event.plain_result("操作被拒绝"))
            return

        key = menu_key(
            umo,
            clicker,
            data.get("p"),
            data.get("k"),
            data.get("plat"),
            data.get("v"),
            data.get("pg"),
            data.get("n"),
        )
        if not self.menu_store.claim_action(key, ttl=NAV_TTL):
            return

        try:
            page = await self._menu_route(event, data)
        except Exception as e:
            logger.warning(f"[菜单] 处理点击失败: {e!r}")
            self.menu_store.release_action(key)
            await event.send(event.plain_result("菜单处理失败，请重试"))
            return
        if page is None:
            await event.send(event.plain_result("该菜单项已失效"))
            return
        await send_menu(event, page, self.menu_store, owner=clicker)

    async def _menu_route(self, event: AstrMessageEvent, data: dict):
        page_id = str(data.get("p") or "home")
        user_id = self._pref_user_id(event)

        if page_id == "help":
            return build_help_page()

        if page_id in {"quality", "quality_page", "set", "clear"}:
            kind = str(data.get("k") or "video")
            if kind not in QUALITY_KINDS:
                kind = "video"
            platform = str(data.get("plat") or "")
            if page_id == "set":
                value = str(data.get("v") or "")
                parser = self.parsers.get(platform)
                allowed = parser.allowed_quality_values(kind) if parser else None
                if parser is not None and allowed and value in allowed:
                    await self.preferences.aset(platform, user_id, kind, value)
                    return self._menu_platform_page(kind, platform, user_id)
                return self._menu_pref_home_page(kind, user_id)
            if page_id == "clear":
                await self.preferences.aclear_kind(user_id, kind)
                return self._menu_pref_home_page(kind, user_id)
            if platform:
                return self._menu_platform_page(kind, platform, user_id)
            try:
                page = int(data.get("pg") or 0)
            except (TypeError, ValueError):
                page = 0
            return self._menu_pref_home_page(kind, user_id, page)

        if page_id == "image":
            value = str(data.get("v") or "")
            if value in {"original", "compressed"}:
                await self.preferences.aset("*", user_id, "image", value)
            elif value == "default":
                await self.preferences.aset("*", user_id, "image", None)
            return build_image_page(self.preferences.get("*", user_id, "image"))

        if page_id == "toggle_menu":
            await self._toggle_quality_menu(event)
            return await self._menu_home_page(event)

        if page_id == "toggle_parse":
            if not self._is_admin(event):
                await event.send(event.plain_result("仅管理员可切换解析开关"))
            else:
                umo = event.unified_msg_origin
                if umo in self.cfg.blacklist:
                    self.cfg.remove_blacklist(umo)
                else:
                    self.cfg.add_blacklist(umo)
            return await self._menu_home_page(event)

        return await self._menu_home_page(event)

    async def _menu_home_page(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        return build_home_page(
            enabled=umo not in self.cfg.blacklist,
            quality_menu=await self._quality_menu_enabled(umo, sender_id),
            platforms=len(self.parsers),
            is_admin=self._is_admin(event),
        )

    def _menu_sections(self, kind: str, user_id: str) -> list[PreferenceSection]:
        sections: list[PreferenceSection] = []
        for name, parser in self.parsers.items():
            allowed = parser.allowed_quality_values(kind)
            if not allowed:
                continue
            stored = resolve_preference(
                self.preferences.get(name, user_id, kind), allowed
            )
            global_value = self._global_quality(parser, kind)
            if stored:
                current, source = stored, "用户"
            elif global_value and global_value in allowed:
                current, source = global_value, "全局"
            else:
                current, source = "未设置", ""
            sections.append(
                PreferenceSection(
                    platform=name,
                    display=parser.platform.display_name,
                    current=current,
                    options=tuple(allowed),
                    source=source,
                )
            )
        return sections

    def _menu_pref_home_page(self, kind: str, user_id: str, page: int = 0):
        return build_preference_overview(kind, self._menu_sections(kind, user_id), page)

    def _menu_platform_page(self, kind: str, platform: str, user_id: str):
        for section in self._menu_sections(kind, user_id):
            if section.platform == platform:
                return build_platform_page(kind, section)
        return self._menu_pref_home_page(kind, user_id)

    async def _toggle_quality_menu(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        sender_id = str(event.get_sender_id() or "")
        if not sender_id:
            return
        enabled = not await self._quality_menu_enabled(umo, sender_id)
        key = self._quality_menu_key(umo, sender_id)
        try:
            await self.put_kv_data(key, enabled)
        except Exception as e:
            logger.warning(f"[菜单] 保存画质菜单开关失败: {e!r}")
            await event.send(event.plain_result("保存画质菜单开关失败"))
            return
        self._quality_pref_cache[key] = enabled

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ---------------- 用户媒体偏好 ----------------

    @staticmethod
    def _pref_user_id(event: AstrMessageEvent) -> str:
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        return sender or str(event.unified_msg_origin)

    def _quality_requests(
        self, platform: str, user_id: str
    ) -> dict[str, QualityRequest]:
        """读取用户偏好并校验为当前平台可用的档位请求。"""
        parser = self.parsers.get(platform)
        requests: dict[str, QualityRequest] = {}
        if parser is None:
            return requests
        for kind in QUALITY_KINDS:
            allowed = parser.allowed_quality_values(kind)
            if not allowed:
                continue
            value = resolve_preference(
                self.preferences.get(platform, user_id, kind), allowed
            )
            if value:
                requests[kind] = QualityRequest(value, kind, strict=False)
        return requests

    def _image_preference(self, user_id: str) -> str | None:
        return resolve_preference(
            self.preferences.get("*", user_id, "image"), IMAGE_VALUES
        )

    async def _parse_with_preferences(
        self,
        parser: BaseParser,
        keyword: str,
        searched: re.Match[str],
        user_id: str,
    ):
        requests = self._quality_requests(parser.platform.name, user_id)
        image = self._image_preference(user_id)
        if not requests and image is None:
            return await parser.parse(keyword, searched)
        with quality_context(requests, image=image):
            return await parser.parse(keyword, searched)

    @staticmethod
    def _global_quality(parser: BaseParser, kind: str) -> str | None:
        mycfg = getattr(parser, "mycfg", None)
        value = getattr(mycfg, f"{kind}_quality", None) if mycfg else None
        return str(value) if value else None

    def _quality_report(self, user_id: str, kind: str) -> str:
        label = "画质" if kind == "video" else "音质"
        lines = [f"{label}偏好（用户设置 > 平台全局）："]
        supported = [
            (name, parser)
            for name, parser in self.parsers.items()
            if parser.allowed_quality_values(kind)
        ]
        if not supported:
            lines.append("当前没有启用支持该偏好的平台")
            return "\n".join(lines)
        for name, parser in supported:
            allowed = parser.allowed_quality_values(kind) or ()
            value = resolve_preference(
                self.preferences.get(name, user_id, kind), allowed
            )
            if value:
                source = "用户"
            else:
                value = self._global_quality(parser, kind)
                source = "全局" if value else "默认"
            display = f"{value}（{source}）" if value else "未设置"
            lines.append(f"- {parser.platform.display_name}: {display}")
        lines.append("可选档位：")
        for name, parser in supported:
            allowed = parser.allowed_quality_values(kind) or ()
            lines.append(f"- {parser.platform.display_name}: {'、'.join(allowed)}")
        lines.append(f"设置：/{label} <档位>；清除：/{label} 默认")
        return "\n".join(lines)

    @staticmethod
    def _pref_args(event: AstrMessageEvent, *names: str) -> list[str]:
        text = (event.message_str or "").strip().lstrip("/")
        tokens = [str(token) for token in text.split()]
        known = {name.casefold() for name in names}
        if tokens and tokens[0].casefold() in known:
            tokens = tokens[1:]
        return tokens

    async def _handle_quality_pref(self, event: AstrMessageEvent, kind: str):
        label = "画质" if kind == "video" else "音质"
        user_id = self._pref_user_id(event)
        args = self._pref_args(
            event, label, "视频画质" if kind == "video" else "音频音质"
        )
        if not args or args[0].casefold() in {"查看", "状态", "status"}:
            yield event.plain_result(self._quality_report(user_id, kind))
            return
        token = args[0]
        if token.casefold() in {"默认", "重置", "清除", "reset", "default"}:
            count = await self.preferences.aclear_kind(user_id, kind)
            yield event.plain_result(
                f"已清除你的{label}偏好（{count} 项）"
                if count
                else f"你还没有设置{label}偏好"
            )
            return

        applied: list[str] = []
        for name, parser in self.parsers.items():
            allowed = parser.allowed_quality_values(kind)
            if not allowed:
                continue
            matched = next(
                (value for value in allowed if value.casefold() == token.casefold()),
                None,
            )
            if matched is None:
                continue
            await self.preferences.aset(name, user_id, kind, matched)
            applied.append(f"{parser.platform.display_name}={matched}")
        if applied:
            yield event.plain_result(
                f"已将{label}偏好设为：{'、'.join(applied)}\n之后解析将优先使用该档位。"
            )
        else:
            yield event.plain_result(
                f"没有平台支持档位 {token}\n\n{self._quality_report(user_id, kind)}"
            )

    @filter.command("画质", alias={"视频画质"})
    async def cmd_video_quality(self, event: AstrMessageEvent):
        """查看/设置个人画质偏好：/画质 <档位> | /画质 默认"""
        async for result in self._handle_quality_pref(event, "video"):
            yield result

    @filter.command("音质", alias={"音频音质"})
    async def cmd_audio_quality(self, event: AstrMessageEvent):
        """查看/设置个人音质偏好：/音质 <档位> | /音质 默认"""
        async for result in self._handle_quality_pref(event, "audio"):
            yield result

    @filter.command("原图", alias={"原图画质"})
    async def cmd_image_preference(self, event: AstrMessageEvent):
        """查看/设置个人图片偏好：/原图 开 | 关 | 默认"""
        user_id = self._pref_user_id(event)
        args = self._pref_args(event, "原图", "原图画质")
        current = self.preferences.get("*", user_id, "image")
        current_label = {"original": "原图", "compressed": "压缩"}.get(
            current or "", "未设置"
        )
        if not args or args[0].casefold() in {"查看", "状态", "status"}:
            yield event.plain_result(
                f"图片偏好：{current_label}\n设置：/原图 开 | 关；清除：/原图 默认"
            )
            return
        token = args[0].casefold()
        if token in {"默认", "重置", "清除", "reset", "default"}:
            await self.preferences.aset("*", user_id, "image", None)
            yield event.plain_result("已清除图片偏好，将使用平台全局设置")
            return
        original_values = {"开", "开启", "原图", "on", "original"}
        compressed_values = {"关", "关闭", "压缩", "off", "compressed"}
        if token in original_values:
            await self.preferences.aset("*", user_id, "image", "original")
            yield event.plain_result("已将图片偏好设为：原图")
        elif token in compressed_values:
            await self.preferences.aset("*", user_id, "image", "compressed")
            yield event.plain_result("已将图片偏好设为：压缩")
        else:
            yield event.plain_result("用法：/原图 开 | 关 | 默认 | 查看")

    def _login_registry(self) -> LoginRegistry:
        bilibili = self.parsers.get("bilibili")
        ncm = self.parsers.get("ncm")
        return LoginRegistry(
            [
                BilibiliLoginProvider(
                    self.cfg,
                    enabled=bilibili is not None,
                    login=getattr(bilibili, "login", None),
                ),
                NeteaseLoginProvider(
                    self.cfg,
                    enabled=ncm is not None,
                    parser=ncm if isinstance(ncm, NCMParser) else None,
                ),
            ]
        )

    @staticmethod
    def _login_args(event: AstrMessageEvent) -> list[str]:
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            text = text[1:]
        tokens: list[str] = [str(token) for token in text.split()]
        if tokens and tokens[0].casefold() == LOGIN_COMMAND.casefold():
            tokens = tokens[1:]
        return tokens

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(LOGIN_COMMAND)
    async def parser_login(self, event: AstrMessageEvent):
        """统一的解析登录入口：B站 / 网易云（仅管理员）"""
        args = self._login_args(event)
        registry = self._login_registry()

        if not args or args[0].casefold() in LOGIN_HELP_ALIASES:
            yield event.plain_result(registry.help_text())
            return

        token = args[0]
        if token.casefold() in LOGIN_STATUS_ALIASES:
            lines = await registry.status_all()
            yield event.plain_result("\n".join(lines))
            return

        provider = registry.resolve(token)
        if provider is None:
            yield event.plain_result(f"未知平台：{token}\n\n{registry.help_text()}")
            return
        if not provider.available():
            yield event.plain_result(
                f"{provider.display_name}未启用，请先在插件配置中开启该平台解析"
            )
            return

        action = args[1] if len(args) > 1 else provider.default_action
        if action.casefold() in LOGIN_HELP_ALIASES:
            yield event.plain_result(provider.help_text())
            return

        try:
            async for item in provider.run(action, args[2:]):
                if isinstance(item, LoginQRCode):
                    yield event.chain_result([Image.fromBytes(item.data)])
                else:
                    yield event.plain_result(item)
        except Exception as e:
            logger.warning(f"[解析登录] {provider.display_name}登录失败: {e!r}")
            yield event.plain_result(f"{provider.display_name}登录失败：{e}")

    async def parse_url(self, url: str, user_id: str | None = None):
        """供其他插件调用的公开解析 API：传入 URL 解析并返回 ParseResult 对象

        user_id 为空时使用平台全局档位；传入时优先使用该用户偏好。
        """
        for kw, pat in self.key_pattern_list:
            if kw in url and (m := pat.search(url)):
                parser = self.parser_map[kw]
                if user_id:
                    return await self._parse_with_preferences(parser, kw, m, user_id)
                return await parser.parse(kw, m)
        raise ValueError(f"未匹配到支持的解析器: {url}")

    async def parse_and_send(self, event: AstrMessageEvent, url: str) -> bool:
        """供其他插件调用的公开方法：解析指定 URL 并直接发送至 event 对应会话"""
        result = await self.parse_url(url, user_id=self._pref_user_id(event))
        return await self.sender.send_parse_result(event, result)

    @filter.llm_tool(name="parse_media_url")
    async def llm_parse_media_url(self, event: AstrMessageEvent, url: str) -> str:
        """
        解析用户提供的视频、图集或音频链接并自动发送解析结果。

        Args:
            url(string): 待解析的视频或图文等媒体链接
        """
        try:
            await self.parse_and_send(event, url)
            return "链接解析成功并已发送"
        except Exception as e:
            logger.error(f"[LLM Tool] 解析链接失败: {url}, error: {e}")
            return f"解析链接失败: {e}"
