"""一次模型调用的身份声明与"本上下文已持有租约"记录（§8.2）。

容量租约的最小单位是**一次实际模型调用**，所以"这次调用是谁发起的"必须在
调用真正发生的那一刻可知。这里提供两条声明路径，二者都只是声明，不取容量：

1. 构造期声明：``get_ChatOpenAI(consumer=..., business_object=..., user_id=...,
   request_id=...)``，把身份烘进模型对象；模型对象长期缓存也不会持有容量。
2. 调用期声明：``with model_call_context(consumer=..., ...)`` 包住一段业务代码，
   期间发生的每一次模型调用都按这个身份排队（嵌套时按字段合并，内层优先）。

优先级：调用期声明 > 构造期声明 > 登记表默认通道 > 未登记回落。

``_HELD_LEASES`` 记录"当前上下文已经取得租约的调用"。闸门在取租约前检查它，
一旦发现外层（路由、整轮对话、整次 Agent Run、整个预审运行）已经持着租约而
内层又要取一次，就抛 ``ModelCapacityReentrancyError``——容量上限为 1 且租约
不可重入，这种情况必然自锁，宁可立刻失败并说明是谁占着容量。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterator, Optional, Tuple

from .consumers import (
    CONSUMER_UNDECLARED,
    channel_of,
    is_registered_consumer,
    normalize_channel,
)

#: 当前上下文声明的调用身份（不持容量）
_CALL_SPEC: ContextVar[Optional["ModelCallSpec"]] = ContextVar(
    "chatchat_model_call_spec", default=None
)
#: 当前上下文已取得的租约编号（按取得顺序）。用 tuple + 整体 set 而不是
#: token reset：流式调用的生成器体运行在调用方的上下文里，取得与释放可能发生在
#: 不同的 Context 对象中，``ContextVar.reset(token)`` 会抛
#: "was created in a different Context"。
_HELD_LEASES: ContextVar[Tuple[str, ...]] = ContextVar(
    "chatchat_model_capacity_held", default=()
)

#: 未登记消费者只告警一次，避免每次调用刷日志
_warned_unregistered: set = set()


@dataclass(frozen=True)
class ModelCallSpec:
    """一次实际模型调用的身份：谁发起、排哪条通道、属于哪个业务对象。"""

    consumer_type: str = ""
    channel: str = ""
    business_object: str = ""
    user_id: str = ""
    request_id: str = ""
    model_name: str = ""
    note: str = ""

    def merge(self, lower: "ModelCallSpec") -> "ModelCallSpec":
        """self 的字段优先，空字段由 ``lower`` 补齐。"""
        merged = {}
        for field in self.__dataclass_fields__:
            mine = getattr(self, field)
            merged[field] = mine if mine else getattr(lower, field)
        return ModelCallSpec(**merged)

    def resolved(self) -> "ModelCallSpec":
        """补齐通道与未登记回落，并写一次告警。"""
        consumer = (self.consumer_type or "").strip()
        if not consumer:
            consumer = CONSUMER_UNDECLARED
        elif not is_registered_consumer(consumer):
            key = (consumer, self.model_name)
            if key not in _warned_unregistered:
                _warned_unregistered.add(key)
                # 延迟导入避免循环依赖（gate 依赖本模块）
                from chatchat.utils import build_logger

                build_logger(__name__).warning(
                    "生成模型消费者 {} 未登记（模型 {}）：按未登记回落通道排队，"
                    "请在 model_capacity/consumers.py 补登记",
                    consumer,
                    self.model_name or "未知",
                )
        channel = normalize_channel(self.channel) or channel_of(consumer)
        return replace(self, consumer_type=consumer, channel=channel)

    def dedup_key(self) -> Optional[Tuple[str, str, str]]:
        """重复请求抑制的键：(消费者类型, 业务对象, 请求编号)。

        请求编号缺失时不去重——没有编号就无法区分"同一请求的重复提交"和
        "同一业务对象上的两次不同调用"，宁可排队也不能把第二次调用误判为重复。
        """
        if not self.consumer_type or not self.request_id:
            return None
        return (self.consumer_type, self.business_object or "", self.request_id)

    def describe(self) -> str:
        """人类可读的一行标识，用于错误信息与日志。"""
        parts = [f"消费者={self.consumer_type}", f"通道={self.channel}"]
        if self.business_object:
            parts.append(f"业务对象={self.business_object}")
        if self.user_id:
            parts.append(f"用户={self.user_id}")
        if self.request_id:
            parts.append(f"请求编号={self.request_id}")
        if self.model_name:
            parts.append(f"模型={self.model_name}")
        return " ".join(parts)


EMPTY_SPEC = ModelCallSpec()


def current_model_call_spec() -> ModelCallSpec:
    """当前上下文的调用期声明（可能为空 spec）。"""
    return _CALL_SPEC.get() or EMPTY_SPEC


@contextmanager
def model_call_context(
    consumer: Optional[str] = None,
    *,
    channel: Optional[str] = None,
    business_object: Optional[str] = None,
    user_id: Optional[str] = None,
    request_id: Optional[str] = None,
    model_name: Optional[str] = None,
    note: Optional[str] = None,
) -> Iterator[ModelCallSpec]:
    """声明"接下来这段代码里的模型调用属于谁"，**不取容量**。

    同步、异步代码都能用（普通上下文管理器在协程里同样可用）。嵌套时按字段
    合并，内层声明优先，因此业务层可以只补业务对象而不丢消费者类型。
    """
    declared = ModelCallSpec(
        consumer_type=(consumer or "").strip(),
        channel=normalize_channel(channel) or (channel or "").strip(),
        business_object=business_object or "",
        user_id=user_id or "",
        request_id=request_id or "",
        model_name=model_name or "",
        note=note or "",
    )
    merged = declared.merge(_CALL_SPEC.get() or EMPTY_SPEC)
    previous = _CALL_SPEC.get()
    _CALL_SPEC.set(merged)
    try:
        yield merged
    finally:
        _CALL_SPEC.set(previous)


def resolve_call_spec(instance_spec: Optional[ModelCallSpec]) -> ModelCallSpec:
    """调用期声明 → 构造期声明 → 登记表默认通道，逐层补齐。"""
    ambient = _CALL_SPEC.get() or EMPTY_SPEC
    return ambient.merge(instance_spec or EMPTY_SPEC).resolved()


# ---------------------------------------------------------------------------
# "本上下文已持有租约"记录：自锁检测的依据
# ---------------------------------------------------------------------------


def held_lease_ids() -> Tuple[str, ...]:
    return _HELD_LEASES.get()


def mark_lease_held(lease_id: str) -> None:
    """在**调用方自己的上下文**里记下租约编号（取得容量后调用）。"""
    held = _HELD_LEASES.get()
    if lease_id not in held:
        _HELD_LEASES.set(held + (lease_id,))


def unmark_lease_held(lease_id: str) -> None:
    """释放容量后从当前上下文摘掉；不在当前上下文里时是 no-op。"""
    held = _HELD_LEASES.get()
    if lease_id in held:
        _HELD_LEASES.set(tuple(item for item in held if item != lease_id))


def clear_held_lease_ids() -> None:
    """上下文里残留的（已释放）租约编号清理，避免长期污染后续调用。"""
    _HELD_LEASES.set(())


def spec_to_dict(spec: ModelCallSpec) -> Dict[str, Any]:
    return {
        "consumer_type": spec.consumer_type,
        "channel": spec.channel,
        "business_object": spec.business_object,
        "user_id": spec.user_id,
        "request_id": spec.request_id,
        "model_name": spec.model_name,
        "note": spec.note,
    }


__all__ = [
    "ModelCallSpec",
    "EMPTY_SPEC",
    "current_model_call_spec",
    "model_call_context",
    "resolve_call_spec",
    "held_lease_ids",
    "mark_lease_held",
    "unmark_lease_held",
    "clear_held_lease_ids",
    "spec_to_dict",
]
