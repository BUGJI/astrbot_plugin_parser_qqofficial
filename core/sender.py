from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    ActionRow,
    BaseMessageComponent,
    Button,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    MediaContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .menu import MAX_BUTTONS_PER_ROW, MenuButton, MenuPage, MenuStore, send_menu
from .platform import CARD_MAX_IMAGES, reply_budget
from .qq_api import (
    QQActor,
    build_keyboard,
    extract_ref_idx,
    get_scene,
    resolve_target,
    valid_https_url,
    valid_url,
)
from .qq_media import QQMediaUploader
from .quality import QualityMenuRegistry
from .render import Renderer
from .utils import ensure_official_image, transcode_video_audio_to_lc


@dataclass
class _Budget:
    """单条被动消息允许的剩余回复次数。"""

    remaining: int

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


@dataclass
class _SendCtx:
    """一次解析结果发送过程中的共享状态。"""

    budget: _Budget
    quote: bool = False
    keyboard: list[ActionRow] | None = None
    markdown: bool = False
    overflow_mode: str = "off"
    overflow_left: int = 0
    audio_send_mode: str | None = None
    sent_media: list[MediaContent] = field(default_factory=list)

    def take_quote(self) -> bool:
        value = self.quote
        self.quote = False
        return value

    def take_keyboard(self) -> list[ActionRow] | None:
        value = self.keyboard
        self.keyboard = None
        return value


