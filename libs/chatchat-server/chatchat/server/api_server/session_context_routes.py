"""会话私有上下文 API（智能聊天记忆第二期设计方案 §4.2）。

- 全部要求登录，且会话必须属于当前用户（不存在/越权统一 404）；
- ``GET    /conversations/{id}/context``  抽屉里显示的内容；
- ``PATCH  /conversations/{id}/context``  用户编辑（编辑过的结论不被自动更新
  静默覆盖）；
- ``POST   /conversations/{id}/context/refresh``  手动"更新摘要"；
- ``DELETE /conversations/{id}/context``  重置本会话上下文（个人偏好不受影响）；
- 临时对话没有会话上下文：读取返回空槽位并带 ``temporary=true``，写入返回 400。

第四期·第一阶段 B 起，读取响应同时给出**上下文状态**（设计方案 §18.3.7）：摘要
类型（规则 / 模型）、执行状态与失败原因、摘要覆盖到哪条消息、上下文版本，以及
最近原文窗口的分界与规模。抽屉据此解释"摘要 + 最近原文 = 全部历史"这条分界，
历史回答的 manifest 也按 ``version`` 对上"当时用的是哪一版上下文"。
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
    #: 已完成事项：与"仍生效的已确认事项"分开，避免模型把做完的事当待办（§7.1）
    completed_items: List[str] = Field(default_factory=list)
    summary: str = ""
    summarized_count: int = 0
    update_time: Optional[datetime] = None
    #: 与 ``confirmed`` 一一对应：该条已确认事项是否已保存为跨会话记忆（第三期 §4.3）
    confirmed_saved: List[bool] = Field(default_factory=list)
    #: 会话所在项目空间（第三期 §4.4）；项目范围记忆只在此会话内召回
    project_id: Optional[str] = None

    # --- 上下文状态（第四期·第一阶段 B，设计方案 §18.3.7 / §7.2 / §7.5）---
    #: 摘要来源：rule（规则摘要）/ llm（模型摘要）。界面必须区分两者，
    #: 模型摘要尚未启用时不能把它显示成"AI 总结"
    summary_type: str = "rule"
    #: 最近一次摘要更新结果：idle / succeeded / failed
    summary_status: str = "idle"
    #: 失败原因（面向管理员，已脱敏）；只在 summary_status=failed 时有值
    summary_error: Optional[str] = None
    summary_updated_at: Optional[datetime] = None
    #: 摘要覆盖到哪条消息；与 ``recent_boundary_message_id`` 一起构成无空洞分界
    summarized_message_id: Optional[str] = None
    #: 上下文乐观锁版本：历史回答的 manifest 里记的就是它（§11.1），
    #: 据此能回答"当时用的是哪一版上下文"
    version: int = 0
    #: 旧摘要文本存在但覆盖位置不可确认（v16 判据），下次进入时安全重建
    needs_rebuild: bool = False
    #: 最近原文窗口起点：它之前的消息必须已进摘要（§6.2）
    recent_boundary_message_id: Optional[str] = None
    #: 窗口内轮数与是否被字符预算截断（§6.2 第 5 条：截断要如实显示）
    recent_window_size: int = 0
    recent_window_truncated: bool = False


class ContextEdit(BaseModel):
    """只覆盖传入的槽位；列表按整组替换（与抽屉里的编辑框一致）。"""

    topic: Optional[str] = Field(None, max_length=200)
    goal: Optional[str] = Field(None, max_length=500)
    confirmed: Optional[List[str]] = Field(None, max_length=20)
    constraints: Optional[List[str]] = Field(None, max_length=20)
    open_questions: Optional[List[str]] = Field(None, max_length=20)
    completed_items: Optional[List[str]] = Field(None, max_length=20)
    summary: Optional[str] = Field(None, max_length=4000)


def _require_owned(session: Session, conversation_id: str, user: UserModel) -> None:
    if get_owned_conversation(session, conversation_id, user.id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问"
        )


def _out(
    context: session_context.SessionContext, session: Optional[Session] = None
) -> ContextOut:
    """映射为响应。

    ``confirmed_saved`` 需要查当前用户的记忆表，因此只在有 session 时给出：
    抽屉里据此把"保存为跨会话记忆"变成"已保存"，避免重复保存（第三期 §4.3）。
    """
    confirmed = [i.text for i in context.confirmed]
    saved: List[bool] = []
    project_id = None
    boundary_id, window_size, window_truncated = None, 0, False
    if session is not None:
        from chatchat.server.db.repository import conversation_repository as conv_repo
        from chatchat.server.memory import context_package, cross_session_recall

        for text in confirmed:
            saved.append(
                cross_session_recall._find_same_content_memory(
                    session, context.owner_id, text
                )
                is not None
            )
        project_id = conv_repo.conversation_project_id(
            session, context.conversation_id, context.owner_id
        )
        # 最近原文窗口按注入层同一条规则现算，界面才能把"摘要覆盖到哪、原文从
        # 哪开始"这条分界讲清楚（§18.3.7）。无记忆对话本来就没有摘要，分界之前
        # 缺内容属设计如此，不显示分界。
        if not context.temporary:
            from chatchat.settings import Settings

            boundary_id, window_size, window_truncated = (
                context_package.recent_window_status(
                    session,
                    context.owner_id,
                    context.conversation_id,
                    min_turns=Settings.model_settings.HISTORY_LEN,
                )
            )
    return ContextOut(
        conversation_id=context.conversation_id,
        temporary=bool(context.temporary),
        topic=(context.topic.text if context.topic else ""),
        goal=(context.goal.text if context.goal else ""),
        confirmed=confirmed,
        constraints=[i.text for i in context.constraints],
        open_questions=[i.text for i in context.open_questions],
        completed_items=[i.text for i in context.completed_items],
        summary=context.summary or "",
        summarized_count=context.summarized_count,
        update_time=context.update_time,
        confirmed_saved=saved,
        project_id=project_id,
        summary_type=context.summary_type or "rule",
        summary_status=context.summary_status or "idle",
        summary_error=context.summary_error,
        summary_updated_at=context.summary_updated_at,
        summarized_message_id=context.summarized_message_id,
        version=int(context.version or 0),
        needs_rebuild=bool(context.needs_rebuild),
        recent_boundary_message_id=boundary_id,
        recent_window_size=window_size,
        recent_window_truncated=window_truncated,
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
    return _out(context, session)


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
        completed_items=body.completed_items,
        summary=body.summary,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="临时对话不保存会话上下文；请退出临时对话后再编辑",
        )
    return _out(updated, session)


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
    return _out(updated, session)


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


# --- 跨会话记忆：候选提升与会话内"本次不使用"（第三期设计方案 §4.2、§4.3）---


class IgnoreRequest(BaseModel):
    memory_id: str = Field(..., min_length=1, max_length=32)


@session_context_router.get(
    "/{conversation_id}/promotions",
    summary="本会话已确认事项里「值得保存为跨会话记忆」的候选（只提出，不写入）",
)
def list_promotions(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """系统可以提出候选，但绝不自动升级为跨会话记忆（设计方案 §4.3 / P3-13）。"""
    _require_owned(session, conversation_id, user)
    from chatchat.server.memory import cross_session_recall

    candidates = cross_session_recall.promotion_candidates(
        session, user.id, conversation_id
    )
    return {"candidates": [c.to_dict() for c in candidates]}


@session_context_router.get(
    "/{conversation_id}/memory-ignores", summary="本会话「本次不使用」的记忆 id"
)
def list_ignores(
    conversation_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    _require_owned(session, conversation_id, user)
    from chatchat.server.db.repository import memory_repository as memory_repo

    return {
        "memory_ids": sorted(
            memory_repo.list_ignored_memory_ids(session, conversation_id, user.id)
        )
    }


@session_context_router.post(
    "/{conversation_id}/memory-ignores",
    summary="本次不使用：只让该记忆在本会话内不被召回（原记录保留）",
)
def add_ignore(
    conversation_id: str,
    body: IgnoreRequest,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """设计方案 §4.2 / P3-04：仅本次回答及当前会话忽略，其他会话不受影响。

    记录放在服务端而不是浏览器里，普通聊天、知识库对话与 Agent 任务才会
    一起遵守；重复调用幂等。
    """
    _require_owned(session, conversation_id, user)
    from chatchat.server.db.repository import memory_repository as memory_repo

    try:
        memory_repo.ignore_memory(session, conversation_id, user.id, body.memory_id)
    except memory_repo.MemoryNotOwned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在或无权访问"
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"ignored": True, "conversation_id": conversation_id, "memory_id": body.memory_id}


@session_context_router.delete(
    "/{conversation_id}/memory-ignores/{memory_id}",
    summary="取消「本次不使用」（只影响本会话）",
)
def remove_ignore(
    conversation_id: str,
    memory_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    _require_owned(session, conversation_id, user)
    from chatchat.server.db.repository import memory_repository as memory_repo

    removed = memory_repo.unignore_memory(session, conversation_id, user.id, memory_id)
    return {"removed": removed, "conversation_id": conversation_id, "memory_id": memory_id}


__all__ = ["session_context_router"]
