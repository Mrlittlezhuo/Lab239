"""用户记忆 Repository（owner-aware）。

所有读写都必须同时校验 ``memory.id`` 与 ``owner_id``；owner 不匹配或
记忆不存在时一律视为"不可见"（返回 None / 抛 ``MemoryNotOwned``），不
区分原因，避免记忆枚举。owner 来自服务端认证上下文，不接受客户端指定。

智能聊天记忆第三期（跨会话召回）在本模块追加：记忆类别/范围/有效期/替代链
的读写、修订历史，以及会话内"本次不使用"的忽略记录。
"""
from __future__ import annotations

import datetime as _dt
import re
import uuid
from sqlalchemy import or_
from sqlalchemy.orm import Session

from chatchat.server.db.models.memory_model import MemoryModel
from chatchat.server.db.models.memory_recall_model import (
    ConversationMemoryIgnoreModel,
    MemoryRevisionModel,
)

#: 记忆类型白名单
VALID_TYPES = ("preference", "fact", "goal", "important")

#: 记忆类别白名单（智能聊天记忆第三期设计方案 §3）
VALID_KINDS = (
    "decision",  # 已确认决策
    "background",  # 稳定背景
    "project_rule",  # 项目规则
    "conclusion",  # 历史结论
    "todo",  # 待办与目标
    "fact",  # 一般记忆（旧数据默认值）
)

#: 记忆范围白名单（设计方案 §4.4）
VALID_SCOPES = ("personal", "project")

#: 自动提取的来源标记：这些内容不是用户明确确认的结论（设计方案 §3）。
#: 其余来源（user/manual/chat/session…）都视为用户自己给出的内容。
AUTO_SOURCES = ("auto_extract", "auto", "extract")

#: 个人偏好类别白名单（智能聊天记忆第一期）。``category`` 为空表示"不是用户
#: 管理的个人偏好"（例如自动提取的长期事实），因此旧数据无需回填。
VALID_CATEGORIES = (
    "answer_style",
    "technical_background",
    "work_style",
    "long_term_goal",
)


class MemoryNotOwned(Exception):
    """记忆不存在或不属于指定用户（调用方映射 404）。"""


def _normalize_type(mtype: str) -> str:
    mtype = (mtype or "").strip().lower()
    return mtype if mtype in VALID_TYPES else "fact"


def _normalize_category(category) -> str | None:
    """个人偏好类别规范化：空→None（不是个人偏好），未知→ValueError。

    与 ``_normalize_type`` 不同，这里**不静默回落**：类别决定注入优先级和
    管理页分组，写错类别会让用户的偏好落到错误的分组里且难以发现。
    """
    if category is None:
        return None
    value = str(category).strip().lower()
    if not value:
        return None
    if value not in VALID_CATEGORIES:
        raise ValueError(f"未知的个人偏好类别：{category!r}")
    return value


def _normalize_importance(importance) -> int:
    try:
        v = int(importance)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, v))


def _normalize_kind(kind) -> str:
    """记忆类别规范化：空→ ``fact``（一般记忆），未知→ ValueError。

    与 ``_normalize_category`` 同理：类别决定它落在哪个分组、受哪个开关
    管辖，静默回落会让用户的记忆落到看不见的桶里。
    """
    if kind is None:
        return "fact"
    value = str(kind).strip().lower()
    if not value:
        return "fact"
    if value not in VALID_KINDS:
        raise ValueError(f"未知的记忆类别：{kind!r}")
    return value


def _normalize_scope(scope) -> str:
    if scope is None:
        return "personal"
    value = str(scope).strip().lower()
    if not value:
        return "personal"
    if value not in VALID_SCOPES:
        raise ValueError(f"未知的记忆范围：{scope!r}")
    return value


def _confirmed_by_source(source) -> bool:
    """按来源判断"是否用户明确确认"（设计方案 §3：未确认内容不跨会话召回）。

    自动提取（auto_extract/auto/extract）是系统的猜测，默认不算已确认；
    用户在管理页写入、在聊天里明确"请记住"、或经确认卡片保存的内容都算。
    """
    return str(source or "").strip().lower() not in AUTO_SOURCES


