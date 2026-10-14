"""会话私有上下文（智能聊天记忆第二期）。

一个会话一份"便签"：主题、目标、已确认事项、限制条件、待解决问题，加上
较早历史的压缩摘要。它只帮本会话连续进行，不是长期事实库，也不跨会话
（设计方案 §3、§2.2）。

关键取舍：

- **只从用户消息里提取结论**。模型回答和知识库文档片段不自动成为"已确认
  事项"（设计方案 P2-12 的同一立场）；模型说的内容进摘要，不算结论。
- **用户明确改过的结论不被自动更新静默覆盖**（设计方案 §4.2）。用户编辑
  写入 ``pinned_json``，并记下编辑发生在第几条消息之后；只有更靠后的用户
  消息才能覆盖它（设计方案 P2-13：最新消息要求英文 → 摘要随后更新）。
- **提取是确定性的规则**，不调用模型：可测、可解释、不会因为模型不可用
  而让会话上下文消失。摘要正文仍走可替换的 summarizer（默认基于规则）。
- 临时对话（``conversation.temporary``）不读不写会话上下文（设计方案 §4.4）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.repository import conversation_repository as conv_repo
from chatchat.server.db.repository import session_summary_repository as summary_repo
from chatchat.server.db.repository.message_repository import list_messages

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 注入段标题与免责说明（设计方案 §5：本会话新要求优先于摘要里的旧内容）
CONTEXT_HEADER = "【本会话上下文（仅当前会话，供参考）】"
CONTEXT_FOOTER = (
    "（以上只属于当前会话，不适用于其他会话；本会话中用户的新要求优先于这里的"
    "旧内容，若与最近的原话矛盾，以用户最新表达为准。）"
)
SUMMARY_LABEL = "较早内容摘要"

#: 槽位显示顺序与中文标签（与前端抽屉一致）
CONTEXT_SECTIONS = (
    ("topic", "当前主题"),
    ("goal", "当前目标"),
    ("confirmed", "已确认事项"),
    ("constraints", "限制条件"),
    ("open_questions", "待解决问题"),
)

#: 每个列表槽位最多保留的条数与单条长度（"少而准"）
MAX_ITEMS_PER_SECTION = 6
MAX_ITEM_CHARS = 200
#: 注入段总长度上限
MAX_CONTEXT_CHARS = 2200

# --- 提取规则（确定性、可测）---

_TOPIC_SHIFT_RE = re.compile(
    r"(换个话题|另外我们|接下来(我们|讨论|聊)|今天(想|要)?讨论|主题(是|换成)|聊聊)"
)
_GOAL_RE = re.compile(
    r"(我想|我要|我希望|我需要|我打算|帮我|目标(是|为)|目的(是|为)|请问|请教)"
)
#: 用户明确达成/给出的结论
_CONFIRMED_RE = re.compile(
    r"(确认|决定|采用|就用|选择|同意|定了|就这样|不考虑|不使用|不用|我们用|方案(是|为)|结论(是|为)|不做|不加)"
)
#: 本会话的临时约束（"本次/这个项目/暂时"这类限定在当前会话的说法）
_CONSTRAINT_RE = re.compile(
    r"(本次|这次|这轮|本会话|这个会话|当前项目|这个项目|暂时|只用|仅限|统一(用|使用)|必须|要求|限制|默认用)"
)
_QUESTION_START_RE = re.compile(r"^(如何|怎么|为什么|什么是|哪些|能否|是否|请问)")
_RESOLVED_RE = re.compile(
    r"(明白了|清楚了|知道了|解决了|可以了|搞定|没问题了|就这样|谢谢|辛苦了)"
)

#: 冲突判定用的"槽位"：同一个槽位的新说法覆盖旧说法
_SLOTS: tuple[tuple[str, re.Pattern], ...] = (
    ("language", re.compile(r"(中文|英文|英语|日文|日语|chinese|english)", re.IGNORECASE)),
    ("verbosity", re.compile(r"(简洁|精简|简短|详细|展开|长一点|短一点)", re.IGNORECASE)),
    ("routing", re.compile(r"(自动路由|意图路由|路由|自动选择)", re.IGNORECASE)),
    ("tools", re.compile(r"(工具|MCP|联网|搜索|检索)", re.IGNORECASE)),
    ("format", re.compile(r"(表格|列表|段落|markdown|代码|引用)", re.IGNORECASE)),
    ("language_only", re.compile(r"(只用|仅用|不要用)", re.IGNORECASE)),
)


def slot_of(text: str) -> str:
    """给一条内容判定冲突槽位；无法归类返回 "other"（不按槽位覆盖）。"""
    for name, pattern in _SLOTS:
        if pattern.search(text or ""):
            return name
    return "other"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ContextItem:
    """一条会话上下文条目（已确认事项 / 限制条件 / 待解决问题）。"""

    text: str
    slot: str = "other"
    message_index: int = -1
    pinned: bool = False

    def normalized(self) -> str:
        return re.sub(r"\s+", "", (self.text or "")).lower()


@dataclass
class SessionContext:
    """一个会话的当前上下文。列表槽位存 ``ContextItem``，读写时转成文本行。"""

    conversation_id: str = ""
    owner_id: str = ""
    temporary: bool = False
    topic: Optional[ContextItem] = None
    goal: Optional[ContextItem] = None
    confirmed: List[ContextItem] = field(default_factory=list)
    constraints: List[ContextItem] = field(default_factory=list)
    open_questions: List[ContextItem] = field(default_factory=list)
    summary: str = ""
    summarized_count: int = 0
    update_time: object = None

    def is_empty(self) -> bool:
        return not (
            self.topic or self.goal or self.confirmed or self.constraints
            or self.open_questions or (self.summary or "").strip()
        )


# ---------------------------------------------------------------------------
# 存储 <-> 结构
# ---------------------------------------------------------------------------

def _lines(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _join(items: Sequence[ContextItem]) -> str:
    return "\n".join((i.text or "").strip() for i in items if (i.text or "").strip())


def _pinned_payload(context: SessionContext) -> str:
    """把用户锁定条目序列化成 ``pinned_json``（含 field，便于回读）。"""
    payload = []
    for item in context.confirmed + context.constraints + context.open_questions:
        if item.pinned:
            payload.append(
                {
                    "text": item.text,
                    "slot": item.slot,
                    "message_index": item.message_index,
                    "field": _field_of(item),
                }
            )
    for item in (context.topic, context.goal):
        if item is not None and item.pinned:
            payload.append(
                {
                    "text": item.text,
                    "slot": item.slot,
                    "message_index": item.message_index,
                    "field": "topic" if item is context.topic else "goal",
                }
            )
    return json.dumps(payload, ensure_ascii=False)


_FIELD_BY_NAME = {name: name for name, _ in CONTEXT_SECTIONS}


def _field_of(item: ContextItem) -> str:
    return getattr(item, "_field", "confirmed")


def _attach_field(items: Sequence[ContextItem], name: str) -> List[ContextItem]:
    for it in items:
        it._field = name  # type: ignore[attr-defined]
    return list(items)


def load_context(
    session: Session, owner_id: str, conversation_id: str
) -> Optional[SessionContext]:
    """读取会话上下文（无会话/无记录返回带空槽位的对象；临时对话带标记）。"""
    if not owner_id or not conversation_id:
        return None
    row = summary_repo.get_session_summary(session, conversation_id, owner_id)
    temporary = conv_repo.conversation_is_temporary(session, conversation_id, owner_id)
    context = SessionContext(
        conversation_id=conversation_id,
        owner_id=owner_id,
        temporary=temporary,
        summary=(row.summary or "") if row else "",
        summarized_count=(row.summarized_count or 0) if row else 0,
        update_time=getattr(row, "update_time", None) if row else None,
    )
    if row is None:
        return context

    pinned = _load_pinned(row)
    pinned_by_text = {re.sub(r"\s+", "", (p["text"] or "")).lower(): p for p in pinned}

    def build(name: str, text: str) -> List[ContextItem]:
        out: List[ContextItem] = []
        for index, line in enumerate(_lines(text)):
            key = re.sub(r"\s+", "", line).lower()
            hit = pinned_by_text.get(key)
            out.append(
                ContextItem(
                    text=line,
                    slot=slot_of(line),
                    message_index=int(hit["message_index"]) if hit else index,
                    pinned=hit is not None,
                )
            )
        return _attach_field(out, name)

    context.topic = (build("topic", row.topic or "") or [None])[0]
    context.goal = (build("goal", row.goal or "") or [None])[0]
    context.confirmed = build("confirmed", row.confirmed or "")
    context.constraints = build("constraints", row.constraints or "")
    context.open_questions = build("open_questions", row.open_questions or "")
    return context


def _load_pinned(row) -> List[dict]:
    try:
        data = json.loads(getattr(row, "pinned_json", None) or "[]")
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and (d.get("text") or "").strip()]


# ---------------------------------------------------------------------------
# 确定性提取
# ---------------------------------------------------------------------------

def _clean(text: str, limit: int = MAX_ITEM_CHARS) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())[:limit]


def derive_context(
    messages: Sequence, pinned: Sequence[dict]
) -> dict:
    """从会话消息里确定性提取上下文槽位。

    只看用户消息（模型回答与检索到的文档不算结论）。返回
    ``{"topic": ContextItem|None, "goal": ..., "confirmed": [...], ...}``。
    """
    topic: Optional[ContextItem] = None
    goal: Optional[ContextItem] = None
    confirmed: List[ContextItem] = []
    constraints: List[ContextItem] = []
    questions: List[tuple[int, ContextItem]] = []
    resolved_after: dict[int, bool] = {}

    for index, message in enumerate(messages):
        text = _clean(getattr(message, "query", "") or "")
        if not text:
            continue
        if _TOPIC_SHIFT_RE.search(text) or (index == 0 and topic is None):
            topic = ContextItem(text=text[:200], slot="other", message_index=index)
        if _GOAL_RE.search(text):
            goal = ContextItem(text=text[:500], slot="other", message_index=index)
        if _CONSTRAINT_RE.search(text):
            constraints.append(
                ContextItem(text=text, slot=slot_of(text), message_index=index)
            )
        elif _CONFIRMED_RE.search(text):
            confirmed.append(
                ContextItem(text=text, slot=slot_of(text), message_index=index)
            )
        is_question = text.endswith(("?", "？")) or bool(_QUESTION_START_RE.search(text))
        if is_question:
            questions.append((index, ContextItem(text=text, slot="other", message_index=index)))
        if _RESOLVED_RE.search(text):
            for q_index, _ in questions:
                if q_index < index:
                    resolved_after[q_index] = True

    open_questions = [item for q_index, item in questions if not resolved_after.get(q_index)]

    # 用户手工编辑/确认过的条目重新挂回去：它们必须继续存在（§4.2）
    for entry in pinned:
        name = entry.get("field") or "confirmed"
        item = ContextItem(
            text=_clean(str(entry.get("text") or "")),
            slot=str(entry.get("slot") or slot_of(str(entry.get("text") or ""))),
            message_index=int(entry.get("message_index") or 0),
            pinned=True,
        )
        if not item.text:
            continue
        if name == "topic":
            topic = item
        elif name == "goal":
            goal = item
        elif name == "constraints":
            constraints.append(item)
        elif name == "open_questions":
            open_questions.append(item)
        else:
            confirmed.append(item)

    return {
        "topic": topic,
        "goal": goal,
        "confirmed": confirmed,
        "constraints": constraints,
        "open_questions": open_questions,
    }


def _resolve(items: Sequence[ContextItem]) -> List[ContextItem]:
    """同槽位冲突时取最新说法（message_index 大者胜，相同则用户锁定条目胜）。

    这就是 P2-13 的落点：旧摘要写"使用中文"，本轮用户要求英文，下一轮自动
    更新时英文胜出并替换旧约束；而用户手工编辑过的结论（pinned）不会被更早
    的消息覆盖（§4.2）。
    """
    kept: List[ContextItem] = []
    for item in items:
        if not (item.text or "").strip():
            continue
        replaced = False
        if item.slot != "other":
            for i, old in enumerate(kept):
                if old.slot != item.slot:
                    continue
                if item.message_index > old.message_index or (
                    item.message_index == old.message_index and item.pinned and not old.pinned
                ):
                    kept[i] = item
                    replaced = True
                    break
                else:
                    # 旧条目更新（或同为 pinned）：新条目不覆盖它
                    replaced = True
                    break
            if replaced:
                continue
        # 文本完全相同：保留后出现的（更新时间更近）
        for i, old in enumerate(kept):
            if old.normalized() == item.normalized():
                kept[i] = item
                replaced = True
                break
        if not replaced:
            kept.append(item)
    # 用户锁定条目排前面，其余按出现顺序
    kept.sort(key=lambda it: (0 if it.pinned else 1))
    return kept[:MAX_ITEMS_PER_SECTION]


def merge_into_row(
    session: Session,
    owner_id: str,
    conversation_id: str,
    derived: dict,
    summary_text: str,
    summarized_message_id: Optional[str],
    summarized_count: int,
    existing: SessionContext,
) -> SessionContext:
    """把提取结果与既有上下文合并后落库，返回最新上下文。"""
    merged = SessionContext(
        conversation_id=conversation_id,
        owner_id=owner_id,
        temporary=existing.temporary,
        topic=_pick(existing.topic, derived.get("topic")),
        goal=_pick(existing.goal, derived.get("goal")),
        confirmed=_resolve(existing.confirmed + list(derived.get("confirmed") or [])),
        constraints=_resolve(existing.constraints + list(derived.get("constraints") or [])),
        open_questions=_resolve(
            existing.open_questions + list(derived.get("open_questions") or [])
        ),
        summary=summary_text,
        summarized_count=summarized_count,
    )
    merged.confirmed = _attach_field(merged.confirmed, "confirmed")
    merged.constraints = _attach_field(merged.constraints, "constraints")
    merged.open_questions = _attach_field(merged.open_questions, "open_questions")
    summary_repo.upsert_session_summary(
        session,
        owner_id=owner_id,
        conversation_id=conversation_id,
        summary=summary_text,
        summarized_message_id=summarized_message_id,
        summarized_count=summarized_count,
        context={
            "topic": (merged.topic.text if merged.topic else ""),
            "goal": (merged.goal.text if merged.goal else ""),
            "confirmed": _join(merged.confirmed),
            "constraints": _join(merged.constraints),
            "open_questions": _join(merged.open_questions),
        },
        pinned_json=_pinned_payload(merged),
    )
    return merged


def _pick(old: Optional[ContextItem], new: Optional[ContextItem]) -> Optional[ContextItem]:
    """单值槽位：新的更新就替换；用户锁定的旧值不被更早的内容替换。"""
    if old is None:
        return new
    if new is None:
        return old
    if new.pinned and not old.pinned:
        return new
    if old.pinned and new.message_index <= old.message_index:
        return old
    return new


# ---------------------------------------------------------------------------
# 更新入口
# ---------------------------------------------------------------------------

def refresh_session_context(
    session: Session,
    owner_id: str,
    conversation_id: str,
    threshold: int = 12,
    force: bool = False,
    summarizer=None,
) -> Optional[SessionContext]:
    """更新会话上下文（自动摘要触发或用户点"更新摘要"）。

    - 临时对话直接返回 None，不写入（设计方案 §4.4）；
    - ``force=True`` 忽略"历史超过阈值"的条件（手动更新）；
    - 摘要正文仍只压缩"超出窗口且尚未摘要"的较早消息，最近的关键消息保持
      完整原文参与回答（设计方案 §4.1）；
    - 失败返回 None，调用方不阻断聊天。
    """
    if not owner_id or not conversation_id:
        return None
    try:
        if conv_repo.conversation_is_temporary(session, conversation_id, owner_id):
            return None
        messages = list_messages(session, conversation_id, owner_id)
        existing = load_context(session, owner_id, conversation_id) or SessionContext(
            conversation_id=conversation_id, owner_id=owner_id
        )
        n = len(messages)
        recent_cut = max(0, n - threshold)
        row = summary_repo.get_session_summary(session, conversation_id, owner_id)
        covered_id = row.summarized_message_id if row is not None else None
        if not force and n <= threshold:
            # 历史还不够长：仍然刷新结构化槽位（用户刚确认的结论应当立刻可见），
            # 但不生成摘要正文，也不动已记录的摘要覆盖位置。
            derived = derive_context(messages, _pinned_rows(session, owner_id, conversation_id))
            return merge_into_row(
                session, owner_id, conversation_id, derived,
                existing.summary, covered_id, existing.summarized_count, existing,
            )

        covered_idx = -1
        if covered_id:
            for i, m in enumerate(messages):
                if m.id == covered_id:
                    covered_idx = i
                    break
        to_summarize = messages[covered_idx + 1: recent_cut]
        summary_text = existing.summary
        marker_id = covered_id
        count = existing.summarized_count
        if to_summarize:
            from chatchat.server.memory.memory_service import rule_based_summarizer

            fn = summarizer or rule_based_summarizer
            older_text = "\n".join(
                f"用户: {(m.query or '').strip()}"
                + (f"\n助手: {(m.response or '').strip()}" if (m.response or "").strip() else "")
                for m in to_summarize
            )
            summary_text = fn(existing.summary, older_text)[:4000]
            marker_id = to_summarize[-1].id
            count = recent_cut

        derived = derive_context(messages, _pinned_rows(session, owner_id, conversation_id))
        return merge_into_row(
            session, owner_id, conversation_id, derived,
            summary_text, marker_id, count, existing,
        )
    except Exception:
        return None


def _pinned_rows(session: Session, owner_id: str, conversation_id: str) -> List[dict]:
    row = summary_repo.get_session_summary(session, conversation_id, owner_id)
    return _load_pinned(row) if row is not None else []


def apply_user_edit(
    session: Session,
    owner_id: str,
    conversation_id: str,
    *,
    topic: Optional[str] = None,
    goal: Optional[str] = None,
    confirmed: Optional[Sequence[str]] = None,
    constraints: Optional[Sequence[str]] = None,
    open_questions: Optional[Sequence[str]] = None,
    summary: Optional[str] = None,
) -> Optional[SessionContext]:
    """用户手工编辑会话上下文（设计方案 §4.2「编辑摘要」）。

    编辑过的条目按"用户明确修改的关键结论"处理：写入 ``pinned_json``，
    并把 message_index 记为当前最后一条消息的位置——之后的新消息可以覆盖
    它（P2-13），更早的消息不能（§4.2 不静默覆盖）。
    """
    if not owner_id or not conversation_id:
        return None
    if conv_repo.conversation_is_temporary(session, conversation_id, owner_id):
        return None
    try:
        messages = list_messages(session, conversation_id, owner_id)
    except Exception:
        messages = []
    last_index = max(0, len(messages) - 1)

    def items(values: Optional[Sequence[str]], name: str) -> Optional[List[ContextItem]]:
        if values is None:
            return None
        out = []
        for raw in values:
            text = _clean(str(raw or ""))
            if text:
                out.append(
                    ContextItem(
                        text=text, slot=slot_of(text), message_index=last_index, pinned=True
                    )
                )
        return _attach_field(out, name)

    context = load_context(session, owner_id, conversation_id) or SessionContext(
        conversation_id=conversation_id, owner_id=owner_id
    )
    if topic is not None:
        text = _clean(topic, 200)
        context.topic = (
            ContextItem(text=text, slot="other", message_index=last_index, pinned=True)
            if text
            else None
        )
    if goal is not None:
        text = _clean(goal, 500)
        context.goal = (
            ContextItem(text=text, slot="other", message_index=last_index, pinned=True)
            if text
            else None
        )
    for name, values in (
        ("confirmed", items(confirmed, "confirmed")),
        ("constraints", items(constraints, "constraints")),
        ("open_questions", items(open_questions, "open_questions")),
    ):
        if values is not None:
            setattr(context, name, values)
    if summary is not None:
        context.summary = (summary or "")[:4000]

    summary_repo.upsert_session_summary(
        session,
        owner_id=owner_id,
        conversation_id=conversation_id,
        summary=context.summary,
        summarized_message_id=None,
        summarized_count=context.summarized_count,
        context={
            "topic": (context.topic.text if context.topic else ""),
            "goal": (context.goal.text if context.goal else ""),
            "confirmed": _join(context.confirmed),
            "constraints": _join(context.constraints),
            "open_questions": _join(context.open_questions),
        },
        pinned_json=_pinned_payload(context),
    )
    return context


def add_session_confirmation(
    session: Session, owner_id: str, conversation_id: str, text: str
) -> Optional[SessionContext]:
    """"记住（仅本会话）"写入已确认事项。临时对话返回 None（设计方案 §4.4）。"""
    if not owner_id or not conversation_id:
        return None
    if conv_repo.conversation_is_temporary(session, conversation_id, owner_id):
        return None
    content = _clean(text)
    if not content:
        raise ValueError("content 不能为空")
    context = load_context(session, owner_id, conversation_id) or SessionContext(
        conversation_id=conversation_id, owner_id=owner_id
    )
    try:
        messages = list_messages(session, conversation_id, owner_id)
    except Exception:
        messages = []
    item = ContextItem(
        text=content,
        slot=slot_of(content),
        message_index=max(0, len(messages) - 1),
        pinned=True,
    )
    context.confirmed = _attach_field([item] + context.confirmed, "confirmed")
    summary_repo.update_session_context(
        session,
        owner_id=owner_id,
        conversation_id=conversation_id,
        context={"confirmed": _join(context.confirmed)},
        pinned_json=_pinned_payload(context),
    )
    return context


def forget_session_items(
    session: Session, owner_id: str, conversation_id: str, text: str
) -> int:
    """"忘记（仅本会话）"：从会话上下文里移除匹配条目，不动个人偏好。"""
    if not owner_id or not conversation_id:
        return 0
    context = load_context(session, owner_id, conversation_id)
    if context is None:
        return 0
    target = re.sub(r"\s+", "", (text or "")).lower()
    if not target:
        return 0

    def keep(items: List[ContextItem]) -> tuple[List[ContextItem], int]:
        out, removed = [], 0
        for it in items:
            norm = it.normalized()
            if target == norm or target in norm or norm in target:
                removed += 1
            else:
                out.append(it)
        return out, removed

    removed_total = 0
    for name in ("confirmed", "constraints", "open_questions"):
        items, removed = keep(getattr(context, name))
        if removed:
            setattr(context, name, _attach_field(items, name))
            removed_total += removed
    for name in ("topic", "goal"):
        item = getattr(context, name)
        if item is not None and (
            target == item.normalized()
            or target in item.normalized()
            or item.normalized() in target
        ):
            setattr(context, name, None)
            removed_total += 1
    if removed_total:
        summary_repo.update_session_context(
            session,
            owner_id=owner_id,
            conversation_id=conversation_id,
            context={
                "topic": (context.topic.text if context.topic else ""),
                "goal": (context.goal.text if context.goal else ""),
                "confirmed": _join(context.confirmed),
                "constraints": _join(context.constraints),
                "open_questions": _join(context.open_questions),
            },
            pinned_json=_pinned_payload(context),
        )
    return removed_total


def reset_session_context(
    session: Session, owner_id: str, conversation_id: str
) -> None:
    """重置本会话上下文（设计方案 P2-06）；个人偏好不受影响。"""
    summary_repo.reset_session_context(session, conversation_id, owner_id)


# ---------------------------------------------------------------------------
# 注入
# ---------------------------------------------------------------------------

def build_context_segment(
    session: Optional[Session], owner_id: Optional[str], conversation_id: Optional[str]
) -> str:
    """构造注入用的会话上下文段；无内容/临时对话返回空串。

    放在近期原始轮次之前，最近的关键消息仍以完整原文参与回答（§4.1）。
    """
    if session is None or not owner_id or not conversation_id:
        return ""
    try:
        context = load_context(session, owner_id, conversation_id)
    except Exception:
        return ""
    if context is None or context.temporary:
        return ""
    return format_context(context)


def format_context(context: SessionContext) -> str:
    """把上下文格式化成独立注入段（与界面抽屉显示的内容一致）。"""
    if context.is_empty():
        return ""
    lines: List[str] = []
    for name, label in CONTEXT_SECTIONS:
        if name in ("topic", "goal"):
            item = getattr(context, name)
            if item is not None and (item.text or "").strip():
                lines.append(f"{label}：{item.text}")
            continue
        items = getattr(context, name)
        if not items:
            continue
        lines.append(f"{label}：")
        for item in items:
            lines.append(f"- {item.text}")
    summary = (context.summary or "").strip()
    if summary:
        lines.append(f"{SUMMARY_LABEL}：")
        lines.append(summary[:MAX_CONTEXT_CHARS])
    body = "\n".join(lines)
    if len(body) > MAX_CONTEXT_CHARS:
        body = body[:MAX_CONTEXT_CHARS]
    return f"{CONTEXT_HEADER}\n{body}\n{CONTEXT_FOOTER}"


__all__ = [
    "CONTEXT_HEADER",
    "CONTEXT_FOOTER",
    "CONTEXT_SECTIONS",
    "MAX_ITEMS_PER_SECTION",
    "ContextItem",
    "SessionContext",
    "slot_of",
    "load_context",
    "derive_context",
    "refresh_session_context",
    "apply_user_edit",
    "add_session_confirmation",
    "forget_session_items",
    "reset_session_context",
    "build_context_segment",
    "format_context",
]
