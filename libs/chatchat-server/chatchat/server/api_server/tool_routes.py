from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, status

from chatchat.server.agent.tools_access import available_tools, tool_block_reason
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.utils import BaseResponse, get_tool, get_tool_config
from chatchat.utils import build_logger


logger = build_logger()

tool_router = APIRouter(
    prefix="/tools",
    tags=["Toolkits"],
    dependencies=[Depends(require_password_changed)],
)


@tool_router.get("", response_model=BaseResponse)
async def list_tools(user: UserModel = Depends(require_password_changed)):
    """当前用户实际能用的工具。

    未配置的工具（假 key、占位连接串）和只给管理员的工具不列出来：
    前端勾选列表与意图路由候选都以此为准，否则用户只会看到"选了但没生效"。
    """
    tools = available_tools(user)
    data = {
        t.name: {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "args": t.args,
            "config": get_tool_config(t.name),
        }
        for t in tools.values()
    }
    return {"data": data}


@tool_router.post("/call", response_model=BaseResponse)
async def call_tool(
    user: UserModel = Depends(require_password_changed),
    name: str = Body(examples=["calculate"]),
    tool_input: dict = Body({}, examples=[{"text": "3+5/2"}]),
):
    reason = tool_block_reason(name, is_admin=getattr(user, "role", None) == "admin")
    if reason:
        # 直接点名也要挡住：shell 这类工具以前任何登录用户都能通过本接口执行
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)

    if tool := get_tool(name):
        try:
            result = await tool.ainvoke(tool_input)
            return {"data": result}
        except Exception as exc:
            # 真实原因带出去。以前只回 "failed to call tool 'x'"，
            # 原文只进服务端日志，调用方永远猜不到是配置还是参数的问题。
            msg = f"调用工具「{name}」失败：{type(exc).__name__}: {exc}"
            logger.exception(msg)
            return {"code": 500, "msg": msg}
    else:
        return {"code": 500, "msg": f"没有名为「{name}」的工具"}
