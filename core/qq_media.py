"""QQ 素材管理：上传图片并获得 ``qqbot-media://`` 引用。

用于在 Markdown 消息中引用本地渲染的卡片图，避免依赖公网图床。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .qq_api import make_route

MAX_IMAGE_BYTES = 10 * 1024 * 1024
CACHE_TTL = 60 * 60 * 24 * 7

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class QQMediaUploader:
    """按文件指纹缓存的上传器（滑动有效期由服务端维护）。"""

    def __init__(self, bot: Any, session: Any, cache_file: Path) -> None:
        self.bot = bot
        self.session = session
        self.cache_file = cache_file
        self._cache: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.cache_file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._cache, ensure_ascii=False), "utf-8"
            )
        except Exception as exc:
            logger.debug(f"[qqofficial] 素材缓存写入失败: {exc}")

    async def upload(self, image_path: Path) -> str | None:
        """上传图片，成功返回 ``qqbot-media://...`` 引用。"""
        if self.bot is None or self.session is None:
            return None

        if not self._loaded:
            self._cache = await asyncio.to_thread(self._load)
            self._loaded = True

        try:
            raw = await asyncio.to_thread(image_path.read_bytes)
        except Exception:
            return None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            return None

        content_type = _MIME.get(image_path.suffix.lower())
        if not content_type:
            return None

        digest = hashlib.md5(raw).hexdigest()
        sha1 = hashlib.sha1(raw).hexdigest()
        key = f"{digest}:{len(raw)}"

        cached = self._cache.get(key)
        if cached and time.time() - float(cached.get("ts", 0)) < CACHE_TTL:
            url = cached.get("url")
            if isinstance(url, str) and url:
                return url

        try:
            prepare = await self.bot.api._http.request(
                make_route("POST", "/v2/media-images/prepare"),
                json={
                    "file_name": image_path.name,
                    "file_size": len(raw),
                    "content_type": content_type,
                    "md5": digest,
                    "sha1": sha1,
                },
            )
        except Exception as exc:
            logger.warning(f"[qqofficial] 素材预上传失败: {exc}")
            return None

        if not isinstance(prepare, dict):
            return None
        upload_id = prepare.get("upload_id")
        presigned = prepare.get("presigned_url")
        if not upload_id or not presigned:
            return None

        etag = digest
        try:
            async with self.session.put(
                str(presigned),
                data=raw,
                headers={"Content-Type": content_type},
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"[qqofficial] 素材直传失败: HTTP {resp.status}")
                    return None
                etag = (resp.headers.get("ETag") or "").strip('"') or digest
        except Exception as exc:
            logger.warning(f"[qqofficial] 素材直传异常: {exc}")
            return None

        try:
            complete = await self.bot.api._http.request(
                make_route("POST", "/v2/media-images/complete"),
                json={"upload_id": upload_id, "md5": etag},
            )
        except Exception as exc:
            logger.warning(f"[qqofficial] 素材完成上传失败: {exc}")
            return None

        if not isinstance(complete, dict):
            return None
        url = complete.get("url")
        if not isinstance(url, str) or not url.startswith("qqbot-media://"):
            return None

        self._cache[key] = {"url": url, "ts": time.time()}
        await asyncio.to_thread(self._save)
        return url
