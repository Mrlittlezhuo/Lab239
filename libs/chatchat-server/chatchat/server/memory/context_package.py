"""统一上下文构建器（智能聊天记忆第四期·第一阶段 A，设计方案 §6.2、§18.2.2-3）。

一份数据、一个分界。普通聊天、知识库对话和 Agent 都从这里取"最近原文窗口 +
会话上下文 + 个人偏好 + 召回记忆"，不再各自查历史、各自算窗口、各自拼注入段
——那正是"界面显示的记忆"和"真正注入的记忆"能各说一套的根源。

关键规则（设计方案 §6.2）：

- **摘要覆盖范围 + 最近原文窗口 = 当前有效会话历史**：分界只有一处，就是
  ``boundary_message_id``（窗口第一条消息）。它之前的消息必须已经进摘要，
  不允许出现"既没进摘要、也不在最近原文"的中间消息。
- 窗口按**字符预算**从最新一轮往前取，不拆散完整问答轮；``HISTORY_LEN`` 只
  作为最小轮数，``SUMMARY_THRESHOLD`` 只作为摘要触发阈值，都不再定义窗口。
- 超限优先移除最旧完整轮次；单轮就超预算时保留最新一轮并标记
  ``history_truncated``，绝不整段丢掉当前问题。
- 摘要没跟上（失败）时宁可把窗口往前扩，也不让历史在 Prompt 里凭空消失；
  扩不动就标记 ``history_gap_due_to_degradation``，让界面和日志如实看到降级。

第一阶段的清单快照：``preference_ids`` / ``recalled_memory_ids`` /
``summary_version`` / 窗口消息 ID 现在就一并算好，第四阶段写"生成使用清单"
时直接复用，不必回头重查或重写三条链路（§18.2.3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from chatchat.server.db.repository import conversation_repository as conv_repo
from chatchat.server.db.repository.message_repository import (
    list_context_messages,
    list_messages,
)
from chatchat.server.memory import session_context
from chatchat.server.memory.session_context import RecentWindow, select_recent_window

#: 摘要掉队时窗口最多扩到预算的多少倍（再多就宁可标记降级也不撑爆上下文）
GAP_WINDOW_FACTOR = 2


@dataclass
class ContextPackage:
    """一次回答要用到的全部上下文，以及它的来源快照。"""

    owner_id: str = ""
    conversation_id: str = ""
    #: 无记忆对话（现界面名"临时对话"）：会话与原始消息照常持久化、最近原文照读，
    #: 但不生成会话上下文/摘要，也不写长期记忆（设计方案 §13.1）
    temporary: bool = False
    #: 最近原文窗口内的消息（MessageModel，升序）
    recent_messages: List = field(default_factory=list)
    window: RecentWindow = field(default_factory=RecentWindow)
    #: 唯一分界：窗口第一条消息的 ID；它之前的消息必须已被摘要覆盖
    boundary_message_id: Optional[str] = None
    #: 会话上下文（主题/目标/已确认/限制/待解决/已完成 + 较早内容摘要）
    context: Optional[session_context.SessionContext] = None
    #: 窗口被字符预算截断（单轮超预算，或最小轮数让位于硬上限）
    history_truncated: bool = False
    #: 摘要没跟上，窗口被迫扩大或历史确实缺段（§6.2 如实标记）
    history_gap_due_to_degradation: bool = False
    #: 本次使用的个人偏好 / 召回记忆 ID 快照（生成使用清单直接复用）
    preference_ids: List[str] = field(default_factory=list)
    recalled_memory_ids: List[str] = field(default_factory=list)
    #: 记忆注入段（个人偏好段 + 跨会话召回段），已按开关裁剪
    memory_segments: List[str] = field(default_factory=list)
    #: 会话上下文段
    context_segment: str = ""

    def history_pairs(self) -> List[tuple]:
        """窗口内消息转成 ``(query, response)`` 升序列表。"""
        pairs = []
        for m in self.recent_messages:
            pairs.append((getattr(m, "query", "") or "", getattr(m, "response", "") or ""))
        return pairs

    def history_message_ids(self) -> List[str]:
        return [m.id for m in self.recent_messages if getattr(m, "id", None)]

    def system_segments(self) -> List[str]:
        """注入用的系统段顺序：偏好 → 召回记忆 → 本会话上下文。"""
        return [s for s in (self.memory_segments + [self.context_segment]) if s]

    def system_messages(self) -> List[dict]:
        """转成聊天链路直接可用的系统消息。"""
        return [{"role": "system", "content": s} for s in self.system_segments()]

    def is_empty(self) -> bool:
        return not self.recent_messages and not self.system_segments()


def build_context_package(
    session: Optional[Session],
    owner_id: Optional[str],
    conversation_id: Optional[str],
    query: str = "",
    *,
    use_memory: bool = True,
    project_id: Optional[str] = None,
    max_chars: Optional[int] = None,
    min_turns: Optional[int] = None,
    refresh: bool = True,
    summarizer=None,
) -> ContextPackage:
    """构造本轮回答的统一上下文。

    ``refresh=True`` 时先按同一分界更新会话上下文（摘要 + 槽位），再取注入段：
    被移出窗口的内容在同一次构建里进入摘要，不会这一轮两头都没有（§6.2 第 4
    条）。界面预览"本次会用到什么"时传 ``refresh=False``，只读不写。

    无记忆对话（现界面名"临时对话"）只跳过会话上下文：会话与原始消息照常
    持久化，最近原文窗口与记忆段照旧给出（设计方案 §13.1）。

    任何一步失败都退回"更少但正确"的结果，绝不阻断聊天主流程。
    """
    package = ContextPackage(owner_id=owner_id or "", conversation_id=conversation_id or "")
    if session is None or not owner_id:
        return package

    # 没有会话（新会话首轮、界面按问题预览记忆）时仍然要给个人偏好与召回记忆；
    # 无记忆对话（现界面名"临时对话"）不生成会话上下文，但会话与原始消息照常
    # 持久化，最近原文窗口照读（设计方案 §13.1）。
    if not conversation_id:
        return _finish(package, session, query, use_memory, project_id)
    try:
        package.temporary = bool(
            conv_repo.conversation_is_temporary(session, conversation_id, owner_id)
        )
    except Exception:
        package.temporary = False

    # 两份升序列表：全部消息（摘要与槽位的扫描面）与有回答的消息（进 Prompt 的
    # 最近原文窗口）。取消/失败的轮次没有助手回答，不该作为助手说法回放，但它
    # 的用户原话仍然参与摘要与槽位——所以扫描面不能等于窗口面，分界只能用
    # 消息 ID 表达。
    try:
        scan_rows = list_messages(session, conversation_id, owner_id)
        history_rows = list_context_messages(session, conversation_id, owner_id)
    except Exception:
        return _finish(package, session, query, use_memory, project_id)

    window = select_recent_window(
        history_rows, max_chars=max_chars, min_turns=min_turns
    )
    package.window = window
    package.history_truncated = window.truncated
    boundary_id = history_rows[window.start_index].id if history_rows else None
    package.boundary_message_id = boundary_id

    # 无记忆对话不生成会话上下文：不刷新、也不回读一条旧记录（它本来就没有）。
    context = None
    if refresh and not package.temporary:
        try:
            context = session_context.refresh_session_context(
                session,
                owner_id,
                conversation_id,
                summarizer=summarizer,
                boundary_message_id=boundary_id,
                messages=scan_rows,
            )
        except Exception:
            context = None
    if context is None and not package.temporary:
        try:
            context = session_context.load_context(session, owner_id, conversation_id)
        except Exception:
            context = None
    package.context = context

    # --- 无空洞不变量：分界之前的消息必须已被摘要覆盖（§6.2）---
    # 无记忆对话本来就没有摘要，分界之前的消息缺失属设计如此，不算降级。
    positions = {m.id: i for i, m in enumerate(scan_rows)}
    boundary_pos = positions.get(boundary_id, -1) if boundary_id else -1
    covered_id = context.summarized_message_id if context is not None else None
    covered_pos = positions.get(covered_id, -1) if covered_id else -1
    if not package.temporary and window.start_index > 0 and covered_pos < boundary_pos - 1:
        package.history_gap_due_to_degradation = True
        budget, _floor = session_context.recent_history_budget()
        # 摘要没跟上：宁可把窗口往前扩（多带几条原文），也不让历史在 Prompt 里
        # 凭空消失；扩到超过 2 倍预算就只标记降级，不撑爆上下文。
        first_uncovered = max(0, covered_pos + 1)
        widened = [m for m in history_rows if positions.get(m.id, 0) >= first_uncovered]
        span = sum(session_context.message_cost(m) for m in widened)
        if widened and span <= budget * GAP_WINDOW_FACTOR:
            package.recent_messages = widened
            package.window.start_index = len(history_rows) - len(widened)
            package.boundary_message_id = widened[0].id
            return _finish(package, session, query, use_memory, project_id)

    package.recent_messages = history_rows[window.start_index:]
    return _finish(package, session, query, use_memory, project_id)


def _finish(
    package: ContextPackage,
    session: Session,
    query: str,
    use_memory: bool,
    project_id: Optional[str],
) -> ContextPackage:
    """记忆段与会话上下文段（两条分支共用，避免复制开关逻辑）。"""
    # 延迟导入：memory_service / cross_session_recall 反过来会用到本模块。
    from chatchat.server.memory import cross_session_recall, memory_service

    owner_id = package.owner_id
    conversation_id = package.conversation_id
    context = package.context

    # --- 记忆段：开关裁剪与界面同源（§12.3 只允许下层收紧）---
    try:
        if use_memory and memory_service.preferences_enabled(session, owner_id):
            prefs = memory_service.select_preferences(session, owner_id, query)
            package.preference_ids = [p.id for p in prefs if getattr(p, "id", None)]
            segment = memory_service.format_preferences(prefs)
            if segment:
                package.memory_segments.append(segment)
        if use_memory:
            recall_segment, used = cross_session_recall.build_recall_segment(
                session, owner_id, query, conversation_id, project_id
            )
            package.recalled_memory_ids = [
                r.memory.id for r in used if getattr(r.memory, "id", None)
            ]
            if recall_segment:
                package.memory_segments.append(recall_segment)
    except Exception:
        pass

    try:
        package.context_segment = (
            session_context.format_context(context) if context is not None else ""
        )
    except Exception:
        package.context_segment = ""
    return package


def recent_boundary_message_id(
    session: Optional[Session],
    owner_id: Optional[str],
    conversation_id: Optional[str],
    *,
    max_chars: Optional[int] = None,
    min_turns: Optional[int] = None,
) -> Optional[str]:
    """只算最近原文窗口的分界消息 ID，不构造完整的包。

    给"只要分界"的调用方（如 Agent 运行先取历史、再单独更新摘要）用，保证
    它们与完整构建器用的是同一条规则（设计方案 §6.2）。
    """
    if session is None or not owner_id or not conversation_id:
        return None
    try:
        rows = list_context_messages(session, conversation_id, owner_id)
    except Exception:
        return None
    if not rows:
        return None
    window = select_recent_window(rows, max_chars=max_chars, min_turns=min_turns)
    return rows[window.start_index].id


__all__ = [
    "ContextPackage",
    "build_context_package",
    "recent_boundary_message_id",
]
