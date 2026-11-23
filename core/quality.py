from __future__ import annotations

import time

SHOW_TTL = 30 * 60
ACTION_TTL = 10 * 60
MAX_ENTRIES = 2048


class QualityMenuRegistry:
    """记录每个文件是否已展示/使用过画质菜单。"""

    def __init__(
        self,
        *,
        show_ttl: float = SHOW_TTL,
        action_ttl: float = ACTION_TTL,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.show_ttl = show_ttl
        self.action_ttl = action_ttl
        self.max_entries = max_entries
        self._shown: dict[tuple[str, str, str, int], float] = {}
        self._actions: dict[tuple[str, str, str, int], float] = {}

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _cleanup(self) -> None:
        now = self._now()
        for store, ttl in (
            (self._shown, self.show_ttl),
            (self._actions, self.action_ttl),
        ):
            expired = [key for key, ts in store.items() if now - ts >= ttl]
            for key in expired:
                store.pop(key, None)
            if len(store) > self.max_entries:
                for key, _ in sorted(store.items(), key=lambda item: item[1])[
                    : len(store) - self.max_entries
                ]:
                    store.pop(key, None)

    def claim_show(self, key: tuple[str, str, str, int]) -> bool:
        """占位展示菜单；同一文件在 TTL 内只返回一次 True。"""
        self._cleanup()
        if key in self._shown:
            return False
        self._shown[key] = self._now()
        return True

    def release_show(self, key: tuple[str, str, str, int]) -> None:
        self._shown.pop(key, None)

    def claim_action(self, key: tuple[str, str, str, int]) -> bool:
        """占位处理一次菜单点击；同一菜单只允许成功处理一次。"""
        self._cleanup()
        if key in self._actions:
            return False
        self._actions[key] = self._now()
        return True

    def release_action(self, key: tuple[str, str, str, int]) -> None:
        self._actions.pop(key, None)

    def clear(self) -> None:
        self._shown.clear()
        self._actions.clear()
