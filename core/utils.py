import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlparse

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")


class LimitedSizeDict(OrderedDict[K, V]):
    """
    定长字典
    """

    def __init__(self, *args, max_size=20, **kwargs):
        self.max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: K, value: V):
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)  # 移除最早添加的项


async def safe_unlink(path: Path):
    """
    安全删除文件
    """
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception:
        logger.warning(f"删除 {path} 失败")


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    """执行命令

    Args:
        cmd (list[str]): 命令序列
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()
        return_code = process.returncode
    except FileNotFoundError:
        raise RuntimeError("ffmpeg 未安装或无法找到可执行文件")

    if return_code != 0:
        error_msg = stderr.decode().strip()
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    target_path = output_path
    if output_path in (v_path, a_path):
        output_path = output_path.with_name(
            f"{output_path.stem}_merged{output_path.suffix}"
        )
    logger.info(f"Merging {v_path.name} and {a_path.name} to {output_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    if output_path != target_path:
        await safe_unlink(target_path)
        await asyncio.to_thread(output_path.replace, target_path)
        output_path = target_path
    cleanup = [p for p in (v_path, a_path) if p != output_path]
    await asyncio.gather(*(safe_unlink(p) for p in cleanup))
    logger.info(f"Merged {output_path.name}, {fmt_size(output_path)}")


async def merge_av_h264(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频，并使用 H.264 编码

    Args:
        v_path (Path): 视频文件路径
        a_path (Path): 音频文件路径
        output_path (Path): 输出文件路径
    """
    logger.info(
        f"Merging {v_path.name} and {a_path.name} to {output_path.name} with H.264"
    )

    # 修改命令以确保视频使用 H.264 编码
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c:v",
        "libx264",  # 明确指定使用 H.264 编码
        "-preset",
        "medium",  # 编码速度和质量的平衡
        "-crf",
        "23",  # 质量因子，值越低质量越高
        "-c:a",
        "aac",  # 音频使用 AAC 编码
        "-b:a",
        "128k",  # 音频比特率
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name} with H.264, {fmt_size(output_path)}")


async def encode_video_to_h264(video_path: Path) -> Path:
    """将视频重新编码到 h264

    Args:
        video_path (Path): 视频路径

    Returns:
        Path: 编码后的视频路径
    """
    output_path = video_path.with_name(f"{video_path.stem}_h264{video_path.suffix}")
    if output_path.exists():
        return output_path
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    await exec_ffmpeg_cmd(cmd)
    logger.info(f"视频重新编码为 H.264 成功: {output_path}, {fmt_size(output_path)}")
    await safe_unlink(video_path)
    return output_path


