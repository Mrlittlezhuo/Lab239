"""项目内研究讨论与结构化研究定义的数据访问（里程碑2）。

隔离规则（设计方案 §4.2、§9.3、§10、§16.3）：

- 研究讨论会话复用既有 ``conversation`` 表：``supervision_project_id``
  决定它属于哪个科研协作项目，``owner_id`` 决定它属于谁。读取任何一条
  会话都必须同时校验"当前用户是项目成员"和"会话所有者就是当前用户"，
  跨用户、跨项目、猜 ID 一律 ``LookupError``（路由层收敛成 404），
  不区分"不存在"和"无权"，避免会话枚举。
- 只有已确认的 ``ResearchBrief`` 版本是项目共享对象。另一角色读到的
  永远是 ``shared_version`` 那一份结构化内容，读不到草稿、来源会话
  和来源消息；``source_*`` 字段只在所有者自己的响应里出现。
- 版本不覆盖：重新提取、修改已确认内容都新增版本行，旧版本只读保留，
  ``shared_version`` 始终指向对外生效的那一版，检索任务因此能绑定
  当时的依据（§4.4）。
- 本模块**不写** ``memory`` / ``session_summary`` 等个人记忆表：项目
  内容只作为项目范围上下文注入，不变成任何人的全局个人偏好（§9.3）。

模型调用通过参数注入（``extract_brief_draft(..., llm=...)``、
``generate_dialogue_reply(..., llm=...)``）：统一容量闸门在别处实现，这里
只要求对象有 ``.invoke(messages)`` 并返回带 ``.content`` 的结果。提示词
组装与结果解析都是纯函数，可单独测试。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.db.models.supervision_model import SupervisionProjectModel
from chatchat.server.db.models.supervision_research_model import (
    BRIEF_CONTENT_FIELDS,
    BRIEF_ENTRY_TYPE_VALUES,
    BRIEF_FIELD_LABELS,
    BRIEF_KIND_VALUES,
    BRIEF_LIST_FIELDS,
    BRIEF_TEXT_FIELDS,
    CODE_BRIEF,
    CODE_DIALOGUE,
    DIALOGUE_CHAT_TYPE,
    SupervisionResearchBriefModel,
    SupervisionResearchBriefVersionModel,
)
from chatchat.server.db.repository.supervision_repository import (
    ROLE_MENTOR,
    ROLE_STUDENT,
    active_member_ids,
    new_id,
    next_code,
    notify,
    record_audit,
    require_member,
)
from chatchat.server.db.repository.supervision_work_repository import (
    WorkError,
    WorkValidationError,
)

KIND_RESEARCH_BRIEF = "research_brief"
KIND_MENTOR_GUIDANCE = "mentor_guidance"

# 提取时最多带回多少条消息：长会话全量塞给模型既慢又容易把无关历史
# 一起提炼进研究定义。
MAX_EXTRACTION_MESSAGES = 60

# 一次讨论回复带入的历史轮次上限。讨论是长对话，全量重放既超预算又会
# 让模型反复回到早已澄清过的旧问题；界面要看的完整历史仍从库里读。
DISCUSSION_HISTORY_LIMIT = 20

# message 表的 query / response 列宽（String(4096)）。超出就截断并在
# meta_data 里标 truncated，绝不静默丢弃后半段。
MAX_QUERY_CHARS = 4096
MAX_RESPONSE_CHARS = 4096

# 结构化内容的长度上限（模型输出与界面输入都受这一条约束）。
MAX_FIELD_CHARS = 8000
MAX_LIST_ITEMS = 50
MAX_LIST_ITEM_CHARS = 300


def _now() -> datetime:
    return datetime.now()


# ---------------------------------------------------------------------------
# 研究讨论会话（复用 conversation 表）
# ---------------------------------------------------------------------------


def dialogue_code(session: Session, conversation: ConversationModel) -> str:
    """现算会话可读编号 ``CV-001``（§4.0）。

    不入库是为了不再为"一个编号"建表：会话本身已经在 ``conversation``
    里。编号按项目内创建顺序排名，只用于界面与审计展示，不作为外键。
    """
    rows = session.execute(
        select(ConversationModel.id)
        .where(
            ConversationModel.supervision_project_id
            == conversation.supervision_project_id
        )
        .order_by(ConversationModel.create_time.asc(), ConversationModel.id.asc())
    ).scalars().all()
    try:
        rank = list(rows).index(conversation.id) + 1
    except ValueError:  # 理论上不会发生：会话刚被自己插入
        rank = 1
    return f"{CODE_DIALOGUE}-{rank:03d}"


def create_dialogue(
    session: Session,
    project_id: str,
    user_id: str,
    title: str,
    *,
    chat_type: str = DIALOGUE_CHAT_TYPE,
    reuse_existing: bool = True,
) -> ConversationModel:
    """为当前用户建立（或复用）本项目里的研究讨论会话。

    首期每个项目成员默认一条主研究会话（§4.2），所以默认复用已有的；
    角色决定会话归属，学生与导师在同一项目里各自一条，彼此看不见。
    ``project_id`` 参数是**科研协作项目**的 id 或编号；写库时只填
    ``supervision_project_id``，``conversation.project_id`` 保持 NULL，
    它继续只指向个人论文研究项目。
    """
    project, role = require_member(session, project_id, user_id)
    if reuse_existing:
        existing = list_dialogues(session, project.id, user_id)
        if existing:
            return existing[0]
    if not (title or "").strip():
        raise WorkValidationError("研究讨论会话必须有标题")
    now = _now()
    conversation = ConversationModel(
        id=new_id(),
        name=title.strip()[:50],
        chat_type=chat_type or DIALOGUE_CHAT_TYPE,
        owner_id=user_id,
        temporary=False,
        supervision_project_id=project.id,
        create_time=now,
        update_time=now,
    )
    session.add(conversation)
    session.flush()
    record_audit(
        session, project_id=project.id, object_type="research_dialogue",
        object_code=dialogue_code(session, conversation), actor_id=user_id,
        actor_role=role, action="创建研究讨论会话", detail=conversation.name,
    )
    session.flush()
    return conversation


def list_dialogues(
    session: Session, project_id: str, user_id: str
) -> list[ConversationModel]:
    """只列当前用户自己在该项目里的会话。"""
    project, _ = require_member(session, project_id, user_id)
    return list(
        session.execute(
            select(ConversationModel)
            .where(
                ConversationModel.supervision_project_id == project.id,
                ConversationModel.owner_id == user_id,
            )
            .order_by(ConversationModel.update_time.desc(), ConversationModel.id.desc())
        )
        .scalars()
        .all()
    )


def _owned_dialogue(
    session: Session, project: SupervisionProjectModel, user_id: str, conversation_id: str
) -> ConversationModel:
    """同时校验会话所有者 + 项目成员；不满足统一 ``LookupError``。

    会话 ID 不是权限（AGENTS.md 鉴权底线）：只按 ID 查再在前端隐藏是不允许的。
    """
    conversation = session.execute(
        select(ConversationModel).where(
            ConversationModel.id == conversation_id,
            ConversationModel.owner_id == user_id,
            ConversationModel.supervision_project_id == project.id,
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise LookupError("会话不存在或无权访问")
    return conversation


def _default_dialogue(
    session: Session, project: SupervisionProjectModel, user_id: str
) -> ConversationModel:
    dialogues = list_dialogues(session, project.id, user_id)
    if not dialogues:
        raise WorkError("本项目里还没有你自己的研究讨论会话")
    return dialogues[0]


def _dialogue_messages(
    session: Session,
    conversation: ConversationModel,
    *,
    message_ids: Optional[Iterable[str]] = None,
    limit: int = 200,
    latest: bool = True,
) -> list[MessageModel]:
    """取会话消息，默认取**最近** ``limit`` 条并按时间正序返回。

    默认取最近而不是最早是刻意的：讨论是长对话，界面和模型上下文都要看
    最新的一段；按最早截断会让"这条会话讨论到什么程度"永远停在开头
    （§4.2）。指定 ``message_ids`` 时不受 ``limit`` 影响（提取来源要精确
    到那几条）。
    """
    wanted = [str(x) for x in (message_ids or []) if str(x).strip()]
    stmt = select(MessageModel).where(MessageModel.conversation_id == conversation.id)
    if wanted:
        stmt = stmt.where(MessageModel.id.in_(wanted)).order_by(
            MessageModel.create_time.asc(), MessageModel.id.asc()
        )
    elif latest:
        stmt = stmt.order_by(
            MessageModel.create_time.desc(), MessageModel.id.desc()
        ).limit(max(1, limit))
    else:
        stmt = stmt.order_by(
            MessageModel.create_time.asc(), MessageModel.id.asc()
        ).limit(max(1, limit))
    rows = list(session.execute(stmt).scalars().all())
    if latest and not wanted:
        rows.reverse()
    if wanted:
        found = {row.id for row in rows}
        missing = [x for x in wanted if x not in found]
        if missing:
            raise WorkValidationError("以下消息不属于你的研究会话或不存在：" + "、".join(missing))
    return rows


def count_dialogue_messages(
    session: Session, conversation: ConversationModel
) -> int:
    """会话里已有多少条消息（界面显示"这条会话讨论到什么程度"）。"""
    return int(
        session.execute(
            select(func.count(MessageModel.id)).where(
                MessageModel.conversation_id == conversation.id
            )
        ).scalar()
        or 0
    )


def get_dialogue_messages(
    session: Session,
    project_id: str,
    user_id: str,
    conversation_id: str,
    *,
    limit: int = 200,
) -> list[MessageModel]:
    """读取自己研究会话的原始消息；别人的会话一律 404。"""
    project, _ = require_member(session, project_id, user_id)
    conversation = _owned_dialogue(session, project, user_id, conversation_id)
    return _dialogue_messages(session, conversation, limit=limit)


# ---------------------------------------------------------------------------
# 项目内 AI 讨论：一次发言 + 一次模型回复（§4.2、§15.2 第1/6条）
# ---------------------------------------------------------------------------


def build_discussion_prompt(
    *,
    project_code: str,
    project_name: str,
    role: str,
    project_context: str,
    history: Iterable[Any],
    question: str,
) -> list[dict]:
    """组装项目内讨论的一次调用（纯函数，可直接单测）。

    上下文边界（§9.3、§16.3 第2、3、6条）：

    - 历史只放**当前用户自己**这条会话里的消息，另一角色的原始消息、
      摘要、私人草稿永远进不来（调用方已按 ``owner_id`` 取过）；
    - 项目范围信息只有 ``build_project_context`` 给的已确认结构化版本；
    - 提示词里明确要求模型不得把项目内容当个人偏好、不得声称已写入
      个人记忆，也不得替成员确认研究内容（确认必须在界面上操作）。
    """
    role_label = "导师" if role == ROLE_MENTOR else "学生"
    system = (
        f"你是科研协作项目「{project_code} {project_name}」里的研究讨论助手，"
        f"正在与{role_label}讨论研究问题。\n"
        "你的职责是帮助成员逐步澄清背景、研究问题、方向、限制和已有尝试，"
        "而不是替成员决定研究方向，也不是替成员写出结论。\n"
        "硬性要求：\n"
        "1. 只能依据成员在讨论中说过的内容和下面给出的「项目共享研究内容」"
        "作答；信息不足时直接提问澄清，不得编造论文、数据、实验结果或"
        "“已完成检索”之类的既成事实。\n"
        "2. 不得把 AI 的建议表述成成员已经采纳或已经确认的决定；研究内容"
        "的确认只能在界面上由成员本人操作。\n"
        "3. 「项目共享研究内容」属于项目范围信息，不是成员的个人偏好或长期"
        "记忆；不得声称已把它写入个人记忆，也不得把它带到本项目之外。\n"
        "4. 已确认的研究内容只代表当前工作定义，不代表已被论文证据证实。\n"
        "5. 用中文回答，简洁、可执行；需要成员补充信息时列出具体问题。"
    )
    if project_context:
        system = f"{system}\n\n{project_context}"
    else:
        system = (
            f"{system}\n\n（本项目还没有已确认共享的研究内容；"
            "需要结构化研究内容时，请提示成员在界面上提取并确认。）"
        )

    messages: list[dict] = [{"role": "system", "content": system}]
    for row in history:
        if isinstance(row, dict):
            query = row.get("query") or ""
            response = row.get("response") or ""
        else:
            query = getattr(row, "query", None) or ""
            response = getattr(row, "response", None) or ""
        if query:
            messages.append({"role": "user", "content": query})
        if response:
            messages.append({"role": "assistant", "content": response})
    messages.append({"role": "user", "content": question})
    return messages


def append_dialogue_message(
    session: Session,
    project_id: str,
    user_id: str,
    *,
    text: str,
    conversation_id: str = "",
) -> tuple[ConversationModel, MessageModel]:
    """把成员这条发言先落库，**不**调用模型。

    分两步是刻意的：模型不可用或调用失败时，发言本身不能跟着回滚——
    讨论记录是成员的工作内容，不是模型的附属品。回复由
    ``generate_dialogue_reply`` 单独写入，失败时这条消息保持
    ``response=""``，界面显示"尚未回复"并可重试。

    逐条聊天消息不写审计事件（EV 列表会被淹没）；模型调用的事实记在
    ``meta_data`` 里，可追溯又不污染审计清单。
    """
    project, _ = require_member(session, project_id, user_id)
    conversation = (
        _owned_dialogue(session, project, user_id, conversation_id)
        if conversation_id
        else _default_dialogue(session, project, user_id)
    )
    content = (text or "").strip()
    if not content:
        raise WorkValidationError("消息内容不能为空")
    if len(content) > MAX_QUERY_CHARS:
        raise WorkValidationError(
            f"单条消息最多 {MAX_QUERY_CHARS} 字，请拆分后重新发送"
        )
    now = _now()
    message = MessageModel(
        id=new_id(),
        conversation_id=conversation.id,
        chat_type=conversation.chat_type or DIALOGUE_CHAT_TYPE,
        query=content,
        response="",
        meta_data={"project_code": project.code, "pending_reply": True},
        create_time=now,
    )
    session.add(message)
    conversation.update_time = now
    session.flush()
    return conversation, message


def generate_dialogue_reply(
    session: Session,
    project_id: str,
    user_id: str,
    *,
    message_id: str,
    llm,
    history_limit: int = DISCUSSION_HISTORY_LIMIT,
) -> MessageModel:
    """为一条已有发言生成 AI 回复并写回同一行。

    - ``llm`` 由路由层注入（统一容量闸门包好的模型）；传 ``None`` 时报
      409，路由层提前返回 503，仓储层绝不自己起模型。
    - 只读本人这条会话的消息；模型上下文里不含另一角色的任何私人内容。
    - 回复超过 ``response`` 列宽时截断保存，并在 ``meta_data`` 里标
      ``truncated_response``，不假装完整。
    - 已有回复的消息不重复生成（重复点击不会白烧一次模型）。
    """
    project, role = require_member(session, project_id, user_id)
    if llm is None:
        raise WorkError("模型能力尚未接入，无法生成研究讨论回复")
    message = session.get(MessageModel, message_id)
    if message is None:
        raise LookupError("消息不存在或无权访问")
    conversation = _owned_dialogue(session, project, user_id, message.conversation_id)
    if (message.response or "").strip():
        raise WorkError("这条消息已经有回复，不需要重新生成")

    # 只取最近 history_limit+1 条（多出来的一条就是本次待回复的发言本身）。
    rows = _dialogue_messages(
        session, conversation, limit=max(1, history_limit) + 1, latest=True
    )
    history = [row for row in rows if row.id != message_id][-max(1, history_limit):]
    context = build_project_context(session, project.id, user_id)
    prompt = build_discussion_prompt(
        project_code=project.code, project_name=project.name, role=role,
        project_context=context, history=history, question=message.query or "",
    )
    raw = _invoke_llm(llm, prompt)
    truncated = len(raw) > MAX_RESPONSE_CHARS
    now = _now()
    message.response = raw[:MAX_RESPONSE_CHARS]
    message.meta_data = {
        **(message.meta_data or {}),
        "pending_reply": False,
        "project_context_injected": bool(context),
        "history_turns": len(history),
        "model_name": str(getattr(llm, "model_name", "") or ""),
        "consumer": "project_discussion",
        "truncated_response": truncated,
        "reply_time": now.isoformat(),
    }
    conversation.update_time = now
    session.flush()
    return message


# ---------------------------------------------------------------------------
# 结构化内容：清洗、比较与提示词
# ---------------------------------------------------------------------------


def _clean_content(content: dict) -> dict:
    """按白名单清洗结构化内容：未知键忽略，列表去空去重，长度截断。

    上限放在仓储层而不是只放在路由的 pydantic 校验里，是因为模型输出
    也走这里：一次失控的长回答不应该把整段聊天原样塞进研究定义。
    """
    if not isinstance(content, dict):
        raise WorkValidationError("研究定义内容必须是对象")
    cleaned: dict[str, Any] = {}
    for name in BRIEF_TEXT_FIELDS:
        value = content.get(name)
        if value is None:
            cleaned[name] = ""
        elif isinstance(value, (list, tuple)):
            # 模型常把"候选方法""评价指标"写成列表；库里是文本，按顿号接起来
            cleaned[name] = "、".join(str(x).strip() for x in value if str(x).strip())
        else:
            cleaned[name] = str(value).strip()
        cleaned[name] = cleaned[name][:MAX_FIELD_CHARS]
    for name in BRIEF_LIST_FIELDS:
        value = content.get(name)
        if value is None or value == "":
            value = []
        if isinstance(value, str):
            value = [x for x in re.split(r"[,，、;；\n]", value) if x.strip()]
        if not isinstance(value, list):
            raise WorkValidationError(f"{BRIEF_FIELD_LABELS[name]}必须是字符串列表")
        items: list[str] = []
        for item in value:
            text_value = str(item).strip()[:MAX_LIST_ITEM_CHARS]
            if text_value and text_value not in items:
                items.append(text_value)
            if len(items) >= MAX_LIST_ITEMS:
                break
        cleaned[name] = items
    return cleaned


def _content_of(row) -> dict:
    return {name: getattr(row, name) for name in BRIEF_CONTENT_FIELDS}


def _apply_content(row, content: dict) -> None:
    for name in BRIEF_CONTENT_FIELDS:
        setattr(row, name, content[name])


def _is_empty_content(content: dict) -> bool:
    for name in BRIEF_TEXT_FIELDS:
        if content.get(name):
            return False
    for name in BRIEF_LIST_FIELDS:
        if content.get(name):
            return False
    return True


def diff_brief_content(before: dict, after: dict) -> list[dict]:
    """两个版本的结构化内容逐字段比较（§16.3 第4条"版本比较"）。

    纯函数：只吃两个内容字典，不碰数据库，界面和测试都能直接用。
    """
    changes: list[dict] = []
    for name in BRIEF_CONTENT_FIELDS:
        old, new = before.get(name), after.get(name)
        if name in BRIEF_LIST_FIELDS:
            old_list = list(old or [])
            new_list = list(new or [])
            if old_list != new_list:
                changes.append({
                    "field": name,
                    "label": BRIEF_FIELD_LABELS[name],
                    "from": old_list,
                    "to": new_list,
                    "added": [x for x in new_list if x not in old_list],
                    "removed": [x for x in old_list if x not in new_list],
                })
            continue
        old_text = str(old or "").strip()
        new_text = str(new or "").strip()
        if old_text != new_text:
            changes.append({
                "field": name,
                "label": BRIEF_FIELD_LABELS[name],
                "from": old_text,
                "to": new_text,
            })
    return changes


def _message_turns(messages: Iterable[Any]) -> list[str]:
    """把消息折成"学生/导师说 → AI 答"的文本轮次。"""
    turns: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            query = message.get("query") or ""
            response = message.get("response") or ""
        else:
            query = getattr(message, "query", None) or ""
            response = getattr(message, "response", None) or ""
        if query:
            turns.append(f"成员：{query}")
        if response:
            turns.append(f"AI：{response}")
    return turns


def build_extraction_prompt(
    project_code: str, project_name: str, role: str, messages: Iterable[Any]
) -> list[dict]:
    """组装"从本人聊天提取研究内容"的提示词（纯函数，可直接单测）。

    只允许从给定的对话里提炼，不允许编造；输出必须是固定 JSON 结构，
    字段名与库里的结构化内容列一一对应。
    """
    schema = ",\n".join(f'  "{name}": ""' for name in BRIEF_TEXT_FIELDS)
    list_schema = ",\n".join(f'  "{name}": []' for name in BRIEF_LIST_FIELDS)
    labels = "；".join(f"{name}={BRIEF_FIELD_LABELS[name]}" for name in BRIEF_CONTENT_FIELDS)
    turns = _message_turns(messages)
    system = (
        "你是科研协作项目里的研究内容整理助手。你的任务是把成员与 AI 的"
        "研究讨论对话整理成结构化研究定义草稿。\n"
        "硬性要求：\n"
        "1. 只能使用对话中出现过的信息，不得补充对话里没有的判断；"
        "对话没有提到的字段留空字符串或空列表。\n"
        "2. 这是草稿，不是已确认结论；不要替成员做决定，也不要把 AI 的"
        "建议写成成员已经采纳的结论。\n"
        "3. 只输出一个 JSON 对象，不要输出解释、前后缀或 Markdown 代码块。\n"
        f"4. JSON 的键固定为：{labels}。\n"
        "5. 全部用中文，关键词和子问题写成短词组列表。"
    )
    user = (
        f"科研协作项目：{project_code} {project_name}\n"
        f"当前整理人项目角色：{role}\n\n"
        "研究讨论对话：\n" + "\n".join(turns) + "\n\n"
        "请按下面的结构输出 JSON：\n{\n" + schema + ",\n" + list_schema + "\n}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_LABEL_TO_FIELD: dict[str, str] = {}
for _field, _label in BRIEF_FIELD_LABELS.items():
    _LABEL_TO_FIELD[_label] = _field
    _LABEL_TO_FIELD[_field] = _field
for _alias, _field in {
    "研究问题": "question",
    "核心问题": "question",
    "研究背景": "background",
    "背景": "background",
    "目标": "objectives",
    "研究对象": "subjects",
    "范围": "scope",
    "边界": "scope",
    "范围边界": "scope",
    "已有尝试": "prior_attempts",
    "假设": "hypotheses",
    "约束": "constraints",
    "方法": "methods",
    "候选方法": "methods",
    "指标": "metrics",
    "预期成果": "expected_outcomes",
    "成果": "expected_outcomes",
    "待验证问题": "to_verify",
    "待验证判断": "to_verify",
    "关键词": "keywords",
    "检索关键词": "keywords",
    "子问题": "sub_questions",
}.items():
    _LABEL_TO_FIELD[_alias] = _field


def _parse_json_payload(raw: str) -> Optional[dict]:
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            # 允许模型把内容包在 brief / research_brief 里
            for key in ("brief", "research_brief", "content"):
                inner = data.get(key)
                if isinstance(inner, dict):
                    return inner
            return data
    return None


def _parse_labeled_text(raw: str) -> dict:
    """兜底解析"标签：值"式纯文本（模型不按 JSON 返回时）。"""
    content: dict[str, Any] = {}
    current: Optional[str] = None
    for line in raw.splitlines():
        stripped = line.strip().strip("*").strip()
        if not stripped:
            continue
        matched = re.match(r"^[\-•*]?\s*([^:：]{1,24})[:：]\s*(.*)$", stripped)
        if matched and matched.group(1).strip() in _LABEL_TO_FIELD:
            current = _LABEL_TO_FIELD[matched.group(1).strip()]
            value = matched.group(2).strip()
        elif current:
            value = stripped
        else:
            continue
        if current in BRIEF_LIST_FIELDS:
            items = content.setdefault(current, [])
            for item in re.split(r"[,，、;；]", value):
                item = item.strip()
                if item and item not in items:
                    items.append(item)
        else:
            existing = content.get(current, "")
            content[current] = f"{existing}\n{value}".strip() if existing else value
    return content


def parse_brief_payload(raw: str) -> dict:
    """把模型返回内容解析成结构化草稿（纯函数，可直接单测）。

    先按 JSON 解析（容忍 Markdown 代码块与外层包裹），失败再按
    "标签：值"文本兜底；两种都拿不到内容时报 409，而不是静默生成
    一份空研究定义。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise WorkError("模型没有返回可整理的内容")
    payload = _parse_json_payload(raw)
    if payload is None:
        payload = _parse_labeled_text(raw)
    content = _clean_content(payload)
    if _is_empty_content(content):
        raise WorkError("模型返回的内容无法解析成结构化研究定义")
    return content


