"""Parser 基类定义"""

from abc import ABC
from asyncio import Task, TimeoutError, sleep
from collections.abc import Callable, Coroutine, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from re import Match, Pattern, compile
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from aiohttp import ClientError, ClientSession, ClientTimeout
from typing_extensions import Unpack

from ..config import ParserItem, PluginConfig
from ..constants import ANDROID_HEADER, COMMON_HEADER, IOS_HEADER
from ..cookie import CookieJar
from ..data import (
    AudioContent,
    Author,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    ParseResultKwargs,
    Platform,
    VideoContent,
)
from ..download import Downloader
from ..exception import ParseException, RedirectException

T = TypeVar("T", bound="BaseParser")
HandlerFunc = Callable[[T, Match[str]], Coroutine[Any, Any, ParseResult]]
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"


def ensure_ok(resp: Any, what: str = "") -> None:
    """统一检查 HTTP 响应状态，``>=400`` 时抛出带上下文的 ``ClientError``。"""
    if resp.status >= 400:
        prefix = f"{what} " if what else ""
        raise ClientError(f"{prefix}{resp.status} {resp.reason}")


@dataclass(frozen=True, slots=True)
class QualityRequest:
    """一次解析中针对某类媒体的档位请求。

    key: 档位标识（各平台自己的枚举值）
    kind: ``video`` / ``audio``
    strict: True 表示显式选择（不可用即报错）；False 表示用户偏好（不可用时回退）
    """

    key: str
    kind: str = "video"
    strict: bool = False


_QUALITY_CONTEXT: ContextVar[Mapping[str, QualityRequest]] = ContextVar(
    "parser_quality_context", default={}
)
_IMAGE_PREFERENCE: ContextVar[str | None] = ContextVar(
    "parser_image_preference", default=None
)


def get_quality_request(kind: str = "video") -> QualityRequest | None:
    """获取本次解析针对指定媒体的档位请求。"""

    return _QUALITY_CONTEXT.get().get(kind)


def get_quality_override(kind: str = "video") -> str | None:
    """获取本次解析临时指定的媒体档位标识。"""

    request = get_quality_request(kind)
    return request.key if request else None


def get_image_preference() -> str | None:
    """获取本次解析的图片偏好：``original`` / ``compressed``。"""

    return _IMAGE_PREFERENCE.get()


@contextmanager
def quality_context(
    requests: Mapping[str, QualityRequest] | None = None,
    *,
    image: str | None = None,
):
    """在解析期间注入媒体档位与图片偏好（协程安全）。"""

    quality_token = _QUALITY_CONTEXT.set(dict(requests or {}))
    image_token = _IMAGE_PREFERENCE.set(image)
    try:
        yield
    finally:
        _IMAGE_PREFERENCE.reset(image_token)
        _QUALITY_CONTEXT.reset(quality_token)


# 注册处理器装饰器
def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""

    def decorator(func: HandlerFunc[T]) -> HandlerFunc[T]:
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])

        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))

        return func

    return decorator


