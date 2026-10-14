"""会话私有上下文 API（智能聊天记忆第二期设计方案 §4.2）。

- 全部要求登录，且会话必须属于当前用户（不存在/越权统一 404）；
- ``GET    /conversations/{id}/context``  抽屉里显示的内容；
- ``PATCH  /conversations/{id}/context``  用户编辑（编辑过的结论不被自动更新
  静默覆盖）；
- ``POST   /conversations/{id}/context/refresh``  手动"更新摘要"；
- ``DELETE /conversations/{id}/context``  重置本会话上下文（个人偏好不受影响）；
- 临时对话没有会话上下文：读取返回空槽位并带 ``temporary=true``，写入返回 400。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import get_owned_conversation
from chatchat.server.db.session import get_db
from chatchat.server.memory import session_context

session_context_router = APIRouter(prefix="/conversations", tags=["Session Context"])


class ContextOut(BaseModel):
    conversation_id: str
    temporary: bool = False
    topic: str = ""
    goal: str = ""
    confirmed: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    summary: str = ""
    summarized_count: int = 0
    update_time: Optional[datetime] = None


class ContextEdit(BaseModel):
    """只覆盖传入的槽位；列表按整组替换（与抽屉里的编辑框一致）。"""

    topic: Optional[str] = Field(None, max_length=200)
    goal: Optional[str] = Field(None, max_length=500)
    confirmed: Optional[List[str]] = Field(None, max_length=20)
    constraints: Optional[List[str]] = Field(None, max_length=20)
    open_questions: Optional[List[str]] = Field(None, max_length=20)
    summary: Optional[str] = Field(None, max_length=4000)


def _require_owned(session: Session, conversation_id: str, user: UserModel) -> None:
    if get_owned_conversation(session, conversation_id, user.id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问"
        )


def _out(context: session_context.SessionContext) -> ContextOut:
    return ContextOut(
        conversation_id=context.conversation_id,
        temporary=bool(context.temporary),
        topic=(context.topic.text if context.topic else ""),
        goal=(context.goal.text if context.goal else ""),
        confirmed=[i.text for i in context.confirmed],
        constraints=[i.text for i in context.constraints],
        open_questions=[i.text for i in context.open_questions],
        summary=context.summary or "",
        summarized_count=context.summarized_count,
        update_time=context.update_time,
    )


@session_context_router.get("/{conversation_id}/context", summary="本会话上下文")
def get_context(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ContextOut:
    _require_owned(session, conversation_id, user)
    context = session_context.load_context(session, user.id, conversation_id)
    if context is None:
        context = session_context.SessionContext(
            conversation_id=conversation_id, owner_id=user.id
        )
    return _out(context)


@session_context_router.patch("/{conversation_id}/context", summary="编辑本会话上下文")
def edit_context(
    conversation_id: str,
    body: ContextEdit,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ContextOut:
    """用户编辑立即生效，并作为"用户明确修改的结论"参与后续自动更新。"""
    _require_owned(session, conversation_id, user)
    updated = session_context.apply_user_edit(
        session,
        user.id,
        conversation_id,
        topic=body.topic,
        goal=body.goal,
        confirmed=body.confirmed,
        constraints=body.constraints,
        open_questions=body.open_questions,
        summary=body.summary,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="临时对话不保存会话上下文；请退出临时对话后再编辑",
        )
    return _out(updated)


@session_context_router.post(
    "/{conversation_id}/context/refresh", summary="手动更新本会话摘要与上下文"
)
def refresh_context(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ContextOut:
    """对应抽屉里的「更新摘要」：忽略"历史够长才摘要"的阈值条件。"""
    _require_owned(session, conversation_id, user)
    updated = session_context.refresh_session_context(
        session, user.id, conversation_id, force=True
    )
    if updated is None:
        context = session_context.load_context(session, user.id, conversation_id)
        if context is not None and context.temporary:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="临时对话不生成会话上下文",
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="更新会话上下文失败"
        )
    return _out(updated)


@session_context_router.delete("/{conversation_id}/context", summary="重置本会话上下文")
def reset_context(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """清空摘要、主题、目标、已确认事项与待办；个人偏好不受影响（P2-06）。"""
    _require_owned(session, conversation_id, user)
    session_context.reset_session_context(session, user.id, conversation_id)
    return {"reset": True, "conversation_id": conversation_id}


__all__ = ["session_context_router"]
