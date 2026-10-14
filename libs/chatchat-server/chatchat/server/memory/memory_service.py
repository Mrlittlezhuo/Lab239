"""用户分层记忆服务（写入/更新/召回/摘要/自动提取）。

设计要点：
- 长期记忆独立于 conversation/message，按 owner_id 隔离；不进入公共知识库。
- 召回只读 ``memory``（enabled），不读 ``session_summary``（摘要不是长期事实）。
- 显式"请记住"可靠写入并去重/替换旧值；"忘记"/管理页删除后停止召回。
- 自动提取只处理"较稳定、未来可能有用的"信息，由用户级开关控制；
  显式意图（记住/忘记）始终生效。
- 会话摘要为增量：只摘要"超出窗口且尚未摘要"的较早消息，记录覆盖位置。
- 个人偏好（智能聊天记忆第一期）：用户在"我的记忆"页保存、带 ``category``
  的记忆，按固定优先级少量注入，作为辅助上下文而非命令；受用户级总开关与
  单次请求的"本次不使用"共同控制。
- 会话私有上下文（智能聊天记忆第二期）：见 :mod:`session_context`；显式
  "记住/忘记"改走确认卡片（见 :mod:`memory_intents`），不再由本模块直接写入。
- 跨会话相关记忆召回（智能聊天记忆第三期）：见 :mod:`cross_session_recall`。
  原先不受开关管辖的"长期记忆段"由它取代：只有用户级总开关与分项开关允许、
  且与当前问题真正相关的已确认记忆才会注入，界面展示的"本次参考了哪些记忆"
  与注入内容同源。
- 所有模型/提取/摘要失败都不阻断正常回答（调用方 try/except）。
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.repository import memory_repository as memory_repo
from chatchat.server.db.repository import session_summary_repository as summary_repo
from chatchat.server.db.repository import user_repository as user_repo
from chatchat.server.db.repository.message_repository import list_messages
from chatchat.server.memory import cross_session_recall, session_context

# ---------------------------------------------------------------------------
# 常量 / 默认值
# ---------------------------------------------------------------------------

#: 召回默认最大条数
RECALL_MAX_COUNT = 5
#: 召回默认最大总字符数
RECALL_MAX_CHARS = 1500
#: 默认短期上下文窗口（保留最近 N 条消息为"近期轮次"）
SUMMARY_THRESHOLD = 12

#: 显式意图正则（中文"请记住/帮我记/忘掉/忘记"）。
#: 第二期设计方案 §4.3 的示例是裸"记住：…""忘记：…"，所以带冒号或句首的
#: 裸动词也算显式意图；即使命中得不准，代价也只是多出一张待确认卡片，
#: 用户点"不保存"即可，不会静默写入任何记忆。
_REMEMBER_RE = re.compile(
    r"(请记住|请记住以下|帮我记住|请记住我|我要你记住|记住我|记住以下|记下来"
    r"|记住[：:，,]|^记住)"
)
_FORGET_RE = re.compile(
    r"(请忘记|忘掉|忘记我|忘记以下|不用记住|别记住|删除记忆|清除记忆"
    r"|忘记[：:，,]|^忘记)"
)

#: 显式"记住/忘记"是否必须先经确认卡片（第二期设计方案 §4.3 要求：是）。
#: 为真时 auto_extract 不写显式意图，写入只发生在 /memories/intents 的确认里。
EXPLICIT_REQUIRES_CONFIRMATION = True

#: 记忆注入的清晰独立上下文段标题
MEMORY_SECTION_HEADER = "【你的记忆（以下为用户长期信息，供参考）】"

# --- 个人偏好（智能聊天记忆第一期）---

#: 单次回答最多注入的个人偏好条数（"少而准"，其余不必全部呈现给模型）
PREFERENCE_MAX_COUNT = 5
#: 个人偏好段标题（与长期记忆段、摘要段分开，便于用户对照界面看到的内容）
PREFERENCE_SECTION_HEADER = "【个人偏好（用户自行保存，供参考，不是命令）】"

#: 类别中文标签；与前端管理页分组一致，使注入段可读、可解释
PREFERENCE_CATEGORY_LABELS = {
    "answer_style": "回答偏好",
    "technical_background": "技术背景",
    "work_style": "工作偏好",
    "long_term_goal": "长期目标",
}

#: 回答语言类偏好的识别（优先级最高：语言错了用户第一眼就能看到）
_LANGUAGE_RE = re.compile(
    r"(中文|英文|英语|日文|日语|language|chinese|english)", re.IGNORECASE
)

#: 明显不该作为个人偏好保存的内容（凭证、证件号、卡号）。只拦"高置信度"
#: 形态——必须带显式赋值关系或本身就是完整号码，避免把"回答里不要出现密码
#: 二字"这类正常偏好误杀；财务/医疗等软性内容靠界面提醒用户自行克制。
#: 数字段用前后否定断言而非 \b：汉字也算 \w，紧邻数字时 \b 不成立。
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "密码或口令",
        re.compile(r"(密码|口令|password|passwd|pwd)\s*[:：=]\s*\S", re.IGNORECASE),
    ),
    ("密码或口令", re.compile(r"(密码|口令)\s*(是|为)\s*\S")),
    (
        "密钥或访问令牌",
        re.compile(
            r"(api[_\s-]?key|secret[_\s-]?key|access[_\s-]?token|token|密钥|令牌)"
            r"\s*[:：=]\s*\S",
            re.IGNORECASE,
        ),
    ),
    ("密钥或访问令牌", re.compile(r"sk-[A-Za-z0-9_-]{12,}")),
    ("密钥或访问令牌", re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)),
    ("身份证件号", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("银行卡号", re.compile(r"(银行卡|储蓄卡|信用卡|卡号)\D{0,8}\d{12,19}", re.IGNORECASE)),
    ("银行卡号", re.compile(r"(?<!\d)\d{15,19}(?!\d)")),
)


# ---------------------------------------------------------------------------
# 可替换的"摘要器"（测试用假摘要器；默认基于规则的简单压缩，不依赖模型）
# ---------------------------------------------------------------------------

def rule_based_summarizer(existing_summary: str, older_text: str) -> str:
    """默认基于规则的轻量摘要：保留已有摘要 + 较早消息的要点行。

    不依赖真实模型；对"每轮重复总结全部历史"做增量处理。测试可整体替换
    本函数（``summarize_conversation(..., summarizer=fake)``）。
    """
    parts: List[str] = []
    if existing_summary:
        parts.append(existing_summary)
    # 把较早消息按"用户/助手"行化，仅保留内容要点
    for line in (older_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts.append(line[:200])
    text = "\n".join(parts)
    # 限长，避免无限增长
    return text[:4000]


# ---------------------------------------------------------------------------
# 召回与注入
# ---------------------------------------------------------------------------

def recall_memories(
    session: Session,
    owner_id: str,
    query: str,
    max_count: int = RECALL_MAX_COUNT,
    max_chars: int = RECALL_MAX_CHARS,
    uncategorized_only: bool = False,
) -> List[memory_repo.MemoryModel]:
    """召回相关且启用的少量记忆（限制数量与总长度）。

    ``uncategorized_only=True`` 用于长期记忆注入段：带个人偏好类别的记忆
    只经 ``select_preferences`` 注入，不在这里重复出现。
    """
    memories = memory_repo.search_memories(
        session, owner_id, query, limit=max_count, uncategorized_only=uncategorized_only
    )
    kept: List[memory_repo.MemoryModel] = []
    total = 0
    for m in memories:
        c = len(m.content or "")
        if kept and total + c > max_chars:
            break
        kept.append(m)
        total += c
    return kept


def format_memory_context(
    memories: Sequence[memory_repo.MemoryModel],
    header: str = MEMORY_SECTION_HEADER,
) -> str:
    """把记忆格式化成一段清晰、独立的上下文。无记忆时返回空串。"""
    if not memories:
        return ""
    lines = []
    for m in memories:
        src = m.source or "chat"
        lines.append(f"- [{m.type}](来源:{src}) {m.content}")
    return header + "\n" + "\n".join(lines)


def build_memory_context(
    session: Optional[Session],
    owner_id: Optional[str],
    query: str,
) -> str:
    """构造注入 Prompt 的记忆上下文段；失败/无会话/无记忆返回空串。

    - 只读 ``memory`` 表（enabled、owner 匹配），不把会话摘要当长期事实；
    - 个人偏好（带 ``category``）不在这里注入，见 ``select_preferences``；
    - 用户本轮明确表达与记忆冲突时，以当前表达为准（见注入说明）；
    - 任何异常都不阻断聊天主流程。
    """
    if session is None or not owner_id:
        return ""
    try:
        memories = recall_memories(session, owner_id, query, uncategorized_only=True)
        ctx = format_memory_context(memories)
        if ctx:
            ctx += (
                "\n（以上为长期记忆；若用户本轮明确表达了新的偏好或与之冲突，"
                "以用户本轮表达为准。）"
            )
        return ctx
    except Exception:
        # 记忆召回失败绝不阻断正常回答
        return ""


# ---------------------------------------------------------------------------
# 个人偏好（第一期）：挑选、格式化、开关、敏感内容
# ---------------------------------------------------------------------------

def sensitive_reason(content: str) -> Optional[str]:
    """识别明显不该保存为个人偏好的内容，返回可读原因；无则返回 None。

    写入接口据此拒绝（400），使"敏感信息不会被静默保存"成为服务端保证，
    而不是只靠界面提醒。
    """
    text = content or ""
    for reason, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def preferences_enabled(session: Optional[Session], owner_id: Optional[str]) -> bool:
    """用户级"个人记忆"总开关（关闭只停止注入，不删除任何数据）。

    读不到用户时按关闭处理：宁可不注入，也不把记忆给身份不明的请求。
    """
    if session is None or not owner_id:
        return False
    try:
        user = user_repo.get_by_id(session, owner_id)
    except Exception:
        return False
    if user is None:
        return False
    return bool(getattr(user, "memory_preferences_enabled", True))


def select_preferences(
    session: Optional[Session],
    owner_id: Optional[str],
    query: str = "",
    max_count: int = PREFERENCE_MAX_COUNT,
    max_chars: int = RECALL_MAX_CHARS,
) -> List[memory_repo.MemoryModel]:
    """按第一期优先级挑选本次要注入的个人偏好（少而准、可解释）。

    优先级（设计方案 §5）：回答语言 → 输出风格 → 与当前问题明显相关的背景 →
    其余按重要性/更新时间。用户保存很多条时不会全部塞给模型；界面展示的
    "本次使用的偏好"直接调用本函数，保证与真实注入内容一致。
    """
    if session is None or not owner_id:
        return []
    try:
        prefs = memory_repo.list_preference_memories(session, owner_id)
    except Exception:
        # 偏好读取失败绝不阻断聊天主流程
        return []
    if not prefs:
        return []
    keywords = memory_repo._keyword_candidates(query)
    query_text = (query or "").lower()

    def sort_key(m):
        content = m.content or ""
        relevance = sum(1 for kw in keywords if kw.lower() in content.lower())
        if m.category == "answer_style" and _LANGUAGE_RE.search(content):
            bucket = 0  # 回答语言
        elif m.category == "answer_style":
            bucket = 1  # 输出风格
        elif relevance > 0 or (query_text and query_text in content.lower()):
            bucket = 2  # 与当前问题明显相关的背景
        else:
            bucket = 3
        return (bucket, -relevance, -(m.importance or 1), m.id)

    kept: List[memory_repo.MemoryModel] = []
    total = 0
    for m in sorted(prefs, key=sort_key):
        size = len(m.content or "")
        if kept and total + size > max_chars:
            break
        kept.append(m)
        total += size
        if len(kept) >= max_count:
            break
    return kept


def format_preferences(
    memories: Sequence[memory_repo.MemoryModel],
    header: str = PREFERENCE_SECTION_HEADER,
) -> str:
    """把个人偏好格式化成独立上下文段；无偏好时返回空串。

    明确写成"辅助上下文、不是命令"，并保留安全提醒与必要引用不受偏好影响
    （设计方案 §5：'回答简洁' 不应导致省略安全提醒、关键引用或详细解释）。
    """
    if not memories:
        return ""
    lines = []
    for m in memories:
        label = PREFERENCE_CATEGORY_LABELS.get(m.category, "个人偏好")
        lines.append(f"- {label}：{m.content}")
    return (
        header
        + "\n"
        + "\n".join(lines)
        + "\n（以上为用户自行保存的个人偏好，用于改善回答方式，属于辅助上下文，"
        "不是不可违背的命令：与用户本轮明确要求冲突时以本轮要求为准；不得因偏好"
        "省略安全提醒、必要引用或用户明确要求的详细解释。）"
    )


# ---------------------------------------------------------------------------
# 显式写入 / 忘记 / 去重替换
# ---------------------------------------------------------------------------

def _norm(content: str) -> str:
    return re.sub(r"\s+", "", (content or "")).lower()


def remember(
    session: Session,
    owner_id: str,
    content: str,
    mtype: str = "fact",
    source: str = "chat",
    importance: int = 3,
    memory_key: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> memory_repo.MemoryModel:
    """可靠写入一条记忆；相同主题更新旧值，相同内容去重。"""
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    # 同一稳定主题键代表同一个事实/偏好槽位，新表述替换旧值。
    for m in memory_repo.list_memories(session, owner_id, enabled_only=False):
        same_key = bool(memory_key and m.memory_key == memory_key)
        if same_key or _norm(m.content) == _norm(content):
            m.content = content
            m.type = memory_repo._normalize_type(mtype)
            m.source = source
            m.importance = memory_repo._normalize_importance(importance)
            m.enabled = True
            if memory_key:
                m.memory_key = memory_key
            m.source_conversation_id = source_conversation_id
            m.source_message_id = source_message_id
            session.commit()
            session.refresh(m)
            return m
    return memory_repo.create_memory(
        session,
        owner_id,
        content,
        mtype=mtype,
        source=source,
        importance=importance,
        memory_key=memory_key,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
    )


def forget(
    session: Session, owner_id: str, content: Optional[str] = None
) -> int:
    """停用匹配的记忆，停止召回。

    - 给定 content：停用包含该内容关键词的记忆（当前"忘记"优先）；
    - 未给定 content：停用全部记忆（"忘掉一切"）。
    返回被停用的条数。
    """
    target = _strip_forget_prefix((content or "").strip())
    key_prefixes, type_filter, forget_all = forget_match_plan(target)

    affected = 0
    for m in memory_repo.list_memories(session, owner_id, enabled_only=False):
        if m.enabled is False:
            continue
        if matches_forget(m, target, key_prefixes, type_filter, forget_all):
            m.enabled = False
            affected += 1
    if affected:
        session.commit()
    return affected


def forget_match_plan(
    target: str
) -> tuple[list[str], Optional[str], bool]:
    """把"忘记 X"的措辞解析成匹配条件：主题键前缀 / 类型 / 是否忘记全部。

    确认卡片用它列出"将被停用的记忆"，因此卡片上看到的与实际停用的必须是
    同一批（设计方案 §4.3：忘记也要展示将被删除或重置的对象）。
    """
    key_prefixes: list[str] = []
    type_filter: str | None = None
    if re.search(r"回答|回复|风格|简洁|详细|语言", target):
        key_prefixes.append("preference:response_style")
    elif "偏好" in target:
        type_filter = "preference"
    if re.search(r"研究|方向|专业", target):
        key_prefixes.append("profile:research")
    if re.search(r"名字|称呼", target):
        key_prefixes.append("profile:name")
    if re.search(r"工作|职业|公司", target):
        key_prefixes.append("profile:work")
    if re.search(r"目标|计划", target):
        key_prefixes.append("goal")
    forget_all = not target or bool(re.search(r"全部|所有|一切", target))
    return key_prefixes, type_filter, forget_all


def matches_forget(
    m,
    target: str,
    key_prefixes: Sequence[str],
    type_filter: Optional[str],
    forget_all: bool,
) -> bool:
    """一条记忆是否落在"忘记 X"的范围内。"""
    if forget_all:
        return True
    if type_filter and m.type == type_filter:
        return True
    if any(
        m.memory_key == prefix or m.memory_key.startswith(f"{prefix}:")
        for prefix in key_prefixes
    ):
        return True
    normalized_target = _norm(target)
    if normalized_target and (
        normalized_target in _norm(m.content) or _norm(m.content) in normalized_target
    ):
        return True
    return False


def auto_extract(
    session: Session,
    owner_id: str,
    query: str,
    response: str = "",
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
) -> List[str]:
    """从一轮对话中自动提取"值得长期记住"的信息。

    显式“记住/忘记”始终生效；自动开关只控制普通陈述中的稳定信息提取。
    默认规则仅覆盖姓名/身份、研究工作方向、回答偏好、项目和长期目标等
    可明确识别的内容，避免把闲聊或模型回答当成用户事实。
    """
    actions: List[str] = []
    q = (query or "").strip()
    if not q or session is None or not owner_id:
        return actions

    # 设计方案 §4.4 / P2-11：临时对话不写入任何记忆，包括跨会话个人偏好。
    # 会话上下文侧由 session_context 拦住，长期记忆侧在这里拦住。
    if source_conversation_id:
        try:
            from chatchat.server.db.repository import (
                conversation_repository as conv_repo,
            )

            if conv_repo.conversation_is_temporary(
                session, source_conversation_id, owner_id
            ):
                return actions
        except Exception:
            pass

    explicit_forget = bool(_FORGET_RE.search(q))
    explicit_remember = bool(_REMEMBER_RE.search(q))

    # 设计方案 §4.3：显式"记住/忘记"不直接写入，先出确认卡片（范围由用户选，
    # "忘记"要列出将被停用的对象）。卡片走 /memories/intents，确认后才写。
    # 关掉这个常量只给脚本类调用留逃生口，界面路径始终走确认卡片。
    if (explicit_forget or explicit_remember) and EXPLICIT_REQUIRES_CONFIRMATION:
        return actions

    if explicit_forget:
        n = forget(session, owner_id, content=q)
        if n:
            actions.append(f"已停用 {n} 条相关记忆")
        return actions

    # 用户级自动记忆开关只影响隐式提取，显式“请记住”不受影响。
    auto_enabled = False
    try:
        from chatchat.server.db.models.user_model import UserModel

        u = session.get(UserModel, owner_id)
        auto_enabled = bool(u is not None and u.memory_auto_enabled)
    except Exception:
        auto_enabled = False
    if not explicit_remember and not auto_enabled:
        return actions

    text = _strip_intent_prefix(q) if explicit_remember else q
    extracted = extract_stable_memories(text)
    if not extracted and explicit_remember and text:
        digest = hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:20]
        extracted = [ExtractedMemory(f"explicit:{digest}", _infer_type(text), text)]
    for item in extracted:
        remember(
            session,
            owner_id,
            item.content,
            mtype=item.mtype,
            source="chat" if explicit_remember else "auto_extract",
            importance=item.importance,
            memory_key=item.key,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
        )
        actions.append(f"已记住: {item.content}")
    return actions


@dataclass(frozen=True)
class ExtractedMemory:
    key: str
    mtype: str
    content: str
    importance: int = 3


def extract_stable_memories(text: str) -> List[ExtractedMemory]:
    """从常见自然表达中确定性提取稳定信息，一句话可产生多条记忆。"""
    found: dict[str, ExtractedMemory] = {}
    clauses = [
        c.strip() for c in re.split(r"[，,；;。\n]+", text or "") if c.strip()
    ]
    for clause in clauses:
        # 回答风格使用固定主题键，确保“简洁”能被“详细”替换。
        if re.search(r"简洁|精简|详细|展开|中文|英文", clause) and re.search(
            r"偏好|回答|回复|风格|尽量|以后|请|使用", clause
        ):
            styles = []
            for token in ("简洁", "精简", "详细", "展开", "中文", "英文"):
                if token in clause:
                    styles.append(token)
            style = "、".join(styles).replace("精简", "简洁").replace("展开", "详细")
            found["preference:response_style"] = ExtractedMemory(
                "preference:response_style", "preference", f"回答偏好：{style}", 4
            )

        patterns = (
            (r"(?:我的)?(?:名字|姓名)(?:是|叫|为)?\s*(.+)|我叫\s*(.+)", "profile:name", "fact", "姓名"),
            (r"(?:我的)?研究方向(?:是|为)?\s*(.+)|我(?:在|正在)?研究\s*(.+)", "profile:research", "fact", "研究方向"),
            (r"我在\s*(.+?)\s*工作|(?:我的)?(?:工作|职业)(?:是|为)?\s*(.+)", "profile:work", "fact", "工作"),
            (r"(?:我的)?项目(?:是|叫|为)?\s*(.+)", "profile:project", "fact", "项目"),
            (r"(?:我的)?(?:长期)?(?:目标|计划)(?:是|为)?\s*(.+)|我(?:计划|打算)\s*(.+)", "goal:primary", "goal", "长期目标"),
            (r"我(?:是|是一名)\s*(.+)", "profile:identity", "fact", "身份"),
        )
        for pattern, key, mtype, label in patterns:
            match = re.search(pattern, clause)
            if not match:
                continue
            value = next((g for g in match.groups() if g), "").strip(" ：:")
            if value:
                found[key] = ExtractedMemory(key, mtype, f"{label}：{value}", 4)
            break
    return list(found.values())


def _strip_forget_prefix(q: str) -> str:
    return re.sub(
        r"^(请)?(?:帮我)?(?:忘记|忘掉|删除|清除)(?:关于)?[：:，,\s]*",
        "",
        (q or "").strip(),
    ).strip()


def _strip_intent_prefix(q: str) -> str:
    """去掉'请记住/帮我记住'等前缀，取出要记住的内容本体。"""
    q = re.sub(r"^(请)?帮我?记住(以下)?[：:，,\s]*", "", (q or "").strip())
    q = re.sub(r"^(请)?记住[：:，,\s]*", "", q.strip())
    return q.strip()


def _infer_type(q: str) -> str:
    """粗分类：偏好/目标/事实。"""
    if re.search(r"(偏好|喜欢|讨厌|习惯|风格|简洁|详细)", q):
        return "preference"
    if re.search(r"(目标|计划|打算|要|将|长期|研究|方向)", q):
        return "goal"
    return "fact"


# ---------------------------------------------------------------------------
# 增量会话摘要
# ---------------------------------------------------------------------------

def get_recent_window_text(messages: Sequence) -> str:
    """把一组消息按"用户/助手"行化（供摘要输入）。"""
    lines: List[str] = []
    for m in messages:
        q = (getattr(m, "query", "") or "").strip()
        r = (getattr(m, "response", "") or "").strip()
        if q:
            lines.append(f"用户: {q}")
        if r:
            lines.append(f"助手: {r}")
    return "\n".join(lines)


def summarize_conversation(
    session: Session,
    owner_id: str,
    conversation_id: str,
    threshold: int = SUMMARY_THRESHOLD,
    summarizer: Callable[[str, str], str] = rule_based_summarizer,
    force: bool = False,
) -> Optional[session_context.SessionContext]:
    """更新会话私有上下文（含较早历史的增量摘要）。

    第二期把"摘要"扩展为"会话上下文"：主题、目标、已确认事项、限制条件、
    待解决问题 + 摘要正文，实现见 :mod:`session_context`。

    - 近期轮次（最近 threshold 条）保持原样，不并入摘要（设计方案 §4.1）；
    - 只摘要尚未覆盖的较早消息（用 summarized_message_id 定位）；
    - 用户手工编辑过的结论不被更早的内容静默覆盖（设计方案 §4.2）；
    - 临时对话不写入（设计方案 §4.4）；
    - 失败返回 None，不阻断聊天（调用方 try/except）。
    """
    return session_context.refresh_session_context(
        session, owner_id, conversation_id, threshold=threshold, force=force,
        summarizer=summarizer,
    )


def get_summary_text(
    session: Session, owner_id: str, conversation_id: str
) -> str:
    """取会话摘要文本（无则空串）。摘要仅短期上下文，不当长期事实。"""
    if not owner_id or not conversation_id:
        return ""
    try:
        row = summary_repo.get_session_summary(session, conversation_id, owner_id)
    except Exception:
        return ""
    return (row.summary or "").strip() if row else ""


def build_short_term_context(
    session: Optional[Session],
    owner_id: Optional[str],
    conversation_id: Optional[str],
    query: str,
    threshold: int = SUMMARY_THRESHOLD,
) -> str:
    """会话上下文注入段（主题/目标/已确认/限制/待解决 + 较早内容摘要）。

    近期原始轮次仍由聊天链路的 ``filter_message`` 提供，最近的关键消息始终
    以完整原文参与回答；这里只补"本会话已经讨论到哪里"，使重启后仍能延续，
    且与长期记忆、个人偏好分段清晰（设计方案 §5）。临时对话返回空串。
    """
    return session_context.build_context_segment(session, owner_id, conversation_id)


def memory_system_messages(
    session: Optional[Session],
    owner_id: Optional[str],
    query: str,
    conversation_id: Optional[str] = None,
    threshold: int = SUMMARY_THRESHOLD,
    use_memory: bool = True,
    project_id: Optional[str] = None,
) -> List[dict]:
    """普通聊天与 RAG 共用的一组"记忆系统消息"（置于近期轮次之前）。

    顺序（设计方案第三期 §5：个人偏好 + 当前会话摘要 + 0~数条相关历史记忆 +
    当前用户问题 + 知识库检索结果）：

    1. 个人偏好段（用户在"我的记忆"页保存、带 ``category`` 的记忆，按
       §5 优先级少量注入）；
    2. 相关历史记忆段（第三期：跨会话召回的已确认决策/背景/项目规则/待办，
       受用户级总开关与分项开关管辖，"本次不使用"的条目跳过）；
    3. 会话上下文段（``session_summary``：主题/目标/已确认事项/限制条件/
       待解决问题 + 较早内容摘要；仅当前会话有效，不当长期事实，临时对话
       不注入）。

    ``use_memory=False`` 对应界面上的"本次不使用个人偏好"：跳过上面 1、2 两段
    （即"我的记忆"页列出的内容本轮一律不用），会话摘要不受影响；只影响本次
    请求，不改动任何数据。用户级总开关关闭时效果相同，且对该用户全部会话生效。
    写入侧（自动提取、"请记住"）不受该开关影响——开关控制的是"用不用"。

    ``project_id`` 不传时由召回层取会话选定的项目（第三期 §4.4：项目范围记忆
    只在该项目空间内参与）。

    返回 ``[{"role": "system", "content": ...}, ...]``；无内容时为空列表。
    所有失败都吞掉（返回已构造的部分），绝不阻断聊天主流程。
    """
    msgs: List[dict] = []
    try:
        if use_memory and preferences_enabled(session, owner_id):
            prefs = format_preferences(select_preferences(session, owner_id, query))
            if prefs:
                msgs.append({"role": "system", "content": prefs})
        if use_memory:
            segment, _used = cross_session_recall.build_recall_segment(
                session, owner_id, query, conversation_id, project_id
            )
            if segment:
                msgs.append({"role": "system", "content": segment})
        short = build_short_term_context(
            session, owner_id, conversation_id, query, threshold
        )
        if short:
            msgs.append({"role": "system", "content": short})
    except Exception:
        # 记忆/摘要构造失败绝不阻断正常回答
        pass
    return msgs


__all__ = [
    "RECALL_MAX_COUNT",
    "RECALL_MAX_CHARS",
    "SUMMARY_THRESHOLD",
    "EXPLICIT_REQUIRES_CONFIRMATION",
    "MEMORY_SECTION_HEADER",
    "PREFERENCE_MAX_COUNT",
    "PREFERENCE_SECTION_HEADER",
    "PREFERENCE_CATEGORY_LABELS",
    "SENSITIVE_PATTERNS",
    "rule_based_summarizer",
    "recall_memories",
    "format_memory_context",
    "build_memory_context",
    "sensitive_reason",
    "preferences_enabled",
    "select_preferences",
    "format_preferences",
    "remember",
    "forget",
    "forget_match_plan",
    "matches_forget",
    "auto_extract",
    "summarize_conversation",
    "get_summary_text",
    "build_short_term_context",
    "memory_system_messages",
]
