# -*- coding: utf-8 -*-
"""安全校验与管理员认证模块。"""

from __future__ import annotations

import hmac
import re
from urllib.parse import unquote

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import Settings

# GitHub owner 与 repo 合法字符集（不允许 ..、/、\、空格及控制字符）
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TAG_RE = re.compile(r"^[A-Za-z0-9._+@~-]+$")

SESSION_COOKIE_NAME = "relay_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 天


def is_safe_segment(value: str) -> bool:
    """校验路径段（owner / repo / filename）不包含路径穿越或非法字符。"""
    decoded = unquote(value or "")
    if not decoded or decoded in (".", ".."):
        return False
    if ".." in decoded or "/" in decoded or "\\" in decoded or "\x00" in decoded:
        return False
    return bool(_SEGMENT_RE.fullmatch(decoded))


def is_safe_tag(value: str) -> bool:
    """校验 Release tag 不包含路径穿越或斜杠。"""
    decoded = unquote(value or "")
    if not decoded or decoded in (".", ".."):
        return False
    if ".." in decoded or "/" in decoded or "\\" in decoded or "\x00" in decoded:
        return False
    return bool(_TAG_RE.fullmatch(decoded))


def normalize_repo(owner: str, repo: str) -> str | None:
    """规范化并校验 owner/repo，不合法返回 None。"""
    o = owner.strip()
    r = repo.strip()
    if not (is_safe_segment(o) and is_safe_segment(r)):
        return None
    return f"{o}/{r}"


def verify_credentials(username: str, password: str, settings: Settings) -> bool:
    """使用常量时间比较验证管理员账号密码。"""
    user_ok = hmac.compare_digest(username.encode("utf-8"), settings.admin_username.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), settings.admin_password.encode("utf-8"))
    return user_ok and pass_ok


def create_session_token(username: str, settings: Settings) -> str:
    serializer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
    return serializer.dumps({"u": username})


def verify_session_token(token: str, settings: Settings) -> str | None:
    if not token:
        return None
    serializer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
        user = data.get("u")
        if isinstance(user, str) and hmac.compare_digest(user, settings.admin_username):
            return user
    except (BadSignature, SignatureExpired):
        return None
    return None


def get_authenticated_admin(request: Request, settings: Settings) -> str | None:
    """同时支持 Session Cookie 与 Authorization: Bearer <ADMIN_PASSWORD> 认证。"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token and hmac.compare_digest(token.encode("utf-8"), settings.admin_password.encode("utf-8")):
            return settings.admin_username

    cookie_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    return verify_session_token(cookie_token, settings)


def get_external_base_url(request: Request, settings: Settings) -> str:
    """获取对外可访问的基础 URL，优先使用 PUBLIC_BASE_URL，否则结合反代头推断。"""
    if settings.public_base_url:
        return settings.public_base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{proto}://{host}".rstrip("/")
