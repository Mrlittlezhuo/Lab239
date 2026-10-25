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

模型调用统一走 ``_build_gated_model``：返回的是经过统一容量闸门的模型
对象（每次 ``invoke`` 内部取放一次租约，消费者类型决定它排在哪条业务
通道）。模型构造不出来时接口返回 503 + 中文说明，绝不假装跑过；讨论
发言本身在这种情况下**仍然保存**，回复可以之后补（``…/messages/{id}/reply``）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from chatchat.server.api_server.api_schemas import ResponseModel
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.model_capacity import (
    CONSUMER_PROJECT_DISCUSSION,
    CONSUMER_RESEARCH_DEFINITION,
)
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.db.models.supervision_research_model import (
    SupervisionResearchBriefModel,
    SupervisionResearchBriefVersionModel,
)
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import supervision_research_repository as research
from chatchat.server.db.repository.supervision_repository import require_member
from chatchat.server.db.session import get_db
from chatchat.utils import build_logger

logger = build_logger(__name__)

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


#: 模型对象按消费者类型缓存：构造与缓存模型对象不持有任何容量，真正的
#: 租约在每次 ``invoke`` 内部由统一容量闸门取放（里程碑0B）。
_MODEL_CACHE: Dict[str, Any] = {}


def _build_gated_model(consumer: str) -> Optional[Any]:
    """构造经过统一容量闸门的模型对象；构造失败返回 ``None``（→ 503）。

    刻意不在仓储层里起模型：仓储只接受注入的对象，测试才能塞假模型；
    也只有路由/运行时这一层知道"这次调用是谁发起的"（消费者类型决定
    它排在哪条业务通道）。失败不缓存，配置热更新后下一次就能恢复。
    """
    cached = _MODEL_CACHE.get(consumer)
    if cached is not None:
        return cached
    try:
        from chatchat.settings import Settings
        from chatchat.server.utils import get_ChatOpenAI, get_default_llm

        model = get_ChatOpenAI(
            model_name=get_default_llm(),
            temperature=Settings.model_settings.TEMPERATURE,
            max_tokens=Settings.model_settings.MAX_TOKENS,
            streaming=False,
            verbose=False,
            consumer=consumer,
        )
    except Exception:  # 模型没配好：接口返回 503，不假装跑过
        logger.exception("构造研究讨论生成模型失败（consumer={}）", consumer)
        return None
    if model is None:
        return None
    _MODEL_CACHE[consumer] = model
    return model


def reset_research_model_cache() -> None:
    """测试与运维用：清掉模型缓存，下一次调用重新构造。"""
    _MODEL_CACHE.clear()


def _resolve_llm():
    """提取结构化研究定义用的模型（``research_definition`` 通道）。"""
    return _build_gated_model(CONSUMER_RESEARCH_DEFINITION)


def _resolve_discussion_llm():
    """项目内讨论回复用的模型（``project_discussion`` 前台交互通道）。"""
    return _build_gated_model(CONSUMER_PROJECT_DISCUSSION)


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class DialogueBody(BaseModel):
    title: str = Field(min_length=1, max_length=50)
    chat_type: str = Field(default="", max_length=50)


class DialogueMessageBody(BaseModel):
    # 上限与 message.query 列宽一致（4096），超了直接 422，不静默截断。
    text: str = Field(min_length=1, max_length=4096)


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


class DialogueOut(ResponseModel):
    conversation_id: str
    code: str
    name: Optional[str] = None
    chat_type: Optional[str] = None
    supervision_project_id: Optional[str] = None
    message_count: int = 0
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None


class DialogueMessageOut(ResponseModel):
    message_id: str
    query: Optional[str] = None
    response: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    create_time: Optional[datetime] = None


class DialogueMessagesOut(ResponseModel):
    conversation_id: str
    code: str
    messages: List[DialogueMessageOut] = Field(default_factory=list)


class BriefOut(ResponseModel):
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


