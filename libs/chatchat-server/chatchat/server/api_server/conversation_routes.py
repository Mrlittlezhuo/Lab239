"""会话 API 路由：全部要求登录，且只能操作当前用户自己的会话。

- 创建请求只接收业务字段，owner 来自当前用户；
- 列表只返回当前用户会话，按 ``update_time DESC``；
- 详情/重命名/删除/历史读取同时过滤 ``id + owner_id``；
- 访问不存在、owner 为 NULL 或其他用户的会话统一 404；
- 历史消息按 ``create_time ASC`` 返回；
- 响应包含 id/name/chat_type/create_time/update_time，不返回 owner_id。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.session import get_db
from chatchat.server.db.repository import (
    ConversationNotOwned,
    create_conversation,
    delete_owned_conversation,
    get_owned_conversation,
    list_conversations,
    rename_owned_conversation,
)
from chatchat.server.db.repository.message_repository import list_messages


class ConversationCreate(BaseModel):
    name: str = Field("", max_length=50, description="对话框名称")
    chat_type: str = Field("llm_chat", max_length=50, description="聊天类型")


class ConversationRename(BaseModel):
    name: str = Field("", max_length=50, description="对话框名称")


class ConversationOut(BaseModel):
    id: str
    name: Optional[str] = None
    chat_type: Optional[str] = None
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class MessageOut(BaseModel):
    id: str
    chat_type: Optional[str] = None
    query: Optional[str] = None
    response: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    create_time: Optional[datetime] = None
    # 前端反馈 UI 需要显示当前评分状态（-1 表示未反馈）
    feedback_score: Optional[int] = -1
    feedback_reason: Optional[str] = ""


def _conv_out(c: ConversationModel) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        name=c.name,
        chat_type=c.chat_type,
        create_time=c.create_time,
        update_time=c.update_time,
    )


def _msg_out(m) -> MessageOut:
    return MessageOut(
        id=m.id,
        chat_type=m.chat_type,
        query=m.query,
        response=m.response,
        metadata=m.meta_data or {},
        create_time=m.create_time,
        feedback_score=m.feedback_score if m.feedback_score is not None else -1,
        feedback_reason=m.feedback_reason or "",
    )


conversation_router = APIRouter(prefix="/conversations", tags=["Conversations"])


@conversation_router.post("", summary="创建会话（owner 来自当前用户）")
def create_my_conversation(
    body: ConversationCreate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ConversationOut:
    conv_id = create_conversation(session, user.id, body.chat_type, body.name)
    c = get_owned_conversation(session, conv_id, user.id)
    return _conv_out(c)


@conversation_router.get("", summary="当前用户会话列表（update_time DESC）")
def list_my_conversations(
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[ConversationOut]:
    return [_conv_out(c) for c in list_conversations(session, user.id)]


@conversation_router.get("/{conversation_id}", summary="会话详情")
def get_my_conversation(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ConversationOut:
    c = get_owned_conversation(session, conversation_id, user.id)
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问"
        )
    return _conv_out(c)


@conversation_router.patch("/{conversation_id}", summary="重命名会话")
def rename_my_conversation(
    conversation_id: str,
    body: ConversationRename,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> ConversationOut:
    try:
        c = rename_owned_conversation(session, conversation_id, user.id, body.name)
    except ConversationNotOwned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权操作"
        )
    return _conv_out(c)


@conversation_router.delete("/{conversation_id}", summary="删除会话及其消息")
def delete_my_conversation(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        delete_owned_conversation(session, conversation_id, user.id)
    except ConversationNotOwned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权操作"
        )
    return {"deleted": True, "conversation_id": conversation_id}


@conversation_router.get(
    "/{conversation_id}/messages", summary="会话历史消息（create_time ASC）"
)
def list_my_conversation_messages(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        messages = list_messages(session, conversation_id, user.id)
    except ConversationNotOwned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问"
        )
    return {
        "conversation_id": conversation_id,
        "messages": [_msg_out(m) for m in messages],
    }


__all__ = ["conversation_router"]