class MessageSender:
    """消息发送器（QQ 官方机器人版）。

    与 OneBot 版的关键差异：
    - QQ 官方不支持合并转发，因此**绝不使用 Nodes**；
    - 单条消息只能携带一个富媒体；
    - 被动回复次数有限（私聊 4 次 / 群聊 5 次），必须做预算控制；
    - 图集统一渲染为卡片（每卡最多 9 图），超额拆分多卡；
    - 纯文本强制关闭 Markdown，避免链接被格式化解构。

    官方能力增强（可按配置关闭）：
    - 图文卡片 ``msg_type=8``（替代单作品 PIL 预览卡，可点击回源）；
    - 引用回复（``Reply`` → ``message_reference``）；
    - 内嵌键盘（打开原链接 / 重新解析）；
    - Markdown 文本 + 素材管理（``qqbot-media://``）。
    """

    def __init__(
        self,
        config: PluginConfig,
        renderer: Renderer,
        downloader=None,
        menu_registry: QualityMenuRegistry | None = None,
        menu_store: MenuStore | None = None,
    ):
        self.cfg = config
        self.renderer = renderer
        self.downloader = downloader
        self.menu_registry = menu_registry or QualityMenuRegistry()
        self.menu_store = menu_store or MenuStore()
        # 记录机器人直发消息的 ID（用于撤回命令）
        self._last_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 组件构造
    # ------------------------------------------------------------------

    @staticmethod
    def _to_file_uri(path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    @staticmethod
    def _iter_contents(result: ParseResult) -> list[MediaContent]:
        contents = list(result.contents)
        if result.repost:
            contents.extend(result.repost.contents)
        return contents

    @staticmethod
    def _classify(items: list[MediaContent]):
        texts: list[str] = []
        images: list[ImageContent] = []
        graphics: list[GraphicsContent] = []
        heavies: list[MediaContent] = []
        others: list[MediaContent] = []

        for cont in items:
            if isinstance(cont, TextContent):
                if cont.text:
                    texts.append(cont.text)
            elif isinstance(cont, ImageContent):
                images.append(cont)
            elif isinstance(cont, GraphicsContent):
                graphics.append(cont)
            elif isinstance(
                cont, (VideoContent, AudioContent, FileContent, DynamicContent)
            ):
                heavies.append(cont)
            else:
                others.append(cont)

        return texts, images, graphics, heavies, others

    @staticmethod
    def _squash(text: object | None, limit: int) -> str:
        if not text:
            return ""
        value = " ".join(str(text).split())
        return value[:limit]

    def _md_text(self, result: ParseResult) -> str | None:
        parts: list[str] = []
        if result.title:
            parts.append(f"# {result.title}")
        if result.author and result.author.name:
            parts.append(f"> {result.author.name}")
        body = result.text or result.extra_info
        if body:
            parts.append(str(body))
        if result.url:
            parts.append(f"[🔗查看原内容]({result.url})")
        text = "\n\n".join(p.strip() for p in parts if p and p.strip())
        return text[:1800] if text else None

    def _markdown_enabled(self, result: ParseResult) -> bool:
        mode = str(self.cfg.qq_markdown or "auto").lower()
        if mode == "off":
            return False
        return bool(result.url or result.title)

    def _quote_segment(self, event: AstrMessageEvent) -> Reply | None:
        if not self.cfg.qq_quote_reply:
            return None
        try:
            if event.is_button_interaction():
                return None
        except Exception:
            pass
        msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        return Reply(id=str(msg_id or "self"))

    def _keyboard(self, result: ParseResult) -> list[ActionRow] | None:
        if self.cfg.qq_keyboard is False:
            return None

        url = valid_url(getattr(result, "url", None))
        if not url:
            return None

        buttons: list[Button] = []
        if self.cfg.qq_keyboard_open_link is not False:
            try:
                buttons.append(
                    Button(
                        id="open_link",
                        label="打开原链接",
                        style=Button.Style.PRIMARY,
                        action=Button.UrlAction(url=url),
                    )
                )
            except Exception:
                pass
        if self.cfg.qq_keyboard_reparse is not False:
            try:
                buttons.append(
                    Button(
                        id="reparse",
                        label="重新解析",
                        action=Button.CallbackAction(data={"a": "reparse", "u": url}),
                    )
                )
            except Exception:
                pass

        if not buttons:
            return None
        return [ActionRow(buttons=buttons, fallback_text="")]

    def _raw_keyboard(self, result: ParseResult) -> dict | None:
        if self.cfg.qq_keyboard is False:
            return None
        return build_keyboard(
            getattr(result, "url", None) or "",
            open_link=self.cfg.qq_keyboard_open_link is not False,
            reparse=self.cfg.qq_keyboard_reparse is not False,
        )

    async def _send_chain(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        *,
        markdown: bool = False,
        quote: bool = False,
        keyboards: list[ActionRow] | None = None,
    ) -> bool:
        if not segs:
            return False

        chain = list(segs)
        if quote:
            quote_seg = self._quote_segment(event)
            if quote_seg is not None:
                chain.insert(0, quote_seg)
        if keyboards:
            chain.extend(keyboards)

        try:
            result = event.chain_result(chain)
            result.use_markdown(bool(markdown))
            await event.send(result)
            return True
        except Exception as e:
            logger.error(
                f"发送解析结果失败： error={e}, segments={self._collect_seg_meta(chain)}"
            )

            # 官方对「富媒体 + 键盘」组合支持不稳定，失败时去掉键盘重试一次。
            if keyboards:
                drop = {id(row) for row in keyboards}
                retry_chain = [seg for seg in chain if id(seg) not in drop]
                try:
                    retry = event.chain_result(retry_chain)
                    retry.use_markdown(bool(markdown))
                    await event.send(retry)
                    return True
                except Exception as retry_err:
                    logger.error(f"重试发送（无键盘）失败： error={retry_err}")
            return False

    async def _send_with_ctx(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        ctx: _SendCtx,
        *,
        markdown: bool = False,
    ) -> bool:
        quote = ctx.take_quote()
        keyboards = ctx.take_keyboard()
        ok = await self._send_chain(
            event, segs, markdown=markdown, quote=quote, keyboards=keyboards
        )
        if not ok:
            if quote:
                ctx.quote = True
            if keyboards and ctx.keyboard is None:
                ctx.keyboard = keyboards
        return ok

    # ------------------------------------------------------------------
    # 溢出投递（主动消息 / 群转私聊）与撤回
    # ------------------------------------------------------------------

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            return str(event.unified_msg_origin)
        except Exception:
            return str(getattr(getattr(event, "message_obj", None), "session_id", ""))

    def get_last_sent_id(self, umo: str) -> str | None:
        return self._last_ids.get(str(umo))

    def clear_last_sent_id(self, umo: str) -> None:
        self._last_ids.pop(str(umo), None)

    def _overflow_target(
        self, event: AstrMessageEvent, mode: str
    ) -> tuple[str, str] | None:
        if mode == "private" and get_scene(event) == "group":
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            uid = getattr(sender, "user_id", None)
            return ("c2c", str(uid)) if uid else None
        return resolve_target(event)

    @staticmethod
    def _overflow_file_type(cont: MediaContent) -> int:
        if isinstance(cont, ImageContent):
            return 1
        if isinstance(cont, (VideoContent, DynamicContent)):
            return 2
        if isinstance(cont, AudioContent):
            return 3
        if isinstance(cont, FileContent):
            return 4
        return 0

    async def _overflow_media_path(self, cont: MediaContent) -> Path:
        path = await cont.get_path()
        if isinstance(cont, ImageContent):
            return ensure_official_image(
                path, self.cfg.cache_dir, self.cfg.qq_image_format or "jpg"
            )
        if isinstance(cont, (VideoContent, DynamicContent)):
            return await transcode_video_audio_to_lc(path)
        return path

    async def _send_overflow_media(
        self,
        event: AstrMessageEvent,
        cont: MediaContent,
        ctx: _SendCtx,
    ) -> bool:
        if ctx.overflow_left <= 0 or ctx.overflow_mode == "off":
            return False

        upload = getattr(event, "upload_group_and_c2c_media", None)
        if upload is None:
            return False

        target = self._overflow_target(event, ctx.overflow_mode)
        file_type = self._overflow_file_type(cont)
        if target is None or not file_type:
            return False

        try:
            path = await self._overflow_media_path(cont)
        except Exception as e:
            logger.warning(f"[qqofficial] 溢出媒体准备失败: {e}")
            return False

        kind, tid = target
        kwargs = {"group_openid": tid} if kind == "group" else {"openid": tid}
        try:
            await upload(str(path), file_type, srv_send_msg=True, **kwargs)
            ctx.overflow_left -= 1
            ctx.sent_media.append(cont)
            return True
        except Exception as e:
            logger.warning(f"[qqofficial] 溢出媒体发送失败: {e}")
            ctx.overflow_left = 0
            return False

    async def _send_overflow_text(
        self,
        event: AstrMessageEvent,
        text: str,
        ctx: _SendCtx,
        *,
        markdown: bool = False,
    ) -> bool:
        if ctx.overflow_left <= 0 or ctx.overflow_mode != "proactive" or not text:
            return False

        actor = QQActor(event, self.downloader.client if self.downloader else None)
        if not actor.available:
            return False

        payload = (
            {"markdown": {"content": text}, "msg_type": 2}
            if markdown
            else {"content": text, "msg_type": 0}
        )
        try:
            ret = await actor.send_payload(payload, proactive=True)
            if isinstance(ret, dict) and ret.get("id"):
                ctx.overflow_left -= 1
                return True
        except Exception as e:
            logger.warning(f"[qqofficial] 溢出文本发送失败: {e}")
        ctx.overflow_left = 0
        return False

    # ------------------------------------------------------------------
    # 单条消息/组件
    # ------------------------------------------------------------------

    async def _card_segment(
        self,
        result: ParseResult,
        images: list[ImageContent] | None = None,
        footer: str | None = None,
        include_repost: bool = True,
    ) -> Image | None:
        path = await self.renderer.render_card(
            result,
            images=images,
            footer=footer,
            include_repost=include_repost,
        )
        return self._image_from_path(path) if path else None

    async def _image_segment(self, cont: MediaContent) -> BaseMessageComponent | None:
        try:
            path: Path = await cont.get_path()
        except (DownloadLimitException, ZeroSizeException):
            return None
        except DownloadException:
            return (
                Plain("此项媒体下载失败") if self.cfg.show_download_fail_tip else None
            )

        path = ensure_official_image(
            path, self.cfg.cache_dir, self.cfg.qq_image_format or "jpg"
        )
        return self._image_from_path(path)

    async def _heavy_segment(
        self,
        cont: MediaContent,
        *,
        send_mode: str | None = None,
    ) -> BaseMessageComponent | None:
        try:
            path: Path = await cont.get_path()
        except (SizeLimitException, DurationLimitException) as exc:
            if self.cfg.show_download_fail_tip:
                message = (
                    "此项媒体超过时长限制"
                    if isinstance(exc, DurationLimitException)
                    else "此项媒体超过大小限制"
                )
                return Plain(message)
            return None
        except DownloadException:
            return (
                Plain("此项媒体下载失败") if self.cfg.show_download_fail_tip else None
            )

        match cont:
            case VideoContent() | DynamicContent():
                path = await transcode_video_audio_to_lc(path)
                return self._video_from_path(path)
            case AudioContent():
                effective_mode = (
                    send_mode
                    if send_mode is not None
                    else getattr(cont, "send_as", None)
                )
                use_file = (
                    self.cfg.audio_to_file
                    if effective_mode is None
                    else str(effective_mode).lower() == "file"
                )
                return (
                    File(name=path.name, file=self._to_file_uri(path))
                    if use_file
                    else self._record_from_path(path)
                )
            case FileContent():
                return File(name=path.name, file=self._to_file_uri(path))

        return None

    # ------------------------------------------------------------------
    # 图文卡片 / 渲染卡片
    # ------------------------------------------------------------------

    @staticmethod
    def _card_https(url: object | None) -> str | None:
        """把 http / 协议相对地址升级为 https，供官方图文卡片使用。"""
        if not isinstance(url, str):
            return None
        value = url.strip()
        if value.startswith("//"):
            value = "https:" + value
        elif value.startswith("http://"):
            value = "https://" + value[len("http://") :]
        return valid_https_url(value)

    @staticmethod
    def _native_card_payload(result: ParseResult) -> dict | None:
        url = MessageSender._card_https(getattr(result, "url", None))
        picture = MessageSender._card_https(result.cover_url)
        if not url or not picture:
            return None

        title = MessageSender._squash(
            result.title or result.header or result.platform.display_name, 50
        )
        description = MessageSender._squash(
            result.text or result.extra_info or result.header or title, 200
        )
        if not title or not description:
            return None

        return {
            "msg_type": 8,
            "card": {
                "type": "tuwen",
                "content": {
                    "title": title,
                    "description": description,
                    "url": url,
                    "pic_url": picture,
                },
            },
        }

    async def _send_native_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        ctx: _SendCtx,
    ) -> bool:
        if self.cfg.qq_native_card is False:
            return False

        payload = self._native_card_payload(result)
        if not payload:
            return False

        took_budget = ctx.budget.take()
        if not took_budget and (ctx.overflow_mode == "off" or ctx.overflow_left <= 0):
            return False

        actor = QQActor(event, self.downloader.client if self.downloader else None)
        if not actor.available:
            if took_budget:
                ctx.budget.remaining += 1
            return False

        keyboard = self._raw_keyboard(result)
        if keyboard:
            payload["keyboard"] = keyboard

        quoted = False
        if ctx.quote:
            ref = extract_ref_idx(getattr(event.message_obj, "raw_message", None))
            if ref:
                payload["message_reference"] = {"message_id": ref}
                quoted = True

        try:
            ret = await actor.send_payload(payload, proactive=not took_budget)
            if isinstance(ret, dict) and ret.get("id"):
                if not took_budget:
                    ctx.overflow_left -= 1
                if quoted:
                    ctx.quote = False
                if keyboard:
                    ctx.keyboard = None
                self._last_ids[self._session_key(event)] = str(ret["id"])
                return True
            logger.warning(f"[qqofficial] 图文卡片发送失败: {ret}")
        except Exception as e:
            logger.error(f"[qqofficial] 图文卡片发送异常: {e}")

        if took_budget:
            ctx.budget.remaining += 1
        return False

    async def _upload_card(self, event: AstrMessageEvent, path: Path) -> str | None:
        if self.downloader is None:
            return None
        uploader = QQMediaUploader(
            getattr(event, "bot", None),
            self.downloader.client,
            self.cfg.cache_dir / "qq_media.json",
        )
        return await uploader.upload(path)

    async def _send_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        ctx: _SendCtx,
        *,
        images: list[ImageContent] | None = None,
        footer: str | None = None,
        include_repost: bool = True,
    ) -> bool:
        """发送渲染卡片：可选「素材 + Markdown」，否则发图片。"""
        path = await self.renderer.render_card(
            result,
            images=images,
            footer=footer,
            include_repost=include_repost,
        )
        if not path:
            return False

        if self.cfg.qq_material_upload and self.downloader is not None:
            media_url = await self._upload_card(event, path)
            if media_url:
                md = f"![卡片]({media_url})"
                if valid_url(result.url):
                    md += f"\n\n[🔗查看原内容]({result.url})"
                if footer:
                    md += f"\n\n{footer}"
                if await self._send_with_ctx(event, [Plain(md)], ctx, markdown=True):
                    return True

        seg = self._image_from_path(path)
        return await self._send_with_ctx(event, [seg], ctx)

    # ------------------------------------------------------------------
    # 发送计划
    # ------------------------------------------------------------------

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        contents = list(result.contents)
        if not contents and result.repost:
            contents = list(result.repost.contents)
        if not contents:
            return []
        return [SendGroup(contents=contents)]

    @staticmethod
    def _group_has_media(group: SendGroup) -> bool:
        return any(
            isinstance(
                c,
                (
                    ImageContent,
                    GraphicsContent,
                    VideoContent,
                    AudioContent,
                    FileContent,
                    DynamicContent,
                ),
            )
            for c in group.contents
        )

    async def _send_image_gallery(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        images: list[ImageContent],
        ctx: _SendCtx,
    ) -> tuple[bool, bool]:
        """发送图片图集，返回 (是否发送成功, 是否发送了卡片)。"""
        count = len(images)
        render_gallery = self.cfg.qq_gallery_render_card is not False
        has_meta = bool(result.title or result.text or result.author or result.repost)

        # 单张图片：有元信息时渲染卡片（保留标题/作者/正文），
        # 否则直接原图发送，节省渲染开销且保留画质。
        if count == 1:
            if render_gallery and has_meta:
                if await self._send_native_card(event, result, ctx):
                    return True, True
                if not ctx.budget.take():
                    return False, False
                ok = await self._send_card(
                    event, result, ctx, images=images, include_repost=True
                )
                return ok, ok
            if not ctx.budget.take():
                ok = await self._send_overflow_media(event, images[0], ctx)
                return ok, False
            seg = await self._image_segment(images[0])
            ok = await self._send_with_ctx(event, [seg], ctx) if seg else False
            return ok, False

        # 关闭图集卡片时逐图发送（受回复额度限制）。
        if not render_gallery:
            sent_any = False
            for cont in images:
                if not ctx.budget.take():
                    if await self._send_overflow_media(event, cont, ctx):
                        sent_any = True
                        continue
                    logger.warning(
                        f"[qqofficial] 回复额度不足，省略后续图片（共 {count} 张）"
                    )
                    break
                seg = await self._image_segment(cont)
                if seg and await self._send_with_ctx(event, [seg], ctx):
                    sent_any = True
            return sent_any, False

        available = ctx.budget.remaining
        if available <= 0:
            sent = False
            for cont in images:
                if await self._send_overflow_media(event, cont, ctx):
                    sent = True
                else:
                    break
            if not sent:
                logger.warning(f"[qqofficial] 回复额度不足，省略 {count} 张图片")
            return sent, False

        per_card = max(
            1, min(self.cfg.qq_card_max_images or CARD_MAX_IMAGES, CARD_MAX_IMAGES)
        )
        pages = math.ceil(count / per_card)
        if pages > available:
            per_card = max(1, min(CARD_MAX_IMAGES, math.ceil(count / available)))
            pages = math.ceil(count / per_card)

        truncated = 0
        if pages > available:
            pages = available
            truncated = max(0, count - pages * per_card)

        sent_any = False
        for index in range(pages):
            if not ctx.budget.take():
                break
            start = index * per_card
            slice_images = images[start : start + per_card]
            footer = f"第 {index + 1}/{pages} 张 · 共 {count} 张"
            ok = await self._send_card(
                event,
                result,
                ctx,
                images=slice_images,
                footer=footer,
                include_repost=index == 0,
            )
            if ok:
                sent_any = True

        if truncated > 0:
            remaining = images[pages * per_card :]
            for cont in remaining:
                if await self._send_overflow_media(event, cont, ctx):
                    sent_any = True
                else:
                    logger.warning(
                        f"[qqofficial] 回复额度不足，已省略最后 {truncated} 张图片"
                    )
                    break

        return sent_any, sent_any

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        ctx: _SendCtx,
    ) -> tuple[bool, bool]:
        """发送一个分组，返回 (是否发送成功, 是否发送了卡片)。"""
        items = list(group.contents) if group.contents else self._iter_contents(result)
        texts, images, graphics, heavies, _others = self._classify(items)

        sent_any = False
        sent_card = False
        sent_plain = False

        # 显式要求渲染卡片：整组一张卡片（已包含文本/图片/转发），
        # 轻媒体不再重复发送，仅重媒体单独补发。
        if group.render_card is True:
            if ctx.budget.take():
                if await self._send_card(event, result, ctx):
                    sent_any = True
                    sent_card = True
            for cont in heavies:
                if not ctx.budget.take():
                    if await self._send_overflow_media(event, cont, ctx):
                        sent_any = True
                        continue
                    logger.warning("[qqofficial] 回复额度不足，省略后续重媒体")
                    break
                seg = await self._heavy_segment(cont, send_mode=ctx.audio_send_mode)
                if seg and await self._send_with_ctx(event, [seg], ctx):
                    sent_any = True
                    if not isinstance(seg, Plain):
                        ctx.sent_media.append(cont)
            return sent_any, sent_card

        single_heavy = len(heavies) == 1 and not images and not graphics
        preview_card = (
            group.render_card is None
            and single_heavy
            and self.cfg.single_heavy_render_card
        )

        # 单条重媒体的可选预览卡片（优先原生图文卡片）。
        if preview_card:
            if await self._send_native_card(event, result, ctx):
                sent_any = True
                sent_card = True
            elif ctx.budget.take():
                if await self._send_card(event, result, ctx):
                    sent_any = True
                    sent_card = True

        # 重媒体：每个占一条消息
        for cont in heavies:
            if not ctx.budget.take():
                if await self._send_overflow_media(event, cont, ctx):
                    sent_any = True
                    continue
                logger.warning("[qqofficial] 回复额度不足，省略后续重媒体")
                break
            seg = await self._heavy_segment(cont, send_mode=ctx.audio_send_mode)
            if seg and await self._send_with_ctx(event, [seg], ctx):
                sent_any = True
                if isinstance(seg, Plain):
                    sent_plain = True  # 已发送文本提示，无需再补正文
                else:
                    ctx.sent_media.append(cont)

        # 图文内容：整组渲染为一张卡片，避免逐图占用额度
        if graphics and ctx.budget.take():
            if await self._send_card(event, result, ctx):
                sent_any = True
                sent_card = True

        # 纯图片图集：卡片化并分页
        if images and not graphics:
            ok, card = await self._send_image_gallery(event, result, images, ctx)
            sent_any = sent_any or ok
            sent_card = sent_card or card

        # 纯文本
        if texts and not sent_card and not sent_plain and not images and not graphics:
            if ctx.budget.take():
                text = "\n".join(texts)
                if ctx.markdown:
                    md = self._md_text(result) or text
                    ok = await self._send_with_ctx(
                        event, [Plain(md)], ctx, markdown=True
                    )
                else:
                    ok = await self._send_with_ctx(event, [Plain(text)], ctx)
                if ok:
                    sent_any = True

        return sent_any, sent_card

    @staticmethod
    def _media_kind(cont: MediaContent) -> str | None:
        if isinstance(cont, AudioContent):
            return "audio"
        if isinstance(cont, VideoContent):
            return "video"
        return None

    @staticmethod
    def _media_index(result: ParseResult, cont: MediaContent) -> int | None:
        for index, item in enumerate(result.contents):
            if item is cont:
                return index
        return None

    def _current_send_mode(self, ctx: _SendCtx) -> str:
        mode = ctx.audio_send_mode or ("file" if self.cfg.audio_to_file else "record")
        return "file" if str(mode).lower() == "file" else "record"

    def _quality_menu_page(
        self,
        result: ParseResult,
        cont: MediaContent,
        kind: str,
        options,
        index: int,
        owner: str,
        ctx: _SendCtx,
    ) -> MenuPage:
        label = (
            getattr(cont, "quality_label", None)
            or getattr(cont, "quality", None)
            or "默认"
        )
        current_mode = self._current_send_mode(ctx)
        if isinstance(cont, AudioContent):
            current = f"{'文件' if current_mode == 'file' else '语音'}·{label}"
        else:
            current = label

        modes: list[tuple[str | None, str | None]] = (
            [("file", "文件"), ("record", "语音")]
            if kind == "audio"
            else [(None, None)]
        )
        rows: list[list[MenuButton]] = []
        resource_id = result.get_resource_id()
        url = result.url or ""
        platform = result.platform.name

        for mode, mode_label in modes:
            buttons: list[MenuButton] = []
            for option in options:
                text = f"{mode_label}·{option.label}" if mode_label else option.label
                selected = option.key == getattr(cont, "quality", None) and (
                    mode is None or mode == current_mode
                )
                buttons.append(
                    MenuButton(
                        label=text,
                        data={
                            "a": "parser_quality",
                            "u": url,
                            "p": platform,
                            "r": resource_id,
                            "i": index,
                            "q": option.key,
                            "l": option.label,
                            "k": kind,
                            "m": mode or "",
                            "o": owner,
                        },
                        style="primary" if selected else "default",
                        action_id="parser_quality",
                        group=f"q{index}{mode or 'v'}",
                    )
                )
                if len(buttons) == MAX_BUTTONS_PER_ROW:
                    rows.append(buttons)
                    buttons = []
            if buttons:
                rows.append(buttons)

        title = f"文件 {index + 1}" if len(ctx.sent_media) > 1 else "切换档位"
        body = f"当前：**{current}**\n\n点击下方按钮切换档位（仅发起者可用）。"
        return MenuPage(id=f"quality:{index}", title=title, body=body, rows=rows)

    async def _send_quality_menu(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        ctx: _SendCtx,
    ) -> None:
        owner = event.get_sender_id()
        if not owner or not result.url:
            return
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            umo = ""
        resource_id = result.get_resource_id()
        seen: set[int] = set()

        for cont in list(ctx.sent_media):
            if id(cont) in seen:
                continue
            seen.add(id(cont))
            kind = self._media_kind(cont)
            if kind is None:
                continue
            options = [opt for opt in result.quality_options if opt.kind == kind]
            if not options:
                continue
            if kind == "video" and len(options) <= 1:
                continue

            index = self._media_index(result, cont)
            if index is None:
                continue
            key = (umo, owner, resource_id, index)
            if not self.menu_registry.claim_show(key):
                continue
            if not ctx.budget.take():
                self.menu_registry.release_show(key)
                continue

            page = self._quality_menu_page(
                result, cont, kind, options, index, owner, ctx
            )
            if not page.rows:
                ctx.budget.remaining += 1
                self.menu_registry.release_show(key)
                continue

            ctx.keyboard = None
            if not await send_menu(event, page, self.menu_store, owner=owner):
                logger.warning("[qqofficial] 画质菜单发送失败")
                ctx.budget.remaining += 1
                self.menu_registry.release_show(key)

    async def send_media_only(
        self,
        event: AstrMessageEvent,
        cont: MediaContent,
        *,
        send_mode: str | None = None,
    ) -> bool:
        """只发送一个已选档位的媒体，不重复发送元信息。"""
        ctx = _SendCtx(
            budget=_Budget(reply_budget(event, self.cfg.qq_reply_budget_override)),
            audio_send_mode=send_mode,
        )
        try:
            if event.is_button_interaction():
                ctx.quote = False
        except Exception:
            pass

        seg = await self._heavy_segment(cont, send_mode=send_mode)
        if not seg:
            return False
        ok = await self._send_with_ctx(event, [seg], ctx)
        if (
            not ok
            and isinstance(cont, AudioContent)
            and (send_mode or ("file" if self.cfg.audio_to_file else "record"))
            == "record"
        ):
            seg = await self._heavy_segment(cont, send_mode="file")
            if seg:
                ok = await self._send_with_ctx(event, [seg], ctx)
        return ok

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        quality_menu: bool = False,
    ) -> bool:
        groups = self._resolve_groups(result)
        ctx = _SendCtx(
            budget=_Budget(reply_budget(event, self.cfg.qq_reply_budget_override)),
            quote=bool(self.cfg.qq_quote_reply),
            keyboard=self._keyboard(result),
            markdown=self._markdown_enabled(result),
            overflow_mode=str(self.cfg.qq_overflow_mode or "off").lower(),
            overflow_left=max(0, int(self.cfg.qq_overflow_limit or 0)),
        )
        try:
            if event.is_button_interaction():
                ctx.quote = False
        except Exception:
            pass

        # 若后续存在媒体分组，则跳过只用于展示元信息的空卡片分组，
        # 元信息会随媒体卡片一并展示，避免重复占用回复额度。
        has_media_groups = any(self._group_has_media(g) for g in groups)

        sent = False
        sent_any_card = False
        for group in groups:
            # 空分组仅在“唯一分组且显式要求卡片”时有意义；
            # 其余空分组要么无内容，要么元信息会随媒体卡片一起展示。
            if not group.contents and not (group.render_card and not has_media_groups):
                continue
            ok, sent_card = await self._send_group(event, result, group, ctx)
            sent = sent or ok
            sent_any_card = sent_any_card or sent_card

        # 转发内容单独成卡（图集首张卡片已包含转发内容时会跳过）
        if result.repost and not sent_any_card:
            if await self._send_native_card(event, result, ctx):
                sent = True
            elif ctx.budget.take():
                if await self._send_card(event, result, ctx, include_repost=True):
                    sent = True

        if not sent:
            segs = self._build_text_fallback(result)
            if ctx.markdown:
                md = self._md_text(result)
                if md:
                    segs = [Plain(md)]
            text = (
                segs[0].text
                if segs and isinstance(segs[0], Plain) and segs[0].text
                else ""
            )
            if segs and ctx.budget.take():
                await self._send_with_ctx(event, segs, ctx, markdown=ctx.markdown)
            elif text:
                await self._send_overflow_text(event, text, ctx, markdown=ctx.markdown)

        if quality_menu and sent and ctx.sent_media:
            await self._send_quality_menu(event, result, ctx)

        return sent

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta
