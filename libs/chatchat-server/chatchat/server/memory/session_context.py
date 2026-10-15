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
- **提取与摘要都是确定性的规则**，不调用模型：可测、可解释，不会因为模型
  不可用而让会话上下文消失（设计方案 §7.2 的"规则摘要"）。
- **摘要覆盖范围与最近原文窗口无缝衔接**（设计方案 §6.2）：分界由
  ``context_package`` 按字符预算算出并传进来，本模块只摘要"分界之前、
  游标之后"的消息，不允许出现既没进摘要也不在最近原文的中间消息。
- **摘要正文是结构化压缩，不是原文拼接**（设计方案 §4.2/§4.3）：按角色
  分行、去重、同槽位冲突替换，超过容量时丢最旧行而不是截断最新内容。
- **槽位提取按游标增量进行**（设计方案 §4.9）：每轮只扫描新增消息，长会话
  不再平方级重扫。
- 临时对话（``conversation.temporary``）不读不写会话上下文（设计方案 §4.4）。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
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
    ("completed_items", "已完成事项"),
)

#: 每个列表槽位最多保留的条数与单条长度（"少而准"）
MAX_ITEMS_PER_SECTION = 6
MAX_ITEM_CHARS = 200
#: 注入段总长度上限
MAX_CONTEXT_CHARS = 2200

#: 摘要正文容量（设计方案 §4.3：超过容量丢最旧行，不再"追加后截前 4000 字"）
SUMMARY_MAX_CHARS = 4000
#: 摘要单行长度；用户原话与助手回答分别成行
SUMMARY_USER_LINE_CHARS = 200
SUMMARY_ASSISTANT_LINE_CHARS = 160

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
#: 已经完成的事项（设计方案 §7.1 的 completed_items）
_COMPLETED_RE = re.compile(
    r"(完成了|已完成|做完了|已经做完|已解决|已经解决|已经实现|已经部署|已经上线|"
    r"测试通过|跑通了|验证通过|已经写好|已写好|写好了|改好了|调好了|配好了|"
    r"搭好了|部署好了|上线了|弄好了|做好了|搞定)"
)
#: 否定/替换此前说法（设计方案 §7.2：识别确认、否定、替换、完成和重新打开）
_NEGATION_RE = re.compile(
    r"(不对|不是这样|搞错|弄错|换用|换成|改用|改成|放弃|推翻|重新选择|不再用|"
    r"不考虑之前|之前说的不算|换个方案|换一套)"
)
#: 重新打开已完成或被搁置的事项
_REOPEN_RE = re.compile(
    r"(重新打开|还没解决|没解决|还没做完|没做完|未完成|重新考虑|回到之前|回到刚才|"
    r"还要继续|还需要处理|重新启用)"
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


#: 主题词匹配用：英文/数字词 + 中文二字片段（与召回侧同一取向）
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]+")


def _tokens(text: str) -> set:
    """把一条内容切成可比较的主题词；中文按二字片段参与匹配。"""
    out = set()
    for piece in _TOKEN_RE.findall(re.sub(r"\s+", "", (text or "")).lower()):
        if piece.isascii():
            if len(piece) >= 2:
                out.add(piece)
        else:
            for i in range(len(piece) - 1):
                out.add(piece[i : i + 2])
    return out


