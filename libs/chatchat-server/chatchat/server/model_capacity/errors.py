"""模型容量闸门异常族（里程碑0B · 重新设计方案 §8.2、AI预审方案 §12）。

闸门只抛这一族异常，业务层据此把页面状态收敛成「等待中 / 已取消 / 失败」，
不需要再去解析底层 ``openai`` 或 ``langchain`` 的错误文本：

- ``ModelCapacityQueueTimeoutError``：排队超过 ``MODEL_CAPACITY_QUEUE_TIMEOUT_SECONDS``
  仍未取得容量。租约已按 ``queue_timeout`` 出队并进入释放历史，不会留在队列里。
- ``ModelCapacityCancelledError``：排队中被取消（用户点了"取消等待"，或等待方
  自身的 asyncio 任务被取消）。已在运行的调用不会被强行中断，只标记
  ``cancel_requested``。
- ``ModelCapacityDuplicateError``：同一 (消费者类型, 业务对象, 请求编号) 已经在
  排队或运行，重复请求不再入队（§8.2「重复请求抑制」）。
- ``ModelCapacityReentrancyError``：外层（路由、整轮对话、整次 Agent Run、整个
  预审运行）已经持有一次调用的租约，内层又试图再取一次。容量上限为 1 且租约
  不可重入，这种情况下外层永远等不到自己释放，必然自锁，因此在取租约之前直接
  失败并说明是谁占着容量。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class ModelCapacityError(RuntimeError):
    """容量闸门错误基类；消息一律中文且带上可定位的业务标识。"""

    def __init__(self, message: str, *, lease: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        #: 触发本错误的租约快照（可能为 None），供 API 与前端显示"谁在等/谁在占"
        self.lease = lease


class ModelCapacityQueueTimeoutError(ModelCapacityError):
    """排队等待容量超时。"""


class ModelCapacityCancelledError(ModelCapacityError):
    """排队中的调用被取消。"""


class ModelCapacityDuplicateError(ModelCapacityError):
    """重复请求：同一业务请求已在排队或运行，不再重复入队。"""


class ModelCapacityReentrancyError(ModelCapacityError):
    """同一调用上下文里嵌套取租约（外层持锁、内层再取会自锁）。"""

    def __init__(
        self,
        message: str,
        *,
        lease: Optional[Dict[str, Any]] = None,
        outer_lease: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message, lease=lease)
        #: 当前正占着容量的那次调用
        self.outer_lease = outer_lease


__all__ = [
    "ModelCapacityError",
    "ModelCapacityQueueTimeoutError",
    "ModelCapacityCancelledError",
    "ModelCapacityDuplicateError",
    "ModelCapacityReentrancyError",
]
