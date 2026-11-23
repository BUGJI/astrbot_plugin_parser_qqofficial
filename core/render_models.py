"""渲染数据模型与装饰器。

从 ``render.py`` 拆出，避免渲染实现文件同时承担数据模型定义。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache, wraps
from io import BytesIO
from typing import ParamSpec, TypeVar

from apilmoji.core import get_font_height
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

from .data import ParseResult

P = ParamSpec("P")
T = TypeVar("T")

Color = tuple[int, int, int]
PILImage = Image.Image


def suppress_exception(
    func: Callable[P, T],
) -> Callable[P, T | None]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


def suppress_exception_async(
    func: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T | None]]:
    """装饰器：捕获所有异常并返回 None"""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.debug(f"函数 {func.__name__} 执行失败: {e}")
            return None

    return wrapper


@dataclass(eq=False, frozen=True, slots=True)
class FontInfo:
    """字体信息数据类"""

    font: ImageFont.FreeTypeFont
    line_height: int
    cjk_width: int

    def __hash__(self) -> int:
        """实现哈希方法以支持 @lru_cache"""
        return hash((id(self.font), self.line_height, self.cjk_width))

    @lru_cache(maxsize=400)
    def get_char_width(self, char: str) -> int:
        """获取字符宽度，使用缓存优化"""
        return int(self.font.getlength(char))

    def get_char_width_fast(self, char: str) -> int:
        """快速获取单个字符宽度"""
        if "\u4e00" <= char <= "\u9fff":
            return self.cjk_width
        return self.get_char_width(char)

    def get_text_width(self, text: str) -> int:
        """计算文本宽度，使用预计算的字符宽度优化性能"""
        if not text:
            return 0

        total_width = 0
        for char in text:
            total_width += self.get_char_width_fast(char)
        return total_width


@dataclass(eq=False, frozen=True, slots=True)
class FontSet:
    """字体集数据类"""

    _FONT_SIZES = (
        ("name", 28),
        ("title", 30),
        ("text", 24),
        ("extra", 24),
        ("indicator", 60),
    )
    """字体大小"""

    name_font: FontInfo
    title_font: FontInfo
    text_font: FontInfo
    extra_font: FontInfo
    indicator_font: FontInfo

    @classmethod
    def new(cls, font_data: bytes):
        font_infos: dict[str, FontInfo] = {}
        for name, size in cls._FONT_SIZES:
            font = ImageFont.truetype(BytesIO(font_data), size)
            font_infos[f"{name}_font"] = FontInfo(
                font=font,
                line_height=get_font_height(font),
                cjk_width=size,
            )
        return FontSet(**font_infos)


@dataclass(eq=False, frozen=True, slots=True)
class SectionData:
    """基础部分数据类"""

    height: int


@dataclass(eq=False, frozen=True, slots=True)
class HeaderSectionData(SectionData):
    """Header 部分数据"""

    avatar: PILImage | None
    name_lines: list[str]
    time_lines: list[str]
    text_height: int


@dataclass(eq=False, frozen=True, slots=True)
class TitleSectionData(SectionData):
    """标题部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class CoverSectionData(SectionData):
    """封面部分数据"""

    cover_img: PILImage


@dataclass(eq=False, frozen=True, slots=True)
class TextSectionData(SectionData):
    """文本部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class ExtraSectionData(SectionData):
    """额外信息部分数据"""

    lines: list[str]


@dataclass(eq=False, frozen=True, slots=True)
class RepostSectionData(SectionData):
    """转发部分数据"""

    scaled_image: PILImage


@dataclass(eq=False, frozen=True, slots=True)
class ImageGridSectionData(SectionData):
    """图片网格部分数据"""

    images: list[PILImage]
    cols: int
    rows: int
    has_more: bool
    remaining_count: int


@dataclass(eq=False, frozen=True, slots=True)
class GraphicsSectionData(SectionData):
    """图文内容部分数据"""

    text_lines: list[str]
    image: PILImage
    alt_text: str | None = None


@dataclass
class RenderContext:
    """渲染上下文，存储渲染过程中的状态信息"""

    result: ParseResult
    """解析结果"""
    card_width: int
    """卡片宽度"""
    content_width: int
    """内容宽度"""
    image: PILImage
    """当前图像"""
    draw: ImageDraw.ImageDraw
    """绘图对象"""
    not_repost: bool = True
    """是否为非转发内容"""
    y_pos: int = 0
    """当前绘制位置（绘制阶段使用）"""