def _overlaps(a: str, b: str, *, min_shared: int = 2) -> bool:
    """两条说法是否指向同一件事（至少 ``min_shared`` 个主题词相同）。

    否定/重新打开类消息没有明确槽位，只能靠主题词找回它指的是哪条旧结论。
    默认要求两个词相同，避免一句泛泛的"不对"把无关结论抹掉；否定句已经带
    着"改用/换用/不对"这类明确指向，允许只共享一个主题词（"改用 PostgreSQL
    存储"要能顶掉"采用 MySQL 存储"）。
    """
    left, right = _tokens(a), _tokens(b)
    return bool(left) and bool(right) and len(left & right) >= min_shared


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ContextItem:
    """一条会话上下文条目（已确认事项 / 限制条件 / 待解决问题 / 已完成事项）。"""

    text: str
    slot: str = "other"
    message_index: int = -1
    pinned: bool = False
    #: 条目来源角色：user（用户原话）/ assistant（模型说法，只进摘要）
    source_role: str = "user"

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
    completed_items: List[ContextItem] = field(default_factory=list)
    summary: str = ""
    summarized_count: int = 0
    update_time: object = None
    # --- 第四期第一阶段 A（设计方案 §7.1/§7.5/§7.7、§15.1）---
    #: 摘要覆盖到哪条消息；与最近原文窗口的分界必须严丝合缝（§6.2）
    summarized_message_id: Optional[str] = None
    #: 重置起点：只从这条消息之后重新建立上下文（§7.7）
    context_start_after_message_id: Optional[str] = None
    #: 乐观锁版本（§7.5）
    version: int = 0
    #: 摘要来源与执行状态（§7.2：规则摘要 / 模型摘要必须可区分）
    summary_type: str = "rule"
    summary_status: str = "idle"
    summary_error: Optional[str] = None
    summary_updated_at: object = None
    #: 旧摘要文本存在但覆盖位置不可确认（v16 判据），下次进入时安全重建
    needs_rebuild: bool = False
    #: 槽位增量游标（§4.9）
    slots_scanned_through_message_id: Optional[str] = None
    slots_scanned_count: int = 0
    #: 最近原文窗口被字符预算截断（§6.2：注入层如实标记，不静默丢内容）
    window_truncated: bool = False

    def is_empty(self) -> bool:
        return not (
            self.topic or self.goal or self.confirmed or self.constraints
            or self.open_questions or self.completed_items
            or (self.summary or "").strip()
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
    for item in (
        context.confirmed
        + context.constraints
        + context.open_questions
        + context.completed_items
    ):
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
        summarized_message_id=getattr(row, "summarized_message_id", None) if row else None,
        context_start_after_message_id=(
            getattr(row, "context_start_after_message_id", None) if row else None
        ),
        version=int(getattr(row, "version", 0) or 0) if row else 0,
        summary_type=(getattr(row, "summary_type", None) or "rule") if row else "rule",
        summary_status=(getattr(row, "summary_status", None) or "idle") if row else "idle",
        summary_error=getattr(row, "summary_error", None) if row else None,
        summary_updated_at=getattr(row, "summary_updated_at", None) if row else None,
        needs_rebuild=bool(getattr(row, "needs_rebuild", False)) if row else False,
        slots_scanned_through_message_id=(
            getattr(row, "slots_scanned_through_message_id", None) if row else None
        ),
        slots_scanned_count=int(getattr(row, "slots_scanned_count", 0) or 0) if row else 0,
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
    context.completed_items = build(
        "completed_items", getattr(row, "completed_items", "") or ""
    )
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
    messages: Sequence,
    pinned: Sequence[dict],
    *,
    start_index: int = 0,
    existing: Optional[SessionContext] = None,
) -> dict:
    """从会话消息里确定性提取上下文槽位（增量）。

    只看用户消息（模型回答与检索到的文档不算结论）。

    - ``start_index`` 之前的消息已经扫描过，不再重扫（设计方案 §4.9 / M-…成本
      用例）：长会话每轮只处理新增消息，不再平方级重扫。``messages`` 仍是升序
      全量列表，因此条目的 ``message_index`` 跨轮稳定，冲突判定不受影响。
    - ``existing`` 是当前已存的上下文。增量扫描后，"明白了"、否定和重新打开
      这类消息必须还能作用在早先扫描出来的条目上，否则游标一推进它们就失效。

    返回除各槽位外，还有三个**动作列表**，由 ``merge_into_row`` 负责落到旧条目上：

    - ``negated``：被本轮否定/替换掉的旧条目文本（归一化后）；
    - ``resolved``：被"明白了/解决了"消解掉的待解决问题文本；
    - ``reopened``：被重新打开的已完成事项条目本身。
    """
    existing = existing or SessionContext()
    topic: Optional[ContextItem] = None
    goal: Optional[ContextItem] = None
    confirmed: List[ContextItem] = []
    constraints: List[ContextItem] = []
    completed: List[ContextItem] = []
    questions: List[tuple[int, ContextItem]] = []
    resolved_after: dict[int, bool] = {}
    resolved: List[str] = []
    negated: List[str] = []
    reopened: List[ContextItem] = []

    for index, message in enumerate(messages):
        if index < start_index:
            continue
        text = _clean(getattr(message, "query", "") or "")
        if not text:
            continue
        if _TOPIC_SHIFT_RE.search(text) or (index == 0 and topic is None):
            topic = ContextItem(text=text[:200], slot="other", message_index=index)
        if _GOAL_RE.search(text):
            goal = ContextItem(text=text[:500], slot="other", message_index=index)
        # 完成/重新打开先于确认判定：同一句"完成了 X"既是结论也是完成状态
        if _COMPLETED_RE.search(text):
            completed.append(
                ContextItem(text=text, slot=slot_of(text), message_index=index)
            )
        if _REOPEN_RE.search(text):
            for item in existing.completed_items + completed:
                if _overlaps(text, item.text):
                    reopened.append(item)
        if _NEGATION_RE.search(text):
            # 否定/替换：把更早的同一件事从"已确认/约束"里摘掉，只保留新说法。
            # 这句本身也是新结论（"改用 PostgreSQL"），否则替换后槽位会空掉。
            for item in existing.confirmed + existing.constraints + confirmed + constraints:
                if item.message_index < index and _overlaps(text, item.text, min_shared=1):
                    negated.append(item.normalized())
        if _CONSTRAINT_RE.search(text):
            constraints.append(
                ContextItem(text=text, slot=slot_of(text), message_index=index)
            )
        elif _CONFIRMED_RE.search(text) or _NEGATION_RE.search(text):
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
            for item in existing.open_questions:
                if item.message_index < index:
                    resolved.append(item.normalized())

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
        elif name == "completed_items":
            completed.append(item)
        else:
            confirmed.append(item)

    return {
        "topic": topic,
        "goal": goal,
        "confirmed": confirmed,
        "constraints": constraints,
        "open_questions": open_questions,
        "completed_items": completed,
        "negated": negated,
        "resolved": resolved,
        "reopened": reopened,
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


def _drop(items: Sequence[ContextItem], texts: Sequence[str]) -> List[ContextItem]:
    """按归一化文本移除条目（否定/消解动作落到早先扫描出来的条目上）。"""
    bad = {t for t in texts if t}
    if not bad:
        return list(items)
    return [it for it in items if it.normalized() not in bad]


def merge_into_row(
    session: Session,
    owner_id: str,
    conversation_id: str,
    derived: dict,
    summary_text: str,
    summarized_message_id: Optional[str],
    summarized_count: int,
    existing: SessionContext,
    *,
    summary_type: Optional[str] = None,
    summary_status: Optional[str] = None,
    summary_error: Optional[str] = None,
    summary_updated_at=None,
    needs_rebuild: Optional[bool] = None,
    slots_scanned_through_message_id: Optional[str] = None,
    slots_scanned_count: Optional[int] = None,
    expected_version: Optional[int] = None,
) -> SessionContext:
    """把提取结果与既有上下文合并后落库，返回最新上下文。

    摘要正文、覆盖位置与版本号经 ``upsert_session_summary`` 一次原子写入；
    ``expected_version`` 不匹配时抛 ``ContextVersionConflict``，本次不写任何
    字段（设计方案 §7.5 / M-49）。
    """
    reopened = list(derived.get("reopened") or [])
    reopened_texts = {it.normalized() for it in reopened}
    moved_back = [
        ContextItem(text=it.text, slot=it.slot, message_index=it.message_index, pinned=it.pinned)
        for it in reopened
    ]
    merged = SessionContext(
        conversation_id=conversation_id,
        owner_id=owner_id,
        temporary=existing.temporary,
        topic=_pick(existing.topic, derived.get("topic")),
        goal=_pick(existing.goal, derived.get("goal")),
        confirmed=_resolve(
            _drop(existing.confirmed + list(derived.get("confirmed") or []),
                  derived.get("negated") or [])
        ),
        constraints=_resolve(
            _drop(existing.constraints + list(derived.get("constraints") or []),
                  derived.get("negated") or [])
        ),
        open_questions=_resolve(
            _drop(existing.open_questions + list(derived.get("open_questions") or []),
                  derived.get("resolved") or [])
            + moved_back
        ),
        completed_items=_resolve(
            _drop(existing.completed_items + list(derived.get("completed_items") or []),
                  reopened_texts)
        ),
        summary=summary_text,
        summarized_count=summarized_count,
        summarized_message_id=summarized_message_id or existing.summarized_message_id,
        context_start_after_message_id=existing.context_start_after_message_id,
        version=existing.version,
        summary_type=summary_type or existing.summary_type,
        summary_status=summary_status or existing.summary_status,
        summary_error=summary_error if summary_error is not None else existing.summary_error,
        summary_updated_at=summary_updated_at or existing.summary_updated_at,
        needs_rebuild=existing.needs_rebuild if needs_rebuild is None else needs_rebuild,
        slots_scanned_through_message_id=(
            slots_scanned_through_message_id or existing.slots_scanned_through_message_id
        ),
        slots_scanned_count=(
            existing.slots_scanned_count
            if slots_scanned_count is None
            else slots_scanned_count
        ),
    )
    merged.confirmed = _attach_field(merged.confirmed, "confirmed")
    merged.constraints = _attach_field(merged.constraints, "constraints")
    merged.open_questions = _attach_field(merged.open_questions, "open_questions")
    merged.completed_items = _attach_field(merged.completed_items, "completed_items")
    row = summary_repo.upsert_session_summary(
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
            "completed_items": _join(merged.completed_items),
        },
        pinned_json=_pinned_payload(merged),
        summary_type=summary_type,
        summary_status=summary_status,
        summary_error=summary_error,
        summary_updated_at=summary_updated_at,
        needs_rebuild=needs_rebuild,
        slots_scanned_through_message_id=slots_scanned_through_message_id,
        slots_scanned_count=slots_scanned_count,
        expected_version=expected_version,
    )
    merged.version = int(getattr(row, "version", merged.version + 1) or 0)
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
# 最近原文窗口与摘要覆盖的分界（设计方案 §6.2）
# ---------------------------------------------------------------------------

