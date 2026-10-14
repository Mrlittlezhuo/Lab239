"""记忆 API 路由：全部要求登录，且只能操作当前用户自己的记忆。

- 创建只接收业务字段（type/category/content/importance/source），owner 来自当前用户；
- 列表只返回当前用户记忆，``update_time DESC``；
- 详情/更新/删除同时过滤 ``id + owner_id``；
- 访问不存在或其他用户的记忆统一 404；
- 响应不返回 owner_id；
- ``/preferences`` 返回本次对话实际会注入的个人偏好与用户级总开关，使界面
  展示与真实注入一致；``DELETE /memories`` 清空当前用户全部记忆（二次确认
  由前端完成）；
- 明显敏感的内容（密码、令牌、证件号、卡号）一律 400 拒绝，不静默入库。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.session import get_db
from chatchat.server.db.repository import (
    VALID_CATEGORIES,
    MemoryNotOwned,
    create_memory,
    delete_all_memories,
    delete_memory,
    get_owned_memory,
    list_memories,
    search_memories,
    update_memory,
)
from chatchat.server.memory.memory_intents import apply_intent, detect_intents
from chatchat.server.memory.memory_service import (
    PREFERENCE_MAX_COUNT,
    preferences_enabled,
    select_preferences,
    sensitive_reason,
)


def _reject_sensitive(content: Optional[str]) -> None:
    """明显敏感内容直接 400：个人记忆不是保存凭证的地方。"""
    reason = sensitive_reason(content or "")
    if reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"个人记忆不应保存{reason}，已拒绝保存；请改用密码管理工具保存该类信息",
        )


def _reject_category(category: Optional[str]) -> None:
    if category and category.strip().lower() not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未知的记忆类别："
            f"{category!r}，可选：{'、'.join(VALID_CATEGORIES)} 或留空",
        )


class MemoryCreate(BaseModel):
    type: str = Field("fact", max_length=16, description="记忆类型 preference/fact/goal/important")
    category: Optional[str] = Field(
        None,
        max_length=32,
        description="个人偏好类别 answer_style/technical_background/work_style/long_term_goal",
    )
    content: str = Field(..., min_length=1, max_length=2000, description="记忆内容")
    source: str = Field("user", max_length=32, description="来源（user/extract 等）")
    importance: int = Field(1, ge=1, le=5, description="重要性 1-5")


class MemoryUpdate(BaseModel):
    type: Optional[str] = Field(None, max_length=16)
    category: Optional[str] = Field(None, max_length=32)
    content: Optional[str] = Field(None, min_length=1, max_length=2000)
    source: Optional[str] = Field(None, max_length=32)
    importance: Optional[int] = Field(None, ge=1, le=5)
    enabled: Optional[bool] = Field(None, description="启用/停用")

    def repo_fields(self) -> dict:
        """转换为 ``repository.update_memory`` 的键名（type→mtype）。

        ``category`` 传空串表示"清除类别"（不再是用户管理的个人偏好），
        因此不能按 ``is not None`` 过滤掉。
        """
        fields = {k: v for k, v in self.model_dump().items() if v is not None}
        if "type" in fields:
            fields["mtype"] = fields.pop("type")
        return fields


class MemoryOut(BaseModel):
    id: str
    memory_key: str
    type: str
    category: Optional[str]
    content: str
    source: str
    importance: int
    enabled: bool
    source_conversation_id: Optional[str]
    source_message_id: Optional[str]
    create_time: Optional[object]
    update_time: Optional[object]


class PreferencesOut(BaseModel):
    """本次对话实际会注入的个人偏好 + 用户级个人记忆总开关。"""

    enabled: bool
    max_count: int
    preferences: List[MemoryOut]


def _out(m) -> MemoryOut:
    return MemoryOut(
        id=m.id,
        memory_key=m.memory_key,
        type=m.type,
        category=m.category,
        content=m.content,
        source=m.source,
        importance=m.importance,
        enabled=m.enabled,
        source_conversation_id=m.source_conversation_id,
        source_message_id=m.source_message_id,
        create_time=m.create_time,
        update_time=m.update_time,
    )


memory_router = APIRouter(prefix="/memories", tags=["Memories"])


@memory_router.post("", summary="创建记忆（owner 来自当前用户）")
def create_my_memory(
    body: MemoryCreate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> MemoryOut:
    _reject_category(body.category)
    _reject_sensitive(body.content)
    m = create_memory(
        session,
        owner_id=user.id,
        content=body.content,
        mtype=body.type,
        source=body.source,
        importance=body.importance,
        category=body.category,
    )
    return _out(m)


@memory_router.get("", summary="当前用户记忆列表（update_time DESC）")
def list_my_memories(
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[MemoryOut]:
    return [_out(m) for m in list_memories(session, user.id)]


@memory_router.get(
    "/search", summary="按查询关键字/重要性检索当前用户相关记忆（离线）"
)
def search_my_memories(
    q: str = "",
    limit: int = 5,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> List[MemoryOut]:
    return [_out(m) for m in search_memories(session, user.id, q, limit=limit)]


@memory_router.get("/auto", summary="查询当前用户自动记忆开关")
def get_auto_memory(
    user: UserModel = Depends(require_password_changed),
) -> dict:
    return {"auto_memory": bool(user.memory_auto_enabled)}


@memory_router.patch("/auto", summary="设置当前用户自动记忆开关")
def set_auto_memory(
    enabled: bool,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    user.memory_auto_enabled = bool(enabled)
    session.commit()
    session.refresh(user)
    return {"auto_memory": bool(user.memory_auto_enabled)}


@memory_router.get(
    "/preferences",
    summary="本次对话将注入的个人偏好与个人记忆总开关（与真实注入一致）",
)
def get_my_preferences(
    q: str = "",
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> PreferencesOut:
    """聊天页"已使用个人偏好"直接调用本端点，避免界面与后端注入不一致。

    ``q`` 传当前输入可让"与当前问题相关的背景"排到前面；不传时按固定优先级。
    """
    enabled = preferences_enabled(session, user.id)
    prefs = select_preferences(session, user.id, q) if enabled else []
    return PreferencesOut(
        enabled=enabled,
        max_count=PREFERENCE_MAX_COUNT,
        preferences=[_out(m) for m in prefs],
    )


@memory_router.patch(
    "/preferences", summary="设置当前用户的个人记忆总开关（不删除数据）"
)
def set_my_preferences(
    enabled: bool,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """关闭只是停止注入，已保存的偏好全部保留，重新开启即恢复生效。"""
    user.memory_preferences_enabled = bool(enabled)
    session.commit()
    session.refresh(user)
    return {"enabled": bool(user.memory_preferences_enabled)}


@memory_router.delete("", summary="清空当前用户全部记忆（前端需二次确认）")
def clear_my_memories(
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """只删除当前用户的记忆，返回删除条数；不动会话、摘要、知识库。"""
    deleted = delete_all_memories(session, user.id)
    return {"deleted": deleted}


# --- “记住 / 忘记”确认卡片（智能聊天记忆第二期设计方案 §4.3）---


class IntentDetectRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000, description="用户本轮原话")
    conversation_id: Optional[str] = Field(
        None, max_length=32, description="当前会话（用于判断临时对话与仅本会话范围）"
    )


class IntentApplyRequest(BaseModel):
    kind: str = Field(..., description="remember | forget")
    content: str = Field(..., min_length=1, max_length=2000)
    scope: str = Field(..., description="session（仅本会话）| preference（跨会话个人偏好）")
    conversation_id: Optional[str] = Field(None, max_length=32)
    category: Optional[str] = Field(None, max_length=32)
    target_ids: Optional[List[str]] = Field(
        None, description="forget 时卡片上确认过的记忆 id；不传则按内容匹配"
    )


def _check_conversation(session: Session, conversation_id: Optional[str], user) -> None:
    """带 conversation_id 时必须属于当前用户，否则 404（不区分不存在/越权）。"""
    if not conversation_id:
        return
    from chatchat.server.db.repository import get_owned_conversation

    if get_owned_conversation(session, conversation_id, user.id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在或无权访问"
        )


@memory_router.post(
    "/intents", summary="检测“记住/忘记”意图并返回待确认卡片（不写入任何数据）"
)
def detect_memory_intents(
    body: IntentDetectRequest,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """显式"记住/忘记"不直接写入：先返回卡片，由用户选范围并确认（P2-07~P2-09）。

    只看用户原话，知识库文档片段与模型回答不会触发（P2-12）。
    """
    _check_conversation(session, body.conversation_id, user)
    intents = detect_intents(session, user.id, body.text, body.conversation_id)
    return {"proposals": [i.to_dict() for i in intents]}


@memory_router.post("/intents/apply", summary="确认卡片后才写入（范围由用户选定）")
def apply_memory_intent(
    body: IntentApplyRequest,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    """``scope=session`` 写入本会话上下文；``scope=preference`` 写入个人偏好。

    临时对话两种范围都拒绝写入（设计方案 §4.4 / P2-11），错误信息说明原因。
    """
    _check_conversation(session, body.conversation_id, user)
    _reject_category(body.category)
    try:
        return apply_intent(
            session,
            user.id,
            kind=body.kind,
            content=body.content,
            scope=body.scope,
            conversation_id=body.conversation_id,
            category=body.category,
            target_ids=body.target_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@memory_router.get("/{memory_id}", summary="记忆详情")
def get_my_memory(
    memory_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> MemoryOut:
    m = get_owned_memory(session, memory_id, user.id)
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在或无权访问")
    return _out(m)


@memory_router.patch("/{memory_id}", summary="更新记忆")
def patch_my_memory(
    memory_id: str,
    body: MemoryUpdate,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> MemoryOut:
    fields = body.repo_fields()
    if not fields:
        raise HTTPException(status_code=400, detail="无更新字段")
    if "category" in fields:
        _reject_category(fields["category"])
    if "content" in fields:
        _reject_sensitive(fields["content"])
    try:
        m = update_memory(session, memory_id, user.id, **fields)
    except MemoryNotOwned:
        raise HTTPException(status_code=404, detail="记忆不存在或无权访问")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if m is None:
        raise HTTPException(status_code=404, detail="记忆不存在或无权访问")
    return _out(m)


@memory_router.delete("/{memory_id}", summary="删除记忆")
def delete_my_memory(
    memory_id: str,
    user: UserModel = Depends(require_password_changed),
    session: Session = Depends(get_db),
) -> dict:
    try:
        delete_memory(session, memory_id, user.id)
    except MemoryNotOwned:
        raise HTTPException(status_code=404, detail="记忆不存在或无权访问")
    return {"deleted": True, "memory_id": memory_id}


__all__ = ["memory_router"]
