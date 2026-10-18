"""项目内研究讨论与结构化研究定义接口（里程碑2）。

与 ``supervision_routes`` / ``supervision_work_routes`` 共用 ``/supervision``
前缀，覆盖：项目内研究讨论会话的创建与列表、本人会话消息读取、
``ResearchBrief`` 草稿的创建/编辑/否定/丢弃、从本人聊天提取草稿、确认共享、
版本比较、项目共享区读取，以及给聊天主链路用的项目共享上下文。

鉴权与错误口径与里程碑0A/1 完全一致：

- 当前用户只来自 ``require_password_changed`` 的服务端认证上下文，
  请求体里的任何 user_id 都不被采信；
- 非成员、不存在、别人的会话/草稿一律 404（不区分原因，避免枚举）；
- 项目角色不足 403，状态机不允许 409，字段非法 400。

模型调用留接口不实现：``_resolve_llm()`` 现在恒返回 ``None``，提取端点
因此返回 503 + 中文说明。集成阶段把统一容量闸门包好的模型接进
``_resolve_llm()`` 即可，仓储层签名不变。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.db.models.supervision_research_model import (
    SupervisionResearchBriefModel,
    SupervisionResearchBriefVersionModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_research_repository as research
from chatchat.server.db.repository.supervision_repository import require_member
from chatchat.server.db.session import get_db

supervision_research_router = APIRouter(
    prefix="/supervision", tags=["Supervision - 研究讨论与结构化研究定义"],
    dependencies=[Depends(require_password_changed)],
)


def _not_found(detail: str = "资源不存在或无权访问") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


def call(session: Session, action, *args, **kwargs):
    """把仓储层异常收敛成统一 HTTP 语义，并在失败时回滚。

    ``PermissionError`` 里带"角色"字样的是权限不足（403），其余按不可见
    处理（404），与 ``supervision_work_routes.call`` 同一套口径。
    """
    try:
        result = action(session, *args, **kwargs)
        session.commit()
        return result
    except LookupError as exc:
        session.rollback()
        raise _not_found(str(exc) or "资源不存在或无权访问")
    except PermissionError as exc:
        session.rollback()
        message = str(exc)
        raise _forbidden(message) if "角色" in message else _not_found(message)
    except research.WorkValidationError as exc:
        session.rollback()
        raise _bad_request(str(exc))
    except research.WorkError as exc:
        session.rollback()
        raise _conflict(str(exc))


def _resolve_llm():
    """返回可用于提取的模型对象；当前恒为 ``None``。

    统一模型容量闸门（``chatchat/server/model_capacity/``）由另一条线在写，
    这里不 import 它、也不自己起模型，避免绕过闸门抢占容量。集成时把
    闸门包好的、带 ``.invoke(messages) -> .content`` 的模型在这里返回。
    """
    return None


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class DialogueBody(BaseModel):
    title: str = Field(min_length=1, max_length=50)
    chat_type: str = Field(default="", max_length=50)


# 结构化内容里的"文本卡片"字段。库里是文本，但"候选方法""评价指标"这类
# 卡片前端和模型都很自然写成数组，所以入参容忍字符串或字符串数组，
# 由仓储层按顿号接成文本；响应里始终是接好的字符串。
BriefTextField = Union[str, List[str]]


class BriefContentIn(BaseModel):
    """结构化研究定义内容（§4.3 的卡片字段）。"""

    background: BriefTextField = Field(default="")
    question: BriefTextField = Field(default="")
    objectives: BriefTextField = Field(default="")
    subjects: BriefTextField = Field(default="")
    scope: BriefTextField = Field(default="")
    prior_attempts: BriefTextField = Field(default="")
    hypotheses: BriefTextField = Field(default="")
    constraints: BriefTextField = Field(default="")
    methods: BriefTextField = Field(default="")
    metrics: BriefTextField = Field(default="")
    expected_outcomes: BriefTextField = Field(default="")
    to_verify: BriefTextField = Field(default="")
    keywords: List[str] = Field(default_factory=list)
    sub_questions: List[str] = Field(default_factory=list)


class BriefBody(BaseModel):
    title: str = Field(default="", max_length=200)
    kind: str = Field(default="", max_length=32)
    entry_type: str = Field(default="mixed", max_length=32)
    conversation_id: str = Field(default="", max_length=32)
    message_ids: List[str] = Field(default_factory=list)
    content: BriefContentIn = Field(default_factory=BriefContentIn)


class BriefPatchBody(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    entry_type: Optional[str] = Field(default=None, max_length=32)
    content: Optional[BriefContentIn] = None


class ExtractBody(BaseModel):
    conversation_id: str = Field(default="", max_length=32)
    message_ids: List[str] = Field(default_factory=list)
    brief_ref: str = Field(default="", max_length=32)
    title: str = Field(default="", max_length=200)


class ConfirmBody(BaseModel):
    note: str = Field(default="", max_length=4000)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class DialogueOut(BaseModel):
    conversation_id: str
    code: str
    name: Optional[str] = None
    chat_type: Optional[str] = None
    supervision_project_id: Optional[str] = None
    message_count: int = 0
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class DialogueMessageOut(BaseModel):
    message_id: str
    query: Optional[str] = None
    response: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    create_time: Optional[datetime] = None


class DialogueMessagesOut(BaseModel):
    conversation_id: str
    code: str
    messages: List[DialogueMessageOut] = Field(default_factory=list)


class BriefOut(BaseModel):
    brief_id: str
    code: str
    display_code: str
    project_id: str
    owner_user_id: str
    owner_role: str
    kind: str
    title: str
    status: str
    version_status: str
    entry_type: str
    current_version: int
    shared_version: Optional[int] = None
    shown_version: int
    confirmed_by: str = ""
    confirmed_at: Optional[datetime] = None
    confirm_note: str = ""
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    is_owner: bool = False
    can_edit: bool = False
    # 来源信息只对提取人本人返回；另一角色永远是 None/空（§16.3 第5条）。
    source_conversation_id: Optional[str] = None
    source_message_ids: List[str] = Field(default_factory=list)
    background: str = ""
    question: str = ""
    objectives: str = ""
    subjects: str = ""
    scope: str = ""
    prior_attempts: str = ""
    hypotheses: str = ""
    constraints: str = ""
    methods: str = ""
    metrics: str = ""
    expected_outcomes: str = ""
    to_verify: str = ""
    keywords: List[str] = Field(default_factory=list)
    sub_questions: List[str] = Field(default_factory=list)


class BriefVersionOut(BaseModel):
    version: int
    status: str
    entry_type: str
    edited_fields: List[str] = Field(default_factory=list)
    confirmed_by: str = ""
    confirmed_at: Optional[datetime] = None
    confirm_note: str = ""
    superseded_at: Optional[datetime] = None
    create_time: Optional[datetime] = None
    source_message_ids: List[str] = Field(default_factory=list)
    background: str = ""
    question: str = ""
    objectives: str = ""
    subjects: str = ""
    scope: str = ""
    prior_attempts: str = ""
    hypotheses: str = ""
    constraints: str = ""
    methods: str = ""
    metrics: str = ""
    expected_outcomes: str = ""
    to_verify: str = ""
    keywords: List[str] = Field(default_factory=list)
    sub_questions: List[str] = Field(default_factory=list)


class ProjectContextOut(BaseModel):
    project_id: str
    context: str
    has_project_context: bool


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _dialogue_out(
    session: Session, conversation: ConversationModel
) -> DialogueOut:
    return DialogueOut(
        conversation_id=conversation.id,
        code=research.dialogue_code(session, conversation),
        name=conversation.name, chat_type=conversation.chat_type,
        supervision_project_id=conversation.supervision_project_id,
        message_count=research.count_dialogue_messages(session, conversation),
        create_time=conversation.create_time, update_time=conversation.update_time,
    )


def _brief_out(
    session: Session, brief: SupervisionResearchBriefModel, user_id: str
) -> BriefOut:
    return BriefOut(**research.brief_payload(session, brief, user_id))


def _version_out(row: SupervisionResearchBriefVersionModel) -> BriefVersionOut:
    return BriefVersionOut(
        version=row.version, status=row.status, entry_type=row.entry_type,
        edited_fields=list(row.edited_fields or []), confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at, confirm_note=row.confirm_note,
        superseded_at=row.superseded_at, create_time=row.create_time,
        source_message_ids=list(row.source_message_ids or []),
        background=row.background, question=row.question, objectives=row.objectives,
        subjects=row.subjects, scope=row.scope, prior_attempts=row.prior_attempts,
        hypotheses=row.hypotheses, constraints=row.constraints, methods=row.methods,
        metrics=row.metrics, expected_outcomes=row.expected_outcomes,
        to_verify=row.to_verify, keywords=list(row.keywords or []),
        sub_questions=list(row.sub_questions or []),
    )


def _message_out(message: MessageModel) -> DialogueMessageOut:
    return DialogueMessageOut(
        message_id=message.id, query=message.query, response=message.response,
        metadata=message.meta_data or {}, create_time=message.create_time,
    )


# ---------------------------------------------------------------------------
# 研究讨论会话（复用 conversation 表，按角色隔离）
# ---------------------------------------------------------------------------


@supervision_research_router.post(
    "/projects/{project_ref}/research/dialogues", response_model=DialogueOut
)
def create_dialogue(
    project_ref: str,
    body: DialogueBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """建立当前用户在该项目里的研究讨论会话（已有则复用）。

    会话归属只由服务端认证上下文与项目角色决定；学生与导师在同一项目里
    各自一条，彼此读不到对方的原始消息。
    """
    conversation = call(
        session, research.create_dialogue, project_ref, user.id, body.title,
        chat_type=body.chat_type,
    )
    return _dialogue_out(session, conversation)


@supervision_research_router.get(
    "/projects/{project_ref}/research/dialogues", response_model=List[DialogueOut]
)
def list_dialogues(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, research.list_dialogues, project_ref, user.id)
    return [_dialogue_out(session, c) for c in rows]


@supervision_research_router.get(
    "/projects/{project_ref}/research/dialogues/{conversation_id}/messages",
    response_model=DialogueMessagesOut,
)
def get_dialogue_messages(
    project_ref: str,
    conversation_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """读取自己研究会话的原始消息；猜别人的会话 ID 同样 404。"""
    messages = call(
        session, research.get_dialogue_messages, project_ref, user.id,
        conversation_id, limit=limit,
    )
    conversation = session.get(ConversationModel, conversation_id)
    return DialogueMessagesOut(
        conversation_id=conversation_id,
        code=research.dialogue_code(session, conversation),
        messages=[_message_out(m) for m in messages],
    )


# ---------------------------------------------------------------------------
# 结构化研究定义草稿
# ---------------------------------------------------------------------------


@supervision_research_router.post(
    "/projects/{project_ref}/research/briefs", response_model=BriefOut
)
def create_brief_draft(
    project_ref: str,
    body: BriefBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """手工创建研究定义草稿。草稿只对创建者可见，另一角色读不到。"""
    brief = call(
        session, research.create_brief_draft, project_ref, user.id,
        title=body.title, kind=body.kind, entry_type=body.entry_type,
        content=body.content.model_dump(), conversation_id=body.conversation_id,
        message_ids=body.message_ids,
    )
    return _brief_out(session, brief, user.id)


@supervision_research_router.post(
    "/projects/{project_ref}/research/briefs/extract", response_model=BriefOut
)
def extract_brief_draft(
    project_ref: str,
    body: ExtractBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """从**本人**研究会话的消息里提取结构化研究定义草稿。

    模型能力尚未接入时返回 503；接入后行为不变：只读本人消息、只生成
    草稿、不共享、不写个人记忆。传 ``brief_ref`` 时在原研究定义上追加
    新版本，不覆盖旧版本。
    """
    llm = _resolve_llm()
    if llm is None:
        raise _unavailable(
            "模型能力尚未接入：从聊天提取研究内容需要注入生成模型，"
            "等统一模型容量闸门接入后即可使用。"
        )
    brief = call(
        session, research.extract_brief_draft, project_ref, user.id, llm=llm,
        conversation_id=body.conversation_id, message_ids=body.message_ids,
        brief_ref=body.brief_ref, title=body.title,
    )
    return _brief_out(session, brief, user.id)


@supervision_research_router.get(
    "/projects/{project_ref}/research/briefs", response_model=List[BriefOut]
)
def list_briefs(
    project_ref: str,
    scope: str = Query(default="all", pattern="^(mine|shared|all)$"),
    status_filter: str = Query(default="", alias="status", max_length=16),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(
        session, research.list_briefs, project_ref, user.id, scope=scope,
        status=status_filter,
    )
    return [_brief_out(session, b, user.id) for b in rows]


@supervision_research_router.get(
    "/projects/{project_ref}/research/shared-briefs", response_model=List[BriefOut]
)
def list_shared_briefs(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """项目共享区：只有已确认的结构化版本，不含来源消息与草稿。"""
    rows = call(session, research.list_shared_briefs, project_ref, user.id)
    return [_brief_out(session, b, user.id) for b in rows]


@supervision_research_router.get(
    "/projects/{project_ref}/research/briefs/{brief_ref}", response_model=BriefOut
)
def get_brief(
    project_ref: str,
    brief_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """本人看最新版本，另一角色只看到已共享的那一版。"""
    brief = call(session, research.get_brief, project_ref, user.id, brief_ref)
    return _brief_out(session, brief, user.id)


@supervision_research_router.patch(
    "/projects/{project_ref}/research/briefs/{brief_ref}", response_model=BriefOut
)
def update_brief(
    project_ref: str,
    brief_ref: str,
    body: BriefPatchBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """编辑本人研究定义。改已确认的内容会生成新版本，旧版本保留。"""
    fields: dict[str, Any] = {
        k: v for k, v in body.model_dump().items()
        if k != "content" and v is not None
    }
    if body.content is not None:
        fields["content"] = body.content.model_dump()
    brief = call(session, research.update_brief, project_ref, user.id, brief_ref, fields)
    return _brief_out(session, brief, user.id)


@supervision_research_router.post(
    "/projects/{project_ref}/research/briefs/{brief_ref}/confirm",
    response_model=BriefOut,
)
def confirm_brief(
    project_ref: str,
    brief_ref: str,
    body: ConfirmBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """确认并发布当前版本：进入项目共享区，写审计并通知另一角色。"""
    brief = call(
        session, research.confirm_brief, project_ref, user.id, brief_ref,
        note=body.note,
    )
    return _brief_out(session, brief, user.id)


@supervision_research_router.post(
    "/projects/{project_ref}/research/briefs/{brief_ref}/reject",
    response_model=BriefOut,
)
def reject_brief(
    project_ref: str,
    brief_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    brief = call(
        session, research.reject_brief, project_ref, user.id, brief_ref,
        reason=body.reason,
    )
    return _brief_out(session, brief, user.id)


@supervision_research_router.delete(
    "/projects/{project_ref}/research/briefs/{brief_ref}", response_model=BriefOut
)
def delete_brief(
    project_ref: str,
    brief_ref: str,
    body: ReasonBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """丢弃未共享的草稿；已共享的研究定义作为项目审计资产保留。"""
    brief = call(
        session, research.delete_brief, project_ref, user.id, brief_ref,
        reason=body.reason,
    )
    return _brief_out(session, brief, user.id)


# ---------------------------------------------------------------------------
# 版本历史、比较与来源消息
# ---------------------------------------------------------------------------


@supervision_research_router.get(
    "/projects/{project_ref}/research/briefs/{brief_ref}/versions",
    response_model=List[BriefVersionOut],
)
def list_brief_versions(
    project_ref: str,
    brief_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    rows = call(session, research.list_brief_versions, project_ref, user.id, brief_ref)
    return [_version_out(row) for row in rows]


@supervision_research_router.get(
    "/projects/{project_ref}/research/briefs/{brief_ref}/compare"
)
def compare_brief_versions(
    project_ref: str,
    brief_ref: str,
    from_version: int = Query(..., ge=1),
    to_version: int = Query(..., ge=1),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """两个版本的逐字段结构化差异（§16.3 第4条）。"""
    return call(
        session, research.compare_brief_versions, project_ref, user.id, brief_ref,
        from_version, to_version,
    )


@supervision_research_router.get(
    "/projects/{project_ref}/research/briefs/{brief_ref}/source-messages",
    response_model=DialogueMessagesOut,
)
def get_brief_source_messages(
    project_ref: str,
    brief_ref: str,
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """打开研究定义的来源消息——只对提取人本人开放。

    另一角色即使能读共享的结构化版本，也反查不到来源原始消息
    （§16.3 第5条、场景 0-C 第5步）：这里按所有者过滤，一律 404。
    """
    messages = call(
        session, research.get_brief_source_messages, project_ref, user.id,
        brief_ref, limit=limit,
    )
    brief = call(session, research.get_brief, project_ref, user.id, brief_ref)
    conversation = (
        session.get(ConversationModel, brief.source_conversation_id)
        if brief.source_conversation_id
        else None
    )
    return DialogueMessagesOut(
        conversation_id=conversation.id if conversation else "",
        code=research.dialogue_code(session, conversation) if conversation else "",
        messages=[_message_out(m) for m in messages],
    )


# ---------------------------------------------------------------------------
# 项目共享上下文（聊天主链路注入用）
# ---------------------------------------------------------------------------


@supervision_research_router.get(
    "/projects/{project_ref}/research/context", response_model=ProjectContextOut
)
def get_project_context(
    project_ref: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """预览将要注入模型上下文的项目共享内容。

    聊天主链路直接调 ``research.build_project_context(session, project_id,
    user_id)``；这个端点只为界面和排障服务，两者输出一致。内容里只有
    已确认的结构化研究定义，不含任何成员的私人聊天，也不是个人偏好。
    """
    project, _ = call(session, require_member, project_ref, user.id)
    context = call(session, research.build_project_context, project.id, user.id)
    return ProjectContextOut(
        project_id=project.id, context=context, has_project_context=bool(context)
    )


__all__ = ["supervision_research_router"]
