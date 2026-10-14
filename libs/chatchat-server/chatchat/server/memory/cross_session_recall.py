"""跨会话相关记忆召回（智能聊天记忆第三期）。

第三期要解决的是"新会话不知道应当找哪一段历史"：不是把全部旧聊天塞给
模型，而是在用户明确允许的前提下，从**同一用户**的历史记忆里选出少量与
当前问题真正相关、可信且仍有效的信息（设计方案 §1、§5）。

四条底线在本模块里是服务端保证，不靠界面自觉：

1. **明确允许**：用户级总开关 + 分项开关（:class:`RecallSwitches`）。默认只
   开"个人偏好"（第一期，走 :mod:`memory_service` 那条通道）与"已确认决策"；
   项目内记忆与一般历史会话结论必须用户主动开启（§4.1）。
2. **可信**：只召回 ``confirmed`` 为真（用户自己给出/确认）的内容；自动提取
   的猜测落在"一般历史会话结论"桶里，默认不召回（§3）。已停用、已被替代、
   已过期、临时对话内容、其他用户的记忆一律排除。
3. **少而准**：必须与当前问题有关键词相关度，最多 ``MAX_RECALL_COUNT`` 条、
   ``MAX_RECALL_CHARS`` 字符；无关问题一律不带出历史记忆（P3-02）。
4. **可解释、可撤销**：每条都带来源（会话名 + 日期）与召回原因，界面据此显示
   "本次参考了 N 条历史记忆"，用户可"本次不使用"（只影响该会话）、停用、
   编辑（留修订历史）或删除（§4.2、§7）。

与第一、二期的分工：个人偏好段与会话上下文段仍由 :mod:`memory_service` 与
:mod:`session_context` 负责；本模块只新增"相关历史记忆"这一段，并取代第一期
那条不受开关管辖的长期记忆段——否则"关闭跨会话召回"根本关不掉任何东西。
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository import conversation_repository as conv_repo
from chatchat.server.db.repository import memory_repository as memory_repo

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 注入段标题与免责说明（设计方案 §5：当前消息优先、历史记忆不能替代知识库引用）
RECALL_SECTION_HEADER = "【相关历史记忆（来自你以往会话，供参考）】"
RECALL_SECTION_FOOTER = (
    "（以上来自你以往会话中已确认的内容，只作背景参考：与本轮明确要求冲突时以"
    "本轮为准；涉及知识库的事实仍以检索到的文档为依据；这些记忆不属于当前会话。）"
)

#: 每次回答最多注入的跨会话记忆条数与总长度（"少而准"，设计方案 §5）
MAX_RECALL_COUNT = 3
MAX_RECALL_CHARS = 1200

#: 召回桶（与"我的记忆"页的 4 个分项开关一一对应，设计方案 §4.1）
BUCKET_PREFERENCE = "preference"
BUCKET_DECISION = "decision"
BUCKET_PROJECT = "project"
BUCKET_GENERAL = "general"

#: 记忆类别中文标签（设计方案 §3 的五类 + 旧数据的一般记忆）
KIND_LABELS = {
    "decision": "已确认决策",
    "background": "稳定背景",
    "project_rule": "项目规则",
    "conclusion": "历史结论",
    "todo": "待办与目标",
    "fact": "一般记忆",
}

#: 状态中文标签（界面直接显示，回答"它现在还算数吗"）
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"
STATUS_DISABLED = "disabled"
STATUS_CONFIRMED = "confirmed"
STATUS_UNCONFIRMED = "unconfirmed"

#: 提升为长期记忆时的类别推荐（设计方案 §4.3 的"类别：决策/背景/规则/待办"）
_KIND_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("project_rule", re.compile(r"(本项目|这个项目|项目里|规则|要求|必须|不得|禁止)")),
    ("todo", re.compile(r"(待办|下一步|还要|未完成|计划做|接下来(要|做)|TODO)", re.IGNORECASE)),
    (
        "decision",
        re.compile(
            r"(确认|决定|采用|就用|选择|同意|定了|结论(是|为)|方案(是|为)|不做|不加|不使用|不做)"
        ),
    ),
    ("background", re.compile(r"(背景|一直|长期|我们用的?是|团队|环境|部署|架构)")),
)

#: 指向"某个项目"的措辞（§4.4：项目范围记忆只在项目内召回）
_PROJECT_HINT_RE = re.compile(r"(本项目|这个项目|项目里|在项目|项目空间|项目规则)")

#: 措辞明显是"一次性的当前会话要求"，不该建议提升为跨会话记忆
_ONE_OFF_RE = re.compile(r"(本次|这次|这轮|本会话|这个会话|暂时|今天|这里)")

#: 明确给出结论的措辞（与第二期会话上下文的"已确认事项"同一取向）
_CONFIRMED_DECISION_RE = re.compile(
    r"(确认|决定|采用|就用|同意|定了|结论(是|为)|方案(是|为)|不做|不加|不使用|不用|规划)"
)


# ---------------------------------------------------------------------------
# 用户级开关（设计方案 §4.1）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecallSwitches:
    """当前用户的召回开关。关闭只停止召回，**不删除任何记忆**。

    ``preferences`` 就是第一期的 ``memory_preferences_enabled``：跨会话召回
    总开关关掉时它仍然独立生效（P3-11：关闭跨会话召回后，第一期个人偏好
    仍按用户自己的开关工作）。
    """

    enabled: bool = True
    decisions: bool = True
    project: bool = False
    general: bool = False
    preferences: bool = True

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "decisions": self.decisions,
            "project": self.project,
            "general": self.general,
            "preferences": self.preferences,
        }


_SWITCH_FIELDS = {
    "enabled": "memory_recall_enabled",
    "decisions": "memory_recall_decisions_enabled",
    "project": "memory_recall_project_enabled",
    "general": "memory_recall_general_enabled",
    "preferences": "memory_preferences_enabled",
}


def get_switches(session: Optional[Session], owner_id: Optional[str]) -> RecallSwitches:
    """读用户级召回开关；读不到用户时按"全部关闭"处理。

    身份不明就不注入任何记忆，与第一期 ``preferences_enabled`` 同一取向。
    """
    if session is None or not owner_id:
        return RecallSwitches(
            enabled=False, decisions=False, project=False, general=False, preferences=False
        )
    try:
        user = session.get(UserModel, owner_id)
    except Exception:
        return RecallSwitches(
            enabled=False, decisions=False, project=False, general=False, preferences=False
        )
    if user is None:
        return RecallSwitches(
            enabled=False, decisions=False, project=False, general=False, preferences=False
        )
    return RecallSwitches(
        enabled=bool(getattr(user, "memory_recall_enabled", True)),
        decisions=bool(getattr(user, "memory_recall_decisions_enabled", True)),
        project=bool(getattr(user, "memory_recall_project_enabled", False)),
        general=bool(getattr(user, "memory_recall_general_enabled", False)),
        preferences=bool(getattr(user, "memory_preferences_enabled", True)),
    )


def set_switches(
    session: Session, owner_id: str, **changes: bool
) -> RecallSwitches:
    """更新当前用户的召回开关（未知键抛 ValueError，不静默忽略）。"""
    unknown = [k for k in changes if k not in _SWITCH_FIELDS]
    if unknown:
        raise ValueError(f"未知的召回开关：{unknown}")
    user = session.get(UserModel, owner_id)
    if user is None:
        raise ValueError("用户不存在")
    for key, value in changes.items():
        setattr(user, _SWITCH_FIELDS[key], bool(value))
    session.commit()
    session.refresh(user)
    return get_switches(session, owner_id)


# ---------------------------------------------------------------------------
# 召回结果
# ---------------------------------------------------------------------------

@dataclass
class RecalledMemory:
    """一条被本次回答使用的历史记忆 + 界面要能解释的信息。"""

    memory: memory_repo.MemoryModel
    bucket: str
    kind_label: str
    confirmed: bool
    scope: str
    source_label: str
    reason: str
    score: float = 0.0
    project_id: Optional[str] = None
    project_name: Optional[str] = None

    def to_dict(self) -> dict:
        m = self.memory
        return {
            "id": m.id,
            "content": m.content,
            "kind": m.kind,
            "kind_label": self.kind_label,
            "scope": self.scope,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "confirmed": self.confirmed,
            "status": memory_status(m),
            "source_label": self.source_label,
            "reason": self.reason,
            "bucket": self.bucket,
            "importance": m.importance,
            "update_time": m.update_time,
            "expires_at": m.expires_at,
            "source_conversation_id": m.source_conversation_id,
        }


def memory_status(m: memory_repo.MemoryModel, now: Optional[_dt.datetime] = None) -> str:
    """记忆当前状态（界面显示"已确认/未确认/已被替代/已过期/已停用"）。"""
    if not m.enabled:
        return STATUS_DISABLED
    if getattr(m, "superseded_by", None):
        return STATUS_SUPERSEDED
    if memory_repo.is_expired(m, now):
        return STATUS_EXPIRED
    return STATUS_CONFIRMED if getattr(m, "confirmed", False) else STATUS_UNCONFIRMED


# ---------------------------------------------------------------------------
# 召回
# ---------------------------------------------------------------------------

def recall_related_memories(
    session: Optional[Session],
    owner_id: Optional[str],
    query: str,
    conversation_id: Optional[str] = None,
    project_id: Optional[str] = None,
    max_count: int = MAX_RECALL_COUNT,
    max_chars: int = MAX_RECALL_CHARS,
    now: Optional[_dt.datetime] = None,
) -> List[RecalledMemory]:
    """本次回答可用的相关历史记忆（少而准、可解释）。

    - ``project_id`` 不传时取会话选定的项目（``conversation.project_id``）：
      项目范围记忆只有在该项目空间内、或用户在问题里点名该项目时才参与
      （设计方案 §4.4 / P3-09、P3-10）；
    - 该会话里被"本次不使用"的记忆一律跳过（§4.2 / P3-04）；
    - 来源是临时对话的记忆永不召回（§3）；来源就是当前会话的记忆也跳过：
      它已经在会话上下文段里，重复注入只会浪费预算；
    - 与问题没有关键词相关度时不返回（P3-02），除非问法本身是在问"关于我"。
    """
    if session is None or not owner_id:
        return []
    switches = get_switches(session, owner_id)
    if not switches.enabled:
        return []
    if max_count <= 0:
        return []

    candidates = memory_repo.list_recallable_memories(session, owner_id, now=now)
    if not candidates:
        return []

    active_project = project_id or conv_repo.conversation_project_id(
        session, conversation_id, owner_id
    )
    ignored = memory_repo.list_ignored_memory_ids(session, conversation_id, owner_id)
    keywords = memory_repo._keyword_candidates(query or "")
    key_hints = memory_repo._query_key_hints(query or "")
    general_query = memory_repo._is_general_memory_query(query or "")
    project_names = _mentioned_project_names(session, owner_id, query)
    temporary_cache: dict[str, bool] = {}

    scored: list[tuple[float, RecalledMemory]] = []
    for m in candidates:
        # 个人偏好只经第一期那条通道注入：不在这里重复出现，也保证关闭
        # 个人记忆时不会被跨会话召回"顺带"带出来。
        if (m.category or "") in memory_repo.VALID_CATEGORIES:
            continue
        if m.id in ignored:
            continue
        if m.source_conversation_id and m.source_conversation_id == conversation_id:
            continue

        bucket = _bucket_of(m, switches)
        if bucket is None:
            continue

        if m.scope == "project":
            if not _project_matches(m, active_project, project_names):
                continue

        if m.source_conversation_id:
            key = m.source_conversation_id
            if key not in temporary_cache:
                temporary_cache[key] = conv_repo.conversation_is_temporary(
                    session, key, owner_id
                )
            if temporary_cache[key]:
                continue

        hits, reason = _relevance(m, query, keywords, key_hints)
        if hits <= 0 and not general_query:
            continue

        score = hits + (1.5 if m.confirmed else 0.0) + (m.importance or 1) * 0.1
        if m.scope == "project":
            score += 1.0  # 用户明确在项目空间里工作，项目规则更贴近当前任务
        if not reason:
            reason = "与当前问题相关"
        scored.append(
            (
                score,
                RecalledMemory(
                    memory=m,
                    bucket=bucket,
                    kind_label=KIND_LABELS.get(m.kind or "fact", "一般记忆"),
                    confirmed=bool(m.confirmed),
                    scope=m.scope or "personal",
                    project_id=m.project_id,
                    project_name=m.project_name,
                    source_label=_source_label(session, owner_id, m),
                    reason=reason,
                    score=score,
                ),
            )
        )

    scored.sort(key=lambda x: (-x[0], x[1].memory.id))
    kept: List[RecalledMemory] = []
    total = 0
    for _, item in scored:
        size = len(item.memory.content or "")
        if kept and total + size > max_chars:
            break
        kept.append(item)
        total += size
        if len(kept) >= max_count:
            break
    return kept


def _bucket_of(m, switches: RecallSwitches) -> Optional[str]:
    """一条记忆落在哪个召回桶；不该用就返回 None。

    - 项目范围记忆 → ``project`` 桶（未确认的还要"一般历史会话结论"开关也开）；
    - 个人范围 + 已确认 → ``decision`` 桶；
    - 个人范围 + 未确认（自动提取的猜测）→ ``general`` 桶，默认关闭。
    """
    if m.scope == "project":
        if not switches.project:
            return None
        if not m.confirmed and not switches.general:
            return None
        return BUCKET_PROJECT
    if m.confirmed:
        return BUCKET_DECISION if switches.decisions else None
    return BUCKET_GENERAL if switches.general else None


def _project_matches(m, active_project: Optional[str], mentioned: Sequence[str]) -> bool:
    """项目范围记忆能否用于本次回答（设计方案 §4.4）。

    两种情形：会话选定了同一个项目；或者用户在问题里点名了该项目
    （"普通智能聊天…用户需要明确选择项目或提及项目后才可使用"）。
    """
    if active_project and m.project_id and active_project == m.project_id:
        return True
    name = (m.project_name or "").strip()
    return bool(name) and name in mentioned


def _mentioned_project_names(session: Session, owner_id: str, query: str) -> list[str]:
    """当前用户项目里，名字出现在问题中的那些（用于"提及项目"通道）。"""
    text = (query or "").strip()
    if not text:
        return []
    try:
        from chatchat.server.db.repository import research_repository as research_repo

        projects = research_repo.list_projects(session, owner_id)
    except Exception:
        # 项目表读不到不影响召回：最多这次不用项目记忆
        return []
    names = []
    for p in projects:
        name = (getattr(p, "name", "") or "").strip()
        if name and name in text:
            names.append(name)
    return names


def _relevance(
    m, query: str, keywords: Sequence[str], key_hints: Sequence[str]
) -> tuple[int, str]:
    """关键词相关度与可解释的召回原因。"""
    content = (m.content or "").lower()
    hits = sum(1 for kw in keywords if kw.lower() in content)
    matched = [kw for kw in keywords if kw.lower() in content]
    memory_key = (m.memory_key or "").lower()
    key_hits = sum(
        1 for hint in key_hints
        if memory_key == hint or memory_key.startswith(f"{hint}:")
    )
    if key_hits:
        return hits + key_hits * 3, "与当前问题的主题一致"
    if matched:
        return hits, "命中关键词：" + "、".join(matched[:4])
    return 0, ""


def _source_label(session: Session, owner_id: str, m) -> str:
    """界面显示用的来源说明（设计方案 §4.2：来自哪个会话、哪天）。"""
    created = getattr(m, "create_time", None) or getattr(m, "update_time", None)
    date = ""
    if isinstance(created, _dt.datetime):
        date = created.strftime("%Y-%m-%d")
    conv_id = m.source_conversation_id
    if conv_id:
        name = ""
        try:
            conv = conv_repo.get_owned_conversation(session, conv_id, owner_id)
            name = (conv.name or "").strip() if conv else ""
        except Exception:
            name = ""
        if name:
            return f"会话「{name}」" + (f"，{date}" if date else "")
        if date:
            return f"{date} 的会话"
    if m.scope == "project":
        return "项目内保存" + (f"，{date}" if date else "")
    return "手动保存" + (f"，{date}" if date else "")


def format_recall_segment(recalled: Sequence[RecalledMemory]) -> str:
    """把召回结果格式化成独立上下文段；无记忆时返回空串。"""
    if not recalled:
        return ""
    lines = []
    for item in recalled:
        flag = "已确认" if item.confirmed else "未确认"
        lines.append(
            f"- [{item.kind_label}·{flag}]（{item.source_label}）{item.memory.content}"
        )
    return RECALL_SECTION_HEADER + "\n" + "\n".join(lines) + "\n" + RECALL_SECTION_FOOTER


def build_recall_segment(
    session: Optional[Session],
    owner_id: Optional[str],
    query: str,
    conversation_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> tuple[str, List[RecalledMemory]]:
    """注入段文本 + 本次实际用到的记忆列表（界面展示与注入内容同源）。

    任何异常都吞掉：记忆召回失败绝不能阻断正常回答。
    """
    try:
        recalled = recall_related_memories(
            session, owner_id, query, conversation_id, project_id
        )
    except Exception:
        return "", []
    return format_recall_segment(recalled), recalled


# ---------------------------------------------------------------------------
# 从会话上下文提升为长期记忆（设计方案 §4.3）
# ---------------------------------------------------------------------------

@dataclass
class PromotionSuggestion:
    """一条"值得保存为跨会话记忆"的候选（只提出，绝不自动保存）。"""

    content: str
    recommended_kind: str
    recommended_scope: str
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    reason: str = ""
    already_saved: bool = False

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "recommended_kind": self.recommended_kind,
            "recommended_scope": self.recommended_scope,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "reason": self.reason,
            "already_saved": self.already_saved,
        }


def infer_kind(content: str) -> str:
    """推断记忆类别（决策/背景/规则/待办）；推不出返回 ``fact``。"""
    for kind, pattern in _KIND_RULES:
        if pattern.search(content or ""):
            return kind
    return "fact"


def suggest_promotion(
    session: Session,
    owner_id: str,
    text: str,
    conversation_id: Optional[str] = None,
) -> Optional[PromotionSuggestion]:
    """判断一句话是否"值得保存为跨会话记忆"，给出候选（不写任何数据）。

    只提建议：用户不确认就不保存（设计方案 §4.3 / P3-13）。措辞明显是一次性
    的当前会话要求（"本次/暂时"）时不提，避免和第二期"仅本会话"打架。
    """
    raw = (text or "").strip()
    if not raw or not owner_id:
        return None
    if not _CONFIRMED_DECISION_RE.search(raw):
        return None
    if _ONE_OFF_RE.search(raw) and not _PROJECT_HINT_RE.search(raw):
        return None

    project = None
    active = conv_repo.conversation_project_id(session, conversation_id, owner_id)
    mentioned = _mentioned_project_names(session, owner_id, raw)
    if _PROJECT_HINT_RE.search(raw) or mentioned:
        project = _find_project(session, owner_id, active, mentioned)
    scope = "project" if project else "personal"
    kind = infer_kind(raw)
    if scope == "project" and kind == "fact":
        kind = "project_rule"
    saved = bool(_find_same_content_memory(session, owner_id, raw))
    return PromotionSuggestion(
        content=raw,
        recommended_kind=kind,
        recommended_scope=scope,
        project_id=project.id if project else None,
        project_name=project.name if project else None,
        reason=(
            "内容是一条已确认的结论，可能在其他会话里仍然有效"
            if scope == "personal"
            else "内容指向某个项目，只在该项目内召回"
        ),
        already_saved=saved,
    )


def _find_project(session: Session, owner_id: str, active: Optional[str], names: Sequence[str]):
    """按会话选定项目或问题里提到的项目名找到项目行（找不到返回 None）。"""
    try:
        from chatchat.server.db.repository import research_repository as research_repo

        if active:
            project = research_repo.get_owned_project(session, active, owner_id)
            if project is not None:
                return project
        projects = research_repo.list_projects(session, owner_id)
    except Exception:
        return None
    for name in names:
        for p in projects:
            if (getattr(p, "name", "") or "").strip() == name:
                return p
    return None


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _find_same_content_memory(
    session: Session, owner_id: str, content: str
):
    """同一用户是否已有内容等价的记忆（去重，不产生重复行）。"""
    target = _norm_text(content)
    if not target:
        return None
    for m in memory_repo.list_memories(session, owner_id, enabled_only=False):
        if _norm_text(m.content) == target and not m.superseded_by:
            return m
    return None


def _slot_key(kind: str, content: str) -> str:
    """同一主题槽位的稳定键：同槽位的新决策会替代旧决策（设计方案 §4.5）。

    复用第二期的槽位判定（语言/详略/路由/工具/格式…），因此"不做自动模型
    路由"与"下一版本恢复自动模型路由"落在同一个槽位上。槽位判不出来时用
    随机键，表示这是一条独立事实，不覆盖别的内容。
    """
    from chatchat.server.memory.session_context import slot_of

    slot = slot_of(content)
    if slot and slot != "other":
        return f"recall:{kind}:{slot}"
    import uuid

    return f"recall:{kind}:{uuid.uuid4().hex[:12]}"


def _slot_of_key(memory_key: str) -> str:
    """去掉 ``_unique_slot_key`` 加的唯一后缀，还原槽位键。

    ``memory`` 表对 ``(owner_id, memory_key)`` 有唯一约束，而替代链要求旧行
    保留、新行另起一条，因此同槽位第二条记忆的键会带 ``#xxxxxxxx`` 后缀。
    """
    return (memory_key or "").split("#", 1)[0]


def _unique_slot_key(session: Session, owner_id: str, base_key: str) -> str:
    """槽位键在用户名下必须唯一；已被占用时加短后缀另起一条。"""
    taken = {
        m.memory_key or ""
        for m in memory_repo.list_memories(session, owner_id, enabled_only=False)
    }
    if base_key not in taken:
        return base_key
    import uuid

    return f"{base_key}#{uuid.uuid4().hex[:8]}"


def promote_to_long_term(
    session: Session,
    owner_id: str,
    *,
    content: str,
    kind: str = "decision",
    scope: str = "personal",
    project_id: Optional[str] = None,
    expires_at=None,
    source_conversation_id: Optional[str] = None,
    confirmed: bool = True,
) -> memory_repo.MemoryModel:
    """把一条已确认内容保存为跨会话记忆（用户确认后才调用）。

    抛 ``ValueError`` 表示请求不合法（内容为空、敏感信息、类别/范围未知、
    项目不属于当前用户、来源会话不可用或为临时对话），由路由映射为 400。

    冲突处理（设计方案 §4.5）：同一范围、同一槽位上已有别的已确认记忆时，
    新记忆生效、旧记忆标记为"已被替代"（记录保留、修订链完整），因此
    注入时只会看到最新那条（P3-07）。
    """
    from chatchat.server.memory.memory_service import sensitive_reason

    text = (content or "").strip()
    if not text:
        raise ValueError("content 不能为空")
    reason = sensitive_reason(text)
    if reason:
        raise ValueError(f"个人记忆不应保存{reason}，已拒绝保存")
    kind_value = memory_repo._normalize_kind(kind)
    scope_value = memory_repo._normalize_scope(scope)

    project = None
    if scope_value == "project":
        if not project_id:
            raise ValueError("项目范围记忆必须指定 project_id")
        from chatchat.server.db.repository import research_repository as research_repo

        project = research_repo.get_owned_project(session, project_id, owner_id)
        if project is None:
            raise ValueError("项目不存在或无权访问")

    if source_conversation_id:
        if conv_repo.get_owned_conversation(
            session, source_conversation_id, owner_id
        ) is None:
            raise ValueError("会话不存在或无权访问")
        if conv_repo.conversation_is_temporary(
            session, source_conversation_id, owner_id
        ):
            raise ValueError("临时对话的内容不能保存为跨会话记忆")

    same_content = _find_same_content_memory(session, owner_id, text)
    if same_content is not None:
        # 内容完全相同：不再造一条重复记忆，只把它重新启用并补齐元数据
        return memory_repo.update_memory(
            session,
            same_content.id,
            owner_id,
            kind=kind_value,
            scope=scope_value,
            project_id=project_id or None,
            project_name=project.name if project else "",
            confirmed=True if confirmed else None,
            expires_at=expires_at,
            enabled=True,
            record_revision=False,
        )

    key = _slot_key(kind_value, text)
    previous = _find_slot_memory(session, owner_id, key, scope_value, project_id)
    key = _unique_slot_key(session, owner_id, key)

    m = memory_repo.create_memory(
        session,
        owner_id=owner_id,
        content=text,
        mtype=_type_for_kind(kind_value),
        source="manual",
        importance=4,
        memory_key=key,
        source_conversation_id=source_conversation_id,
        kind=kind_value,
        scope=scope_value,
        project_id=project.id if project else None,
        project_name=project.name if project else None,
        confirmed=confirmed,
        expires_at=expires_at,
        supersedes=previous.id if previous else None,
    )
    if previous is not None:
        memory_repo.mark_superseded(session, previous.id, owner_id, by_id=m.id)
    return m


def _find_slot_memory(
    session: Session, owner_id: str, key: str, scope: str, project_id: Optional[str]
):
    """同范围同槽位上仍然生效的旧记忆（要被新决策替代的那条）。"""
    for m in memory_repo.list_memories(session, owner_id, enabled_only=True):
        if m.superseded_by:
            continue
        if (m.scope or "personal") != scope:
            continue
        if scope == "project" and (m.project_id or None) != (project_id or None):
            continue
        if _slot_of_key(m.memory_key) == key:
            return m
    return None


def _type_for_kind(kind: str) -> str:
    """类别 → 第一期的 type（保持旧字段有意义，不破坏既有分组）。"""
    if kind == "todo":
        return "goal"
    if kind in ("decision", "conclusion", "project_rule"):
        return "important"
    return "fact"


def promotion_candidates(
    session: Session, owner_id: str, conversation_id: Optional[str]
) -> list[PromotionSuggestion]:
    """本会话已确认事项里"值得保存"的候选（抽屉里的候选卡片）。

    只提出，不自动升级；已经存成跨会话记忆的条目标 ``already_saved``，
    界面据此显示"已保存"而不是再给一个保存按钮（设计方案 §4.3）。
    """
    if not conversation_id or not owner_id:
        return []
    if conv_repo.conversation_is_temporary(session, conversation_id, owner_id):
        return []
    from chatchat.server.memory import session_context

    context = session_context.load_context(session, owner_id, conversation_id)
    if context is None:
        return []
    active = conv_repo.conversation_project_id(session, conversation_id, owner_id)
    out: list[PromotionSuggestion] = []
    for item in context.confirmed:
        text = (item.text or "").strip()
        if not text:
            continue
        saved = _find_same_content_memory(session, owner_id, text) is not None
        project = _find_project(session, owner_id, active, []) if active else None
        one_off = bool(_ONE_OFF_RE.search(text))
        out.append(
            PromotionSuggestion(
                content=text,
                recommended_kind=infer_kind(text),
                recommended_scope="project" if (project and not one_off) else "personal",
                project_id=project.id if project else None,
                project_name=project.name if project else None,
                reason=(
                    "措辞限定在当前会话，默认只作为个人范围记忆"
                    if one_off
                    else "本会话已确认事项，可能对其他会话也有用"
                ),
                already_saved=saved,
            )
        )
    return out


__all__ = [
    "RECALL_SECTION_HEADER",
    "RECALL_SECTION_FOOTER",
    "MAX_RECALL_COUNT",
    "MAX_RECALL_CHARS",
    "BUCKET_PREFERENCE",
    "BUCKET_DECISION",
    "BUCKET_PROJECT",
    "BUCKET_GENERAL",
    "KIND_LABELS",
    "RecallSwitches",
    "RecalledMemory",
    "PromotionSuggestion",
    "get_switches",
    "set_switches",
    "memory_status",
    "recall_related_memories",
    "format_recall_segment",
    "build_recall_segment",
    "infer_kind",
    "suggest_promotion",
    "promote_to_long_term",
    "promotion_candidates",
]