class BriefVersionOut(ResponseModel):
    version: int
    # CHECK（migrations.py:1636）status IN ('draft','confirmed','rejected','superseded')
    status: Literal["draft", "confirmed", "rejected", "superseded"]
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


class ProjectContextOut(ResponseModel):
    project_id: str
    context: str
    has_project_context: bool


class BriefChangeOut(ResponseModel):
    """diff_brief_content 的一条差异。

    `from` / `to` 是 Python 关键字，所以字段名用 from_value / to_value，靠 alias
    保持线上 JSON 的键名不变（FastAPI 按 alias 序列化响应）。
    列表字段给 list[str]，文本字段给 str，所以是 Union。
    """

    model_config = ConfigDict(populate_by_name=True)

    field: str
    label: str
    from_value: Union[str, List[str]] = Field(..., alias="from")
    to_value: Union[str, List[str]] = Field(..., alias="to")
    # 只有列表字段才带这两个键；文本字段省略，序列化时是 null
    added: Optional[List[str]] = None
    removed: Optional[List[str]] = None


class BriefComparisonOut(ResponseModel):
    """GET .../research/briefs/{ref}/compare 的响应。

    之前路由没有 response_model，契约里该响应是空 object，前端只能手抄
    BriefComparison / BriefChange。status 的取值来自 DB CHECK
    （migrations.py:1636）。
    """

    brief_id: str
    code: str
    from_version: int
    to_version: int
    from_status: Literal["draft", "confirmed", "rejected", "superseded"]
    to_status: Literal["draft", "confirmed", "rejected", "superseded"]
    from_confirmed_at: Optional[datetime] = None
    to_confirmed_at: Optional[datetime] = None
    changes: List[BriefChangeOut] = Field(default_factory=list)


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


@supervision_research_router.post(
    "/projects/{project_ref}/research/dialogues/{conversation_id}/messages",
    response_model=DialogueMessageOut,
)
def post_dialogue_message(
    project_ref: str,
    conversation_id: str,
    body: DialogueMessageBody = Body(...),
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """在项目内研究讨论里发一条言，并请求 AI 回复（§4.2、§15.2 第1/6条）。

    刻意分两步提交：发言先独立落库并提交，模型不可用或调用失败时发言
    不会跟着回滚——讨论记录是成员的工作内容，不是模型的附属品。此时
    接口返回 503，消息保持未回复状态，可用下面的 ``…/reply`` 补。

    模型上下文只含本人这条会话的历史 + 项目已确认共享的结构化内容，
    绝不含另一角色的任何私人消息（§16.3 第2、3条）。
    """
    _, message = call(
        session, research.append_dialogue_message, project_ref, user.id,
        text=body.text, conversation_id=conversation_id,
    )
    message_id = message.id
    llm = _resolve_discussion_llm()
    if llm is None:
        raise _unavailable(
            "模型能力尚未接入：这条发言已保存，AI 回复可在模型接入后补生成。"
        )
    message = call(
        session, research.generate_dialogue_reply, project_ref, user.id,
        message_id=message_id, llm=llm,
    )
    return _message_out(message)


@supervision_research_router.post(
    "/projects/{project_ref}/research/messages/{message_id}/reply",
    response_model=DialogueMessageOut,
)
def regenerate_dialogue_reply(
    project_ref: str,
    message_id: str,
    session: Session = Depends(get_db),
    user: UserModel = Depends(require_password_changed),
):
    """给一条还没有回复的发言补 AI 回复（模型刚接入、或上次生成失败）。

    已有回复的消息不会重复生成：仓储层报 409，避免重复点击白烧一次模型。
    """
    llm = _resolve_discussion_llm()
    if llm is None:
        raise _unavailable("模型能力尚未接入，无法生成研究讨论回复。")
    message = call(
        session, research.generate_dialogue_reply, project_ref, user.id,
        message_id=message_id, llm=llm,
    )
    return _message_out(message)


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
    "/projects/{project_ref}/research/briefs/{brief_ref}/compare",
    response_model=BriefComparisonOut,
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