def recent_history_budget() -> tuple:
    """过渡期最近原文窗口的字符预算与最小轮数（设计方案 §6.2）。

    第一阶段没有模型 Token 配置表，先用可配置的字符上限兜住 Prompt 大小；
    第四阶段由真实 Token 预算替代。默认 6000 字符是保守值，不是任何模型的
    精确容量，管理员可调低。
    """
    try:
        max_chars = int(os.getenv("CHATCHAT_RECENT_HISTORY_MAX_CHARS", "6000"))
    except ValueError:
        max_chars = 6000
    try:
        min_turns = int(os.getenv("CHATCHAT_RECENT_HISTORY_MIN_TURNS", "3"))
    except ValueError:
        min_turns = 3
    return max(500, max_chars), max(1, min_turns)


def message_cost(message) -> int:
    """一条消息进 Prompt 的大致字符开销（用户问题 + 助手回答）。

    既接受 ``MessageModel`` 记录，也接受遗留 ``filter_message`` 返回的字典，
    便于三条聊天链路逐步切到同一份数据。
    """
    if isinstance(message, Mapping):
        query = message.get("query") or ""
        response = message.get("response") or ""
    else:
        query = getattr(message, "query", "") or ""
        response = getattr(message, "response", "") or ""
    return len(query) + len(response) + 8


