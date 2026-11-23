import asyncio
import html
import json
import re
from typing import Any, ClassVar
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests

from ..config import PluginConfig
from ..data import MediaContent, Platform, SendGroup, TextContent, VideoContent
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, get_image_preference, handle
from .xiaoheihe_sign import V4_DATA, V4_EP, sign_path


class XiaoheiheParser(BaseParser):
    platform: ClassVar[Platform] = Platform(name="xiaoheihe", display_name="小黑盒")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.xiaoheihe
        self.headers.update(
            {
                "accept": "application/json, text/plain, */*",
                "referer": "https://www.xiaoheihe.cn/",
                "origin": "https://www.xiaoheihe.cn",
            }
        )
        if self.mycfg.cookies:
            self.headers["cookie"] = self.mycfg.cookies

    @handle(
        "xiaoheihe.cn/app/bbs/link",
        r"xiaoheihe\.cn/app/bbs/link/(?P<link_id>[0-9a-z]+)",
    )
    async def _parse_bbs_web(self, searched: re.Match[str]):
        return await self._parse_bbs_by_link_id(searched.group("link_id"))

    @handle(
        "api.xiaoheihe.cn/v3/bbs/app/api/web/share",
        r"api\.xiaoheihe\.cn/v3/bbs/app/api/web/share\?[A-Za-z0-9._%&+=/#@?;-]*link_id=(?P<link_id>[0-9a-z]+)",
    )
    async def _parse_bbs_share(self, searched: re.Match[str]):
        return await self._parse_bbs_by_link_id(searched.group("link_id"))

    @handle(
        "api.xiaoheihe.cn/game/share_game_detail",
        r"api\.xiaoheihe\.cn/game/share_game_detail\?[A-Za-z0-9._%&+=/#@?;-]*appid=(?P<appid>[0-9a-z]+)[A-Za-z0-9._%&+=/#@?;-]*game_type=(?P<game_type>[a-z]+)",
    )
    async def _parse_game_share(self, searched: re.Match[str]):
        return await self._parse_game_by_appid(
            searched.group("appid"), searched.group("game_type")
        )

    @handle(
        "xiaoheihe.cn/app/topic/game",
        r"xiaoheihe\.cn/app/topic/game/(?P<game_type>[a-z]+)/(?P<appid>[0-9a-z]+)",
    )
    async def _parse_game_web(self, searched: re.Match[str]):
        return await self._parse_game_by_appid(
            searched.group("appid"), searched.group("game_type")
        )

    async def _parse_bbs_by_link_id(self, link_id: str):
        request_ctx = await self._build_request_context()
        payload = await self._fetch_link_tree(link_id, request_ctx)
        link = self._extract_link(payload)

        title = self._clean_text(str(link.get("title") or "")) or None
        final_url = f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"
        author = self._build_author(link)

        segments = self._parse_body_segments(link)
        body_text = "\n\n".join(
            value for kind, value in segments if kind == "text"
        ).strip()
        image_urls = [value for kind, value in segments if kind == "image"]
        video_content = self._build_video_content(link)
        show_body_text = bool(getattr(self.mycfg, "show_body_text", False))

        # 构建发送内容
        # 注意：旧配置缺少这些字段时，ConfigNode 会返回 None 而非触发 getattr 默认值，
        # 因此仅将显式的 False 视为关闭，None（未配置）按默认开启处理。
        mixed_layout = getattr(self.mycfg, "mixed_layout", None) is not False
        contents: list[MediaContent] = []

        if mixed_layout:
            # 图文混排：文字块与图片块按原文顺序交错
            for kind, value in segments:
                if kind == "image":
                    contents.extend(
                        self.create_image_contents([value], headers=self.headers)
                    )
                elif kind == "text" and show_body_text:
                    contents.append(TextContent(value))
            if video_content is not None:
                contents.append(video_content)
        else:
            # 旧排版：先发完全部图片（含视频），再统一发文字
            for kind, value in segments:
                if kind == "image":
                    contents.extend(
                        self.create_image_contents([value], headers=self.headers)
                    )
            if video_content is not None:
                contents.append(video_content)
            if show_body_text:
                for kind, value in segments:
                    if kind == "text":
                        contents.append(TextContent(value))

        send_groups: list[SendGroup] = []
        primary_contents = [
            cont for cont in contents if not isinstance(cont, VideoContent)
        ]
        video_contents = [cont for cont in contents if isinstance(cont, VideoContent)]
        if primary_contents:
            send_groups.append(SendGroup(contents=primary_contents))
        for content in video_contents:
            send_groups.append(SendGroup(contents=[content], force_merge=False))

        info_parts: list[str] = []
        if image_urls:
            info_parts.append(f"正文图片 {len(image_urls)} 张")
        if video_content is not None:
            info_parts.append("正文视频 1 个")

        return self.result(
            title=title,
            text=None if show_body_text else (body_text or None),
            url=final_url,
            author=author,
            contents=contents,
            send_groups=send_groups,
            extra={
                "info": "，".join(info_parts) if info_parts else None,
                "body_image_urls": image_urls,
                "source_kind": "xiaoheihe_link_tree",
                "has_video": bool(link.get("has_video")),
                "video_url": link.get("video_url"),
            },
        )

    async def _parse_game_by_appid(self, appid: str, game_type: str):
        appid = appid.strip()
        if not appid:
            raise ParseException("无效的小黑盒游戏 appid")

        web_url = self._canonical_game_web_url(appid, game_type)
        html_text = await self._request_text(web_url, headers=self.headers)
        game_root = self._extract_game_root(html_text, appid)

        steam_appid = self._pick_steam_appid(game_root, appid)
        intro_root = await self._fetch_game_intro(steam_appid) if steam_appid else {}

        title = self._build_game_title(game_root)
        desc = self._build_game_desc(html_text, game_root, intro_root)
        image_urls = self._extract_game_images(game_root, html_text)
        video_entries = self._extract_game_videos(game_root, html_text)

        show_body_text = bool(getattr(self.mycfg, "show_body_text", False))
        text_content = TextContent(desc) if show_body_text and desc else None

        contents: list[MediaContent] = []
        if image_urls:
            contents.extend(
                self.create_image_contents(image_urls, headers=self.headers)
            )
        for video_url, video_cover in video_entries:
            contents.append(self._build_video_content_from_url(video_url, video_cover))
        if text_content is not None:
            contents.append(text_content)

        send_groups: list[SendGroup] = []
        primary_contents = [
            cont for cont in contents if not isinstance(cont, VideoContent)
        ]
        video_contents = [cont for cont in contents if isinstance(cont, VideoContent)]
        if primary_contents:
            send_groups.append(SendGroup(contents=primary_contents))
        for content in video_contents:
            send_groups.append(SendGroup(contents=[content], force_merge=False))

        info_parts: list[str] = []
        if image_urls:
            info_parts.append(f"游戏图片 {len(image_urls)} 张")
        if video_entries:
            info_parts.append(f"游戏视频 {len(video_entries)} 个")

        return self.result(
            title=title,
            text=None if show_body_text else (desc or None),
            url=web_url,
            contents=contents,
            send_groups=send_groups,
            extra={
                "info": "，".join(info_parts) if info_parts else None,
                "body_image_urls": image_urls,
                "source_kind": "xiaoheihe_game_page",
                "game_appid": appid,
                "steam_appid": steam_appid,
                "game_type": game_type,
                "video_url": video_entries[0][0] if video_entries else None,
                "video_urls": [item[0] for item in video_entries],
            },
        )

    async def _build_request_context(self) -> dict[str, str]:
        token = self._extract_xhh_tokenid_from_cookies()
        device_id = None
        if token and token.startswith("B"):
            device_id = token[1:]

        if not token:
            token, device_id = await self._fetch_xhh_tokenid()

        if not token:
            raise ParseException("获取小黑盒 x_xhh_tokenid 失败")

        return {
            "x_xhh_tokenid": token,
            "device_id": device_id or "",
        }

    def _extract_xhh_tokenid_from_cookies(self) -> str | None:
        cookie_header = self.headers.get("cookie", "")
        if not cookie_header:
            return None
        matched = re.search(r"(?:^|;\s*)x_xhh_tokenid=([^;]+)", cookie_header)
        if matched:
            return matched.group(1)
        return None

    @staticmethod
    @staticmethod
    def _canonical_game_web_url(appid: str, game_type: str) -> str:
        return f"https://www.xiaoheihe.cn/app/topic/game/{game_type.strip().lower() or 'pc'}/{appid}"

    async def _fetch_xhh_tokenid(self) -> tuple[str, str | None]:
        device_id = await self._fetch_device_id()
        if not device_id:
            raise ParseException("小黑盒 deviceprofile 未返回 deviceId")
        return f"B{device_id}", device_id

    async def _fetch_device_id(self) -> str | None:
        payload = {
            "appId": "heybox_website",
            "organization": "0yD85BjYvGFAvHaSQ1mc",
            "ep": V4_EP,
            "data": V4_DATA,
            "os": "web",
            "encode": 5,
            "compress": 2,
        }
        response = await self._request_json(
            "POST",
            "https://fp-it.portal101.cn/deviceprofile/v4",
            json=payload,
            headers={"accept": "application/json, text/plain, */*"},
        )
        detail = response.get("detail") or {}
        device_id = detail.get("deviceId")
        return str(device_id) if device_id else None

    async def _fetch_game_intro(self, steam_appid: int) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            "https://api.xiaoheihe.cn/game/game_introduction/",
            params={"steam_appid": steam_appid, "return_json": 1},
            headers={"accept": "application/json, text/plain, */*", **self.headers},
        )
        if payload.get("status") != "ok":
            return {}
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def _fetch_link_tree(
        self, link_id: str, request_ctx: dict[str, str]
    ) -> dict[str, Any]:
        sig = sign_path("/bbs/app/link/tree")
        params = {
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "heybox_id": "",
            "x_os_type": "Windows",
            "device_info": "Chrome",
            "device_id": request_ctx.get("device_id", ""),
            "link_id": link_id,
            "owner_only": "1",
            **sig,
        }

        payload = await self._request_json(
            "GET",
            "https://api.xiaoheihe.cn/bbs/app/link/tree",
            params=params,
            cookies={"x_xhh_tokenid": request_ctx["x_xhh_tokenid"]},
            headers=self.headers,
        )
        status = payload.get("status")
        if status != "ok":
            raise ParseException(f"小黑盒 link/tree 请求失败: {status}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ParseException("小黑盒 link/tree 结果为空")
        return result

    def _extract_link(self, payload: dict[str, Any]) -> dict[str, Any]:
        link = payload.get("link")
        if not isinstance(link, dict):
            raise ParseException("小黑盒 link/tree 缺少 link 节点")
        return link

    def _extract_game_root(self, html_text: str, appid: str) -> dict[str, Any]:
        payload = self._extract_nuxt_data_payload(html_text)
        if not payload:
            raise ParseException("小黑盒游戏页未找到 __NUXT_DATA__")
        root = self._devalue_resolve_root(payload)
        game = self._find_best_game_dict(root, appid)
        if not game:
            raise ParseException("小黑盒游戏页未找到游戏详情数据")
        return game

    def _extract_nuxt_data_payload(self, html_text: str) -> list[Any] | None:
        matched = re.search(
            r'<script[^>]+id="__NUXT_DATA__"[^>]*>(.*?)</script>',
            html_text,
            re.S | re.I,
        )
        if not matched:
            return None
        raw = matched.group(1).strip()
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, list) else None

    def _devalue_resolve_root(self, payload: list[Any]) -> Any:
        total = len(payload)
        memo: dict[int, Any] = {}
        resolving: set[int] = set()

        def resolve(value: Any) -> Any:
            if isinstance(value, int) and 0 <= value < total:
                return resolve_index(value)
            if isinstance(value, list):
                if (
                    len(value) == 2
                    and isinstance(value[0], str)
                    and value[0]
                    in {
                        "ShallowReactive",
                        "Reactive",
                        "Ref",
                        "ShallowRef",
                        "Readonly",
                        "ShallowReadonly",
                    }
                ):
                    return resolve(value[1])
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            return value

        def resolve_index(index: int) -> Any:
            if index in memo:
                return memo[index]
            if index in resolving:
                return None
            resolving.add(index)
            memo[index] = None
            memo[index] = resolve(payload[index])
            resolving.remove(index)
            return memo[index]

        return resolve(0)

    def _find_best_game_dict(self, root: Any, appid: str) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        best_score = -1
        stack: list[Any] = [root]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                current_appid = str(current.get("appid") or "").strip()
                current_steam_appid = str(current.get("steam_appid") or "").strip()
                score = 0
                for key in (
                    "about_the_game",
                    "name",
                    "name_en",
                    "price",
                    "heybox_price",
                    "user_num",
                    "game_award",
                    "comment_stats",
                    "screenshots",
                    "share_url",
                    "share_title",
                ):
                    if key in current:
                        score += 3

                if current_appid == appid or current_steam_appid == appid:
                    score += 50

                share_url = str(current.get("share_url") or "")
                if appid and appid in share_url:
                    score += 20

                if str(current.get("type") or "").strip().lower() == "game":
                    score += 5

                if score > best_score and score >= 12:
                    best = current
                    best_score = score

                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                for value in current:
                    if isinstance(value, (dict, list)):
                        stack.append(value)
        return best

    def _pick_steam_appid(
        self, game: dict[str, Any], fallback_appid: str
    ) -> int | None:
        value = game.get("steam_appid") or fallback_appid
        try:
            return int(str(value).strip())
        except Exception:
            return None

    def _build_game_title(self, game: dict[str, Any]) -> str:
        name = str(game.get("name") or "").strip()
        name_en = str(game.get("name_en") or "").strip()
        if name and name_en:
            return f"{name}（{name_en}）"
        return name or name_en or "小黑盒游戏详情"

    def _build_game_desc(
        self, html_text: str, game: dict[str, Any], intro: dict[str, Any]
    ) -> str:
        lines: list[str] = []
        intro_text = self._format_game_intro_text(
            str(intro.get("about_the_game") or "")
        )
        if intro_text:
            lines.append(intro_text)

        types = self._parse_game_types_from_html(html_text)
        if types:
            lines.append(f"类型：{types}")

        score = str(game.get("score") or "").strip()
        score_comment = (
            (game.get("comment_stats") or {}).get("score_comment")
            if isinstance(game.get("comment_stats"), dict)
            else None
        )
        if score:
            if isinstance(score_comment, int) and score_comment > 0:
                lines.append(
                    f"小黑盒评分：{score}（{self._format_people_count(score_comment)}）"
                )
            else:
                lines.append(f"小黑盒评分：{score}")

        release_date = self._format_cn_ymd_to_dotted(
            str(intro.get("release_date") or "")
        )
        if release_date:
            lines.append(f"发布时间：{release_date}")

        developer = self._extract_company_text(intro.get("developers"))
        if developer:
            lines.append(f"开发商：{developer}")
        publisher = self._extract_company_text(intro.get("publishers"))
        if publisher:
            lines.append(f"发行商：{publisher}")

        if isinstance(game.get("price"), dict):
            price_data = game["price"]
            price = str(
                price_data.get("initial") or price_data.get("current") or ""
            ).strip()
            if price:
                lines.append(f"价格：¥ {price.replace('¥', '').strip()}")
            lowest_price = str(price_data.get("lowest_price") or "").strip()
            if lowest_price:
                lines.append(f"史低价格：¥ {lowest_price.replace('¥', '').strip()}")

        if isinstance(game.get("heybox_price"), dict):
            cost_coin = game["heybox_price"].get("cost_coin")
            yuan = self._format_yuan_from_coin(cost_coin)
            if yuan:
                lines.append(f"当前价格：¥ {yuan}")

        return "\n\n".join(line for line in lines if line).strip()

    def _parse_game_types_from_html(self, html_text: str) -> str:
        group1 = ""
        group2_tags: list[str] = []
        matched = re.search(
            r'<div class="row-2">.*?<div class="tags">(.*?)</div></div>',
            html_text,
            re.S | re.I,
        )
        tags_html = matched.group(1) if matched else ""
        if tags_html:
            matched_group = re.search(
                r'<div class="tag common"[^>]*>(.*?)</div>', tags_html, re.S | re.I
            )
            if matched_group:
                spans = re.findall(
                    r"<span[^>]*>(.*?)</span>", matched_group.group(1), re.S | re.I
                )
                tokens = [self._strip_tags(item) for item in spans]
                tokens = [
                    re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", token) for token in tokens
                ]
                tokens = [token for token in tokens if token]
                if tokens:
                    group1 = " ".join(tokens)
            raw_tags = re.findall(
                r'<p class="tag"[^>]*>(.*?)</p>', tags_html, re.S | re.I
            )
            group2_tags = [self._strip_tags(item) for item in raw_tags]
            group2_tags = [item for item in group2_tags if item]

        parts: list[str] = []
        if group1:
            parts.append(f"[ {group1} ]")
        if group2_tags:
            parts.append(f"[ {' '.join(group2_tags)} ]")
        return " ".join(parts).strip()

    def _extract_game_images(self, game: dict[str, Any], html_text: str) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        def add(candidate: Any) -> None:
            if not isinstance(candidate, str):
                return
            image = candidate.strip()
            if not image.startswith("http"):
                return
            image_lower = image.lower()
            if "/thumbnail/" in image_lower:
                return
            if any(host in image_lower for host in ("open.gtimg.cn", "qq.ugcimg.cn")):
                return
            if not any(
                keyword in image_lower
                for keyword in ("gameimg", "steam_item_assets", "screenshot")
            ):
                return
            key = image.split("?", 1)[0]
            if key in seen:
                return
            seen.add(key)
            result.append(image)

        screenshot_keys = (
            "screenshots",
            "screenshot_list",
            "screen_shot",
            "screen_shots",
            "images",
            "image_list",
            "game_imgs",
        )
        for key in screenshot_keys:
            value = game.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for nested_key in ("url", "image", "img", "src"):
                            add(item.get(nested_key))
                    else:
                        add(item)

        for key in ("header_img", "cover", "cover_img", "poster", "share_img"):
            add(game.get(key))

        if result:
            return result

        all_images = re.findall(
            r'https?://[^"\'\s<>]+\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s<>]*)?',
            html_text,
            re.I,
        )
        for image in all_images:
            add(image)
        return result

    def _extract_game_videos(
        self, game: dict[str, Any], html_text: str
    ) -> list[tuple[str, str | None]]:
        mode = str(getattr(self.mycfg, "video_send_mode", "first") or "first").lower()
        if mode == "none":
            return []

        results: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        video_cover = str(game.get("video_thumb") or "").strip() or None

        def add(url: str | None):
            if not url:
                return False
            value = str(url).strip()
            if not value or value in seen:
                return False
            seen.add(value)
            results.append((value, video_cover))
            return True

        first_only = mode != "all"

        if add(game.get("video_url")) and first_only:
            return results

        matched_m3u8 = re.findall(
            r'https?://[^"\'\s<>]+\.m3u8(?:\?[^"\'\s<>]*)?', html_text, re.I
        )
        for item in matched_m3u8:
            if add(item) and first_only:
                return results

        matched_mp4 = re.findall(
            r'https?://[^"\'\s<>]+\.(?:mp4|mov)(?:\?[^"\'\s<>]*)?', html_text, re.I
        )
        for item in matched_mp4:
            if add(item) and first_only:
                return results

        return results

    @staticmethod
    def _extract_company_text(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        values: list[str] = []
        for item in items:
            if (
                isinstance(item, dict)
                and isinstance(item.get("value"), str)
                and item.get("value")
            ):
                values.append(item["value"])
        return ",".join(values)

    @staticmethod
    def _format_people_count(count: int) -> str:
        if count >= 10000:
            return f"{count / 10000:.1f} 万人评价"
        return f"{count} 人评价"

    @staticmethod
    def _format_yuan_from_coin(coin: Any) -> str:
        try:
            value = int(coin) / 1000.0
        except Exception:
            return ""
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.2f}"

    def _format_game_intro_text(self, text: str) -> str:
        if not text:
            return ""
        stripped = self._strip_tags(text)
        stripped = stripped.replace("\u3000", " ").replace("\xa0", " ")
        stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
        return stripped

    @staticmethod
    def _strip_tags(text: str) -> str:
        if not text:
            return ""
        value = re.sub(r"(?is)<script[^>]*>.*?</script>", "", text)
        value = re.sub(r"(?is)<style[^>]*>.*?</style>", "", value)
        value = re.sub(r"(?is)<video[^>]*>.*?</video>", "", value)
        value = re.sub(r"(?is)<img[^>]*>", "", value)
        value = re.sub(r"(?i)</p\s*>", "\n\n", value)
        value = re.sub(r"(?i)<p[^>]*>", "", value)
        value = re.sub(r"(?i)</div\s*>", "\n", value)
        value = re.sub(r"(?i)<div[^>]*>", "", value)
        value = re.sub(r"(?i)<li[^>]*>", "\n・", value)
        value = re.sub(r"(?i)</li\s*>", "\n", value)
        value = re.sub(r"(?i)</(ul|ol)\s*>", "\n", value)
        value = re.sub(r"(?i)</h[1-6]\s*>", "\n", value)
        value = re.sub(r"(?i)<h[1-6][^>]*>", "\n", value)
        value = re.sub(r"(?i)<br\s*/?>", "\n", value)
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
        return value

    @staticmethod
    def _format_cn_ymd_to_dotted(text: str) -> str:
        if not text:
            return ""
        value = html.unescape(text).strip()
        value = re.sub(r"\s+", "", value)
        matched = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$", value)
        if matched:
            year, month, day = (
                matched.group(1),
                int(matched.group(2)),
                int(matched.group(3)),
            )
            return f"{year}.{month}.{day}"
        matched = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", value)
        if matched:
            year, month, day = (
                matched.group(1),
                int(matched.group(2)),
                int(matched.group(3)),
            )
            return f"{year}.{month}.{day}"
        return text.strip()

    def _build_author(self, link: dict[str, Any]):
        user = link.get("user") or {}
        if not isinstance(user, dict):
            return None
        name = self._clean_text(str(user.get("username") or user.get("nickname") or ""))
        if not name:
            return None
        avatar = str(user.get("avatar") or "") or None
        desc_parts: list[str] = []
        desc = self._clean_text(str(link.get("description") or ""))
        if desc:
            desc_parts.append(desc)
        return self.create_author(
            name=name,
            avatar_url=avatar,
            description=" · ".join(desc_parts) if desc_parts else None,
        )

    def _build_video_content(self, link: dict[str, Any]):
        if not link.get("has_video"):
            return None
        video_url = str(link.get("video_url") or "").strip()
        if not video_url:
            return None
        return self._build_video_content_from_url(video_url, None)

    def _build_video_content_from_url(
        self, video_url: str, cover_url: str | None = None
    ):
        parsed = urlparse(video_url)
        path = (parsed.path or "").lower()
        if path.endswith(".m3u8"):
            task = self.downloader.ytdlp_download_video_relaxed(
                video_url, headers=self.headers, proxy=self.proxy
            )
            return self.create_video_content_by_task(
                task, cover_url, headers=self.headers
            )
        return self.create_video_content(video_url, cover_url, headers=self.headers)

    def _parse_body_segments(self, link: dict[str, Any]) -> list[tuple[str, str]]:
        """解析正文，返回保持原文顺序的图文段列表。

        每项为 ("text", 文本) 或 ("image", 图片URL)；
        相邻的文本块会合并为一段。
        """
        raw_text = link.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return []
        try:
            blocks = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = self._clean_text(raw_text)
            return [("text", cleaned)] if cleaned else []

        if not isinstance(blocks, list):
            cleaned = self._clean_text(raw_text)
            return [("text", cleaned)] if cleaned else []

        segments: list[tuple[str, str]] = []
        seen_images: set[str] = set()

        def push_text(value: str) -> None:
            value = value.strip()
            if not value:
                return
            if segments and segments[-1][0] == "text":
                segments[-1] = ("text", segments[-1][1] + "\n\n" + value)
            else:
                segments.append(("text", value))

        def push_image(url: str) -> None:
            dedup_key = self._image_dedup_key(url)
            if url and dedup_key and dedup_key not in seen_images:
                seen_images.add(dedup_key)
                segments.append(("image", url))

        for block in blocks:
            if not isinstance(block, dict):
                continue

            block_type = str(block.get("type") or "")
            if block_type == "img":
                push_image(
                    self._normalize_image_url(str(block.get("url") or "").strip())
                )
                continue

            html_text = str(block.get("text") or "")
            if html_text:
                for kind, value in self._parse_html_block_segments(html_text):
                    if kind == "text":
                        push_text(value)
                    else:
                        push_image(value)

        return segments

    def _parse_html_block_segments(self, html_block: str) -> list[tuple[str, str]]:
        """把 HTML 文本块按内联 <img> 的出现位置拆为有序的文/图段。

        例如 "文字一<img/>文字二" 会拆为
        [("text", "文字一"), ("image", ...), ("text", "文字二")]。
        """
        segments: list[tuple[str, str]] = []
        for part in re.split(r"(<img\b[^>]*>)", html_block, flags=re.I):
            if not part:
                continue
            if re.fullmatch(r"<img\b[^>]*>", part, flags=re.I):
                matched = re.search(
                    r"data-original=\"([^\"]+)\"|src=\"([^\"]+)\"", part, re.I
                )
                if matched:
                    url = self._normalize_image_url(
                        matched.group(1) or matched.group(2) or ""
                    )
                    if url:
                        segments.append(("image", url))
            else:
                text = self._html_block_to_text(part)
                if text:
                    segments.append(("text", text))
        return segments

    def _normalize_image_url(self, url: str) -> str:
        if not url:
            return ""
        url = html.unescape(url)
        if not url.startswith("http"):
            return ""
        if "/bbs/" not in url:
            return ""
        # 接口给的 url 带七牛 imageMogr2 处理：重压缩并限制在 850x1450 内，大图会糊。
        # 开启原图模式时去掉查询串，获取平台留存的原始文件。
        # 同上：None（旧配置未配置该字段）按默认开启处理，仅显式 False 视为关闭。
        use_original = getattr(self.mycfg, "use_original_image", None) is not False
        preference = get_image_preference()
        if preference == "original":
            use_original = True
        elif preference == "compressed":
            use_original = False
        if "imageMogr2" in url and use_original:
            url = url.split("?", 1)[0]
        return url

    def _image_dedup_key(self, url: str) -> str:
        if not url:
            return ""
        base = url.split("?", 1)[0]
        base = base.replace("imgheybox1.max-c.com", "imgheybox.max-c.com")
        return base

    def _html_block_to_text(self, html_block: str) -> str:
        fragment = html.unescape(html_block)
        fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"</p>\s*<p[^>]*>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"<img\b[^>]*>", "", fragment, flags=re.I)
        fragment = re.sub(r"<[^>]+>", "", fragment)
        lines = [self._clean_text(line) for line in fragment.splitlines()]
        lines = [line for line in lines if line]
        return "\n\n".join(lines).strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text.replace("\xa0", " "))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)

        def do_request():
            return curl_requests.request(
                method,
                url,
                params=params,
                json=json,
                cookies=cookies,
                headers=merged_headers,
                impersonate="chrome131",
                proxies={"https": self.proxy, "http": self.proxy}
                if self.proxy
                else None,
                timeout=self.cfg.common_timeout,
            )

        response = await asyncio.to_thread(do_request)
        try:
            return response.json()
        except Exception as exc:
            raise ParseException(f"小黑盒接口返回非 JSON: {url}") from exc

    async def _request_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)

        def do_request():
            return curl_requests.get(
                url,
                params=params,
                headers=merged_headers,
                impersonate="chrome131",
                proxies={"https": self.proxy, "http": self.proxy}
                if self.proxy
                else None,
                timeout=self.cfg.common_timeout,
                allow_redirects=True,
            )

        response = await asyncio.to_thread(do_request)
        return str(response.text)
