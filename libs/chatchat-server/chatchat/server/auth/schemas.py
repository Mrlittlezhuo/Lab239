"""认证/管理 API 的请求与响应 schema（pydantic）。

响应中绝不包含 password_hash、明文密码或 JWT 密钥。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, SecretStr
from chatchat.server.response_model import ResponseModel


# ---------------- 请求 ----------------


class LoginRequest(BaseModel):
    username: str
    password: SecretStr


class ChangePasswordRequest(BaseModel):
    old_password: SecretStr
    password: SecretStr


class CreateUserRequest(BaseModel):
    username: str
    display_name: str
    password: SecretStr
    role: str = Field("user", description="admin / user")


class ResetPasswordRequest(BaseModel):
    password: SecretStr


class SetStatusRequest(BaseModel):
    status: str = Field(..., description="active / disabled")


# ---------------- 响应（脱敏） ----------------


class UserOut(ResponseModel):
    id: str
    username: str
    display_name: str
    role: str
    status: str
    must_change_password: bool
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class LoginResponse(ResponseModel):
    token: str
    token_type: str = "Bearer"
    expires_in: int
    user: UserOut


__all__ = [
    "LoginRequest",
    "ChangePasswordRequest",
    "CreateUserRequest",
    "ResetPasswordRequest",
    "SetStatusRequest",
    "UserOut",
    "LoginResponse",
]
