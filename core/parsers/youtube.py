import re
from typing import ClassVar

import msgspec
from msgspec import Struct

from astrbot.api import logger

from ..config import PluginConfig
from ..data import QualityOption
from ..download import Downloader
from .base import BaseParser, Platform, ensure_ok, get_quality_request, handle

YOUTUBE_VIDEO_QUALITIES = (
    ("2160", "4K"),
    ("1440", "2K"),
    ("1080", "1080P"),
    ("720", "720P"),
    ("480", "480P"),
    ("360", "360P"),
)
YOUTUBE_QUALITY_LABELS = dict(YOUTUBE_VIDEO_QUALITIES)


class YouTubeParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="youtube", display_name="油管")

    @classmethod
    def allowed_quality_values(cls, kind: str) -> tuple[str, ...] | None:
        if kind != "video":
            return None
        return tuple(key for key, _ in YOUTUBE_VIDEO_QUALITIES)

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update({"Referer": "https://www.youtube.com/"})
        self.cookiejar = self.configure_cookies(
            config.parser.youtube, "youtube.com", targets=[]
        )
        if not self.mycfg.cookies:
            logger.warning("油管Cookie未配置，将无法解析相关媒体")

    @handle("youtu", r"youtu\.be/[A-Za-z\d\._\?%&\+\-=/#]+")
    @handle(
        "youtube",
        r"youtube\.com/(?:watch|shorts)(?:/[A-Za-z\d_\-]+|\?v=[A-Za-z\d_\-]+)",
    )
    async def _parse_video(self, searched: re.Match[str]):
        return await self.parse_video(searched)

    async def parse_video(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url = searched.group(0)
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"

        request = get_quality_request("video")
        override = request.key if request else None
        height = override if override and override.isdigit() else "720"
        quality_label = YOUTUBE_QUALITY_LABELS.get(height, f"{height}P")

        video_info = await self.downloader.ytdlp_extract_info(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
        )
        author = await self._fetch_author_info(video_info.channel_id)

        contents = []
        if video_info.duration <= self.cfg.max_duration:
            video = self.downloader.ytdlp_download_video(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
                format=f"bv*[height<={height}]+ba/b[height<={height}]",
                cache_key=f"{height}p" if override else None,
                node=True,
            )
            contents.append(
                self.create_video_content(
                    video,
                    video_info.thumbnail,
                    video_info.duration,
                    quality=height,
                    quality_label=quality_label,
                )
            )
        else:
            contents.extend(self.create_image_contents([video_info.thumbnail]))

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            quality_options=[
                QualityOption(key, label, "video")
                for key, label in YOUTUBE_VIDEO_QUALITIES
            ],
            timestamp=video_info.timestamp,
            url=url,
        )

    @handle(
        "ym",
        r"^ym(?P<url>https?://(?:www\.)?(youtu\.be/[A-Za-z\d_-]+|youtube\.com/(?:watch|shorts)(?:\?v=[A-Za-z\d_-]+|/[A-Za-z\d_-]+)))",
    )
    async def ym(self, searched: re.Match[str]):
        """获取油管的音频(需加ym前缀)"""
        url = searched.group("url")
        video_info = await self.downloader.ytdlp_extract_info(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
        )
        author = await self._fetch_author_info(video_info.channel_id)

        contents = []
        contents.extend(self.create_image_contents([video_info.thumbnail]))

        if video_info.duration <= self.cfg.max_duration:
            audio_task = self.downloader.ytdlp_download_audio(
                url,
                cookiefile=self.cookiejar.cookie_file,
                headers=self.headers,
                proxy=self.proxy,
            )
            contents.append(
                self.create_audio_content(audio_task, duration=video_info.duration)
            )

        return self.result(
            title=video_info.title,
            author=author,
            contents=contents,
            timestamp=video_info.timestamp,
        )

    async def _fetch_author_info(self, channel_id: str):
        url = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
        payload = {
            "context": {
                "client": {
                    "hl": "zh-HK",
                    "gl": "US",
                    "deviceMake": "Apple",
                    "deviceModel": "",
                    "clientName": "WEB",
                    "clientVersion": "2.20251002.00.00",
                    "osName": "Macintosh",
                    "osVersion": "10_15_7",
                },
                "user": {"lockedSafetyMode": False},
                "request": {
                    "useSsl": True,
                    "internalExperimentFlags": [],
                    "consistencyTokenJars": [],
                },
            },
            "browseId": channel_id,
        }
        async with self.session.post(
            url,
            json=payload,
            headers=self.headers,
            proxy=self.proxy,
        ) as resp:
            ensure_ok(resp, "YouTube browse API")
            browse = msgspec.json.decode(await resp.read(), type=BrowseResponse)

        return self.create_author(browse.name, browse.avatar_url, browse.description)


class Thumbnail(Struct):
    url: str


class AvatarInfo(Struct):
    thumbnails: list[Thumbnail]


class ChannelMetadataRenderer(Struct):
    title: str
    description: str
    avatar: AvatarInfo


class Metadata(Struct):
    channelMetadataRenderer: ChannelMetadataRenderer


class Avatar(Struct):
    thumbnails: list[Thumbnail]


class BrowseResponse(Struct):
    metadata: Metadata

    @property
    def name(self) -> str:
        return self.metadata.channelMetadataRenderer.title

    @property
    def avatar_url(self) -> str | None:
        thumbnails = self.metadata.channelMetadataRenderer.avatar.thumbnails
        return thumbnails[0].url if thumbnails else None

    @property
    def description(self) -> str:
        return self.metadata.channelMetadataRenderer.description
