from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..config import PluginConfig


@dataclass(slots=True)
class LoginQRCode:
    """登录二维码图片"""

    data: bytes


@dataclass(slots=True)
class LoginStatus:
    """统一的平台登录状态"""

    platform: str
    display_name: str
    logged_in: bool
    account: str = ""
    detail: str = ""

    def render(self) -> str:
        if not self.logged_in:
            return f"{self.display_name}：未登录{self.detail}"
        account = self.account or "未知账号"
        return f"{self.display_name}：已登录（{account}）{self.detail}"


LoginMessage = str | LoginQRCode


class LoginProvider(ABC):
    """平台登录适配器，负责把各平台差异收敛到统一入口"""

    key: ClassVar[str]
    display_name: ClassVar[str]
    aliases: ClassVar[frozenset[str]] = frozenset()
    default_action: ClassVar[str] = "状态"

    def __init__(self, config: PluginConfig):
        self.cfg = config

    @property
    def selectors(self) -> set[str]:
        return {self.key, self.display_name, *self.aliases}

    @abstractmethod
    def available(self) -> bool:
        """当前平台是否已启用"""

    @abstractmethod
    async def status(self) -> LoginStatus:
        """查询并返回统一格式的登录状态"""

    @abstractmethod
    def run(self, action: str, args: list[str]) -> AsyncGenerator[LoginMessage, None]:
        """执行登录/刷新动作，按顺序产出文本或二维码"""

    @abstractmethod
    def help_text(self) -> str:
        """该平台的登录帮助文本"""