@dataclass
class RecentWindow:
    """最近原文窗口的计算结果（设计方案 §6.2 的构建规则）。"""

    start_index: int = 0
    chars: int = 0
    truncated: bool = False
    gap_due_to_degradation: bool = False


def select_recent_window(
    messages: Sequence,
    *,
    max_chars: Optional[int] = None,
    min_turns: Optional[int] = None,
) -> RecentWindow:
    """按字符预算从最新一轮向前选择最近原文窗口，返回窗口起点。

    起点之前的消息必须已经全部被摘要覆盖——这是"无空洞"不变量的唯一分界点，
    普通聊天、知识库对话和 Agent 都用它（设计方案 §6.2 / §18.2.9）。

    - 累计不超过 ``max_chars``，且不拆散完整问答轮（一条消息就是一轮）；
    - 字符硬上限优先于最小轮数：放不下就不放，不能为了凑轮数溢出上下文；
    - 连最新一轮都放不下时仍然保留它，由注入层截断尾部并标记 ``truncated``
      （设计方案 §6.2 第 5 条）。
    """
    budget, floor_turns = recent_history_budget()
    if max_chars is not None:
        budget = max(500, int(max_chars))
    if min_turns is not None:
        floor_turns = max(1, int(min_turns))
    n = len(messages)
    if n == 0:
        return RecentWindow(start_index=0, chars=0)

    start = n
    total = 0
    turns = 0
    for i in range(n - 1, -1, -1):
        cost = message_cost(messages[i])
        if total + cost > budget:
            break
        total += cost
        turns += 1
        start = i

    truncated = False
    if start == n:
        # 单轮就超预算：保留最新一轮（注入层负责截断），不能整段丢掉
        start = n - 1
        total = message_cost(messages[start])
        turns = 1
        truncated = True
    elif turns < floor_turns:
        # 最小轮数让位于字符硬上限：这是过渡期已知且如实标记的取舍
        truncated = True
    return RecentWindow(start_index=start, chars=total, truncated=truncated)