async def transcode_video_audio_to_lc(path: Path) -> Path:
    """把视频中非 AAC-LC 的音频转码为标准 AAC-LC，保持视频流 copy。

    用于解决 QQ 等平台因音频 profile（如 HE-AACv2）导致的视频无法正常渲染问题。
    """
    try:
        probe = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=profile",
            "-of",
            "default=nw=1:nk=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await probe.communicate()
        profile = (out or b"").decode(errors="ignore").strip()
    except Exception as e:
        logger.warning(f"[视频] ffprobe 探测失败，跳过转码: {e}")
        return path

    if not profile or profile.lower() in ("lc", "aac_lc", "aac lc"):
        return path

    out_path = path.with_name(f"{path.stem}_lc.mp4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-movflags",
            "+faststart",
            str(out_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except Exception as e:
        logger.warning(f"[视频] ffmpeg 调用异常，跳过转码: {e}")
        return path

    if proc.returncode != 0 or not out_path.exists():
        logger.warning(
            f"[视频] 音频转码失败(profile={profile})，改用原文件: "
            f"{(err or b'')[:200]!r}"
        )
        return path

    logger.info(
        f"[视频] 音频 {profile} -> AAC-LC 完成: {out_path.name} "
        f"({out_path.stat().st_size} bytes)"
    )
    await safe_unlink(path)
    return out_path


def fmt_size(file_path: Path) -> str:
    """格式化文件大小

    Args:
        video_path (Path): 视频路径
    """
    return f"大小: {file_path.stat().st_size / 1024 / 1024:.2f} MB"


def generate_file_name(url: str, default_suffix: str = "") -> str:
    """根据 url 生成文件名

    Args:
        url (str): url
        default_suffix (str): 默认后缀. Defaults to "".

    Returns:
        str: 文件名
    """
    # 根据 url 获取文件后缀
    path = Path(urlparse(url).path)
    suffix = path.suffix if path.suffix else default_suffix
    # 获取 url 的 md5 值
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    file_name = f"{url_hash}{suffix}"
    return file_name


URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def _iter_json_string_values(obj: Any):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_json_string_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_string_values(item)
    elif isinstance(obj, str):
        yield obj


def _clean_embedded_url(value: str) -> str:
    return unquote(value.strip().replace("\\/", "/"))


def extract_json_text(
    html: str,
    pattern: str,
    *,
    fix_undefined: bool = False,
) -> str | None:
    """按正则从 HTML 中提取内嵌 JSON 文本。

    Args:
        html: 页面源码。
        pattern: 需包含一个捕获组，用于取出 JSON 文本。
        fix_undefined: 将 JS 的 ``undefined`` 替换为 JSON 的 ``null``。

    Returns:
        去除首尾空白后的 JSON 文本；未匹配时返回 None。
    """
    matched = re.search(pattern, html)
    if matched is None:
        return None
    text = matched.group(1).strip()
    return text.replace("undefined", "null") if fix_undefined else text


def extract_json_url(data: dict | str) -> str | None:
    """处理 JSON 类型消息段，提取可交给解析器处理的 URL。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    # 优先读取 QQ 卡片中明确表达跳转目标的字段。
    priority_paths = (
        ("miniapp", "legacyUrl"),
        ("miniapp", "pcJumpUrl"),
        ("miniapp", "jumpUrl"),
        ("miniapp", "sourceUrl"),
        ("miniapp", "url"),
        ("detail_1", "qqdocurl"),
        ("detail_1", "jumpUrl"),
        ("detail_1", "url"),
        ("detail_1", "sourceUrl"),
        ("detail_1", "shareUrl"),
        ("detail_1", "targetUrl"),
        ("news", "jumpUrl"),
        ("news", "url"),
        ("news", "sourceUrl"),
        ("news", "shareUrl"),
        ("music", "musicUrl"),
        ("music", "jumpUrl"),
        ("music", "url"),
    )
    for key1, key2 in priority_paths:
        node = meta.get(key1)
        if not isinstance(node, dict):
            continue
        value = node.get(key2)
        if not isinstance(value, str):
            continue
        match = URL_RE.search(_clean_embedded_url(value))
        if match:
            return match.group(0)

    # 部分 OneBot/QQ 卡片会把真实 URL 藏在更深层字段中。
    candidates: list[str] = []
    for value in _iter_json_string_values(data):
        cleaned = _clean_embedded_url(value)
        candidates.extend(match.group(0) for match in URL_RE.finditer(cleaned))

    if not candidates:
        return None

    # QZone 分享卡优先，之后保持常见分享短链优先级。
    preferred_keywords = (
        "mobile.qzone.qq.com/l",
        "h5.qzone.qq.com/ugc/share",
        "m.qzone.qq.com",
        "user.qzone.qq.com",
        "qzone.qq.com",
        "b23.tv",
        "bili2233.cn",
        "bilibili.com",
        "xhslink.cn",
        "xhslink.com",
        "xiaohongshu.com",
    )
    for keyword in preferred_keywords:
        for url in candidates:
            if keyword in url:
                return url

    return candidates[0]


# ==================== QQ 官方机器人辅助 ====================

QQBOT_AT_RE = re.compile(r'<qqbot-at-user\s+id="([^"]+)"\s*/?>')
LEGACY_AT_RE = re.compile(r"<@!?([^>\s]+)>")

# 卡片字段中明确表达跳转目标的键名。
_CARD_URL_KEYS = {
    "jump_url",
    "jumpurl",
    "qqdocurl",
    "sourceurl",
    "shareurl",
    "targeturl",
    "legacyurl",
    "pcjumpurl",
    "url",
    "musicurl",
}

# 卡片字段中属于素材（非跳转目标）的键名，需忽略。
_CARD_IMAGE_KEYS = {
    "preview",
    "source_logo",
    "sourcelogo",
    "avatar",
    "icon",
    "logo",
    "cover",
    "image",
    "img",
    "pic",
}


def extract_qqbot_mentions(text: str | None) -> set[str]:
    """提取 QQ 官方消息里的 @ 标记（含官方与历史 OneBot 两种格式）。"""
    if not text:
        return set()
    return set(QQBOT_AT_RE.findall(text)) | set(LEGACY_AT_RE.findall(text))


def _find_url_by_keys(obj: Any, keys: set[str], depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        # ark_data 常见形态：{"kv": [{"key": "jump_url", "value": "..."}]}
        kv_key = obj.get("key")
        kv_value = obj.get("value")
        if isinstance(kv_key, str) and isinstance(kv_value, str):
            lowered = kv_key.lower()
            if lowered in keys:
                match = URL_RE.search(_clean_embedded_url(kv_value))
                if match:
                    return match.group(0)
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in _CARD_IMAGE_KEYS:
                continue
            if lowered in keys and isinstance(value, str):
                match = URL_RE.search(_clean_embedded_url(value))
                if match:
                    return match.group(0)
        for value in obj.values():
            found = _find_url_by_keys(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_url_by_keys(item, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, str):
        # 兼容 ark_data 以 JSON 字符串形式出现
        text = obj.strip()
        if text.startswith("{"):
            try:
                return _find_url_by_keys(json.loads(text), keys, depth + 1)
            except Exception:
                return None
    return None


def extract_card_url(text: str | None, raw: Any = None) -> str | None:
    """从 QQ 官方结构化卡片中提取可解析的目标 URL。

    优先解析文本渲染里的 ``jump_url:`` 等字段，其次在原始 ``ark_data`` 中
    按跳转类键名递归查找；预览图/头像等素材 URL 会被忽略。
    """
    if text:
        for line in text.splitlines():
            match = re.match(r"^\s*([A-Za-z0-9_]+)\s*[:：]\s*(.+)$", line)
            if not match:
                continue
            key, value = match.group(1).lower(), match.group(2).strip()
            if key in _CARD_IMAGE_KEYS:
                continue
            if key in _CARD_URL_KEYS:
                found = URL_RE.search(_clean_embedded_url(value))
                if found:
                    return found.group(0)

    if raw is not None:
        found = _find_url_by_keys(raw, _CARD_URL_KEYS)
        if found:
            return found

    return None


def ensure_official_image(path: Path, cache_dir: Path, fmt: str = "jpg") -> Path:
    """把图片转换为 QQ 官方更稳妥的 jpg/png（动图取首帧）。"""
    fmt = (fmt or "jpg").lower()
    if fmt in {"jpeg", "jpg"}:
        fmt = "jpg"
    elif fmt != "png":
        fmt = "jpg"

    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            animated = bool(getattr(im, "is_animated", False))
            suffix = path.suffix.lower()
            if not animated and (
                (fmt == "jpg" and suffix in {".jpg", ".jpeg"})
                or (fmt == "png" and suffix == ".png")
            ):
                return path

            if animated:
                im.seek(0)
            try:
                stat = path.stat()
                tag = f"{path}:{stat.st_mtime_ns}:{stat.st_size}"
            except Exception:
                tag = str(path)
            digest = hashlib.md5(tag.encode()).hexdigest()[:16]
            out = cache_dir / (f"qqimg_{digest}{'.png' if fmt == 'png' else '.jpg'}")

            if fmt == "png":
                if im.mode not in ("RGBA", "RGB"):
                    im = im.convert("RGBA")
                im.save(out, format="PNG")
            else:
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = PILImage.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.save(out, format="JPEG", quality=92)
            return out
    except Exception as exc:
        logger.debug(f"[qqofficial] 图片格式转换失败，沿用原图: {path} ({exc})")
        return path