def _normalize_expires_at(value):
    """有效期规范化：接受 ISO 字符串或 datetime；空→ None，非法→ ValueError。"""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"未知的有效期格式：{value!r}，应形如 2026-12-31")


def create_memory(
    session: Session,
    owner_id: str,
    content: str,
    mtype: str = "fact",
    source: str = "chat",
    importance: int = 1,
    enabled: bool = True,
    memory_id: str | None = None,
    memory_key: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
    category: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
    confirmed: bool | None = None,
    expires_at=None,
    supersedes: str | None = None,
) -> MemoryModel:
    """新增一条记忆；owner 由服务端写入。

    ``confirmed=None`` 时按来源推断（``_confirmed_by_source``）：自动提取的
    内容不算已确认，因此默认落在"一般历史会话结论"桶里，需要用户主动开启
    该开关才会被召回（设计方案 §3、§4.1）。
    """
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if not memory_id:
        memory_id = uuid.uuid4().hex
    if not memory_key:
        memory_key = f"manual:{memory_id}"
    scope_value = _normalize_scope(scope)
    m = MemoryModel(
        id=memory_id,
        owner_id=owner_id,
        memory_key=memory_key[:128],
        type=_normalize_type(mtype),
        category=_normalize_category(category),
        content=content,
        source=source,
        source_conversation_id=source_conversation_id,
        source_message_id=source_message_id,
        importance=_normalize_importance(importance),
        enabled=bool(enabled),
        kind=_normalize_kind(kind),
        scope=scope_value,
        project_id=project_id if scope_value == "project" else None,
        project_name=project_name if scope_value == "project" else None,
        confirmed=_confirmed_by_source(source) if confirmed is None else bool(confirmed),
        expires_at=_normalize_expires_at(expires_at),
        supersedes=supersedes,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def list_memories(
    session: Session, owner_id: str, enabled_only: bool = False
) -> list[MemoryModel]:
    """当前用户的记忆列表（update_time DESC，无时间者按 id 兜底）。"""
    q = session.query(MemoryModel).filter(MemoryModel.owner_id == owner_id)
    if enabled_only:
        q = q.filter(MemoryModel.enabled.is_(True))
    return q.order_by(
        MemoryModel.update_time.desc(), MemoryModel.id.desc()
    ).all()


def get_owned_memory(
    session: Session, memory_id: str, owner_id: str
) -> MemoryModel | None:
    """同时按 id + owner_id 过滤；不满足返回 None。"""
    if not memory_id or not owner_id:
        return None
    return (
        session.query(MemoryModel)
        .filter(MemoryModel.id == memory_id, MemoryModel.owner_id == owner_id)
        .one_or_none()
    )


def update_memory(
    session: Session,
    memory_id: str,
    owner_id: str,
    *,
    content: str | None = None,
    mtype: str | None = None,
    importance: int | None = None,
    enabled: bool | None = None,
    source: str | None = None,
    memory_key: str | None = None,
    source_conversation_id: str | None = None,
    source_message_id: str | None = None,
    category: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    project_id: str | None = None,
    project_name: str | None = None,
    confirmed: bool | None = None,
    expires_at=None,
    superseded_by: str | None = None,
    supersedes: str | None = None,
    record_revision: bool = True,
) -> MemoryModel:
    """更新当前用户的一条记忆（owner 不匹配抛 MemoryNotOwned）。

    ``category`` 传空串表示"清除类别"（这条记忆不再是用户管理的个人偏好），
    不传（None）表示不改动。

    内容/类别/范围/有效期发生变化时先把旧值写进 ``memory_revision``，
    使"编辑保留修订历史"与"查看完整修订链"成为服务端保证（设计方案 §4.2、
    §4.5）；只切换启用状态等元数据不产生修订记录。
    """
    m = get_owned_memory(session, memory_id, owner_id)
    if m is None:
        raise MemoryNotOwned(memory_id)
    tracked_changed = any(
        v is not None
        for v in (content, kind, scope, project_id, project_name, expires_at)
    )
    if tracked_changed and record_revision:
        add_revision(
            session,
            m,
            reason="edit",
            commit=False,
        )
    if content is not None:
        content = content.strip()
        if not content:
            raise ValueError("content 不能为空")
        m.content = content
    if mtype is not None:
        m.type = _normalize_type(mtype)
    if category is not None:
        m.category = _normalize_category(category)
    if importance is not None:
        m.importance = _normalize_importance(importance)
    if enabled is not None:
        m.enabled = bool(enabled)
    if source is not None:
        m.source = source
    if memory_key is not None:
        m.memory_key = memory_key[:128]
    if source_conversation_id is not None:
        m.source_conversation_id = source_conversation_id
    if source_message_id is not None:
        m.source_message_id = source_message_id
    if kind is not None:
        m.kind = _normalize_kind(kind)
    if scope is not None:
        m.scope = _normalize_scope(scope)
    # 空串表示"清除"（与 category 的约定一致），None 表示不改动
    if project_id is not None:
        m.project_id = project_id or None
    if project_name is not None:
        m.project_name = project_name or None
    if confirmed is not None:
        m.confirmed = bool(confirmed)
    if expires_at is not None:
        m.expires_at = _normalize_expires_at(expires_at)
    if superseded_by is not None:
        m.superseded_by = superseded_by or None
    if supersedes is not None:
        m.supersedes = supersedes or None
    # 范围改回个人后，项目归属必须一起清掉：否则一条个人记忆仍带着
    # project_id，会在该项目空间里被再次召回（设计方案 §4.4 / P3-09）。
    if m.scope == "personal":
        m.project_id = None
        m.project_name = None
    session.commit()
    session.refresh(m)
    return m


def disable_memory(
    session: Session, memory_id: str, owner_id: str, enabled: bool = False
) -> MemoryModel:
    """停用/启用当前用户的一条记忆。"""
    return update_memory(
        session, memory_id, owner_id, enabled=enabled
    )


def delete_memory(session: Session, memory_id: str, owner_id: str) -> None:
    """删除当前用户的一条记忆（owner 不匹配抛 MemoryNotOwned）。

    一并删除它的修订历史与各会话里的"本次不使用"记录：记忆本体都没了，
    留着这些指针只会成为无主数据（设计方案 §4.2：删除=彻底移除）。
    """
    m = get_owned_memory(session, memory_id, owner_id)
    if m is None:
        raise MemoryNotOwned(memory_id)
    session.query(MemoryRevisionModel).filter(
        MemoryRevisionModel.memory_id == memory_id,
        MemoryRevisionModel.owner_id == owner_id,
    ).delete()
    session.query(ConversationMemoryIgnoreModel).filter(
        ConversationMemoryIgnoreModel.memory_id == memory_id,
        ConversationMemoryIgnoreModel.owner_id == owner_id,
    ).delete()
    session.delete(m)
    session.commit()


def count_by_owner(session: Session, owner_id: str) -> int:
    """统计某用户记忆条数（测试/审计用）。"""
    if not owner_id:
        return 0
    return (
        session.query(MemoryModel)
        .filter(MemoryModel.owner_id == owner_id)
        .count()
    )


def list_preference_memories(session: Session, owner_id: str) -> list[MemoryModel]:
    """当前用户"已启用 + 带个人偏好类别"的记忆（个人偏好注入/展示的唯一入口）。

    只取 ``category`` 非空的行：自动提取的长期事实不属于第一期用户在管理页
    管理的个人偏好。排序与数量上限由 ``memory_service.select_preferences``
    按优先级决定，本函数只负责 owner 过滤与启用过滤。
    """
    if not owner_id:
        return []
    return (
        session.query(MemoryModel)
        .filter(
            MemoryModel.owner_id == owner_id,
            MemoryModel.enabled.is_(True),
            # 白名单之外的类别（例如直接改库留下的值）不算个人偏好，
            # 否则注入优先级和分组都会落到未定义的行为上。
            MemoryModel.category.in_(VALID_CATEGORIES),
        )
        .order_by(MemoryModel.importance.desc(), MemoryModel.update_time.desc())
        .all()
    )


def delete_all_memories(session: Session, owner_id: str) -> int:
    """删除当前用户的全部记忆，返回删除条数（清空全部个人记忆）。

    只按 ``owner_id`` 过滤，绝不跨用户删除；同时清掉该用户的修订历史与
    "本次不使用"记录；不删除会话、消息、会话摘要，也不改动知识库与向量索引。
    """
    if not owner_id:
        return 0
    deleted = (
        session.query(MemoryModel).filter(MemoryModel.owner_id == owner_id).delete()
    )
    session.query(MemoryRevisionModel).filter(
        MemoryRevisionModel.owner_id == owner_id
    ).delete()
    session.query(ConversationMemoryIgnoreModel).filter(
        ConversationMemoryIgnoreModel.owner_id == owner_id
    ).delete()
    session.commit()
    return int(deleted)


# ---------------------------------------------------------------------------
# 修订历史（设计方案 §4.2 编辑留痕、§4.5 完整修订链）
# ---------------------------------------------------------------------------

def add_revision(
    session: Session, memory: MemoryModel, *, reason: str = "edit", commit: bool = True
) -> MemoryRevisionModel:
    """把一条记忆的**当前**值快照成历史版本（在改动之前调用）。"""
    revision = MemoryRevisionModel(
        id=uuid.uuid4().hex,
        memory_id=memory.id,
        owner_id=memory.owner_id,
        content=memory.content,
        kind=memory.kind or "",
        scope=memory.scope or "",
        project_id=memory.project_id,
        project_name=memory.project_name,
        expires_at=memory.expires_at,
        reason=reason,
    )
    session.add(revision)
    if commit:
        session.commit()
        session.refresh(revision)
    return revision


def list_revisions(
    session: Session, memory_id: str, owner_id: str
) -> list[MemoryRevisionModel]:
    """某条记忆的修订记录（新→旧）。owner 不匹配一律为空，不区分原因。"""
    if not memory_id or not owner_id:
        return []
    return (
        session.query(MemoryRevisionModel)
        .filter(
            MemoryRevisionModel.memory_id == memory_id,
            MemoryRevisionModel.owner_id == owner_id,
        )
        .order_by(MemoryRevisionModel.create_time.desc(), MemoryRevisionModel.id.desc())
        .all()
    )


def mark_superseded(
    session: Session, memory_id: str, owner_id: str, *, by_id: str
) -> MemoryModel | None:
    """把旧记忆标记为"已被替代"（设计方案 §4.5）。

    只停用注入，不删除记录：用户仍能在"我的记忆"页看到它以及完整修订链。
    同时留下修订记录，便于回看替代发生的时间。
    """
    m = get_owned_memory(session, memory_id, owner_id)
    if m is None:
        return None
    if m.superseded_by == by_id:
        return m
    add_revision(session, m, reason="supersede", commit=False)
    m.superseded_by = by_id
    session.commit()
    session.refresh(m)
    return m


# ---------------------------------------------------------------------------
# 跨会话召回候选与会话内忽略（设计方案 §4.1、§4.2）
# ---------------------------------------------------------------------------

def list_recallable_memories(
    session: Session, owner_id: str, *, now: _dt.datetime | None = None
) -> list[MemoryModel]:
    """当前用户"可能被召回"的候选：启用、未被替代、未过期。

    桶归属（已确认 / 一般 / 项目）与开关判断在 ``cross_session_recall`` 里做，
    这里只排除设计方案 §3 明确禁止的三类：已停用、已被替代、已过期。
    过期只停止召回，**不删除也不改动记录**。
    """
    if not owner_id:
        return []
    now = now or _dt.datetime.now()
    rows = (
        session.query(MemoryModel)
        .filter(
            MemoryModel.owner_id == owner_id,
            MemoryModel.enabled.is_(True),
            or_(
                MemoryModel.superseded_by.is_(None),
                MemoryModel.superseded_by == "",
            ),
        )
        .order_by(MemoryModel.update_time.desc(), MemoryModel.id.desc())
        .all()
    )
    return [m for m in rows if not is_expired(m, now)]


def is_expired(memory: MemoryModel, now: _dt.datetime | None = None) -> bool:
    """有效期判断（无 ``expires_at`` 视为长期有效）。

    SQLite 里 ``DateTime`` 读回来可能是 naive，也可能带时区；统一去掉时区
    再比较，避免"到期后仍被召回"或"从未到期"这类边界抖动。
    """
    expires = getattr(memory, "expires_at", None)
    if not expires:
        return False
    now = now or _dt.datetime.now()
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return expires <= now


def ignore_memory(
    session: Session, conversation_id: str, owner_id: str, memory_id: str
) -> None:
    """记录"本次不使用"：只让该记忆在这个会话里不被召回。

    必须同时校验会话与记忆都属于当前用户（owner 来自认证上下文），否则
    一个用户可以往别人的会话里塞忽略记录。重复写入幂等。
    """
    from chatchat.server.db.repository.conversation_repository import (
        get_owned_conversation,
    )

    if not conversation_id or not memory_id or not owner_id:
        raise ValueError("conversation_id / memory_id / owner_id 不能为空")
    if get_owned_conversation(session, conversation_id, owner_id) is None:
        raise MemoryNotOwned(conversation_id)
    if get_owned_memory(session, memory_id, owner_id) is None:
        raise MemoryNotOwned(memory_id)
    exists = (
        session.query(ConversationMemoryIgnoreModel)
        .filter(
            ConversationMemoryIgnoreModel.conversation_id == conversation_id,
            ConversationMemoryIgnoreModel.memory_id == memory_id,
        )
        .one_or_none()
    )
    if exists is not None:
        return
    session.add(
        ConversationMemoryIgnoreModel(
            conversation_id=conversation_id,
            memory_id=memory_id,
            owner_id=owner_id,
        )
    )
    session.commit()


def unignore_memory(
    session: Session, conversation_id: str, owner_id: str, memory_id: str
) -> bool:
    """取消"本次不使用"（只作用于该会话）。返回是否删掉了记录。"""
    deleted = (
        session.query(ConversationMemoryIgnoreModel)
        .filter(
            ConversationMemoryIgnoreModel.conversation_id == conversation_id,
            ConversationMemoryIgnoreModel.memory_id == memory_id,
            ConversationMemoryIgnoreModel.owner_id == owner_id,
        )
        .delete()
    )
    session.commit()
    return bool(deleted)


def list_ignored_memory_ids(
    session: Session, conversation_id: str, owner_id: str
) -> set[str]:
    """该会话里被"本次不使用"的记忆 id（越权/无会话返回空集）。"""
    if not conversation_id or not owner_id:
        return set()
    rows = (
        session.query(ConversationMemoryIgnoreModel.memory_id)
        .filter(
            ConversationMemoryIgnoreModel.conversation_id == conversation_id,
            ConversationMemoryIgnoreModel.owner_id == owner_id,
        )
        .all()
    )
    return {r[0] for r in rows}


def clear_ignored_memory_ids(session: Session, conversation_id: str) -> int:
    """会话删除时清理其忽略记录（避免留下无主指针）。"""
    if not conversation_id:
        return 0
    deleted = (
        session.query(ConversationMemoryIgnoreModel)
        .filter(ConversationMemoryIgnoreModel.conversation_id == conversation_id)
        .delete()
    )
    return int(deleted)


def search_memories(
    session: Session,
    owner_id: str,
    query: str,
    limit: int = 5,
    uncategorized_only: bool = False,
) -> list[MemoryModel]:
    """按用户召回相关记忆（结构化 + 关键词，不依赖向量/Embedding）。

    - 只召回 enabled=True 且属于 owner 的记忆；
    - 相关度由"命中关键词数量" + 类型/重要性加成排序；
    - 结果数量与内容长度由调用方（``memory_service``）限制；
    - ``uncategorized_only=True`` 时排除带个人偏好类别的行：个人偏好只经
      ``select_preferences`` 一条通道注入，避免同一条记忆在两个上下文段里
      重复出现，也保证关闭个人记忆时它不会被"顺带"注入。
    """
    if not owner_id or limit <= 0:
        return []
    query = (query or "").strip()
    base = (
        session.query(MemoryModel)
        .filter(MemoryModel.owner_id == owner_id, MemoryModel.enabled.is_(True))
    )
    if uncategorized_only:
        # 空串是历史/手工数据的可能取值，与 NULL 同等对待
        base = base.filter(
            or_(MemoryModel.category.is_(None), MemoryModel.category == "")
        )
    if not query:
        # 无查询时按重要性/更新时间取少量（兜底）
        return (
            base.order_by(MemoryModel.importance.desc(), MemoryModel.update_time.desc())
            .limit(limit)
            .all()
        )
    # 用户级记忆量通常很小。先取当前用户全部启用记忆，再结合稳定主题键与
    # 文本关键词排序，才能支持“研究方向”→“无线通信”这种跨表达召回。
    keywords = _keyword_candidates(query)
    candidates = base.all()
    ranked = _rank_memories(candidates, query, keywords)
    return ranked[:limit]


def _keyword_candidates(query: str, max_keywords: int = 8) -> list[str]:
    """把问题拆成可匹配的关键词（中英文简单切分），去重、限量。"""
    import re

    tokens = re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", query or "")
    out: list[str] = []
    seen = set()
    for t in tokens:
        t = t.strip()
        if not t or t in seen:
            continue
        # 中文长串按 2 字滑窗再拆，提升命中率
        parts = [t]
        if re.fullmatch(r"[一-鿿]+", t) and len(t) > 2:
            parts = [t[i:i + 2] for i in range(len(t) - 1)] + [t]
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                out.append(p)
                if len(out) >= max_keywords:
                    return out
    return out


def _rank_memories(
    candidates: list[MemoryModel], query: str, keywords: list[str]
) -> list[MemoryModel]:
    """综合相关度（命中关键词数）+ 类型/重要性加权排序。"""
    # 类型权重：目标/偏好略高，普通事实次之
    type_weight = {"important": 2.0, "goal": 1.5, "preference": 1.5, "fact": 1.0}
    key_hints = _query_key_hints(query)
    scored = []
    for m in candidates:
        content = (m.content or "").lower()
        memory_key = (m.memory_key or "").lower()
        hits = sum(1 for kw in keywords if kw.lower() in content)
        q_in_content = (query or "").lower()
        q_hits = sum(1 for kw in _keyword_candidates(q_in_content) if kw in content)
        key_hits = sum(
            1 for hint in key_hints
            if memory_key == hint or memory_key.startswith(f"{hint}:")
        )
        relevance = hits + q_hits * 0.5 + key_hits * 6
        score = relevance * type_weight.get(m.type, 1.0) + (m.importance or 1) * 0.1
        # 非空问题只返回真正相关的候选；“关于我/你记得我吗”允许按重要性兜底。
        if relevance > 0 or _is_general_memory_query(query):
            scored.append((score, m))
    scored.sort(key=lambda x: (-x[0], x[1].update_time is None, x[1].id))
    return [m for _, m in scored]


def _query_key_hints(query: str) -> list[str]:
    """把常见问法映射到稳定记忆主题，弥补字面关键词不重合。"""
    q = (query or "").lower()
    hints: list[str] = []
    rules = (
        (r"名字|叫什么|称呼", "profile:name"),
        (r"研究|方向|专业", "profile:research"),
        (r"工作|职业|公司", "profile:work"),
        (r"身份|我是(谁|什么)", "profile:identity"),
        (r"回答|回复|风格|简洁|详细|语言", "preference:response_style"),
        (r"偏好|喜欢|习惯|讨厌", "preference"),
        (r"目标|计划|打算", "goal"),
        (r"项目", "profile:project"),
    )
    for pattern, key in rules:
        if re.search(pattern, q) and key not in hints:
            hints.append(key)
    return hints


def _is_general_memory_query(query: str) -> bool:
    return bool(re.search(r"你还记得我|关于我|我的信息|了解我", query or ""))


__all__ = [
    "MemoryNotOwned",
    "VALID_TYPES",
    "VALID_CATEGORIES",
    "VALID_KINDS",
    "VALID_SCOPES",
    "AUTO_SOURCES",
    "create_memory",
    "list_memories",
    "list_preference_memories",
    "list_recallable_memories",
    "is_expired",
    "get_owned_memory",
    "update_memory",
    "disable_memory",
    "mark_superseded",
    "add_revision",
    "list_revisions",
    "ignore_memory",
    "unignore_memory",
    "list_ignored_memory_ids",
    "clear_ignored_memory_ids",
    "delete_memory",
    "delete_all_memories",
    "count_by_owner",
    "search_memories",
]