def _invoke_llm(llm, prompt: list[dict]) -> str:
    """调用注入的模型对象，取回文本。只要求 ``.invoke(messages).content``。"""
    try:
        result = llm.invoke(prompt)
    except Exception as exc:  # 模型侧任何异常都不能把事务留成半写状态
        raise WorkError(f"模型调用失败：{exc}")
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if not isinstance(content, str):
        raise WorkError("模型返回内容格式不正确")
    return content


# ---------------------------------------------------------------------------
# 结构化研究定义：身份、版本与可见性
# ---------------------------------------------------------------------------


def _resolve_kind(kind: str, role: str) -> str:
    """研究定义的种类必须与项目角色一致（§4.2 学生研究定义 / 导师建议）。"""
    expected = KIND_MENTOR_GUIDANCE if role == ROLE_MENTOR else KIND_RESEARCH_BRIEF
    if not kind:
        return expected
    if kind not in BRIEF_KIND_VALUES:
        raise WorkValidationError("研究定义种类不合法")
    if kind != expected:
        raise PermissionError(f"研究定义种类与当前项目角色不匹配：{role} 只能创建 {expected}")
    return kind


def _get_brief(
    session: Session,
    project: SupervisionProjectModel,
    user_id: str,
    brief_ref: str,
    *,
    owner_only: bool = False,
) -> SupervisionResearchBriefModel:
    """按内部 id 或 ``RB-001`` 定位研究定义。

    非所有者只能看到已共享的；没共享过的草稿与不存在返回同一个
    ``LookupError``，避免通过响应差异枚举别人的草稿。
    """
    brief = session.execute(
        select(SupervisionResearchBriefModel).where(
            SupervisionResearchBriefModel.project_id == project.id,
            (SupervisionResearchBriefModel.id == brief_ref)
            | (SupervisionResearchBriefModel.code == brief_ref),
            SupervisionResearchBriefModel.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if brief is None:
        raise LookupError("研究定义不存在")
    if brief.owner_user_id != user_id:
        if owner_only or brief.shared_version is None:
            raise LookupError("研究定义不存在")
    return brief


def _get_brief_version(
    session: Session, brief_id: str, version: int
) -> SupervisionResearchBriefVersionModel:
    row = session.execute(
        select(SupervisionResearchBriefVersionModel).where(
            SupervisionResearchBriefVersionModel.brief_id == brief_id,
            SupervisionResearchBriefVersionModel.version == version,
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("研究定义版本不存在")
    return row


def visible_version_number(
    brief: SupervisionResearchBriefModel, user_id: str
) -> int:
    """当前用户能看哪一版：本人看最新版，别人只看已共享的那一版。"""
    if brief.owner_user_id == user_id:
        return int(brief.current_version)
    if brief.shared_version is None:
        raise LookupError("研究定义不存在")
    return int(brief.shared_version)


def brief_payload(session: Session, brief: SupervisionResearchBriefModel, user_id: str) -> dict:
    """把研究定义序列化成"该用户有权看到的那一版"。

    来源会话与来源消息只对所有者可见；另一角色拿到的是纯结构化内容，
    无法反查原始聊天（§16.3 第5条）。
    """
    is_owner = brief.owner_user_id == user_id
    version = visible_version_number(brief, user_id)
    row = _get_brief_version(session, brief.id, version)
    data = {
        "brief_id": brief.id,
        "code": brief.code,
        "display_code": f"{brief.code}-v{version}",
        "project_id": brief.project_id,
        "owner_user_id": brief.owner_user_id,
        "owner_role": brief.owner_role,
        "kind": brief.kind,
        "title": brief.title,
        "status": brief.status,
        "version_status": row.status,
        "entry_type": row.entry_type,
        "current_version": int(brief.current_version),
        "shared_version": brief.shared_version,
        "shown_version": version,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": row.confirmed_at,
        "confirm_note": row.confirm_note,
        "create_time": brief.create_time,
        "update_time": brief.update_time,
        "is_owner": is_owner,
        "can_edit": is_owner and brief.status in ("draft", "confirmed"),
    }
    data.update(_content_of(row))
    if is_owner:
        data["source_conversation_id"] = brief.source_conversation_id
        data["source_message_ids"] = list(row.source_message_ids or [])
    else:
        data["source_conversation_id"] = None
        data["source_message_ids"] = []
    return data


def _add_brief_version(
    session: Session,
    brief: SupervisionResearchBriefModel,
    content: dict,
    *,
    created_by: str,
    created_role: str,
    ai_content: Optional[dict] = None,
    edited_fields: Optional[list[str]] = None,
    source_conversation_id: str = "",
    source_message_ids: Optional[list[str]] = None,
    status: str = "draft",
) -> SupervisionResearchBriefVersionModel:
    row = SupervisionResearchBriefVersionModel(
        id=new_id(),
        brief_id=brief.id,
        project_id=brief.project_id,
        version=int(brief.current_version),
        status=status,
        entry_type=brief.entry_type,
        ai_content=ai_content or {},
        edited_fields=list(edited_fields or []),
        source_conversation_id=source_conversation_id or brief.source_conversation_id,
        source_message_ids=list(source_message_ids or []),
        created_by=created_by,
        created_role=created_role,
        create_time=_now(),
    )
    _apply_content(row, content)
    session.add(row)
    session.flush()
    return row


def _append_brief_version(
    session: Session,
    brief: SupervisionResearchBriefModel,
    content: dict,
    *,
    created_by: str,
    created_role: str,
    ai_content: Optional[dict] = None,
    edited_fields: Optional[list[str]] = None,
    source_conversation_id: str = "",
    source_message_ids: Optional[list[str]] = None,
) -> SupervisionResearchBriefVersionModel:
    """追加新版本：旧版本标记被替代但不删（§4.3.1）。"""
    previous = _get_brief_version(session, brief.id, int(brief.current_version))
    previous.superseded_at = _now()
    brief.current_version = int(brief.current_version) + 1
    return _add_brief_version(
        session,
        brief,
        content,
        created_by=created_by,
        created_role=created_role,
        ai_content=ai_content,
        edited_fields=edited_fields,
        source_conversation_id=source_conversation_id,
        source_message_ids=source_message_ids,
        status="draft",
    )


def create_brief_draft(
    session: Session,
    project_id: str,
    user_id: str,
    *,
    title: str = "",
    kind: str = "",
    content: Optional[dict] = None,
    conversation_id: str = "",
    message_ids: Optional[list[str]] = None,
    entry_type: str = "mixed",
) -> SupervisionResearchBriefModel:
    """手工创建研究定义草稿：草稿只属于提取人，另一角色读不到。"""
    project, role = require_member(session, project_id, user_id)
    resolved_kind = _resolve_kind(kind, role)
    cleaned = _clean_content(content or {})
    if entry_type not in BRIEF_ENTRY_TYPE_VALUES:
        raise WorkValidationError("条目类型不合法")
    now = _now()
    for _ in range(5):
        brief = SupervisionResearchBriefModel(
            id=new_id(),
            code=next_code(session, SupervisionResearchBriefModel,
                           SupervisionResearchBriefModel.code, CODE_BRIEF),
            project_id=project.id,
            owner_user_id=user_id,
            owner_role=role,
            kind=resolved_kind,
            title=(title or "").strip()[:200],
            entry_type=entry_type,
            status="draft",
            current_version=1,
            source_conversation_id=conversation_id or None,
            create_time=now,
            update_time=now,
        )
        _apply_content(brief, cleaned)
        session.add(brief)
        try:
            session.flush()
        except IntegrityError:
            # 编号并发撞车：回滚到保存点重试（与 0A/里程碑1 同一套做法）
            session.rollback()
            continue
        _add_brief_version(
            session, brief, cleaned, created_by=user_id, created_role=role,
            source_conversation_id=conversation_id,
            source_message_ids=list(message_ids or []),
        )
        record_audit(
            session, project_id=project.id, object_type="research_brief",
            object_code=brief.code, actor_id=user_id, actor_role=role,
            action="创建研究定义草稿", detail=brief.title or brief.code,
        )
        session.flush()
        return brief
    raise RuntimeError("研究定义编号生成失败，请重试")


def extract_brief_draft(
    session: Session,
    project_id: str,
    user_id: str,
    *,
    llm,
    conversation_id: str = "",
    message_ids: Optional[list[str]] = None,
    brief_ref: str = "",
    title: str = "",
) -> SupervisionResearchBriefModel:
    """从**本人**研究会话的消息里提取结构化研究定义草稿。

    - ``llm`` 由调用方注入（统一容量闸门包好的模型）；传 ``None`` 时
      仓储层直接报 409，路由层提前返回 503，绝不自己偷偷起模型。
    - 只读取当前用户自己的消息；另一角色的消息既进不了提示词，
      也不会出现在草稿里（§16.3 第2、3条）。
    - 给了 ``brief_ref`` 就在原研究定义上追加新版本（重新提取不覆盖
      旧版本，§4.3.1）；否则新建一份草稿。
    - 草稿不共享、不写记忆，只有随后的 ``confirm_brief`` 才进项目共享区。
    """
    project, role = require_member(session, project_id, user_id)
    if llm is None:
        raise WorkError("模型能力尚未接入，无法从聊天提取研究内容")

    if conversation_id:
        conversation = _owned_dialogue(session, project, user_id, conversation_id)
    else:
        conversation = _default_dialogue(session, project, user_id)
    messages = _dialogue_messages(
        session, conversation, message_ids=message_ids, limit=MAX_EXTRACTION_MESSAGES
    )
    if not messages:
        raise WorkError("会话里还没有可提取的消息")

    prompt = build_extraction_prompt(project.code, project.name, role, messages)
    content = parse_brief_payload(_invoke_llm(llm, prompt))
    source_ids = [row.id for row in messages]

    if brief_ref:
        brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
        if brief.status == "rejected":
            raise WorkError("已否定的研究定义不能追加新版本，请新建一份")
        _append_brief_version(
            session, brief, content, created_by=user_id, created_role=role,
            ai_content=content, source_conversation_id=conversation.id,
            source_message_ids=source_ids,
        )
        brief.status = "draft"
        brief.title = (title or "").strip()[:200] or brief.title
        brief.source_conversation_id = conversation.id
        brief.update_time = _now()
        record_audit(
            session, project_id=project.id, object_type="research_brief",
            object_code=brief.code, actor_id=user_id, actor_role=role,
            action="重新提取研究定义",
            detail=f"新增 v{brief.current_version}，v{brief.current_version - 1} 保留只读",
        )
        session.flush()
        return brief

    brief = create_brief_draft(
        session, project.id, user_id,
        title=(title or "").strip()[:200] or "从研究讨论提取",
        content=content, conversation_id=conversation.id, message_ids=source_ids,
    )
    version = _get_brief_version(session, brief.id, 1)
    version.ai_content = content
    record_audit(
        session, project_id=project.id, object_type="research_brief",
        object_code=brief.code, actor_id=user_id, actor_role=role,
        action="从聊天提取研究定义草稿",
        detail=f"来源会话消息 {len(source_ids)} 条；草稿未共享",
    )
    session.flush()
    return brief


def update_brief(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    fields: dict,
) -> SupervisionResearchBriefModel:
    """编辑本人研究定义。

    草稿阶段就地改当前版本（还没人看到，不产生版本噪音）；已确认的内容
    一改就升版本，旧版本保留，``shared_version`` 仍指向对外生效的旧版
    （§4.3.1、§4.4）。
    """
    project, role = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    if brief.status not in ("draft", "confirmed"):
        raise WorkError(f"当前状态不能编辑：{brief.status}")

    raw_content = fields.get("content")
    if raw_content is None:
        raw_content = {name: fields[name] for name in BRIEF_CONTENT_FIELDS if name in fields}
    content = _clean_content(raw_content) if raw_content else None

    if "title" in fields and fields["title"] is not None:
        brief.title = str(fields["title"]).strip()[:200]
    if fields.get("entry_type"):
        brief.entry_type = str(fields["entry_type"])

    version_row = _get_brief_version(session, brief.id, int(brief.current_version))
    if content is not None:
        changed = [
            name for name in BRIEF_CONTENT_FIELDS
            if getattr(version_row, name) != content[name]
        ]
        if brief.status == "confirmed" and changed:
            _append_brief_version(
                session, brief, content, created_by=user_id, created_role=role,
                ai_content=dict(version_row.ai_content or {}), edited_fields=changed,
                source_conversation_id=brief.source_conversation_id or "",
                source_message_ids=list(version_row.source_message_ids or []),
            )
            brief.status = "draft"
            record_audit(
                session, project_id=project.id, object_type="research_brief",
                object_code=brief.code, actor_id=user_id, actor_role=role,
                action="修改已确认研究定义",
                detail=f"生成 v{brief.current_version}；已共享的 "
                       f"v{brief.shared_version} 继续作为项目共享依据，不被追溯改写",
            )
        else:
            _apply_content(version_row, content)
            if changed:
                version_row.edited_fields = sorted(
                    set(list(version_row.edited_fields or []) + changed)
                )
    brief.update_time = _now()
    if brief.status != "confirmed" or content is None:
        record_audit(
            session, project_id=project.id, object_type="research_brief",
            object_code=brief.code, actor_id=user_id, actor_role=role,
            action="修改研究定义草稿", detail=brief.title or brief.code,
        )
    session.flush()
    return brief


def confirm_brief(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    *,
    note: str = "",
) -> SupervisionResearchBriefModel:
    """确认并发布当前版本：草稿进入项目共享区（§4.3.1、§16.3 第5条）。

    确认的是结构化版本本身，不是来源消息；另一角色此后只读得到这一版
    结构化内容。审计与通知只引用 ``RB-`` 编号。
    """
    project, role = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    if brief.status != "draft":
        raise WorkError(f"只有草稿可以确认共享，当前状态：{brief.status}")
    version_row = _get_brief_version(session, brief.id, int(brief.current_version))
    if not (version_row.question or "").strip():
        raise WorkValidationError("确认共享前必须填写核心研究问题")

    now = _now()
    version_row.status = "confirmed"
    version_row.confirmed_by = user_id
    version_row.confirmed_at = now
    version_row.confirm_note = note or ""
    brief.status = "confirmed"
    brief.shared_version = int(brief.current_version)
    brief.confirmed_by = user_id
    brief.confirmed_at = now
    brief.update_time = now

    display = f"{brief.code}-v{brief.shared_version}"
    kind_label = "导师建议" if brief.kind == KIND_MENTOR_GUIDANCE else "研究定义"
    event_code = record_audit(
        session, project_id=project.id, object_type="research_brief",
        object_code=brief.code, actor_id=user_id, actor_role=role,
        action=f"确认共享{kind_label}",
        detail=f"{display}；{(note or brief.title or brief.code)[:2000]}",
    )
    other_role = ROLE_STUDENT if role == ROLE_MENTOR else ROLE_MENTOR
    notify(
        session, event_code=event_code,
        recipients=active_member_ids(session, project_id=project.id, role=other_role),
        project_id=project.id, object_type="research_brief", object_code=display,
        title=f"项目共享{kind_label}已更新：{display}",
        event_type="research_brief.confirmed",
        body=(note or version_row.question or "")[:2000],
    )
    session.flush()
    return brief


def reject_brief(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    *,
    reason: str,
) -> SupervisionResearchBriefModel:
    """否定丢弃（§4.3 状态"已否定"）：留行留审计，不物理删除。"""
    if not (reason or "").strip():
        raise WorkValidationError("否定研究定义必须填写原因")
    project, role = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    if brief.shared_version is not None:
        raise WorkError("已确认共享的研究定义不能直接否定；请创建新版本")
    if brief.status in ("rejected", "superseded"):
        raise WorkError(f"研究定义已是终态：{brief.status}")
    version_row = _get_brief_version(session, brief.id, int(brief.current_version))
    brief.status = "rejected"
    brief.update_time = _now()
    version_row.status = "rejected"
    record_audit(
        session, project_id=project.id, object_type="research_brief",
        object_code=brief.code, actor_id=user_id, actor_role=role,
        action="否定研究定义草稿", detail=reason[:2000],
    )
    session.flush()
    return brief


def delete_brief(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    *,
    reason: str,
) -> SupervisionResearchBriefModel:
    """软删除未共享的草稿；已共享的研究定义作为项目审计资产保留（§13）。"""
    if not (reason or "").strip():
        raise WorkValidationError("丢弃研究定义必须填写原因")
    project, role = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    if brief.shared_version is not None:
        raise WorkError("已确认共享的研究定义不能丢弃；它是项目审计资产，只能出新版本")
    brief.deleted_at = _now()
    brief.delete_reason = reason[:2000]
    brief.update_time = _now()
    record_audit(
        session, project_id=project.id, object_type="research_brief",
        object_code=brief.code, actor_id=user_id, actor_role=role,
        action="丢弃研究定义草稿", detail=reason[:2000],
    )
    session.flush()
    return brief


# ---------------------------------------------------------------------------
# 读取、共享区与版本比较
# ---------------------------------------------------------------------------


def _shared_brief_rows(
    session: Session, project_id: str
) -> list[SupervisionResearchBriefModel]:
    return list(
        session.execute(
            select(SupervisionResearchBriefModel)
            .where(
                SupervisionResearchBriefModel.project_id == project_id,
                SupervisionResearchBriefModel.shared_version.is_not(None),
                SupervisionResearchBriefModel.deleted_at.is_(None),
            )
            .order_by(SupervisionResearchBriefModel.confirmed_at.desc())
        )
        .scalars()
        .all()
    )


def list_briefs(
    session: Session,
    project_id: str,
    user_id: str,
    *,
    scope: str = "all",
    status: str = "",
) -> list[SupervisionResearchBriefModel]:
    """列出当前用户有权看到的研究定义。

    ``mine`` 只看自己的（含草稿），``shared`` 只看已共享的，
    ``all`` 是两者并集。别人的未共享草稿永远不出现。
    """
    project, _ = require_member(session, project_id, user_id)
    stmt = select(SupervisionResearchBriefModel).where(
        SupervisionResearchBriefModel.project_id == project.id,
        SupervisionResearchBriefModel.deleted_at.is_(None),
    )
    if scope == "mine":
        stmt = stmt.where(SupervisionResearchBriefModel.owner_user_id == user_id)
    elif scope == "shared":
        stmt = stmt.where(SupervisionResearchBriefModel.shared_version.is_not(None))
    else:
        stmt = stmt.where(
            (SupervisionResearchBriefModel.owner_user_id == user_id)
            | (SupervisionResearchBriefModel.shared_version.is_not(None))
        )
    if status:
        stmt = stmt.where(SupervisionResearchBriefModel.status == status)
    stmt = stmt.order_by(SupervisionResearchBriefModel.update_time.desc())
    return list(session.execute(stmt).scalars().all())


def list_shared_briefs(
    session: Session, project_id: str, user_id: str
) -> list[SupervisionResearchBriefModel]:
    """项目共享区：只有已确认的结构化版本，不含来源消息。"""
    project, _ = require_member(session, project_id, user_id)
    return _shared_brief_rows(session, project.id)


def get_brief(
    session: Session, project_id: str, user_id: str, brief_ref: str
) -> SupervisionResearchBriefModel:
    project, _ = require_member(session, project_id, user_id)
    return _get_brief(session, project, user_id, brief_ref)


def list_brief_versions(
    session: Session, project_id: str, user_id: str, brief_ref: str
) -> list[SupervisionResearchBriefVersionModel]:
    """版本历史只对所有者开放；别人的版本历史一律 404。"""
    project, _ = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    return list(
        session.execute(
            select(SupervisionResearchBriefVersionModel)
            .where(SupervisionResearchBriefVersionModel.brief_id == brief.id)
            .order_by(SupervisionResearchBriefVersionModel.version)
        )
        .scalars()
        .all()
    )


def compare_brief_versions(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    from_version: int,
    to_version: int,
) -> dict:
    """两个版本的逐字段结构化差异（§16.3 第4条）。"""
    project, _ = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    try:
        from_no = int(from_version)
        to_no = int(to_version)
    except (TypeError, ValueError):
        raise WorkValidationError("版本号必须是整数")
    old = _get_brief_version(session, brief.id, from_no)
    new = _get_brief_version(session, brief.id, to_no)
    return {
        "brief_id": brief.id,
        "code": brief.code,
        "from_version": from_no,
        "to_version": to_no,
        "from_status": old.status,
        "to_status": new.status,
        "from_confirmed_at": old.confirmed_at,
        "to_confirmed_at": new.confirmed_at,
        "changes": diff_brief_content(_content_of(old), _content_of(new)),
    }


def get_brief_source_messages(
    session: Session,
    project_id: str,
    user_id: str,
    brief_ref: str,
    *,
    limit: int = 200,
) -> list[MessageModel]:
    """打开研究定义的来源消息——只对提取人本人开放。

    另一角色即使能读共享的结构化版本，也读不到来源原始消息（§16.3
    第5条、场景 0-C 第5步）：这里按所有者过滤，别人的研究定义一律
    ``LookupError``。
    """
    project, _ = require_member(session, project_id, user_id)
    brief = _get_brief(session, project, user_id, brief_ref, owner_only=True)
    if not brief.source_conversation_id:
        raise WorkError("这份研究定义没有记录来源会话")
    conversation = _owned_dialogue(session, project, user_id, brief.source_conversation_id)
    wanted = None
    versions = session.execute(
        select(SupervisionResearchBriefVersionModel.id, SupervisionResearchBriefVersionModel.source_message_ids)
        .where(SupervisionResearchBriefVersionModel.brief_id == brief.id)
    ).all()
    ids = [x for row in versions for x in (row[1] or [])]
    if ids:
        wanted = ids
    return _dialogue_messages(session, conversation, message_ids=wanted, limit=limit)


# ---------------------------------------------------------------------------
# 上下文装配
# ---------------------------------------------------------------------------


def build_project_context(session: Session, project_id: str, user_id: str) -> str:
    """返回要注入模型上下文的项目共享内容；没有内容时返回空串。

    边界（§9.3、§16.3 第3、6条）：

    - 只读**已确认共享**的结构化研究定义版本，绝不读另一角色的原始
      消息、会话摘要、私人草稿或记忆候选；
    - 输出被明确标注为"项目范围信息"，并要求调用方不要把它写进个人
      长期记忆/偏好；本函数自己也不写任何记忆表；
    - 当前用户自己的私人聊天由聊天主链路按 ``owner_id`` 自己带，这里
      只补项目共享部分，因此同一份内容对学生和导师一致，但都不含对方
      的私人内容。

    非项目成员抛 ``PermissionError``；调用方（聊天主链路）应当捕获并
    直接不带项目上下文，而不是降级读别人内容。
    """
    project, _ = require_member(session, project_id, user_id)
    briefs = _shared_brief_rows(session, project.id)
    usable: list[tuple[SupervisionResearchBriefModel, SupervisionResearchBriefVersionModel]] = []
    for brief in briefs:
        try:
            version = _get_brief_version(session, brief.id, int(brief.shared_version))
        except LookupError:
            continue
        if version.status != "confirmed":
            continue
        usable.append((brief, version))
    if not usable:
        return ""

    lines = [
        f"【项目共享研究内容 · {project.code} {project.name}】",
        "以下内容来自本科研协作项目中已确认的结构化研究定义，属于项目范围信息：",
        "- 它不是任何用户的个人偏好、个人画像或长期记忆，不得写入个人记忆表，"
        "也不得带到本项目之外；",
        "- 已确认只表示它被作为当前研究的工作定义，不代表已被论文证据证实；",
        "- 另一角色的私人聊天、私人草稿和会话摘要不在其中，也不得从这些内容推断。",
    ]
    for brief, version in usable:
        kind_label = "导师建议" if brief.kind == KIND_MENTOR_GUIDANCE else "学生研究定义"
        confirmed_at = version.confirmed_at.isoformat() if version.confirmed_at else ""
        lines.append("")
        lines.append(f"[{brief.code} v{version.version} · {kind_label} · 确认于 {confirmed_at}]")
        if brief.title:
            lines.append(f"- 标题：{brief.title}")
        for name in BRIEF_CONTENT_FIELDS:
            value = getattr(version, name)
            if name in BRIEF_LIST_FIELDS:
                if value:
                    lines.append(f"- {BRIEF_FIELD_LABELS[name]}：{'、'.join(value)}")
                continue
            if value:
                lines.append(f"- {BRIEF_FIELD_LABELS[name]}：{value}")
        if version.confirm_note:
            lines.append(f"- 确认说明：{version.confirm_note}")
    return "\n".join(lines).strip()


__all__ = [
    "KIND_MENTOR_GUIDANCE",
    "KIND_RESEARCH_BRIEF",
    "MAX_EXTRACTION_MESSAGES",
    "MAX_FIELD_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_LIST_ITEM_CHARS",
    "MAX_QUERY_CHARS",
    "MAX_RESPONSE_CHARS",
    "DISCUSSION_HISTORY_LIMIT",
    "append_dialogue_message",
    "brief_payload",
    "build_discussion_prompt",
    "build_extraction_prompt",
    "build_project_context",
    "compare_brief_versions",
    "confirm_brief",
    "count_dialogue_messages",
    "create_brief_draft",
    "create_dialogue",
    "delete_brief",
    "dialogue_code",
    "diff_brief_content",
    "extract_brief_draft",
    "generate_dialogue_reply",
    "get_brief",
    "get_brief_source_messages",
    "get_dialogue_messages",
    "list_brief_versions",
    "list_briefs",
    "list_dialogues",
    "list_shared_briefs",
    "parse_brief_payload",
    "reject_brief",
    "update_brief",
    "visible_version_number",
]
