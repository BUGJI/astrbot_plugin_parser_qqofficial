from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import string
import time
from typing import Any

import qrcode
from aiohttp import ClientSession, ClientTimeout

from astrbot.api import logger

from ..config import PluginConfig
from ..cookie import CookieJar

NCM_DOMAIN = "music.163.com"
NCM_API_BASE = "https://music.163.com"

NCM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

WAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
WAPI_IV = b"0102030405060708"
WAPI_PUBLIC_KEY = 0x010001
WAPI_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
    "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b4"
    "24d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a"
    "22b8e7",
    16,
)
WAPI_CHARS = string.ascii_letters + string.digits

QR_STATUS_EXPIRED = 800
QR_STATUS_WAITING = 801
QR_STATUS_SCANNED = 802
QR_STATUS_SUCCESS = 803

QUALITY_LABELS = {
    "standard": "标准 128K",
    "exhigh": "极高 320K",
    "lossless": "无损 FLAC",
    "hires": "Hi-Res",
}


def _aes_cbc_b64(text: str, key: bytes) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    cipher = AES.new(key, AES.MODE_CBC, WAPI_IV)
    encrypted = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def _rsa_encrypt(text: str) -> str:
    number = int.from_bytes(text[::-1].encode("utf-8"), "big")
    encrypted = pow(number, WAPI_PUBLIC_KEY, WAPI_MODULUS)
    return format(encrypted, "x").zfill(256)


