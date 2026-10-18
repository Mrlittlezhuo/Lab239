"""业务层代持容量租约的适配器（AI 静态预审按 AC 批次取放，AI预审方案 §12.2.1）。

默认形态下闸门挂在 ChatModel 的四个汇聚点上：每次实际调用自己取放，业务层不
用管（见 ``gated_model``）。但 AI 预审的取放位置只有业务层知道——它按 AC 批次
排队，每批一次调用，租约身份（消费者类型 ``student_check`` / ``mentor_review``
/ ``auto_review``、业务对象 ``AR-rN``、发起人）也只有业务层知道。于是把取放
交给业务层，模型对象改用 ``capacity_mode="external"``：它不再自己取容量，只在
发请求前确认当前上下文确实持着租约（``ModelCapacityLeaseNotHeldError``）。

两条必须同时成立的约束：

1. 闸门上限为 1 且租约不可重入，所以适配器取了租约之后，模型**不能**再取一次
   （否则必然自锁，``ModelCapacityReentrancyError``）；
2. 反过来，适配器取了租约，模型就必须确认租约在，否则"由业务层取容量"会退化
   成"谁都没取容量"，§16.1B 的"最多一个并发生成"当场失效。

适配器契约与 ``supervision_ai_review_repository`` 的调用方式一致::

    token = capacity.acquire(consumer=..., business_ref=..., user_id=...)
    try:
        ...llm.invoke(messages)...
    finally:
        capacity.release(token, reason=...)

``acquire`` 是阻塞的（最多等 ``MODEL_CAPACITY_QUEUE_TIMEOUT_SECONDS``）。预审
跑在后台线程里，不会阻塞事件循环；前台请求永远走不到"被预审占死"的境地，因
为预审属于 ``background`` 通道，前台交互与用户短任务可以排到尚未开始的预审前
面（§8.2）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from chatchat.utils import build_logger

from .consumers import CONSUMER_UNDECLARED, channel_of, is_registered_consumer
from .context import ModelCallSpec
from .gate import CapacityLease, get_gate

logger = build_logger(__name__)


class CapacityLeaseAdapter:
    """把闸门租约交给业务层取放的适配器。

    ``default_consumer`` 只用于日志与漏传参数的兜底：真正的消费者类型必须逐次
    传入，因为同一个预审运行里学生自检、导师手动预审、自动预审属于不同通道。
    """

    def __init__(self, *, default_consumer: str = "", timeout: Optional[float] = None):
        self.default_consumer = default_consumer or CONSUMER_UNDECLARED
        self.timeout = timeout

    def acquire(
        self,
        *,
        consumer: str = "",
        business_ref: str = "",
        user_id: str = "",
        request_id: str = "",
    ) -> CapacityLease:
        """取得一次实际模型调用的容量租约（阻塞排队）。

        取到的租约会登记进当前上下文，因此配对的模型对象必须是
        ``capacity_mode="external"``；调用方必须在 ``finally`` 里 ``release``。
        """
        consumer_type = (consumer or "").strip() or self.default_consumer
        if not is_registered_consumer(consumer_type):
            logger.warning(
                "容量适配器收到未登记的消费者类型 {}，按 {} 通道回落",
                consumer_type,
                channel_of(consumer_type),
            )
        spec = ModelCallSpec(
            consumer_type=consumer_type,
            business_object=business_ref or "",
            user_id=user_id or "",
            request_id=request_id or "",
        )
        lease = get_gate().acquire(spec, call_kind="generate", timeout=self.timeout)
        logger.debug(
            "业务层取得容量租约 {}：{}（{}）",
            lease.lease_id,
            spec.describe(),
            business_ref,
        )
        return lease

    def release(self, lease: Optional[CapacityLease], reason: str = "completed") -> None:
        """释放租约；``None``（取容量失败时）与重复释放都是 no-op。"""
        if lease is None:
            return
        get_gate().release(lease, reason or "completed")

    def heartbeat(self, lease: Optional[CapacityLease]) -> None:
        """长批次里刷新心跳，便于 status() 区分"正在生成"与"卡住"。"""
        if lease is None:
            return
        get_gate().heartbeat(lease)

    @staticmethod
    def describe(lease: Optional[CapacityLease]) -> Dict[str, Any]:
        """租约快照，供业务层把租约编号写进自己的调用记录（§14 可追溯）。"""
        if lease is None:
            return {}
        return lease.to_dict()


#: 进程内共享的默认适配器：无状态，可以随便复用。
DEFAULT_CAPACITY_ADAPTER = CapacityLeaseAdapter()


__all__ = ["CapacityLeaseAdapter", "DEFAULT_CAPACITY_ADAPTER"]
