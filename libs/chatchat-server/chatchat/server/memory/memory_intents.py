"""“记住 / 忘记”的确认卡片（智能聊天记忆第二期设计方案 §4.3）。

用户明确说"记住：…""以后默认…""忘记：…"时，系统**不直接写入**，而是先返回
一张待确认卡片：内容 + 推荐保存范围 + 将被删除的对象，由用户点"确认保存"
或"不保存"。因此：

- 检测与写入是两步：``detect_intents`` 只读，``apply_intent`` 才写；
- 知识库文档片段、模型回答不会触发这里——检测只看用户本轮原话（P2-12）；
- 范围推荐按措辞给默认值，含义不明确时**不预选**，要求用户选（P2-07）；
- "忘记"必须列出将被停用的记忆或被移除的会话条目，避免误删（P2-09）；
- 临时对话不写入任何记忆，包括跨会话个人偏好（P2-11）。

范围与第一期的关系：``preference`` 写入 ``memory`` 表（带类别，跨会话注入），
``session`` 写入会话上下文（仅本会话，第二期新增）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from chatchat.server.db.repository import memory_repository as memory_repo
from chatchat.server.memory import session_context
from chatchat.server.memory.memory_service import (
    _strip_forget_prefix,
    _strip_intent_prefix,
    sensitive_reason,
)

SCOPE_SESSION = "session"
SCOPE_PREFERENCE = "preference"
VALID_SCOPES = (SCOPE_SESSION, SCOPE_PREFERENCE)

SCOPE_LABELS = {
    SCOPE_SESSION: "仅本会话",
    SCOPE_PREFERENCE: "作为跨会话个人偏好",
}

#: 措辞指向"当前这次"→ 默认仅本会话（设计方案 §4.3 默认范围表）
_SESSION_HINT_RE = re.compile(
    r"(本次|这次|这轮|本会话|这个会话|当前项目|这个项目|暂时|今天|这里|本轮)"
)
#: 措辞指向"长期"→ 默认跨会话个人偏好
_PREFERENCE_HINT_RE = re.compile(
    r"(以后|今后|长期|一直|习惯|偏好|默认|每次|总是|从现在起|后续|一贯)"
)
#: 设计方案 §4.3 的第二个例子是裸的"以后默认中文回答"——没有"记住"这个动词，
#: 但表达的是长期要求，同样要出确认卡片（P2-08 要求这条输入能存成偏好）。
#: 必须带"以后/长期/每次/默认…"这类前瞻措辞，避免把普通叙述（"以后再说"）
#: 或第一期的隐式提取（"我偏好简洁中文"）也变成卡片。
_STANDING_PREF_RE = re.compile(
    r"(以后|今后|从今以后|从现在起|长期|一贯|一直|每次|总是|默认)"
    r"[^。！？\n]{0,12}"
    r"(使用|用|回答|回复|按|简洁|精简|详细|展开|中文|英文|日文|优先|不要|不用|都)"
)

#: 个人偏好类别推断（第一期的四类白名单）
_CATEGORY_RULES: tuple[tuple[str, re.Pattern], ...] = (
    (
        "answer_style",
        re.compile(
            r"(中文|英文|英语|日文|日语|简洁|精简|简短|详细|展开|语气|风格|表格|列表|"
            r"段落|回答|回复|解释|术语|markdown)",
            re.IGNORECASE,
        ),
    ),
    (
        "technical_background",
        re.compile(
            r"(技术栈|框架|架构|语言|平台|设备|研究方向|专业|领域|背景|用的?是|部署|模型)",
            re.IGNORECASE,
        ),
    ),
    (
        "work_style",
        re.compile(r"(工作|协作|流程|习惯|工具|文档|评审|排期|沟通)", re.IGNORECASE),
    ),
    (
        "long_term_goal",
        re.compile(r"(目标|计划|打算|长期|希望成为|想做出|争取)", re.IGNORECASE),
    ),
)


@dataclass
class IntentTarget:
    """"忘记"将被停用的既有记忆（卡片必须展示，避免误操作）。"""

    id: str
    content: str
    category: Optional[str] = None


@dataclass
class MemoryIntent:
    kind: str  # remember | forget
    content: str
    recommended_scope: Optional[str]
    scope_reason: str
    conversation_id: Optional[str] = None
    conversation_temporary: bool = False
    targets: List[IntentTarget] = field(default_factory=list)
    #: 卡片上的补充说明（例如临时对话不能写入）
    note: str = ""

    @property
    def recommended_category(self) -> Optional[str]:
        """跨会话保存时推荐的偏好类别（推不出为 None，由界面让用户选）。"""
        return infer_category(self.content)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "content": self.content,
            "recommended_scope": self.recommended_scope,
            "scope_reason": self.scope_reason,
            "conversation_temporary": self.conversation_temporary,
            "recommended_category": self.recommended_category,
            "targets": [
                {"id": t.id, "content": t.content, "category": t.category}
                for t in self.targets
            ],
            "note": self.note,
        }


def recommend_scope(text: str) -> tuple[Optional[str], str]:
    """按措辞推荐保存范围；不明确时返回 ``(None, 原因)``，即不预选。"""
    text = text or ""
    session_hit = bool(_SESSION_HINT_RE.search(text))
    pref_hit = bool(_PREFERENCE_HINT_RE.search(text))
    if session_hit and pref_hit:
        return None, "同时提到当前会话与长期习惯，请明确保存范围"
    if session_hit:
        return SCOPE_SESSION, "内容限定了本次任务/当前项目，默认只记本会话"
    if pref_hit:
        return SCOPE_PREFERENCE, "内容表达的是长期习惯，默认作为跨会话个人偏好"
    return None, "未明确范围，请选择仅本会话还是跨会话个人偏好"


def infer_category(content: str) -> Optional[str]:
    """推断个人偏好类别（第一期的四类）；推不出返回 None，由界面让用户选。"""
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(content or ""):
            return category
    return None


def detect_intents(
    session: Session,
    owner_id: str,
    text: str,
    conversation_id: Optional[str] = None,
) -> List[MemoryIntent]:
    """只看用户本轮原话，返回待确认卡片列表（不写任何数据）。

    一句话里可能同时有"记住 A"和"忘记 B"，因此返回列表。
    """
    raw = (text or "").strip()
    if not raw or not owner_id:
        return []
    from chatchat.server.memory.memory_service import _FORGET_RE, _REMEMBER_RE

    temporary = False
    if conversation_id:
        from chatchat.server.db.repository import conversation_repository as conv_repo

        temporary = conv_repo.conversation_is_temporary(session, conversation_id, owner_id)

    intents: List[MemoryIntent] = []
    standing = bool(_STANDING_PREF_RE.search(raw))
    if _REMEMBER_RE.search(raw):
        content = _strip_intent_prefix(raw)
        if content:
            scope, reason = recommend_scope(raw)
            intents.append(
                MemoryIntent(
                    kind="remember",
                    content=content,
                    recommended_scope=scope,
                    scope_reason=reason,
                    conversation_id=conversation_id,
                    conversation_temporary=temporary,
                    note=(
                        "临时对话不保存记忆；如需保存，请退出临时对话后再确认。"
                        if temporary
                        else ""
                    ),
                )
            )
    if _FORGET_RE.search(raw):
        target = _strip_forget_prefix(raw)
        scope, reason = recommend_scope(raw)
        targets = _match_memories(session, owner_id, target)
        intents.append(
            MemoryIntent(
                kind="forget",
                content=target or raw,
                recommended_scope=scope,
                scope_reason=reason,
                conversation_id=conversation_id,
                conversation_temporary=temporary,
                targets=targets,
                note=(
                    f"将停用 {len(targets)} 条个人记忆（停用不删除历史聊天）。"
                    if targets
                    else "没有找到匹配的跨会话个人记忆；也可能是本会话上下文里的条目。"
                ),
            )
        )
    if not intents and standing:
        # "以后默认中文回答"这类没有"记住"动词、但明确指向长期的要求
        scope, reason = recommend_scope(raw)
        intents.append(
            MemoryIntent(
                kind="remember",
                content=raw,
                recommended_scope=scope,
                scope_reason=reason,
                conversation_id=conversation_id,
                conversation_temporary=temporary,
                note=(
                    "临时对话不保存记忆；如需保存，请退出临时对话后再确认。"
                    if temporary
                    else ""
                ),
            )
        )
    return intents


def _match_memories(
    session: Session, owner_id: str, target: str
) -> List[IntentTarget]:
    """列出将被停用的记忆（只读，供卡片展示）。

    复用 ``memory_service`` 的匹配规则，保证卡片上看到的与实际停用的是同一批。
    """
    from chatchat.server.memory.memory_service import forget_match_plan, matches_forget

    key_prefixes, type_filter, forget_all = forget_match_plan(target or "")
    out: List[IntentTarget] = []
    for m in memory_repo.list_memories(session, owner_id, enabled_only=True):
        if matches_forget(m, target or "", key_prefixes, type_filter, forget_all):
            out.append(IntentTarget(id=m.id, content=m.content, category=m.category))
    return out[:10]


def apply_intent(
    session: Session,
    owner_id: str,
    *,
    kind: str,
    content: str,
    scope: str,
    conversation_id: Optional[str] = None,
    category: Optional[str] = None,
    target_ids: Optional[List[str]] = None,
) -> dict:
    """用户点"确认保存"后才真正写入。返回写入结果说明（供界面提示）。

    抛 ``ValueError`` 表示请求不合法（范围未知、内容为空、敏感信息、临时对话
    不允许写入等），由路由映射为 400。
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"保存范围必须是 {' / '.join(SCOPE_LABELS.values())}")
    if kind not in ("remember", "forget"):
        raise ValueError("kind 只能是 remember 或 forget")
    text = (content or "").strip()
    if not text:
        raise ValueError("要保存的内容不能为空")

    temporary = False
    if conversation_id:
        from chatchat.server.db.repository import conversation_repository as conv_repo

        temporary = conv_repo.conversation_is_temporary(session, conversation_id, owner_id)

    # --- 跨会话个人偏好 ---
    if scope == SCOPE_PREFERENCE:
        if temporary:
            # 设计方案 §4.4 / P2-11：临时对话不允许写入个人偏好
            raise ValueError(
                "临时对话不会保存个人偏好；请退出临时对话后再确认保存。"
            )
        if kind == "forget":
            disabled = _disable_targets(session, owner_id, text, target_ids)
            return {
                "scope": scope,
                "kind": kind,
                "disabled": disabled,
                "message": f"已停用 {disabled} 条个人记忆",
            }
        reason = sensitive_reason(text)
        if reason:
            raise ValueError(
                f"个人记忆不应保存{reason}，已拒绝保存；请改用密码管理工具保存该类信息"
            )
        try:
            normalized_category = memory_repo._normalize_category(
                category or infer_category(text)
            )
        except ValueError:
            raise ValueError(
                "请指定个人偏好类别："
                + " / ".join(memory_repo.VALID_CATEGORIES)
            )
        if normalized_category is None:
            raise ValueError(
                "请指定个人偏好类别：" + " / ".join(memory_repo.VALID_CATEGORIES)
            )
        from chatchat.server.memory.memory_service import remember

        saved = remember(
            session,
            owner_id,
            text,
            mtype="preference",
            source="user",
            importance=4,
            source_conversation_id=conversation_id,
        )
        saved.category = normalized_category
        session.commit()
        session.refresh(saved)
        return {
            "scope": scope,
            "kind": kind,
            "memory_id": saved.id,
            "category": saved.category,
            "message": "已保存为跨会话个人偏好",
        }

    # --- 仅本会话 ---
    if not conversation_id:
        raise ValueError("仅本会话需要当前会话 ID")
    if kind == "forget":
        removed = session_context.forget_session_items(
            session, owner_id, conversation_id, text
        )
        return {
            "scope": scope,
            "kind": kind,
            "removed": removed,
            "message": f"已从本会话上下文移除 {removed} 条",
        }
    context = session_context.add_session_confirmation(
        session, owner_id, conversation_id, text
    )
    if context is None:
        raise ValueError(
            "临时对话不保存会话上下文；请退出临时对话后再确认保存。"
        )
    return {
        "scope": scope,
        "kind": kind,
        "message": "已记入本会话上下文（不会带入其他会话）",
    }


def _disable_targets(
    session: Session, owner_id: str, text: str, target_ids: Optional[List[str]]
) -> int:
    """按卡片上确认过的 id 停用记忆（只停当前用户自己的）；没给 id 时按内容匹配。"""
    ids = [i for i in (target_ids or []) if i]
    if ids:
        affected = 0
        for m in memory_repo.list_memories(session, owner_id, enabled_only=True):
            if m.id in ids:
                m.enabled = False
                affected += 1
        if affected:
            session.commit()
        return affected
    from chatchat.server.memory.memory_service import forget

    return forget(session, owner_id, content=text)


__all__ = [
    "SCOPE_SESSION",
    "SCOPE_PREFERENCE",
    "VALID_SCOPES",
    "SCOPE_LABELS",
    "IntentTarget",
    "MemoryIntent",
    "recommend_scope",
    "infer_category",
    "detect_intents",
    "apply_intent",
]