# ---------------------------------------------------------------------------
# 确定性结构化摘要（设计方案 §7.2：不占用模型队列）
# ---------------------------------------------------------------------------

def digest_lines(messages: Sequence) -> List[str]:
    """把待摘要消息转成按角色分行的摘要行。

    用户原话与助手回答分别成行：助手的说法进摘要但不会被当成"用户已确认
    事项"（设计方案 §7.3）。
    """
    lines: List[str] = []
    for message in messages:
        query = _clean(getattr(message, "query", "") or "", SUMMARY_USER_LINE_CHARS)
        response = _clean(
            getattr(message, "response", "") or "", SUMMARY_ASSISTANT_LINE_CHARS
        )
        if query:
            lines.append(f"用户：{query}")
        if response:
            lines.append(f"助手：{response}")
    return lines


def merge_digest(
    existing: str, new_lines: Sequence[str], *, max_chars: int = SUMMARY_MAX_CHARS
) -> str:
    """把新摘要行并入既有摘要：去重、同槽位冲突替换、超容量丢最旧行。

    旧实现是"旧摘要 + 新内容"再截前 4000 字，摘要一旦写满就再也吸收不了新
    内容（设计方案 §4.3）。这里反过来：容量不足时从最旧的行开始丢，最新有效
    结论始终保留（§7.2）。
    """
    lines = _lines(existing)
    for raw in new_lines:
        line = (raw or "").strip()
        if not line:
            continue
        norm = re.sub(r"\s+", "", line).lower()
        slot = slot_of(line)
        if slot != "other":
            # 同一件事的新说法替换旧说法（"改用 PostgreSQL" 顶掉 "采用 MySQL"）
            lines = [
                old
                for old in lines
                if slot_of(old) != slot
                or re.sub(r"\s+", "", old).lower() == norm
            ]
        lines = [old for old in lines if re.sub(r"\s+", "", old).lower() != norm]
        lines.append(line)

    total = sum(len(line) + 1 for line in lines)
    while len(lines) > 1 and total > max_chars:
        dropped = lines.pop(0)
        total -= len(dropped) + 1
    if total > max_chars:
        return lines[-1][:max_chars]
    return "\n".join(lines)


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
    *,
    boundary_message_id: Optional[str] = None,
    messages: Optional[Sequence] = None,
) -> Optional[SessionContext]:
    """更新会话上下文（每轮构建 Prompt 前，或用户点"更新摘要"）。

    - 临时对话直接返回 None，不写入（设计方案 §4.4）；
    - 分界由 ``boundary_message_id`` 给出：统一上下文构建器算出的最近原文
      窗口第一条消息的 ID，该消息**及其之后**都留在窗口内，之前的必须进摘要。
      用消息 ID 而不是下标，是因为摘要扫描看全部消息、窗口只看有回答的消息，
      两者下标不同（设计方案 §6.2 / §18.2.9）。没给就按字符预算自己算；
    - 只摘要"游标之后、分界之前"的消息；摘要失败时**不推进游标**，也不覆盖
      旧摘要（设计方案 §7.5），窗口侧标记降级；
    - 槽位提取按 ``slots_scanned_count`` 增量扫描（设计方案 §4.9）；
    - 摘要正文、覆盖位置与版本号一次原子写入，版本冲突时重读重试一次
      （设计方案 §7.5 / M-49）；
    - 失败返回 None，调用方不阻断聊天。
    """
    if not owner_id or not conversation_id:
        return None
    try:
        if conv_repo.conversation_is_temporary(session, conversation_id, owner_id):
            return None
        rows = list(messages) if messages is not None else list_messages(
            session, conversation_id, owner_id
        )
        return _refresh_once(
            session,
            owner_id,
            conversation_id,
            rows,
            threshold=threshold,
            force=force,
            summarizer=summarizer,
            boundary_message_id=boundary_message_id,
            attempt=0,
        )
    except summary_repo.ContextVersionConflict:
        # 并发更新同一会话：重读最新版本再试一次，仍冲突就交给下一轮
        try:
            rows = list_messages(session, conversation_id, owner_id)
            return _refresh_once(
                session,
                owner_id,
                conversation_id,
                rows,
                threshold=threshold,
                force=force,
                summarizer=summarizer,
                boundary_message_id=boundary_message_id,
                attempt=1,
            )
        except Exception:
            return None
    except Exception:
        return None


