"""会话私有上下文 Repository（owner-aware）。

每会话至多一行：较早历史摘要 + 结构化会话上下文（主题/目标/已确认事项/
限制条件/待解决问题/已完成事项）。上下文不是长期事实，也不跨会话；长期召回只读
``memory`` 表。会话已按用户隔离，这里仍传 owner_id 做双重校验。

第四期第一阶段 A 起（设计方案 §7.5/§7.7）：

- 摘要正文、覆盖位置与版本号必须**原子**更新，否则并发请求会让后完成的旧
  任务覆盖新摘要（M-49）。``upsert_session_summary`` 因此支持乐观锁：传入
  ``expected_version`` 时按 ``WHERE version = expected_version`` 更新，不匹配
  抛 ``ContextVersionConflict``，由调用方重读后按策略重试。
- 重置必须写入 ``context_start_after_message_id``，只清字段会让下一轮重扫
  把旧结论找回来（§4.4）。
"""
from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from chatchat.server.db.models.session_summary_model import SessionSummaryModel

#: 可整体读写的结构化槽位（summary 与摘要位置标记由摘要流程单独维护）
CONTEXT_FIELDS = (
    "topic",
    "goal",
    "confirmed",
    "constraints",
    "open_questions",
    "completed_items",
)

#: 摘要执行状态（设计方案 §15.1）
SUMMARY_STATUSES = ("idle", "succeeded", "failed")
#: 摘要来源（设计方案 §7.2：规则摘要与模型摘要必须在界面上区分）
SUMMARY_TYPES = ("rule", "llm")


class ContextVersionConflict(Exception):
    """乐观锁冲突：读取上下文后它已被别的请求改过（设计方案 §7.5 / M-49）。"""

    def __init__(self, conversation_id: str, expected_version: int):
        super().__init__(
            f"会话上下文版本冲突：conversation_id={conversation_id} "
            f"expected_version={expected_version}"
        )
        self.conversation_id = conversation_id
        self.expected_version = expected_version


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
        session.commit()
        session.refresh(row)
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
    *,
    summary_type: str | None = None,
    summary_status: str | None = None,
    summary_error: str | None = None,
    summary_updated_at=None,
    needs_rebuild: bool | None = None,
    slots_scanned_through_message_id: str | None = None,
    slots_scanned_count: int | None = None,
    expected_version: int | None = None,
) -> SessionSummaryModel:
    """新增或更新会话摘要与结构化上下文（每会话一行）。

    ``context`` 只覆盖传入的槽位（topic/goal/confirmed/...），未传的保持原值；
    ``pinned_json`` 为 None 时不动用户锁定的条目。

    游标参数（``summarized_message_id`` / ``slots_scanned_*``）只在显式传入时
    改动——**用户编辑上下文绝不能清空摘要覆盖位置**（设计方案 §4.5 / M-12），
    因此这里用 ``None`` 表示"不动"，用空串以外的消息 ID 表示"推进"。

    ``expected_version`` 非 None 时按乐观锁更新：版本不匹配抛
    ``ContextVersionConflict``，本次不写入任何字段（设计方案 §7.5 / M-49）。
    """
    if summary_type is not None and summary_type not in SUMMARY_TYPES:
        raise ValueError(f"未知的 summary_type：{summary_type!r}")
    if summary_status is not None and summary_status not in SUMMARY_STATUSES:
        raise ValueError(f"未知的 summary_status：{summary_status!r}")

    row = _get_or_new(session, owner_id, conversation_id)

    values: dict = {"summary": summary or ""}
    for field, value in (context or {}).items():
        if field in CONTEXT_FIELDS:
            values[field] = value or ""
    if pinned_json is not None:
        values["pinned_json"] = pinned_json
    if summarized_message_id is not None:
        values["summarized_message_id"] = summarized_message_id
        values["summarized_count"] = max(0, int(summarized_count))
    if summary_type is not None:
        values["summary_type"] = summary_type
    if summary_status is not None:
        values["summary_status"] = summary_status
        # 状态是最近一次执行结果，失败原因必须跟着状态走：成功时要清掉上一次的
        # 错误，否则界面会一直显示已经恢复的失败原因（设计方案 §15.1）。
        values["summary_error"] = summary_error
    elif summary_error is not None:
        values["summary_error"] = summary_error
    if summary_updated_at is not None:
        values["summary_updated_at"] = summary_updated_at
    if needs_rebuild is not None:
        values["needs_rebuild"] = bool(needs_rebuild)
    if slots_scanned_through_message_id is not None:
        values["slots_scanned_through_message_id"] = slots_scanned_through_message_id
        values["slots_scanned_count"] = max(0, int(slots_scanned_count or 0))

    # 每次写入都推进版本号；带 expected_version 时按条件更新，实现"读到的版本
    # 仍然一致才写"（单条 UPDATE，SQLite 下原子）。
    values["version"] = SessionSummaryModel.version + 1
    stmt = update(SessionSummaryModel).where(SessionSummaryModel.id == row.id)
    if expected_version is not None:
        stmt = stmt.where(SessionSummaryModel.version == int(expected_version))
    result = session.execute(stmt.values(**values))
    if expected_version is not None and result.rowcount == 0:
        session.rollback()
        raise ContextVersionConflict(conversation_id, int(expected_version))
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
    values: dict = {}
    for field, value in (context or {}).items():
        if field in CONTEXT_FIELDS:
            values[field] = value or ""
    if pinned_json is not None:
        values["pinned_json"] = pinned_json
    values["version"] = SessionSummaryModel.version + 1
    session.execute(
        update(SessionSummaryModel)
        .where(SessionSummaryModel.id == row.id)
        .values(**values)
    )
    session.commit()
    session.refresh(row)
    return row


def reset_session_context(
    session: Session,
    conversation_id: str,
    owner_id: str,
    *,
    context_start_after_message_id: str | None = None,
) -> None:
    """重置本会话上下文：摘要、槽位、游标全部清空。

    只影响本会话这一行；``memory`` 表里的个人偏好不受影响（设计方案 P2-06）。
    ``context_start_after_message_id`` 记录重置时最后一条消息：后续只从该消息
    之后重新建立上下文，旧主题与旧结论不会在下一轮自动恢复（设计方案 §7.7 /
    M-09）。原始聊天记录不删除（M-10）。
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
    values: dict = {
        "summary": "",
        "summarized_message_id": None,
        "summarized_count": 0,
        "pinned_json": "[]",
        "context_start_after_message_id": context_start_after_message_id,
        "summary_type": "rule",
        "summary_status": "idle",
        "summary_error": None,
        "summary_updated_at": None,
        "needs_rebuild": False,
        "slots_scanned_through_message_id": None,
        "slots_scanned_count": 0,
        "version": SessionSummaryModel.version + 1,
    }
    for field in CONTEXT_FIELDS:
        values[field] = ""
    session.execute(
        update(SessionSummaryModel)
        .where(SessionSummaryModel.id == row.id)
        .values(**values)
    )
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
    "ContextVersionConflict",
    "SUMMARY_STATUSES",
    "SUMMARY_TYPES",
    "get_session_summary",
    "upsert_session_summary",
    "update_session_context",
    "reset_session_context",
    "delete_session_summary",
]
