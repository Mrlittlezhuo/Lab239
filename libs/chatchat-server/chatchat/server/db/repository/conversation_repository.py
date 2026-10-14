"""会话 Repository（owner-aware）。

所有读写都必须同时校验 conversation.id 与 owner_id；owner 不匹配、
owner 为 NULL 或会话不存在时一律视为"不可见"（返回 None / 抛
``ConversationNotOwned``），不区分原因，避免会话枚举。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from chatchat.server.db.models.conversation_model import ConversationModel


class ConversationNotOwned(Exception):
    """会话不存在或不属于指定用户（调用方映射 404）。"""


def create_conversation(
    session: Session,
    owner_id: str,
    chat_type: str,
    name: str = "",
    conversation_id: str | None = None,
    temporary: bool = False,
) -> str:
    """新增会话；owner 由服务端写入，不接受客户端指定。

    ``temporary=True`` 表示临时对话：后续不创建、不更新、不保留会话私有
    上下文（智能聊天记忆第二期设计方案 §4.4）。
    """
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    if not conversation_id:
        conversation_id = uuid.uuid4().hex
    now = datetime.now()
    c = ConversationModel(
        id=conversation_id,
        chat_type=chat_type,
        name=name,
        owner_id=owner_id,
        temporary=bool(temporary),
        create_time=now,
        update_time=now,
    )
    session.add(c)
    session.commit()
    return c.id


def list_conversations(
    session: Session,
    owner_id: str,
    chat_type: str | None = None,
) -> list[ConversationModel]:
    """当前用户的会话列表，按 update_time 倒序（无时间者按 id 兜底）。

    ``chat_type`` 用于把不同入口的会话分开（智能聊天 agent_chat、
    知识库对话 kb_chat…）。不传就是全部 —— 前端两个页面都用同一个侧栏组件，
    不过滤的话知识库对话页会列出并打开智能聊天的会话，历史混成一串。
    """
    query = session.query(ConversationModel).filter(
        ConversationModel.owner_id == owner_id
    )
    if chat_type:
        query = query.filter(ConversationModel.chat_type == chat_type)
    return query.order_by(
        ConversationModel.update_time.desc(),
        ConversationModel.id.desc(),
    ).all()


def get_owned_conversation(
    session: Session, conversation_id: str, owner_id: str
) -> ConversationModel | None:
    """同时按 id + owner_id 过滤；不满足返回 None。"""
    if not conversation_id or not owner_id:
        return None
    return (
        session.query(ConversationModel)
        .filter(
            ConversationModel.id == conversation_id,
            ConversationModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def rename_owned_conversation(
    session: Session, conversation_id: str, owner_id: str, name: str
) -> ConversationModel:
    """重命名自己的会话；不可见会话抛 ConversationNotOwned。"""
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        raise ConversationNotOwned(conversation_id)
    c.name = name
    c.update_time = datetime.now()
    session.commit()
    return c


def delete_owned_conversation(
    session: Session, conversation_id: str, owner_id: str
) -> None:
    """删除自己的会话及其全部消息；不可见会话抛 ConversationNotOwned。"""
    c = get_owned_conversation(session, conversation_id, owner_id)
    if c is None:
        raise ConversationNotOwned(conversation_id)
    # 同事务内先删短期摘要和消息，再删会话。长期记忆是用户级事实，保留。
    from chatchat.server.db.models.message_model import MessageModel
    from chatchat.server.db.models.session_summary_model import SessionSummaryModel

    session.query(SessionSummaryModel).filter(
        SessionSummaryModel.conversation_id == conversation_id,
        SessionSummaryModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.query(MessageModel).filter(
        MessageModel.conversation_id == conversation_id
    ).delete(synchronize_session=False)
    session.delete(c)
    session.commit()


def conversation_is_temporary(
    session: Session, conversation_id: str, owner_id: str
) -> bool:
    """该会话是否为临时对话（不存在/不属于该用户一律按 False 处理）。

    记忆与摘要层据此跳过会话上下文的读写；读不到就当普通会话，
    宁可不省这一步，也不因为一次查询失败而改变既有聊天行为。
    """
    if not conversation_id or not owner_id:
        return False
    return bool(
        session.query(ConversationModel.temporary)
        .filter(
            ConversationModel.id == conversation_id,
            ConversationModel.owner_id == owner_id,
        )
        .scalar()
    )


__all__ = [
    "ConversationNotOwned",
    "create_conversation",
    "list_conversations",
    "get_owned_conversation",
    "conversation_is_temporary",
    "rename_owned_conversation",
    "delete_owned_conversation",
]
