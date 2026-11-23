from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..parsers.bilibili.login import BilibiliLogin
from .base import LoginMessage, LoginProvider, LoginQRCode, LoginStatus

if TYPE_CHECKING:
    from bilibili_api import Credential

    from ..config import PluginConfig


class BilibiliLoginProvider(LoginProvider):
    """哔哩哔哩扫码登录适配器"""

    key = "bilibili"
    display_name = "B站"
    aliases = frozenset({"bili", "哔哩哔哩"})
    default_action = "状态"

    def __init__(
        self,
        config: PluginConfig,
        *,
        enabled: bool,
        login: BilibiliLogin | None = None,
    ):
        super().__init__(config)
        self._enabled = enabled
        self._login = login

    @property
    def login(self) -> BilibiliLogin:
        if self._login is None:
            self._login = BilibiliLogin(self.cfg)
        return self._login

    def available(self) -> bool:
        return self._enabled

    async def status(self) -> LoginStatus:
        try:
            credential = await self.login.credential
        except Exception as e:
            logger.warning(f"[B站] 查询登录状态失败: {e!r}")
            return LoginStatus(
                platform=self.key,
                display_name=self.display_name,
                logged_in=False,
                detail=f"（查询失败：{e}）",
            )

        if credential is None:
            return LoginStatus(
                platform=self.key,
                display_name=self.display_name,
                logged_in=False,
            )

        uid = str(getattr(credential, "dedeuserid", "") or "")
        nickname = await self._fetch_nickname(credential)
        return LoginStatus(
            platform=self.key,
            display_name=self.display_name,
            logged_in=True,
            account=nickname or uid,
        )

    async def _fetch_nickname(self, credential: Credential) -> str:
        try:
            from bilibili_api import user
        except Exception:
            return ""

        getter = getattr(user, "get_self_info", None)
        if getter is None:
            return ""
        try:
            info = await getter(credential)
        except Exception as e:
            logger.debug(f"[B站] 获取账号信息失败: {e!r}")
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("name") or info.get("uname") or "")

    async def run(
        self, action: str, args: list[str]
    ) -> AsyncGenerator[LoginMessage, None]:
        act = action.strip().casefold()
        if act in {"扫码", "qr", "qrcode", "登录", "login"}:
            async for item in self._run_qr():
                yield item
        elif act in {"状态", "status"}:
            yield (await self.status()).render()
        else:
            yield self.help_text()

    async def _run_qr(self) -> AsyncGenerator[LoginMessage, None]:
        try:
            png = await self.login.login_with_qrcode()
        except Exception as e:
            yield f"获取B站二维码失败：{e}"
            return

        yield LoginQRCode(png)
        async for message in self.login.check_qr_state():
            yield message

    def help_text(self) -> str:
        return (
            "B站登录\n"
            "用法：\n"
            "  /解析登录 B站          查看登录状态\n"
            "  /解析登录 B站 扫码     扫码登录\n"
            "  /解析登录 B站 状态     查看登录状态"
        )
