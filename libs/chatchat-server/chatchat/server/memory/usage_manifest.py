"""每轮实际使用记录 `memory_usage_manifest`（智能聊天记忆第四期·第一阶段 B，§11、§18.3.1-2/4-6/8/12）。

为什么必须有这份快照：历史回答如果在生成时没记下"当时到底注入了什么"，
事后无法准确补算——记忆可能已被修改或删除，摘要版本也已推进。因此它必须在
**模型调用开始之前**由 :class:`~chatchat.server.memory.context_package.ContextPackage`
直接生成并写进助手消息 metadata，不能回答后再查一遍推算（§11.1）。

三条底线：

- **同源**：manifest 与模型输入来自同一份数据。构建器给出什么，这里就记什么。
- **按字段合并**：metadata 里还有引用、Agent 状态、intermediate_steps 等键，
  任何一处写入都必须合并，不能整体覆盖（§11.2）。
- **如实标记未完成**：用户点停止、超时、流式中断时，已产生的文本照常保存并
  标记 ``partial / cancelled / failed``；界面和后续上下文都必须看得出这是
  未完成回答，不能当成已完成的结论（§11.3）。

``generation_status`` 状态机（§11.1）::

    prepared → queued → started → completed
                             ↘ partial / cancelled / failed
    queued → cancelled / failed
    prepared → failed

``queued`` 只在请求确实进入等待队列后才写，不为了凑状态机伪造排队。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

#: 状态机允许的值（界面文案与自动化测试都按这个名字对齐）
GENERATION_STATUSES = (
    "prepared",
    "queued",
    "started",
    "partial",
    "cancelled",
    "failed",
    "completed",
)
#: 有半截回答、不能当作已完成结论的状态
INCOMPLETE_STATUSES = ("partial", "cancelled", "failed")

#: metadata 里的键名（前端历史恢复与 SSE 事件都用同一个名字）
MANIFEST_KEY = "memory_usage_manifest"
STATUS_KEY = "generation_status"

#: 注入 Prompt 时给未完成回答的角色标记（§11.3：最近原文可以带它，
#: 用户下一轮能说"继续"，但模型必须知道这段没写完）
INCOMPLETE_MARKER = "[未完成回答]"

#: 失败原因写库前的脱敏：密钥、令牌、Basic 认证这类内容不能进 metadata
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b(\s*[=:]\s*)\S+"
)
#: 任意 scheme 的 URL 内嵌凭据（数据库连接串这类失败原因很常见，不能进 metadata）
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_reason(text: Optional[str], max_chars: int = 300) -> str:
    """把失败原因压成一行可展示的文案，并脱敏凭据（§11.1 要求经过脱敏）。"""
    if not text:
        return ""
    one_line = " ".join(str(text).split())
    one_line = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[已脱敏]", one_line)
    one_line = _URL_CREDENTIALS_RE.sub(r"\1[已脱敏]@", one_line)
    if len(one_line) > max_chars:
        one_line = one_line[:max_chars] + "…"
    return one_line


def _memory_entry(memory: Any, *, reason: str = "", extra: Optional[Dict] = None) -> Dict:
    """一条记忆的使用快照：内容也一起存，后续改/删记忆都不影响这条记录。"""
    entry: Dict[str, Any] = {
        "id": getattr(memory, "id", None),
        "content": (getattr(memory, "content", "") or "")[:500],
    }
    updated = getattr(memory, "update_time", None)
    if updated is not None:
        entry["updated_at"] = (
            updated.isoformat(timespec="seconds")
            if hasattr(updated, "isoformat")
            else str(updated)
        )
    #: 本表没有记忆版本列（修订历史在 memory_revision），因此记内容 + 更新时间；
    #: 界面要显示"版本"时按这两个字段解释，不伪造整数版本号。
    if reason:
        entry["reason"] = reason
    if extra:
        entry.update({k: v for k, v in extra.items() if v not in (None, "")})
    return entry


def build_usage_manifest(
    package: Any,
    *,
    use_memory: bool = True,
    status: str = "prepared",
    budget_source: str = "estimate",
) -> Dict[str, Any]:
    """从 :class:`ContextPackage` 生成使用快照（§11.1 的字段形状）。

    只读 package 里已经算好的数据，不重新查询数据库——重新查询拿到的可能
    已经不是本轮真正注入的那一份。
    """
    context = getattr(package, "context", None)
    preferences: List[Dict] = [
        _memory_entry(p) for p in (getattr(package, "preferences", []) or [])
    ]
    recalled: List[Dict] = []
    for hit in getattr(package, "recalled", []) or []:
        memory = getattr(hit, "memory", hit)
        recalled.append(
            _memory_entry(
                memory,
                reason=getattr(hit, "reason", "") or "",
                extra={
                    "bucket": getattr(hit, "bucket", None),
                    "scope": getattr(hit, "scope", None),
                    "score": round(float(getattr(hit, "score", 0.0) or 0.0), 4),
                },
            )
        )
    return {
        "conversation_id": getattr(package, "conversation_id", "") or "",
        "session_context_version": int(getattr(context, "version", 0) or 0),
        "summary_through_message_id": getattr(context, "summarized_message_id", None),
        "summary_type": getattr(context, "summary_type", None) or "rule",
        "summary_status": getattr(context, "summary_status", None) or "idle",
        "history_boundary_message_id": getattr(package, "boundary_message_id", None),
        "recent_message_ids": package.history_message_ids(),
        "recent_chars": int(getattr(getattr(package, "window", None), "chars", 0) or 0),
        "preferences": preferences,
        "recalled_memories": recalled,
        "memory_disabled_for_turn": not use_memory,
        "degraded": bool(
            getattr(package, "history_gap_due_to_degradation", False)
            or getattr(package, "history_truncated", False)
        ),
        "budget_source": budget_source,
        "generation_status": status if status in GENERATION_STATUSES else "prepared",
        "model_started_at": None,
        "model_finished_at": None,
        "failure_stage": None,
        "failure_reason": None,
    }


def manifest_payload(manifest: Dict[str, Any], message_id: Optional[str]) -> Dict[str, Any]:
    """SSE 元数据事件的额外字段（正文必须保持空，旧客户端不会拼进回答，§11.4）。

    ``class_name`` 沿用 AgentRunEvent / RouteResult 的既有约定，前端在通用
    token 兜底分支之前按它识别元数据事件。
    """
    return {
        "class_name": "MemoryUsageEvent",
        "message_id": message_id,
        MANIFEST_KEY: manifest,
        STATUS_KEY: manifest.get("generation_status", "prepared"),
    }


def is_incomplete_metadata(meta_data: Any) -> bool:
    """这条消息的回答是不是未完成（partial / cancelled / failed）。"""
    if not isinstance(meta_data, dict):
        return False
    return meta_data.get(STATUS_KEY) in INCOMPLETE_STATUSES


def mark_incomplete(text: str) -> str:
    """给未完成回答加角色标记（幂等：已标记过不重复加）。"""
    body = text or ""
    if INCOMPLETE_MARKER in body:
        return body
    return f"{INCOMPLETE_MARKER} {body}".strip()


class TurnRecorder:
    """一轮回答的 manifest、生成状态与半截文本的统一落库入口（§18.3.8）。

    三条链路（普通聊天 / 知识库对话 / Agent）共用同一份收尾逻辑：正常完成、
    用户停止、异常、客户端断流四条出口都调用 :meth:`finish` 一次，半截文本
    按累加器内容保存，绝不因为取消路径提前返回而让刷新后的回答变成空串。

    ``session`` 传请求会话时复用它；传 ``None`` 时每次写入开一个短会话
    （Agent 的流式生成器跑在响应体里，请求会话可能已经关闭）。
    """

    def __init__(
        self,
        *,
        message_id: Optional[str],
        owner_id: Optional[str],
        manifest: Optional[Dict[str, Any]] = None,
        session: Optional[Session] = None,
        session_factory=None,
    ) -> None:
        self.message_id = message_id
        self.owner_id = owner_id
        self.manifest = manifest
        self.session = session
        #: 自开会话时用这个工厂（Agent 链路用它指向运行轨迹库，测试也靠它换库）
        self.session_factory = session_factory
        self.finalized = False
        self._chunks: List[str] = []
        self._chars = 0

    @property
    def enabled(self) -> bool:
        #: 有 message_id 就能写状态；manifest 为 None 时只更新状态与回答，
        #: 恢复路径会先 :meth:`adopt_existing` 接手原来那份快照（§18.3.6）
        return bool(self.message_id and self.owner_id)

    def _open(self):
        if self.session is not None:
            return self.session, False
        if self.session_factory is not None:
            return self.session_factory(), True
        from chatchat.server.db.base import SessionLocal

        return SessionLocal(), True

    def _write(
        self,
        *,
        response: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> None:
        """按字段合并写入 metadata；失败只记日志，绝不阻断回答。"""
        if not self.message_id or not self.owner_id:
            return
        from chatchat.server.db.repository.message_repository import merge_message_metadata

        updates: Dict[str, Any] = {}
        if self.manifest is not None:
            updates[MANIFEST_KEY] = self.manifest
            # 顶层也放一份状态：历史接口与 SSE 用同一个键读状态，前端不必
            # 先解开 manifest；两处永远同源（都取自这份 manifest）
            updates[STATUS_KEY] = self.manifest.get("generation_status")
        if status:
            # 没有快照（恢复路径里那条消息本来就没有 manifest）也要能记状态，
            # 但不能为此造一份空 manifest 污染 metadata
            updates[STATUS_KEY] = status
        if extra_metadata:
            updates.update(extra_metadata)
        session, own = self._open()
        try:
            merge_message_metadata(
                session,
                self.message_id,
                self.owner_id,
                updates,
                response=response,
            )
        except Exception:  # 落库失败不能影响回答
            from chatchat.utils import build_logger

            build_logger().warning("写入本轮使用记录失败（忽略）", exc_info=True)
        finally:
            if own:
                try:
                    session.close()
                except Exception:
                    pass

    def adopt_existing(self) -> Optional[Dict[str, Any]]:
        """接手消息里已有的 manifest（Agent 恢复路径：保留同一份快照，§18.3.6）。

        恢复一次中断的运行不表示当时注入的内容变了，重新生成一份快照反而会让
        历史回答"当时用了什么"变成"恢复时用了什么"。
        """
        if self.manifest is not None or not self.enabled:
            return None
        from chatchat.server.db.repository.message_repository import get_message_by_id

        session, own = self._open()
        try:
            message = get_message_by_id(session, self.message_id, self.owner_id)
            meta = getattr(message, "meta_data", None)
            if isinstance(meta, dict) and isinstance(meta.get(MANIFEST_KEY), dict):
                self.manifest = dict(meta[MANIFEST_KEY])
        except Exception:
            from chatchat.utils import build_logger

            build_logger().warning("读取已有本轮使用记录失败（忽略）", exc_info=True)
        finally:
            if own:
                try:
                    session.close()
                except Exception:
                    pass
        return self.manifest

    def prepare(self) -> Optional[Dict[str, Any]]:
        """模型调用前保存"已准备的上下文快照"（``prepared``）。"""
        if self.manifest is None:
            return None
        self.manifest["generation_status"] = "prepared"
        self._write()
        return self.manifest

    def queued(self) -> None:
        """请求确实进入等待队列时才写（不伪造排队状态）。"""
        if self.manifest is None:
            return
        self.manifest["generation_status"] = "queued"
        self._write()

    def started(self) -> None:
        if self.manifest is None:
            return
        self.manifest["generation_status"] = "started"
        self.manifest["model_started_at"] = _now()
        self._write()

    def note(self, text: Optional[str]) -> None:
        """累加可见文本，供取消/异常时保存半截回答。"""
        if text:
            self._chunks.append(text)
            self._chars += len(text)

    @property
    def accumulated(self) -> str:
        return "".join(self._chunks)

    def finish(
        self,
        status: str,
        *,
        response: Optional[str] = None,
        failure_stage: Optional[str] = None,
        failure_reason: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """收尾：写最终状态与回答文本（只生效一次，重复调用忽略）。

        ``response`` 为 ``None`` 时退回累加器里的半截文本；两者都为空时只更新
        状态与 manifest，不制造空的助手结论（§11.3 最后一条）。
        """
        if not self.enabled or self.finalized:
            return
        if status not in GENERATION_STATUSES:
            status = "failed"
        if self.manifest is not None:
            self.manifest["generation_status"] = status
            # 不伪造 model_started_at：只有 :meth:`started` 真正被调用过才写
            # （§11.1 要求"只有模型真正开始生成后，才能描述为模型实际使用过"）。
            # 模型没启动就收尾时，model_started_at 保持 None，界面据此显示
            # "上下文已准备，但模型未开始生成"。
            if self.manifest.get("model_started_at") is not None:
                self.manifest["model_finished_at"] = _now()
            if failure_stage or failure_reason:
                self.manifest["failure_stage"] = failure_stage
                self.manifest["failure_reason"] = sanitize_reason(failure_reason)
        text = response if response is not None else self.accumulated
        self._write(
            response=text if text else None,
            extra_metadata=extra_metadata,
            status=status,
        )
        self.finalized = True


__all__ = [
    "GENERATION_STATUSES",
    "INCOMPLETE_MARKER",
    "INCOMPLETE_STATUSES",
    "MANIFEST_KEY",
    "STATUS_KEY",
    "TurnRecorder",
    "build_usage_manifest",
    "is_incomplete_metadata",
    "manifest_payload",
    "mark_incomplete",
    "sanitize_reason",
]