def weapi_encrypt(payload: dict[str, Any]) -> dict[str, str]:
    """按网易云 weapi 规则加密参数"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    secret_key = "".join(secrets.choice(WAPI_CHARS) for _ in range(16))
    params = _aes_cbc_b64(
        _aes_cbc_b64(text, WAPI_PRESET_KEY), secret_key.encode("utf-8")
    )
    return {"params": params, "encSecKey": _rsa_encrypt(secret_key)}


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class NCMLogin:
    """网易云登录、状态查询与 Cookie 持久化"""

    def __init__(
        self,
        config: PluginConfig,
        *,
        jar: CookieJar | None = None,
        session: ClientSession | None = None,
        proxy: str | None = None,
    ) -> None:
        self.cfg = config
        self.parser_cfg = config.parser.ncm
        self.jar = jar or CookieJar(
            config, self.parser_cfg, NCM_DOMAIN, write_raw=False
        )
        self._session = session
        self._owned_session: ClientSession | None = None
        self._proxy = (
            proxy
            if proxy is not None
            else (
                config.proxy if getattr(self.parser_cfg, "use_proxy", False) else None
            )
        )
        self._last_refresh = 0.0

    @property
    def session(self) -> ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        if self._owned_session is None or self._owned_session.closed:
            self._owned_session = ClientSession(
                proxy=self._proxy,
                timeout=ClientTimeout(total=self.cfg.common_timeout),
            )
        return self._owned_session

    async def close(self) -> None:
        if self._owned_session is not None and not self._owned_session.closed:
            await self._owned_session.close()
        self._owned_session = None

    def cookie_header(self) -> str:
        return self.jar.get_cookie_header(domain=NCM_DOMAIN, path="/", secure=True)

    def has_login(self) -> bool:
        return bool(
            self.jar.get(domain=NCM_DOMAIN, path="/", secure=True).get("MUSIC_U")
        )

    def _csrf(self) -> str:
        return self.jar.get(domain=NCM_DOMAIN, path="/", secure=True).get("__csrf", "")

    @staticmethod
    def _merge_cookie_header(cookie: str, extra_cookies: dict[str, str] | None) -> str:
        if not extra_cookies:
            return cookie
        cookies: dict[str, str] = {}
        for item in cookie.split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name:
                cookies[name] = value
        cookies.update(extra_cookies)
        return "; ".join(f"{name}={value}" for name, value in cookies.items())

    async def request(
        self,
        api_path: str,
        data: dict[str, Any] | None = None,
        *,
        extra_cookies: dict[str, str] | None = None,
    ) -> dict:
        payload = dict(data or {})
        payload.setdefault("csrf_token", self._csrf())

        params = weapi_encrypt(payload)
        headers = {
            "User-Agent": NCM_UA,
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        cookie = self._merge_cookie_header(self.cookie_header(), extra_cookies)
        if cookie:
            headers["Cookie"] = cookie

        url = f"{NCM_API_BASE}/weapi{api_path.removeprefix('/api')}"
        async with self.session.post(
            url, data=params, headers=headers, proxy=self._proxy
        ) as resp:
            set_cookies: list[str] = []
            try:
                set_cookies = list(resp.headers.getall("Set-Cookie", []))
            except Exception:
                set_cookies = []
            if set_cookies:
                self.jar.update_from_response(set_cookies)

            text = await resp.text()
            try:
                body = json.loads(text) if text else {}
            except Exception:
                body = {"code": resp.status, "message": text[:200]}

            if not isinstance(body, dict):
                body = {"code": resp.status, "message": str(body)[:200]}

            body_cookie = body.get("cookie")
            if isinstance(body_cookie, str) and body_cookie.strip():
                self.jar.replace_from_cookie_string(body_cookie)
            return body

    def persist(self) -> str:
        """把当前 Cookie 写回配置与 Cookie 文件"""
        cookie = self.cookie_header()
        if not cookie:
            return ""

        try:
            self.parser_cfg.cookies = cookie
            self.cfg.save_config()
        except Exception as e:
            logger.warning(f"[NCM] 保存 Cookie 配置失败: {e}")
        return cookie

    async def login_status(self) -> dict:
        body = await self.request("/api/w/nuser/account/get", {})
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        return {
            "code": body.get("code", data.get("code")),
            "account": data.get("account") or {},
            "profile": data.get("profile") or {},
        }

    async def send_captcha(
        self, phone: str, countrycode: str = "86"
    ) -> tuple[bool, str]:
        body = await self.request(
            "/api/sms/captcha/sent",
            {"cellphone": phone, "ctcode": countrycode},
        )
        ok = body.get("code") == 200
        message = str(body.get("message") or body.get("msg") or "")
        if ok and not message:
            message = "验证码已发送"
        return ok, message

    async def login_cellphone(
        self,
        phone: str,
        *,
        password: str | None = None,
        captcha: str | None = None,
        countrycode: str = "86",
    ) -> tuple[bool, str, str]:
        data: dict[str, Any] = {
            "phone": phone,
            "countrycode": countrycode,
            "rememberLogin": "true",
        }
        if captcha:
            data["captcha"] = captcha
        elif password:
            data["password"] = md5_hex(password)
        else:
            return False, "需要手机密码或短信验证码", ""

        body = await self.request("/api/login/cellphone", data)
        return self._login_result(body)

    async def login_email(self, email: str, password: str) -> tuple[bool, str, str]:
        body = await self.request(
            "/api/login",
            {
                "username": email,
                "password": md5_hex(password),
                "rememberLogin": "true",
            },
        )
        return self._login_result(body)

    def _login_result(self, body: dict) -> tuple[bool, str, str]:
        code = body.get("code")
        if code != 200:
            message = str(body.get("message") or body.get("msg") or f"code={code}")
            return False, message, ""

        body_cookie = body.get("cookie")
        if isinstance(body_cookie, str) and body_cookie.strip():
            self.jar.replace_from_cookie_string(body_cookie)

        if not self.has_login():
            return False, "登录成功但未获得 MUSIC_U，请检查账号状态", ""

        cookie = self.persist()
        nickname = ""
        account = body.get("account") or {}
        profile = body.get("profile") or {}
        if isinstance(profile, dict):
            nickname = str(profile.get("nickname") or "")
        if not nickname and isinstance(account, dict):
            nickname = str(account.get("userName") or "")
        return True, nickname, cookie

    async def refresh(
        self, *, force: bool = False, min_interval: int = 3600
    ) -> tuple[bool, str]:
        if not self.has_login():
            return False, "当前没有已登录的 MUSIC_U"

        now = time.monotonic()
        if not force and now - self._last_refresh < min_interval:
            return False, "刷新过于频繁，已跳过"
        self._last_refresh = now

        body = await self.request("/api/login/token/refresh", {})
        if body.get("code") != 200:
            message = str(body.get("message") or body.get("msg") or "")
            return False, f"刷新失败 code={body.get('code')} {message}".strip()

        self.persist()
        return True, "登录状态已刷新"

    async def create_qr(self) -> tuple[str, bytes]:
        body = await self.request("/api/login/qrcode/unikey", {"type": 1})
        key = str(body.get("unikey") or "")
        if body.get("code") != 200 or not key:
            raise ValueError(
                f"获取二维码失败: {body.get('message') or body.get('code')}"
            )

        url = f"https://music.163.com/login?codekey={key}"
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        return key, buffer.getvalue()

    async def check_qr(self, key: str) -> tuple[int, str]:
        body = await self.request(
            "/api/login/qrcode/client/login", {"key": key, "type": 1}
        )
        try:
            code = int(body.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        message = str(body.get("message") or body.get("nickname") or "")
        if code == QR_STATUS_SUCCESS:
            self.persist()
        return code, message


__all__ = [
    "NCMLogin",
    "NCM_DOMAIN",
    "QUALITY_LABELS",
    "QR_STATUS_EXPIRED",
    "QR_STATUS_SCANNED",
    "QR_STATUS_SUCCESS",
    "QR_STATUS_WAITING",
    "md5_hex",
    "weapi_encrypt",
]
