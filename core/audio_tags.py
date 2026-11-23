"""音频元数据写入。

依赖 ``ffmpeg``（``-c copy`` 重封装，不重新编码音频），可选把封面作为
attached picture 写入。全流程失败均为非致命，由调用方决定是否继续发送。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from aiohttp import ClientSession

SUPPORTED_SUFFIXES = frozenset({".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".opus"})

# ogg/opus 的封面支持不稳定，只写文本标签
COVER_SUFFIXES = frozenset({".mp3", ".flac", ".m4a", ".mp4"})

_FFMPEG_FORMAT = {
    ".mp3": "mp3",
    ".flac": "flac",
    ".m4a": "mp4",
    ".mp4": "mp4",
    ".ogg": "ogg",
    ".opus": "opus",
}

_COVER_MAX_BYTES = 5 * 1024 * 1024

_LRC_LINE = re.compile(r"^(\[[^\]]+\])(.*)$")


def merge_lrc_lyrics(lrc: str, tlyric: str) -> str:
    """按时间轴合并原文与翻译歌词。"""
    lrc = (lrc or "").strip()
    tlyric = (tlyric or "").strip()
    if not tlyric:
        return lrc
    if not lrc:
        return tlyric

    translations: dict[str, str] = {}
    for line in tlyric.splitlines():
        matched = _LRC_LINE.match(line.strip())
        if matched and matched.group(2).strip():
            translations[matched.group(1)] = matched.group(2).strip()
    if not translations:
        return lrc

    merged: list[str] = []
    used: set[str] = set()
    for line in lrc.splitlines():
        matched = _LRC_LINE.match(line.strip())
        if matched:
            tag, text = matched.group(1), matched.group(2).strip()
            translation = translations.get(tag)
            if translation and text:
                used.add(tag)
                merged.append(f"{tag}{text} / {translation}")
                continue
        merged.append(line)
    for tag, translation in translations.items():
        if tag not in used:
            merged.append(f"{tag}{translation}")
    return "\n".join(merged)


@dataclass(slots=True, frozen=True)
class AudioTags:
    """写入音频文件的标签字段，空值会被忽略。"""

    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track: int | None = None
    date: str = ""
    comment: str = ""
    lyrics: str = ""

    def metadata(self) -> dict[str, str]:
        entries: dict[str, Any] = {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "date": self.date,
            "comment": self.comment,
            "lyrics": self.lyrics,
        }
        if self.track and self.track > 0:
            entries["track"] = str(self.track)
        return {key: value for key, value in entries.items() if value and value.strip()}


def _format_args(suffix: str, has_cover: bool) -> list[str]:
    """按容器格式返回 mapping / 编码参数。"""
    if suffix == ".mp3":
        if has_cover:
            return [
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c",
                "copy",
                "-id3v2_version",
                "3",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
        return ["-map", "0:a", "-c:a", "copy", "-id3v2_version", "3"]

    if suffix == ".flac":
        if has_cover:
            return [
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c",
                "copy",
                "-disposition:v:0",
                "attached_pic",
            ]
        return ["-map", "0:a", "-c", "copy"]

    if suffix in (".m4a", ".mp4"):
        if has_cover:
            args = [
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c",
                "copy",
                "-disposition:v:0",
                "attached_pic",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
        else:
            args = ["-map", "0:a", "-c:a", "copy"]
        if suffix == ".mp4":
            args += ["-movflags", "+faststart"]
        return args

    # ogg / opus：只写文本标签，避免写入失败
    return ["-map", "0:a", "-c:a", "copy"]


def build_tag_command(
    src: Path,
    dst: Path,
    metadata: dict[str, str],
    *,
    cover_path: Path | None = None,
) -> list[str]:
    """构造 ffmpeg 写标签命令（便于单测）。"""
    suffix = src.suffix.lower()
    has_cover = (
        cover_path is not None and suffix in COVER_SUFFIXES and cover_path.exists()
    )

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if has_cover:
        cmd += ["-i", str(cover_path)]

    for key, value in metadata.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]

    cmd += _format_args(suffix, has_cover)
    fmt = _FFMPEG_FORMAT.get(suffix)
    if fmt:
        cmd += ["-f", fmt]
    cmd.append(str(dst))
    return cmd


async def _read_format_tags(path: Path) -> dict[str, str]:
    """读取已有标签，用于跳过重复重封装。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        payload = json.loads(out.decode(errors="ignore") or "{}")
        raw: dict[str, str] = dict((payload.get("format") or {}).get("tags") or {})
        # ogg/opus 把标签放在 stream 里
        for stream in payload.get("streams") or []:
            for key, value in (stream.get("tags") or {}).items():
                raw.setdefault(key, value)
        return {str(key).lower(): str(value) for key, value in raw.items()}
    except Exception:
        return {}


async def embed_audio_tags(
    path: Path,
    tags: AudioTags,
    *,
    cover_path: Path | None = None,
    timeout: float = 120.0,
    skip_if_tagged: bool = True,
) -> bool:
    """把标签写入音频文件（原地替换）。

    Returns:
        是否写入（或已存在）成功。任何失败只记录日志，不会抛出。
    """
    if not path.is_file():
        return False

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        logger.debug(f"[音频标签] 跳过不支持的格式: {path.name}")
        return False

    metadata = tags.metadata()
    if not metadata:
        return False

    if skip_if_tagged and tags.title:
        existing = await _read_format_tags(path)
        title_ok = existing.get("title", "").strip() == tags.title.strip()
        lyrics_ok = not tags.lyrics or bool(existing.get("lyrics"))
        if title_ok and lyrics_ok:
            logger.debug(f"[音频标签] 已存在标签，跳过: {path.name}")
            return True

    tmp_path = path.with_name(f"{path.stem}.tagging{path.suffix}")
    try:
        cmd = build_tag_command(path, tmp_path, metadata, cover_path=cover_path)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0 or not tmp_path.exists():
            detail = (stderr or b"").decode(errors="ignore").strip()[:300]
            logger.warning(f"[音频标签] 写入失败 {path.name}: {detail}")
            return False

        await asyncio.to_thread(tmp_path.replace, path)
        logger.debug(f"[音频标签] 已写入 {path.name}")
        return True
    except FileNotFoundError:
        logger.warning("[音频标签] 未找到 ffmpeg，跳过元数据写入")
        return False
    except TimeoutError:
        logger.warning(f"[音频标签] ffmpeg 超时，跳过: {path.name}")
        return False
    except Exception as exc:
        logger.warning(f"[音频标签] 写入异常 {path.name}: {exc!r}")
        return False
    finally:
        with suppress(OSError):
            tmp_path.unlink()


async def download_cover(
    session: ClientSession,
    url: str,
    dest_dir: Path,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 15.0,
) -> Path | None:
    """下载封面到临时文件，失败返回 None。"""
    if not url:
        return None

    dest = dest_dir / f".audio_cover_{uuid.uuid4().hex}.jpg"
    try:
        async with session.get(url, headers=headers, proxy=proxy) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            if resp.content_length and resp.content_length > _COVER_MAX_BYTES:
                raise RuntimeError(f"封面过大: {resp.content_length} bytes")
            data = await asyncio.wait_for(resp.read(), timeout)
        if not data or len(data) > _COVER_MAX_BYTES:
            raise RuntimeError("封面内容为空或过大")
        await asyncio.to_thread(dest.write_bytes, data)
        return dest
    except Exception as exc:
        logger.debug(f"[音频标签] 封面下载失败，跳过封面: {exc!r}")
        return None
