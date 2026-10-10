"""认证 API 路由：/auth/login、/auth/me、/auth/logout、/auth/change-password。

路由保护由后续任务统一挂到业务 Router；本任务仅新增并注册
auth/admin
路由，不改变既有 chat/kb/tool/MCP/OpenAI 接口。
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from chatchat.server.db.session import get_db
from chatchat.server.auth import jwt as jwt_mod
from chatchat.server.auth import service
from chatchat.server.auth.deps import get_current_user
from chatchat.server.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    UserOut,
)
from chatchat.server.db.models.user_model import UserModel


def _user_out(user: UserModel) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        status=user.status,
        must_change_password=user.must_change_password,
        create_time=user.create_time,
        update_time=user.update_time,
    )


auth_router = APIRouter(prefix="/auth", tags=["Auth"])


@auth_router.post("/login", summary="登录，返回短期 Bearer Token")
def login(
    body: LoginRequest, session: Session = Depends(get_db)
) -> LoginResponse:
    try:
        user = service.authenticate(
            session,
            body.username,
            body.password.get_secret_value(),
        )
    except service.UserError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        token = jwt_mod.create_token(user.id, user.auth_version)
    except jwt_mod.AuthConfigError:
        # 服务端配置错误：失败关闭，绝不打印密钥
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="服务端认证配置错误，无法签发 Token",
        )
    session.commit()
    expires_in = int(math.ceil(jwt_mod.get_token_ttl().total_seconds()))
    return LoginResponse(
        token=token, token_type="Bearer", expires_in=expires_in, user=_user_out(user)
    )


@auth_router.get("/me", summary="当前登录用户信息（脱敏）")
def me(user: UserModel = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


@auth_router.post(
    "/logout",
    summary="退出登录（无状态，通知客户端删除 Token）",
)
def logout(user: UserModel = Depends(get_current_user)) -> dict:
    # logout 是无服务端会话表的幂等接口，不声称撤销无状态 Token
    return {"message": "已退出登录，请删除本地 Token"}


@auth_router.post("/change-password", summary="修改密码（需验证旧密码）")
def change_password(
    body: ChangePasswordRequest,
    user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> UserOut:
    try:
        service.change_password(
            session,
            user,
            body.old_password.get_secret_value(),
            body.password.get_secret_value(),
        )
    except service.AuthFailed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="旧密码错误"
        )
    except service.InvalidInput as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    session.commit()
    return _user_out(user)


__all__ = ["auth_router"]
