from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from astrbot.api import logger

from ..cookie import CookieJar
from ..parsers.ncm_login import (
    NCM_DOMAIN,
    NCMLogin,
    QR_STATUS_EXPIRED,
    QR_STATUS_SCANNED,
    QR_STATUS_SUCCESS,
)
from .base import LoginMessage, LoginProvider, LoginQRCode, LoginStatus

if TYPE_CHECKING:
    from ..config import PluginConfig
    from ..parsers.ncm import NCMParser


class NeteaseLoginProvider(LoginProvider):
    """网易云音乐登录适配器"""

    key = "ncm"
    display_name = "网易云"
    aliases = frozenset({"网易云音乐", "netease", "163"})
    default_action = "状态"

    def __init__(
        self,
        config: PluginConfig,
        *,
        enabled: bool,
        parser: NCMParser | None = None,
    ):
        super().__init__(config)
        self._enabled = enabled
        self._parser = parser

    def available(self) -> bool:
        return self._enabled

    def _make_login(self) -> NCMLogin:
        if self._parser is not None:
            return NCMLogin(
                self.cfg,
                jar=self._parser.cookiejar,
                proxy=self._parser.proxy,
            )

        parser_cfg = self.cfg.parser.ncm
        jar = CookieJar(self.cfg, parser_cfg, NCM_DOMAIN, write_raw=False)
        proxy = self.cfg.proxy if getattr(parser_cfg, "use_proxy", False) else None
        return NCMLogin(self.cfg, jar=jar, proxy=proxy)

    def _reload_parser(self) -> None:
        if self._parser is None:
            return
        try:
            self._parser.reload_cookie()
        except Exception as e:
            logger.warning(f"[NCM] 重载 Cookie 失败: {e!r}")

    async def status(self) -> LoginStatus:
        login = self._make_login()
        try:
            return await self._status_from(login)
        finally:
            await login.close()

    async def _status_from(self, login: NCMLogin) -> LoginStatus:
        try:
            data = await login.login_status()
        except Exception as e:
            logger.warning(f"[NCM] 查询登录状态失败: {e!r}")
            return LoginStatus(
                platform=self.key,
                display_name=self.display_name,
                logged_in=False,
                detail=f"（查询失败：{e}）",
            )

        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}

        if not account:
            return LoginStatus(
                platform=self.key,
                display_name=self.display_name,
                logged_in=False,
            )
        if account.get("anonimousUser"):
            return LoginStatus(
                platform=self.key,
                display_name=self.display_name,
                logged_in=True,
                account="游客",
                detail="（无 VIP 权益）",
            )

        name = profile.get("nickname") or account.get("userName") or account.get("id")
        vip_type = profile.get("vipType")
        detail = f"，VIP 类型 {vip_type}" if vip_type else ""
        return LoginStatus(
            platform=self.key,
            display_name=self.display_name,
            logged_in=True,
            account=str(name or ""),
            detail=detail,
        )

    async def run(
        self, action: str, args: list[str]
    ) -> AsyncGenerator[LoginMessage, None]:
        act = action.strip().casefold()
        if act in {"帮助", "help", "用法", "-h", "--help"}:
            yield self.help_text()
            return

        login = self._make_login()
        try:
            if act in {"扫码", "qr", "qrcode", "登录", "login"}:
                async for item in self._run_qr(login):
                    yield item
            elif act in {"手机", "phone"}:
                if len(args) < 2:
                    yield "用法：/解析登录 网易云 手机 <手机号> <密码>"
                else:
                    ok, info, _ = await login.login_cellphone(args[0], password=args[1])
                    yield self._login_result(ok, info)
            elif act in {"短信", "sms"}:
                if len(args) < 1:
                    yield "用法：/解析登录 网易云 短信 <手机号> [验证码]"
                elif len(args) == 1:
                    ok, info = await login.send_captcha(args[0])
                    yield f"验证码已发送：{info}" if ok else f"验证码发送失败：{info}"
                else:
                    ok, info, _ = await login.login_cellphone(args[0], captcha=args[1])
                    yield self._login_result(ok, info)
            elif act in {"邮箱", "email"}:
                if len(args) < 2:
                    yield "用法：/解析登录 网易云 邮箱 <邮箱> <密码>"
                else:
                    ok, info, _ = await login.login_email(args[0], args[1])
                    yield self._login_result(ok, info)
            elif act in {"状态", "status"}:
                yield (await self._status_from(login)).render()
            elif act in {"刷新", "refresh"}:
                ok, info = await login.refresh(force=True)
                if ok:
                    self._reload_parser()
                yield info
            else:
                yield self.help_text()
        finally:
            await login.close()

    async def _run_qr(self, login: NCMLogin) -> AsyncGenerator[LoginMessage, None]:
        try:
            key, png = await login.create_qr()
        except Exception as e:
            yield f"获取网易云二维码失败：{e}"
            return

        yield LoginQRCode(png)
        yield "请使用网易云音乐 App 扫码登录（2 分钟内有效）"

        scanned = False
        for _ in range(60):
            await asyncio.sleep(2)
            try:
                code, _message = await login.check_qr(key)
            except Exception as e:
                logger.warning(f"[NCM] 查询扫码状态失败: {e!r}")
                continue

            if code == QR_STATUS_SUCCESS:
                if not login.has_login():
                    yield ("扫码成功，但未获取到 MUSIC_U，请重试或改用手机/邮箱登录")
                    return
                self._reload_parser()
                yield "网易云扫码登录成功，Cookie 已保存"
                return
            if code == QR_STATUS_EXPIRED:
                yield "二维码已过期，请重新执行登录命令"
                return
            if code == QR_STATUS_SCANNED and not scanned:
                scanned = True
                yield "已扫码，请在手机上确认登录"

        yield "二维码登录超时，请重新执行登录命令"

    def _login_result(self, ok: bool, info: str) -> str:
        if not ok:
            return f"网易云登录失败：{info}"
        self._reload_parser()
        suffix = f"（{info}）" if info else ""
        return f"网易云登录成功{suffix}，Cookie 已保存"

    def help_text(self) -> str:
        return (
            "网易云登录\n"
            "用法：\n"
            "  /解析登录 网易云                   查看登录状态\n"
            "  /解析登录 网易云 扫码              扫码登录\n"
            "  /解析登录 网易云 手机 <手机号> <密码>\n"
            "  /解析登录 网易云 短信 <手机号>     发送短信验证码\n"
            "  /解析登录 网易云 短信 <手机号> <验证码>\n"
            "  /解析登录 网易云 邮箱 <邮箱> <密码>\n"
            "  /解析登录 网易云 刷新              刷新登录 Cookie\n"
            "提示：扫码 Cookie 无法通过刷新接口保活，"
            "长期会话请使用手机/邮箱登录。"
        )
