from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any

QUALITY_KINDS = ("video", "audio")
IMAGE_VALUES = ("original", "compressed")


def resolve_preference(
    stored: str | None, allowed: Collection[str] | None
) -> str | None:
    """校验用户存储的档位；不在白名单内时视为未设置。"""
    if not stored:
        return None
    if allowed is not None and stored not in allowed:
        return None
    return stored


class PreferenceStore:
    """按平台 + 用户持久化媒体偏好。

    数据结构: ``{platform: {user_id: {kind: value}}}``，
    其中 kind 为 ``video`` / ``audio`` / ``image``。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, str]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for platform, users in raw.items():
            if not isinstance(platform, str) or not isinstance(users, dict):
                continue
            for user, prefs in users.items():
                if not isinstance(user, str) or not isinstance(prefs, dict):
                    continue
                clean = {
                    key: value
                    for key, value in prefs.items()
                    if isinstance(key, str) and isinstance(value, str) and value
                }
                if clean:
                    self._data.setdefault(platform, {})[user] = clean

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, platform: str, user_id: str, kind: str) -> str | None:
        with self._lock:
            return self._data.get(platform, {}).get(user_id, {}).get(kind)

    def set(self, platform: str, user_id: str, kind: str, value: str | None) -> None:
        with self._lock:
            users = self._data.setdefault(platform, {})
            prefs = users.setdefault(user_id, {})
            if value is None:
                prefs.pop(kind, None)
            else:
                prefs[kind] = value
            if not prefs:
                users.pop(user_id, None)
            if not users:
                self._data.pop(platform, None)
            self._save_locked()

    def clear_platform(self, platform: str, user_id: str) -> None:
        with self._lock:
            users = self._data.get(platform)
            if not users or user_id not in users:
                return
            users.pop(user_id, None)
            if not users:
                self._data.pop(platform, None)
            self._save_locked()

    def clear_kind(self, user_id: str, kind: str) -> int:
        """清除某用户在所有平台的指定 kind，返回清除项数。"""
        cleared = 0
        with self._lock:
            for platform in list(self._data):
                users = self._data[platform]
                prefs = users.get(user_id)
                if not prefs or kind not in prefs:
                    continue
                prefs.pop(kind, None)
                cleared += 1
                if not prefs:
                    users.pop(user_id, None)
                if not users:
                    self._data.pop(platform, None)
            if cleared:
                self._save_locked()
        return cleared

    async def aset(
        self, platform: str, user_id: str, kind: str, value: str | None
    ) -> None:
        """异步版 ``set``：磁盘写入在线程池执行，避免阻塞事件循环。"""
        await asyncio.to_thread(self.set, platform, user_id, kind, value)

    async def aclear_platform(self, platform: str, user_id: str) -> None:
        """异步版 ``clear_platform``。"""
        await asyncio.to_thread(self.clear_platform, platform, user_id)

    async def aclear_kind(self, user_id: str, kind: str) -> int:
        """异步版 ``clear_kind``。"""
        return await asyncio.to_thread(self.clear_kind, user_id, kind)

    def items(self, user_id: str) -> dict[str, dict[str, str]]:
        """返回某用户全部平台偏好（副本）。"""
        with self._lock:
            result: dict[str, dict[str, str]] = {}
            for platform, users in self._data.items():
                prefs = users.get(user_id)
                if prefs:
                    result[platform] = dict(prefs)
            return result
