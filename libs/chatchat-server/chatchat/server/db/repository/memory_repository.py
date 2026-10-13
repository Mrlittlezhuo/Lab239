"""用户记忆 Repository（owner-aware）。

所有读写都必须同时校验 ``memory.id`` 与 ``owner_id``；owner 不匹配或
记忆不存在时一律视为"不可见"（返回 None / 抛 ``MemoryNotOwned``），不
区分原因，避免记忆枚举。owner 来自服务端认证上下文，不接受客户端指定。
"""
from __future__ import annotations

import re
import uuid
from sqlalchemy import or_
from sqlalchemy.orm import Session

from chatchat.server.db.models.memory_model import MemoryModel

#: 记忆类型白名单
VALID_TYPES = ("preference", "fact", "goal", "important")

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
) -> MemoryModel:
    """新增一条记忆；owner 由服务端写入。"""
    if not owner_id:
        raise ValueError("owner_id 不能为空")
    content = (content or "").strip()
    if not content:
        raise ValueError("content 不能为空")
    if not memory_id:
        memory_id = uuid.uuid4().hex
    if not memory_key:
        memory_key = f"manual:{memory_id}"
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
) -> MemoryModel:
    """更新当前用户的一条记忆（owner 不匹配抛 MemoryNotOwned）。

    ``category`` 传空串表示"清除类别"（这条记忆不再是用户管理的个人偏好），
    不传（None）表示不改动。
    """
    m = get_owned_memory(session, memory_id, owner_id)
    if m is None:
        raise MemoryNotOwned(memory_id)
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
    """删除当前用户的一条记忆（owner 不匹配抛 MemoryNotOwned）。"""
    m = get_owned_memory(session, memory_id, owner_id)
    if m is None:
        raise MemoryNotOwned(memory_id)
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

    只按 ``owner_id`` 过滤，绝不跨用户删除；不删除会话、消息、会话摘要，
    也不改动知识库与向量索引。
    """
    if not owner_id:
        return 0
    deleted = (
        session.query(MemoryModel).filter(MemoryModel.owner_id == owner_id).delete()
    )
    session.commit()
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
    "create_memory",
    "list_memories",
    "list_preference_memories",
    "get_owned_memory",
    "update_memory",
    "disable_memory",
    "delete_memory",
    "delete_all_memories",
    "count_by_owner",
    "search_memories",
]
