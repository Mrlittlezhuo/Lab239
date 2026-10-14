"""会话私有上下文 Repository（owner-aware）。

每会话至多一行：较早历史摘要 + 结构化会话上下文（主题/目标/已确认事项/
限制条件/待解决问题）。上下文不是长期事实，也不跨会话；长期召回只读
``memory`` 表。会话已按用户隔离，这里仍传 owner_id 做双重校验。
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from chatchat.server.db.models.session_summary_model import SessionSummaryModel

#: 可整体读写的结构化槽位（summary 与摘要位置标记由摘要流程单独维护）
CONTEXT_FIELDS = (
    "topic",
    "goal",
    "confirmed",
    "constraints",
    "open_questions",
)


def get_session_summary(
    session: Session, conversation_id: str, owner_id: str
) -> SessionSummaryModel | None:
    """按会话取摘要（owner 不匹配/无会话返回 None）。"""
    if not conversation_id or not owner_id:
        return None
    return (
        session.query(SessionSummaryModel)
        .filter(
            SessionSummaryModel.conversation_id == conversation_id,
            SessionSummaryModel.owner_id == owner_id,
        )
        .one_or_none()
    )


def _get_or_new(
    session: Session, owner_id: str, conversation_id: str
) -> SessionSummaryModel:
    if not owner_id or not conversation_id:
        raise ValueError("owner_id 与 conversation_id 不能为空")
    row = (
        session.query(SessionSummaryModel)
        .filter(
            SessionSummaryModel.conversation_id == conversation_id,
            SessionSummaryModel.owner_id == owner_id,
        )
        .one_or_none()
    )
    if row is None:
        row = SessionSummaryModel(
            id=uuid.uuid4().hex,
            owner_id=owner_id,
            conversation_id=conversation_id,
            summary="",
            summarized_count=0,
        )
        session.add(row)
    return row


def upsert_session_summary(
    session: Session,
    owner_id: str,
    conversation_id: str,
    summary: str,
    summarized_message_id: str | None = None,
    summarized_count: int = 0,
    context: dict | None = None,
    pinned_json: str | None = None,
) -> SessionSummaryModel:
    """新增或更新会话摘要与结构化上下文（每会话一行）。

    ``context`` 只覆盖传入的槽位（topic/goal/confirmed/...），未传的保持原值；
    ``pinned_json`` 为 None 时不动用户锁定的条目。
    """
    row = _get_or_new(session, owner_id, conversation_id)
    row.summary = summary or ""
    row.summarized_message_id = summarized_message_id
    row.summarized_count = max(0, int(summarized_count))
    for field, value in (context or {}).items():
        if field in CONTEXT_FIELDS:
            setattr(row, field, value or "")
    if pinned_json is not None:
        row.pinned_json = pinned_json
    session.commit()
    session.refresh(row)
    return row


def update_session_context(
    session: Session,
    owner_id: str,
    conversation_id: str,
    context: dict,
    pinned_json: str | None = None,
) -> SessionSummaryModel:
    """只更新结构化槽位（用户手工编辑/确认写入），不动摘要与摘要位置标记。"""
    row = _get_or_new(session, owner_id, conversation_id)
    for field, value in (context or {}).items():
        if field in CONTEXT_FIELDS:
            setattr(row, field, value or "")
    if pinned_json is not None:
        row.pinned_json = pinned_json
    session.commit()
    session.refresh(row)
    return row


def reset_session_context(
    session: Session, conversation_id: str, owner_id: str
) -> None:
    """重置本会话上下文：摘要、主题、目标、已确认事项、待办全部清空。

    只影响本会话这一行；``memory`` 表里的个人偏好不受影响（设计方案 P2-06）。
    """
    if not conversation_id or not owner_id:
        return
    row = (
        session.query(SessionSummaryModel)
        .filter(
            SessionSummaryModel.conversation_id == conversation_id,
            SessionSummaryModel.owner_id == owner_id,
        )
        .one_or_none()
    )
    if row is None:
        return
    row.summary = ""
    row.summarized_message_id = None
    row.summarized_count = 0
    for field in CONTEXT_FIELDS:
        setattr(row, field, "")
    row.pinned_json = "[]"
    session.commit()


def delete_session_summary(
    session: Session, conversation_id: str, owner_id: str
) -> None:
    """删除会话摘要（会话删除时一并清理）。"""
    if not conversation_id or not owner_id:
        return
    session.query(SessionSummaryModel).filter(
        SessionSummaryModel.conversation_id == conversation_id,
        SessionSummaryModel.owner_id == owner_id,
    ).delete(synchronize_session=False)
    session.commit()


__all__ = [
    "CONTEXT_FIELDS",
    "get_session_summary",
    "upsert_session_summary",
    "update_session_context",
    "reset_session_context",
    "delete_session_summary",
]
