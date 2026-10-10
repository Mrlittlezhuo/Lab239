"""短期 Bearer Token 的签发与验证（项目已有 PyJWT，仅 HS256）。

- 密钥来自环境变量 ``CHATCHAT_AUTH_SECRET``，缺失或 UTF-8 长度 < 32 bytes
  时失败关闭（绝不使用弱默认值）；
- 有效期由环境变量 ``CHATCHAT_AUTH_TTL_MINUTES`` 配置，默认 720 分钟（12
  小时），clamp 到 5–10080 分钟（前端重构 B7）；
- claims 含 ``sub``(user id)、``ver``(auth_version)、``iat``、``exp``、
  ``iss=lab239-workbench``；
- 解码时固定算法列表、校验 issuer 与必需 claims。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

ALGORITHM = "HS256"
ISSUER = "lab239-workbench"
TOKEN_TTL_MINUTES_ENV = "CHATCHAT_AUTH_TTL_MINUTES"
DEFAULT_TOKEN_TTL_MINUTES = 720  # 12 小时
MIN_TOKEN_TTL_MINUTES = 5
MAX_TOKEN_TTL_MINUTES = 10080  # 7 天
MIN_SECRET_BYTES = 32

_REQUIRED_CLAIMS = ("sub", "ver", "iat", "exp", "iss")
_USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class AuthConfigError(RuntimeError):
    """JWT 密钥缺失或过短（服务端配置错误，失败关闭）。"""


class TokenValidationError(jwt.PyJWTError):
    """Token 无效（过期、签名错误、issuer/claims 不符）。"""


def describe_secret_misconfig() -> Optional[str]:
    """返回 ``CHATCHAT_AUTH_SECRET`` 的配置问题说明；配置正确时返回 ``None``。

    给 ``chatchat start`` 启动自检用：登录签发对密钥失败关闭，但只有用户点
    登录才会撞 ``POST /auth/login`` 的 500，且该分支刻意不打印 traceback，
    现场很难定位。这里把原因提前说清楚。只报告字节数，绝不回显密钥内容。
    """
    size = len((os.environ.get("CHATCHAT_AUTH_SECRET") or "").encode("utf-8"))
    if not size:
        return "CHATCHAT_AUTH_SECRET 未设置：登录签发已禁用，POST /auth/login 返回 500"
    if size < MIN_SECRET_BYTES:
        return (
            f"CHATCHAT_AUTH_SECRET 只有 {size} 字节，不足 {MIN_SECRET_BYTES} 字节："
            "登录签发已禁用，POST /auth/login 返回 500"
        )
    return None


def get_secret() -> str:
    """读取 JWT 密钥；缺失或过短时失败关闭。"""
    problem = describe_secret_misconfig()
    if problem:
        raise AuthConfigError(problem)
    return os.environ["CHATCHAT_AUTH_SECRET"]


def get_token_ttl() -> timedelta:
    """读取令牌有效期（分钟）；非法值回落默认，越界值 clamp。"""
    raw = os.environ.get(TOKEN_TTL_MINUTES_ENV, "").strip()
    if not raw:
        minutes = DEFAULT_TOKEN_TTL_MINUTES
    else:
        try:
            minutes = int(raw)
        except ValueError:
            minutes = DEFAULT_TOKEN_TTL_MINUTES
    minutes = max(MIN_TOKEN_TTL_MINUTES, min(MAX_TOKEN_TTL_MINUTES, minutes))
    return timedelta(minutes=minutes)


def create_token(
    user_id: str,
    auth_version: int,
    now: Optional[datetime] = None,
) -> str:
    """为 active 用户签发短期 Bearer Token。"""
    if not isinstance(user_id, str) or not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("user_id 必须为 32 位 UUID hex")
    if type(auth_version) is not int or auth_version < 1:
        raise ValueError("auth_version 必须为正整数")
    now = now or datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": user_id,
        "ver": auth_version,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + get_token_ttl()).timestamp()),
    }
    return jwt.encode(payload, get_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> Dict[str, Any]:
    """验证并解码 Token；任何无效情况抛 TokenValidationError。"""
    try:
        claims = jwt.decode(
            token,
            get_secret(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise TokenValidationError(str(exc)) from exc
    if not isinstance(claims.get("sub"), str) or not _USER_ID_RE.fullmatch(
        claims["sub"]
    ):
        raise TokenValidationError("sub claim 类型或格式无效")
    if type(claims.get("ver")) is not int or claims["ver"] < 1:
        raise TokenValidationError("ver claim 类型或取值无效")
    for claim in ("iat", "exp"):
        if type(claims.get(claim)) is not int:
            raise TokenValidationError(f"{claim} claim 类型无效")
    return claims


__all__ = [
    "AuthConfigError",
    "TokenValidationError",
    "get_secret",
    "describe_secret_misconfig",
    "create_token",
    "decode_token",
    "ALGORITHM",
    "ISSUER",
    "TOKEN_TTL_MINUTES_ENV",
    "DEFAULT_TOKEN_TTL_MINUTES",
    "MIN_TOKEN_TTL_MINUTES",
    "MAX_TOKEN_TTL_MINUTES",
    "get_token_ttl",
    "MIN_SECRET_BYTES",
]