class BaseParser:
    """所有平台 Parser 的抽象基类

    子类必须实现：
    - platform: 平台信息（包含名称和显示名称)
    """

    _registry: ClassVar[list[type["BaseParser"]]] = []
    """ 存储所有已注册的 Parser 类 """

    platform: ClassVar[Platform]
    """ 平台信息（包含名称和显示名称） """

    if TYPE_CHECKING:
        _key_patterns: ClassVar[KeyPatterns]
        _handlers: ClassVar[dict[str, HandlerFunc]]

    def __init__(self, config: PluginConfig, downloader: Downloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.cfg = config
        self.data_dir = self.cfg.data_dir
        self.downloader = downloader
        self._session: ClientSession | None = None

    def configure_cookies(
        self,
        parser_cfg: ParserItem,
        domain: str,
        *,
        targets: Sequence[MutableMapping[str, str]] | None = None,
    ) -> CookieJar:
        """设置 ``self.mycfg``、创建 CookieJar，并把 cookie 注入请求头。

        - ``targets=None``：注入 ``self.headers``。
        - ``targets=[]``：不注入任何请求头（例如仅供 yt-dlp 使用的 cookie 文件）。
        - 其他：注入给定的多个请求头字典。
        """
        self.mycfg = parser_cfg
        cookiejar = CookieJar(self.cfg, parser_cfg, domain=domain)
        if cookiejar.cookies_str:
            for target in (self.headers,) if targets is None else targets:
                target["cookie"] = cookiejar.cookies_str
        return cookiejar

    @property
    def proxy(self) -> str | None:
        try:
            parser_cfg: ParserItem = getattr(
                self.cfg.parser, self.__class__.platform.name
            )
            return self.cfg.proxy if parser_cfg.use_proxy else None
        except AttributeError:
            return None

    def __init_subclass__(cls, **kwargs):
        """自动注册子类到 _registry"""
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:  # 跳过抽象类
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        # 获取所有被 handle 装饰的方法
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                handler = cast(HandlerFunc, attr)
                for keyword, pattern in key_patterns:
                    cls._handlers[keyword] = handler
                    cls._key_patterns.append((keyword, pattern))

        # 按关键字长度降序排序
        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        """获取所有已注册的 Parser 类"""
        return cls._registry

    @classmethod
    def allowed_quality_values(cls, kind: str) -> tuple[str, ...] | None:
        """该平台支持的档位（保持展示顺序）；None 表示不区分档位。"""
        return None

    @property
    def session(self) -> ClientSession:
        """获取当前实例的 session，惰性创建"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                proxy=self.proxy, timeout=ClientTimeout(total=self.cfg.common_timeout)
            )
        return self._session

    async def close_session(self) -> None:
        """关闭当前实例的 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        """解析 URL 提取信息

        Args:
            keyword: 关键词
            searched: 正则表达式匹配对象，由平台对应的模式匹配得到

        Returns:
            ParseResult: 解析结果

        Raises:
            ParseException: 解析失败时抛出
        """
        return await self._handlers[keyword](self, searched)

    async def parse_with_quality(
        self,
        url: str,
        quality: str | None = None,
        *,
        kind: str = "video",
        strict: bool = True,
        image: str | None = None,
    ) -> ParseResult:
        """按指定档位重新解析 URL（默认视为显式选择）。"""
        keyword, searched = self.search_url(url)
        requests = {kind: QualityRequest(quality, kind, strict)} if quality else {}
        with quality_context(requests, image=image):
            return await self.parse(keyword, searched)

    async def parse_with_redirect(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> ParseResult:
        """先重定向再解析"""
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        """搜索 URL 匹配模式"""
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise ParseException(f"无法匹配 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        """构建解析结果"""
        return ParseResult(platform=cls.platform, **kwargs)

    async def get_redirect_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 单次重定向"""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        headers = headers or COMMON_HEADER.copy()
        retries = self.cfg.download_retry_times
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=False, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(f"redirect check {resp.status} {resp.reason}")
                    return resp.headers.get("Location", url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    async def get_final_url(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """获取重定向后的 URL, 允许多次重定向"""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        headers = headers or COMMON_HEADER.copy()
        retries = 2
        for attempt in range(retries + 1):
            try:
                async with self.session.get(
                    url, headers=headers, allow_redirects=True, proxy=self.proxy
                ) as resp:
                    if resp.status >= 400:
                        raise ClientError(
                            f"final url check {resp.status} {resp.reason}"
                        )
                    return str(resp.url)
            except (ClientError, TimeoutError):
                if attempt < retries:
                    await sleep(1 + attempt)
                    continue
                raise RedirectException()
        raise RedirectException()

    def create_author(
        self,
        name: str,
        avatar_url: str | None = None,
        description: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建作者对象"""

        avatar_task = None
        if avatar_url:
            avatar_task = self.downloader.download_img(
                avatar_url, headers=headers or self.headers, proxy=self.proxy
            )
        return Author(name=name, avatar=avatar_task, description=description)

    def create_video_content(
        self,
        url_or_task: str | Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        quality: str | None = None,
        quality_label: str | None = None,
    ):
        """创建视频内容"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=headers or self.headers, proxy=self.proxy
            )
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_video(
                url_or_task, headers=headers or self.headers, proxy=self.proxy
            )

        return VideoContent(
            url_or_task,
            cover_task,
            duration,
            cover_url=cover_url,
            quality=quality,
            quality_label=quality_label,
        )

    def create_video_content_by_task(
        self,
        path_task: Task[Path],
        cover_url: str | None = None,
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        quality: str | None = None,
        quality_label: str | None = None,
    ):
        """创建视频内容，允许调用方自行决定下载任务实现"""
        cover_task = None
        if cover_url:
            cover_task = self.downloader.download_img(
                cover_url, headers=headers or self.headers, proxy=self.proxy
            )
        return VideoContent(
            path_task,
            cover_task,
            duration,
            cover_url=cover_url,
            quality=quality,
            quality_label=quality_label,
        )

    def create_image_contents(
        self,
        image_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建图片内容列表"""
        contents: list[ImageContent] = []
        for url in image_urls:
            task = self.downloader.download_img(
                url, headers=headers or self.headers, proxy=self.proxy
            )
            contents.append(ImageContent(task, source_url=url))
        return contents

    def create_dynamic_contents(
        self,
        dynamic_urls: list[str],
        headers: dict[str, str] | None = None,
    ):
        """创建动态图片内容列表"""
        contents: list[DynamicContent] = []
        for url in dynamic_urls:
            task = self.downloader.download_video(
                url, headers=headers or self.headers, proxy=self.proxy
            )
            contents.append(DynamicContent(task))
        return contents

    def create_audio_content(
        self,
        url_or_task: str | Task[Path],
        duration: float = 0.0,
        headers: dict[str, str] | None = None,
        quality: str | None = None,
        quality_label: str | None = None,
        send_as: str | None = None,
    ):
        """创建音频内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_audio(
                url_or_task, headers=headers or self.headers, proxy=self.proxy
            )

        return AudioContent(
            url_or_task,
            duration,
            quality=quality,
            quality_label=quality_label,
            send_as=send_as,
        )

    def create_graphics_content(
        self,
        image_url: str,
        text: str | None = None,
        alt: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建图文内容 图片不能为空 文字可空 渲染时文字在前 图片在后"""
        image_task = self.downloader.download_img(
            image_url, headers=headers or self.headers, proxy=self.proxy
        )
        return GraphicsContent(image_task, text, alt, source_url=image_url)

    def create_file_content(
        self,
        url_or_task: str | Task[Path],
        name: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        """创建文件内容"""
        if isinstance(url_or_task, str):
            url_or_task = self.downloader.download_file(
                url_or_task,
                headers=headers or self.headers,
                file_name=name,
                proxy=self.proxy,
            )

        return FileContent(url_or_task)
