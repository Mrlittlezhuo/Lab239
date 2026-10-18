"""统一模型容量闸门（里程碑0B）。

一句话：**所有实际生成模型调用最多一个并发，闸门在最终 ChatModel 调用层**。

业务层需要知道的三件事：

1. 不用自己加锁。``get_ChatOpenAI`` 返回的 ``GatedChatOpenAI`` 在每次
   ``invoke / stream / ainvoke / astream`` 内部取放租约，Agent 工具内部再次
   调用模型、意图路由之后的回答生成，都是各自独立排队的一次调用。
2. 声明这次调用是谁发起的，决定它排在哪条通道：

   - 构造期：``get_ChatOpenAI(consumer=CONSUMER_..., business_object=...,
     user_id=..., request_id=...)``；
   - 调用期：``with model_call_context(consumer=..., business_object=...,
     user_id=..., request_id=...)``（包住一段业务代码，期间每次模型调用都
     按这个身份排队）。

   只写 ``consumer`` 就够了：通道由 ``consumers.py`` 的登记表推导。
3. 不走 ChatModel 的生成调用（例如直接用 openai SDK 打兼容接口）用
   ``gated_call`` / ``agated_call`` 手动取放**一次**调用的租约；不要把它套在
   已经经过闸门的模型外面，那会触发 ``ModelCapacityReentrancyError``。

观测：``get_gate().status()`` 给出当前占用数、容量上限、持有者、排队位置、
释放历史与**进程内最大占用值**；只读端点 ``GET /server/model_capacity``。
"""
from __future__ import annotations

from .audit import (
    audit_generation_consumers,
    embedding_paths_are_not_gated,
    scan_generation_construction_points,
)
from .consumers import (
    CHANNEL_BACKGROUND,
    CHANNEL_INTERACTIVE,
    CHANNEL_LABELS,
    CHANNEL_PRIORITY,
    CHANNEL_USER_TASK,
    DEFAULT_UNDECLARED_CHANNEL,
    CONSUMER_AGENT_FINAL_ANSWER,
    CONSUMER_AGENT_PLAN,
    CONSUMER_AGENT_TOOL,
    CONSUMER_AUTO_REVIEW,
    CONSUMER_INTENT_ROUTE,
    CONSUMER_KB_CHAT,
    CONSUMER_KB_SUMMARY,
    CONSUMER_MENTOR_REVIEW,
    CONSUMER_PAPER_COMPARE,
    CONSUMER_PAPER_EXTRACT,
    CONSUMER_PROGRESS_DRAFT,
    CONSUMER_PROJECT_DISCUSSION,
    CONSUMER_REGISTRY,
    CONSUMER_RESEARCH_DEFINITION,
    CONSUMER_RESEARCH_REPORT,
    CONSUMER_RESEARCH_PLAN,
    CONSUMER_LEGACY_COMPLETION,
    CONSUMER_SESSION_SUMMARY,
    CONSUMER_SMART_CHAT,
    CONSUMER_STUDENT_CHECK,
    CONSUMER_UNDECLARED,
    ConsumerSpec,
    channel_of,
    describe_consumers,
    is_registered_consumer,
    lookup_consumer,
)
from .context import (
    ModelCallSpec,
    current_model_call_spec,
    model_call_context,
)
from .errors import (
    ModelCapacityCancelledError,
    ModelCapacityDuplicateError,
    ModelCapacityError,
    ModelCapacityLeaseNotHeldError,
    ModelCapacityQueueTimeoutError,
    ModelCapacityReentrancyError,
)
from .lease_adapter import DEFAULT_CAPACITY_ADAPTER, CapacityLeaseAdapter
from .gate import (
    CapacityLease,
    ModelCapacityGate,
    get_gate,
    initialize_model_capacity,
    reset_stale_leases,
    set_gate,
    shutdown_model_capacity,
)
from .gated_model import (
    CAPACITY_KWARGS,
    CAPACITY_KWARG_ALIASES,
    CAPACITY_MODE_EXTERNAL,
    CAPACITY_MODE_ACQUIRE,
    GatedChatOpenAI,
    GatedChatPlatformAI,
    GatedOpenAI,
    ModelCapacityCallMixin,
    agated_call,
    gated_call,
    pop_model_call_kwargs,
)

__all__ = [
    # 闸门与租约
    "ModelCapacityGate",
    "CapacityLease",
    "get_gate",
    "set_gate",
    "initialize_model_capacity",
    "shutdown_model_capacity",
    # 异常
    "ModelCapacityError",
    "ModelCapacityQueueTimeoutError",
    "ModelCapacityCancelledError",
    "ModelCapacityDuplicateError",
    "ModelCapacityReentrancyError",
    "ModelCapacityLeaseNotHeldError",
    # 身份声明
    "ModelCallSpec",
    "model_call_context",
    "current_model_call_spec",
    # 便捷入口（不走 ChatModel 的生成调用）
    "gated_call",
    "agated_call",
    # 模型包装
    "GatedChatOpenAI",
    "GatedChatPlatformAI",
    "GatedOpenAI",
    "ModelCapacityCallMixin",
    "pop_model_call_kwargs",
    "CAPACITY_KWARGS",
    "CAPACITY_KWARG_ALIASES",
    "CAPACITY_MODE_ACQUIRE",
    "CAPACITY_MODE_EXTERNAL",
    # 业务层代持租约（AI 预审按 AC 批次取放）
    "CapacityLeaseAdapter",
    "DEFAULT_CAPACITY_ADAPTER",
    # 通道与消费者登记
    "CHANNEL_INTERACTIVE",
    "CHANNEL_USER_TASK",
    "CHANNEL_BACKGROUND",
    "CHANNEL_PRIORITY",
    "CHANNEL_LABELS",
    "DEFAULT_UNDECLARED_CHANNEL",
    "CONSUMER_REGISTRY",
    "ConsumerSpec",
    "channel_of",
    "describe_consumers",
    "is_registered_consumer",
    "lookup_consumer",
    "CONSUMER_SMART_CHAT",
    "CONSUMER_KB_CHAT",
    "CONSUMER_PROJECT_DISCUSSION",
    "CONSUMER_INTENT_ROUTE",
    "CONSUMER_AGENT_PLAN",
    "CONSUMER_AGENT_FINAL_ANSWER",
    "CONSUMER_AGENT_TOOL",
    "CONSUMER_RESEARCH_DEFINITION",
    "CONSUMER_RESEARCH_REPORT",
    "CONSUMER_RESEARCH_PLAN",
    "CONSUMER_MENTOR_REVIEW",
    "CONSUMER_STUDENT_CHECK",
    "CONSUMER_AUTO_REVIEW",
    "CONSUMER_PAPER_EXTRACT",
    "CONSUMER_PAPER_COMPARE",
    "CONSUMER_PROGRESS_DRAFT",
    "CONSUMER_KB_SUMMARY",
    "CONSUMER_SESSION_SUMMARY",
    "CONSUMER_LEGACY_COMPLETION",
    "CONSUMER_UNDECLARED",
    # 遗漏检查
    "audit_generation_consumers",
    "embedding_paths_are_not_gated",
    "scan_generation_construction_points",
]