def _refresh_once(
    session: Session,
    owner_id: str,
    conversation_id: str,
    rows: Sequence,
    *,
    threshold: int,
    force: bool,
    summarizer,
    boundary_message_id: Optional[str],
    attempt: int,
) -> Optional[SessionContext]:
    """``refresh_session_context`` 的一次尝试（乐观锁冲突时最多重试一次）。"""
    n = len(rows)
    existing = load_context(session, owner_id, conversation_id) or SessionContext(
        conversation_id=conversation_id, owner_id=owner_id
    )

    # --- 分界：最近原文窗口起点 ---
    # 调用方（统一上下文构建器）给出分界时以它为准，保证摘要与最近原文用的是
    # 同一个边界；没给就按字符预算自己算。threshold 只保留为兼容参数，不再
    # 定义保护窗口（设计方案 §6.2 / §18.2.9）。
    window = select_recent_window(rows)
    if boundary_message_id:
        boundary = None
        for i, m in enumerate(rows):
            if m.id == boundary_message_id:
                boundary = i
                break
        if boundary is None:
            # 分界消息不在本次读取的历史里（例如刚被删除）：退回自己算的边界，
            # 宁可多摘要一条也不留空洞。
            boundary = window.start_index
    else:
        boundary = window.start_index

    # --- 摘要起点：重置标记优先于全部历史（设计方案 §7.7 / M-09）---
    start_idx = 0
    marker_id = existing.context_start_after_message_id
    if marker_id:
        for i, m in enumerate(rows):
            if m.id == marker_id:
                start_idx = i + 1
                break

    covered_id = existing.summarized_message_id
    covered_idx = start_idx - 1
    if covered_id:
        found = -1
        for i, m in enumerate(rows):
            if m.id == covered_id:
                found = i
                break
        covered_idx = found if found >= 0 else start_idx - 1
    if covered_idx < start_idx - 1:
        covered_idx = start_idx - 1

    # --- 待重建：有摘要文本但覆盖位置不可确认（v16 判据 / M-103）---
    # 规则摘要是确定性的，从头重算一遍既安全又便宜，不猜测覆盖位置。
    rebuild = bool(existing.needs_rebuild) or (
        (existing.summary or "").strip() and not covered_id
    )
    if rebuild:
        covered_idx = start_idx - 1

    to_summarize = rows[covered_idx + 1: boundary]
    summary_text = existing.summary
    summary_type = existing.summary_type
    summary_status = existing.summary_status
    summary_error = existing.summary_error
    summary_updated_at = existing.summary_updated_at
    #: 只有真正重算过才清掉待重建标记；没得可算时留着，下次进入再试
    needs_rebuild = bool(existing.needs_rebuild) and not to_summarize
    marker = covered_id
    count = existing.summarized_count

    if to_summarize:
        # 兼容旧调用：显式传进来的 rule_based_summarizer 仍是规则摘要，不能
        # 因为"传了摘要器"就标成模型摘要（设计方案 §7.2 要求两者可区分）。
        is_rule_fn = getattr(summarizer, "__name__", "") == "rule_based_summarizer"
        try:
            if summarizer is not None and not is_rule_fn:
                # 模型摘要（第四阶段）：由调用方提供，失败时不覆盖旧摘要
                older_text = "\n".join(
                    f"用户: {(m.query or '').strip()}"
                    + (
                        f"\n助手: {(m.response or '').strip()}"
                        if (m.response or "").strip()
                        else ""
                    )
                    for m in to_summarize
                )
                summary_text = summarizer(existing.summary, older_text)[:SUMMARY_MAX_CHARS]
                summary_type = "llm"
            else:
                summary_text = merge_digest(existing.summary, digest_lines(to_summarize))
                summary_type = "rule"
            marker = to_summarize[-1].id
            count = boundary
            summary_status = "succeeded"
            summary_error = None
            summary_updated_at = datetime.now()
        except Exception as exc:  # 摘要失败：游标不推进，旧摘要保留（§7.5 / M-46）
            degraded = True
            summary_status = "failed"
            summary_error = str(exc)[:500]

    # --- 槽位增量扫描（设计方案 §4.9）---
    # 起点标记同样限制槽位扫描：重置之后，标记之前的旧主题与旧结论不能靠
    # 重扫历史找回来（设计方案 §7.7 / M-09）。
    scanned = existing.slots_scanned_count
    if scanned > n or scanned < 0:
        scanned = 0
    scanned = max(scanned, start_idx)
    derived = derive_context(
        rows,
        _pinned_rows(session, owner_id, conversation_id),
        start_index=scanned,
        existing=existing,
    )
    last_id = rows[-1].id if rows else existing.slots_scanned_through_message_id

    merged = merge_into_row(
        session,
        owner_id,
        conversation_id,
        derived,
        summary_text,
        marker,
        count,
        existing,
        summary_type=summary_type,
        summary_status=summary_status,
        summary_error=summary_error,
        summary_updated_at=summary_updated_at,
        needs_rebuild=needs_rebuild,
        slots_scanned_through_message_id=last_id,
        slots_scanned_count=n,
        expected_version=existing.version,
    )
    merged.window_truncated = window.truncated
    return merged


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
    completed_items: Optional[Sequence[str]] = None,
    summary: Optional[str] = None,
) -> Optional[SessionContext]:
    """用户手工编辑会话上下文（设计方案 §4.2「编辑摘要」）。

    编辑过的条目按"用户明确修改的关键结论"处理：写入 ``pinned_json``，
    并把 message_index 记为当前最后一条消息的位置——之后的新消息可以覆盖
    它（P2-13），更早的消息不能（§4.2 不静默覆盖）。

    编辑**不动摘要覆盖游标**：编辑正文不等于重新摘要，游标被清空会让下一轮
    把已经摘要过的消息再摘要一遍（设计方案 §4.5 / M-12）。
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
        ("completed_items", items(completed_items, "completed_items")),
    ):
        if values is not None:
            setattr(context, name, values)
    if summary is not None:
        context.summary = (summary or "")[:SUMMARY_MAX_CHARS]

    summary_repo.upsert_session_summary(
        session,
        owner_id=owner_id,
        conversation_id=conversation_id,
        summary=context.summary,
        # 游标参数一律不传：None 表示"不动"，编辑正文不能把摘要游标清掉（M-12）
        context={
            "topic": (context.topic.text if context.topic else ""),
            "goal": (context.goal.text if context.goal else ""),
            "confirmed": _join(context.confirmed),
            "constraints": _join(context.constraints),
            "open_questions": _join(context.open_questions),
            "completed_items": _join(context.completed_items),
        },
        pinned_json=_pinned_payload(context),
    )
    return load_context(session, owner_id, conversation_id) or context


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
    for name in ("confirmed", "constraints", "open_questions", "completed_items"):
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
                "completed_items": _join(context.completed_items),
            },
            pinned_json=_pinned_payload(context),
        )
    return removed_total


def reset_session_context(
    session: Session, owner_id: str, conversation_id: str
) -> None:
    """重置本会话上下文（设计方案 P2-06、§7.7）；个人偏好不受影响。

    只清字段是不够的：下一轮重扫历史会把旧主题和旧结论找回来（设计方案
    §4.4 / M-09）。因此重置必须落下起点标记——从当时最后一条消息之后重新
    建立上下文，原始聊天记录本身不删除（M-10）。
    """
    try:
        messages = list_messages(session, conversation_id, owner_id)
    except Exception:
        messages = []
    last_id = messages[-1].id if messages else None
    summary_repo.reset_session_context(
        session, conversation_id, owner_id, context_start_after_message_id=last_id
    )


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
    "RecentWindow",
    "SUMMARY_MAX_CHARS",
    "slot_of",
    "recent_history_budget",
    "message_cost",
    "select_recent_window",
    "digest_lines",
    "merge_digest",
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
