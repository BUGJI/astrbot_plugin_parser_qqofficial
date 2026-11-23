from __future__ import annotations

from collections.abc import Iterable

from .base import LoginProvider, LoginStatus


class LoginRegistry:
    """登录平台注册表，负责平台解析与统一状态汇总"""

    def __init__(self, providers: Iterable[LoginProvider]):
        self._providers = list(providers)
        self._selectors: dict[str, LoginProvider] = {}
        for provider in self._providers:
            for selector in provider.selectors:
                self._selectors[selector.casefold()] = provider

    def resolve(self, token: str) -> LoginProvider | None:
        return self._selectors.get(token.strip().casefold())

    @property
    def available_providers(self) -> list[LoginProvider]:
        return [provider for provider in self._providers if provider.available()]

    async def status_all(self) -> list[str]:
        providers = self.available_providers
        if not providers:
            return ["没有已启用的登录平台"]

        lines: list[str] = []
        for provider in providers:
            try:
                status = await provider.status()
            except Exception as e:
                status = LoginStatus(
                    platform=provider.key,
                    display_name=provider.display_name,
                    logged_in=False,
                    detail=f"（查询失败：{e}）",
                )
            lines.append(status.render())
        return lines

    def help_text(self) -> str:
        available = self.available_providers
        names = "、".join(provider.display_name for provider in available) or "无"
        lines = [
            "解析登录（仅管理员）",
            "用法：",
            "  /解析登录                 查看本帮助",
            "  /解析登录 状态            查看各平台登录状态",
            "  /解析登录 <平台>          查看该平台登录状态",
            "  /解析登录 <平台> <动作>    执行指定登录/刷新动作",
            "",
            f"可用平台：{names}",
        ]
        for provider in available:
            lines.extend(["", provider.help_text()])
        return "\n".join(lines)
