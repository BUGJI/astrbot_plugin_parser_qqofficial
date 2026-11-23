import time
from re import Match, sub
from typing import ClassVar
from urllib.parse import urlparse

from aiohttp import ClientError

from astrbot.api import logger

from ..config import PluginConfig
from ..data import Platform, QualityOption
from ..download import Downloader
from .base import BaseParser, QualityRequest, get_quality_request, handle
from .ncm_login import QUALITY_LABELS, NCMLogin

QUALITY_LEVELS = ("standard", "exhigh", "lossless", "hires")
FALLBACK_LEVELS = ("exhigh", "standard")
AUDIO_EXTENSIONS = ("flac", "mp3", "m4a", "mp4", "ogg", "opus")


def _safe_filename(text: str) -> str:
    """清理文件名中的路径分隔符与非法字符"""
    value = sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .")
    return value[:80] or "song"


class NCMParser(BaseParser):
    """网易云音乐解析器"""

    platform: ClassVar[Platform] = Platform(name="ncm", display_name="网易云")

    @classmethod
    def allowed_quality_values(cls, kind: str) -> tuple[str, ...] | None:
        return QUALITY_LEVELS if kind == "audio" else None

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://music.163.com"})
        self.cookiejar = self.configure_cookies(
            config.parser.ncm, "music.163.com", targets=[]
        )
        self.reload_cookie()

        preferred = str(getattr(self.mycfg, "audio_quality", "lossless") or "").lower()
        self.audio_quality = preferred if preferred in QUALITY_LEVELS else "lossless"
        fallback = str(
            getattr(self.mycfg, "audio_quality_fallback", "exhigh") or ""
        ).lower()
        self.audio_quality_fallback = (
            fallback if fallback in FALLBACK_LEVELS else "exhigh"
        )
        self._last_refresh_attempt = 0.0

    def reload_cookie(self) -> None:
        cookie = self.cookiejar.cookies_str
        if cookie:
            self.headers["cookie"] = cookie
        else:
            self.headers.pop("cookie", None)

    @handle("163cn.tv", r"163cn\.tv/(?P<short_key>\w+)")
    async def _parse_short(self, searched: Match[str]):
        short_url = f"https://163cn.tv/{searched.group('short_key')}"
        # 让框架跟随 302 后再走通用解析
        return await self.parse_with_redirect(short_url)

    @handle("y.music.163.com", r"y\.music\.163\.com/m/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com/#/song\?.*id=(?P<song_id>\d+)")
    @handle("music.163.com", r"music\.163\.com/song\?.*id=(?P<song_id>\d+)")
    async def _parse_song(self, searched: Match[str]):
        song_id = searched.group("song_id")
        detail_url = (
            f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        )
        # 1. 取歌曲元数据
        async with self.session.get(detail_url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"[NCM] 获取歌曲信息失败 HTTP {resp.status}")
            detail_json = await resp.json(content_type=None)
            logger.debug(f"[NCM] 歌曲信息: {detail_json}")

        songs = detail_json.get("songs") or []
        if not songs:
            raise ValueError("[NCM] 未找到该歌曲")
        song = songs[0]

        title = song.get("name", "")
        aliases = song.get("alias") or []
        sub_title = aliases[0] if aliases else ""  # 别名
        album = song.get("album") or {}
        album_name = album.get("name", "")
        pic_url = album.get("picUrl") or ""
        cover_url = f"{pic_url}?param=640y640" if pic_url else ""
        duration_ms = song.get("duration", 0)

        # 作者信息
        ar_list = song.get("artists") or []
        author_name = " / ".join(ar.get("name", "") for ar in ar_list)
        author_avatar = ar_list[0].get("img1v1Url", "") if ar_list else ""

        # 2. 取播放地址（偏好音质失败时按降级档位重试）
        request = get_quality_request("audio")
        play_info = await self._get_play_info(song_id, request=request)
        audio_url = play_info.get("url") or ""
        if not audio_url:
            raise ValueError("[NCM] 无法获取播放地址（可能受版权或登录态限制）")

        # 3. 组装结果
        author = self.create_author(author_name, author_avatar)
        # 音频按普通文件发送（audio_to_file 默认开启），避免 mp3 伪装成视频被 QQ 拒绝
        file_stem = _safe_filename(f"{title} - {author_name}" if author_name else title)
        ext = self._audio_ext(play_info, audio_url)
        requested_level = request.key if request else self.audio_quality
        actual_level = str(play_info.get("level") or requested_level)
        level_tag = _safe_filename(actual_level)
        audio_name = (
            f"{file_stem} [{level_tag}].{ext}" if request else f"{file_stem}.{ext}"
        )
        audio = self.create_audio_content(
            self.downloader.download_audio(
                audio_url,
                audio_name=audio_name,
                headers=self.headers,
                proxy=self.proxy,
            ),
            duration=duration_ms / 1000,
            quality=actual_level,
            quality_label=QUALITY_LABELS.get(actual_level, actual_level),
        )

        # 4. 返回
        return self.result(
            title=f"{title}{'（' + sub_title + '）' if sub_title else ''}",
            text=f"专辑：{album_name}",
            author=author,
            contents=[audio],
            quality_options=[
                QualityOption(level, QUALITY_LABELS.get(level, level), "audio")
                for level in QUALITY_LEVELS
            ],
            timestamp=None,
            url=f"https://music.163.com/#/song?id={song_id}",
            extra={"cover_url": cover_url} if cover_url else {},
        )

    def _quality_levels(self) -> list[str]:
        levels: list[str] = []
        for level in (self.audio_quality, self.audio_quality_fallback):
            if level and level not in levels:
                levels.append(level)
        return levels

    async def _get_play_info(
        self, song_id: str, request: QualityRequest | None = None
    ) -> dict:
        login = NCMLogin(
            self.cfg, jar=self.cookiejar, session=self.session, proxy=self.proxy
        )
        if request and request.strict:
            # 显式选择：只尝试该档位，失败即报错
            levels = [request.key]
        elif request:
            # 用户偏好：先试偏好档位，再回退到平台默认梯度
            levels = [request.key, *self._quality_levels()]
        else:
            levels = self._quality_levels()
        levels = list(dict.fromkeys(levels))
        for level in levels:
            item = await self._fetch_play_item(login, song_id, level)
            if item:
                return item

        if (request is None or not request.strict) and self._should_try_refresh():
            logger.info("[NCM] 所有音质档位均失败，尝试刷新登录状态")
            if await self._try_refresh(login):
                item = await self._fetch_play_item(login, song_id, levels[0])
                if item:
                    return item

        if request and request.strict:
            label = QUALITY_LABELS.get(request.key, request.key)
            raise ValueError(
                f"[NCM] 所选音质 {label} 不可用（可能受版权、VIP 或登录态限制）"
            )
        raise ValueError("[NCM] 无法获取播放地址（可能受版权、VIP 或登录态限制）")

    async def _fetch_play_item(
        self, login: NCMLogin, song_id: str, level: str
    ) -> dict | None:
        body = await login.request(
            "/api/song/enhance/player/url/v1",
            {
                "ids": f"[{song_id}]",
                "level": level,
                "encodeType": "flac" if level in ("lossless", "hires") else "mp3",
            },
            extra_cookies={"os": "pc"},
        )
        if self.headers.get("cookie") != self.cookiejar.cookies_str:
            self.reload_cookie()

        data = body.get("data") or []
        item = data[0] if isinstance(data, list) and data else {}
        if not isinstance(item, dict):
            return None

        url = str(item.get("url") or "")
        if url:
            if item.get("freeTrialInfo"):
                logger.warning(
                    f"[NCM] {QUALITY_LABELS.get(level, level)} 仅返回试听片段，尝试下一档位"
                )
                return None
            actual = str(item.get("level") or level)
            logger.debug(
                f"[NCM] 请求音质 {QUALITY_LABELS.get(level, level)} -> "
                f"实际 {actual}, type={item.get('type')}, url={url[:80]}"
            )
            return item

        if body.get("code") not in (200, None):
            logger.debug(
                f"[NCM] 获取 {QUALITY_LABELS.get(level, level)} 播放地址失败 "
                f"code={body.get('code')} msg={body.get('message')}"
            )
        return None

    def _should_try_refresh(self) -> bool:
        cookies = self.cookiejar.get(domain="music.163.com", path="/", secure=True)
        if not cookies.get("MUSIC_U"):
            return False
        now = time.monotonic()
        if now - self._last_refresh_attempt < 3600:
            return False
        self._last_refresh_attempt = now
        return True

    async def _try_refresh(self, login: NCMLogin) -> bool:
        try:
            ok, message = await login.refresh(force=True)
        except Exception as e:
            logger.warning(f"[NCM] 刷新登录状态失败: {e!r}")
            return False
        if ok:
            self.reload_cookie()
            logger.info("[NCM] 登录状态已刷新")
        else:
            logger.warning(f"[NCM] 刷新登录状态未成功: {message}")
        return ok

    def _audio_ext(self, item: dict, url: str) -> str:
        type_name = str(item.get("type") or "").lower().lstrip(".")
        if type_name in AUDIO_EXTENSIONS:
            return type_name

        path = urlparse(url).path.lower()
        file_name = path.rsplit("/", 1)[-1]
        if "." in file_name:
            suffix = file_name.rsplit(".", 1)[-1]
            if suffix in AUDIO_EXTENSIONS:
                return suffix

        actual_level = str(item.get("level") or "").lower()
        return "flac" if actual_level in ("lossless", "hires") else "mp3"

    # 3. 直链 mp3 —— 直接下载
    @handle("music.126.net", r"https?://[^/]*music\.126\.net/.*\.mp3(?:\?.*)?$")
    async def _parse_direct_mp3(self, searched: Match[str]):
        url = searched.group(0)  # 整条 url
        audio = self.create_audio_content(url)
        return self.result(
            title="网易云音乐",
            text="直链音频",
            contents=[audio],
            url=url,
        )

    @handle(
        "music.163.com/song/media/outer/url",
        r"(https?://music\.163\.com/song/media/outer/url\?[^>\s]+)",
    )
    async def _parse_private_outer(self, searched: Match[str]):
        # 整条原始 URL 就是直链
        private_url = searched.group(0)
        logger.debug(f"[NCM] 私人直链: {private_url}")
        audio = self.create_audio_content(private_url)
        return self.result(
            title="网易云音乐（私人直链）",
            text="直链音频",
            contents=[audio],
            url=private_url,
        )
