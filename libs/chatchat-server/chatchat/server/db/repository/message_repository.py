"""消息 Repository（owner-aware）。

消息本身不保存 owner；其归属通过所属 conversation 的 owner_id 验证。
新增、更新回答、反馈与历史读取都必须先验证 conversation 属于指定用户。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.db.repository.conversation_repository import (
    ConversationNotOwned,
    get_owned_conversation,
)


def _owned_message(
    session: Session, message_id: str, owner_id: str
) -> Optional[MessageModel]:
    """按 id 取消息并验证其所属 conversation 的 owner；不满足返回 None。"""
    if not message_id or not owner_id:
        return None
    m = (
        session.query(MessageModel)
        .filter(MessageModel.id == message_id)
        .one_or_none()
    )
    if m is None:
        return None
    c = get_owned_conversation(session, m.conversation_id, owner_id)
    if c is None:
        return None
    return m


def add_message(
    session: Session,
    conversation_id: str,
    owner_id: str,
    chat_type: str,
    query: str,
    response: str = "",
    message_id: str | None = None,
    metadata: Dict | None = None,
) -> str:
    """在自己的会话中新增消息，并刷新会话活动时间。"""
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        raise ConversationNotOwned(conversation_id)
    if metadata is None:
        metadata = {}
    if not message_id:
        message_id = uuid.uuid4().hex
    m = MessageModel(
        id=message_id,
        chat_type=chat_type,
        query=query,
        response=response,
        conversation_id=conversation_id,
        meta_data=metadata,
    )
    session.add(m)
    c.update_time = datetime.now()
    session.commit()
    return m.id


def update_message(
    session: Session,
    message_id: str,
    owner_id: str,
    response: str = None,
    metadata: Dict = None,
    merge_metadata: bool = False,
) -> str:
    """在自己的会话中更新回答，并刷新会话活动时间。

    ``merge_metadata=True`` 时按字段合并而不是整体覆盖：一条消息的 metadata
    里同时有本轮使用记录（manifest）、引用、Agent 状态、intermediate_steps，
    各写各的键，整体覆盖会把别人写好的快照抹掉（设计方案 §11.2）。
    """
    m = _owned_message(session, message_id, owner_id)
    if m is None:
        raise ConversationNotOwned(message_id)
    if response is not None:
        m.response = response
    if isinstance(metadata, dict):
        if merge_metadata:
            m.meta_data = _merged(m.meta_data, metadata)
        else:
            m.meta_data = metadata
    c = session.get(ConversationModel, m.conversation_id)
    if c is not None:
        c.update_time = datetime.now()
    session.commit()
    return m.id


def _merged(current, updates: Dict) -> Dict:
    """读出旧 metadata 并按字段合并；值为 ``None`` 的键表示删除该键。

    JSON 列必须整体重新赋值才会被 SQLAlchemy 识别成变更，所以这里总是返回
    一个新 dict。
    """
    merged: Dict = dict(current) if isinstance(current, dict) else {}
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def merge_message_metadata(
    session: Session,
    message_id: str,
    owner_id: str,
    updates: Dict,
    response: str = None,
) -> str:
    """只合并 metadata（和可选的 response），绝不覆盖其他键（§11.2）。

    消息不存在或不属于该用户时抛 ``ConversationNotOwned``：使用记录必须落在
    当前用户自己的消息上，不能凭一个 message_id 写别人的会话。
    """
    return update_message(
        session=session,
        message_id=message_id,
        owner_id=owner_id,
        response=response,
        metadata=updates,
        merge_metadata=True,
    )


def get_message_by_id(
    session: Session, message_id: str, owner_id: str
) -> Optional[MessageModel]:
    """查询自己会话中的消息；不满足返回 None。"""
    return _owned_message(session, message_id, owner_id)


def feedback_message(
    session: Session,
    message_id: str,
    owner_id: str,
    feedback_score: int,
    feedback_reason: str,
) -> str:
    """反馈自己会话中的消息；不可见消息抛 ConversationNotOwned。"""
    m = _owned_message(session, message_id, owner_id)
    if m is None:
        raise ConversationNotOwned(message_id)
    m.feedback_score = feedback_score
    m.feedback_reason = feedback_reason
    session.commit()
    return m.id


def filter_message(
    session: Session,
    conversation_id: str,
    owner_id: str,
    limit: int = 10,
) -> List[Dict]:
    """取自己会话最近 limit 条有回答的记录（倒序），供 LLM 上下文。"""
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        return []
    messages = (
        session.query(MessageModel)
        .filter_by(conversation_id=conversation_id)
        .filter(MessageModel.response != "")
        .order_by(MessageModel.create_time.desc())
        .limit(limit)
        .all()
    )
    data = []
    for m in messages:
        data.append({"query": m.query, "response": m.response, "metadata": m.meta_data})
    return data


def list_messages(
    session: Session, conversation_id: str, owner_id: str
) -> List[MessageModel]:
    """自己会话的全部消息，按 create_time 正序（历史接口）。"""
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        raise ConversationNotOwned(conversation_id)
    return (
        session.query(MessageModel)
        .filter(MessageModel.conversation_id == conversation_id)
        .order_by(MessageModel.create_time.asc())
        .all()
    )


def list_context_messages(
    session: Session, conversation_id: str, owner_id: str
) -> List[MessageModel]:
    """自己会话里**有回答**的消息，按 create_time 正序，供统一上下文构建器。

    与遗留的 ``filter_message`` 用同一个"有回答"判据（取消或失败的轮次没有
    助手回答，不该作为助手说法回放），但返回 ORM 记录且是正序：最近原文窗口
    要按字符预算从最新一轮往前取，摘要也要用同一份顺序（设计方案 §6.2）。
    """
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        return []
    return (
        session.query(MessageModel)
        .filter(
            MessageModel.conversation_id == conversation_id,
            MessageModel.response != "",
        )
        .order_by(MessageModel.create_time.asc())
        .all()
    )


__all__ = [
    "add_message",
    "update_message",
    "merge_message_metadata",
    "get_message_by_id",
    "feedback_message",
    "filter_message",
    "list_messages",
    "list_context_messages",
]
