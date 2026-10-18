"""统一生成模型容量闸门（里程碑0B · 重新设计方案 §8.2、§15.0B、§16.1B；
AI预审方案 §12）。

上游账号托管平台的生成模型同一时间只能处理一个请求，这是项目所有者确认的
运行事实。本模块是**唯一**的容量闸门：

- 租约的最小单位是"一次实际模型调用"，不是 HTTP 请求、聊天轮次、Agent Run、
  LangGraph 运行、预审运行或论文批任务；
- 任意时刻实际运行的生成调用数 ``<= MODEL_CAPACITY_LIMIT``（默认 1）；
- 三条业务通道（前台交互 / 用户主动短任务 / 后台批任务）同通道 FIFO，
  高优先级可以排在尚未开始的低优先级之前，但绝不中断已经开始的生成；
- 排队可见（位置、等待数、当前持有者）、可取消、可超时、重复请求抑制；
- 每次取放都留下运行时证据，并记录进程生命周期内的**最大占用值**，
  验收以运行时记录证明"实际占用从未超过 1"，不靠静态扫描。

同步等待用 ``threading.Condition``；异步等待用调用方事件循环上的
``asyncio.Future``，由释放方通过 ``call_soon_threadsafe`` 唤醒。异步路径**绝不
阻塞事件循环**：否则正在流式生成的请求无法继续收片，排队方与它会在同一个
事件循环线程上互相等死。

进程内注册表不跨进程、不落库，因此进程重启后旧租约天然不会显示为占用；
``reset_stale_leases()`` 负责清理同一进程内因异常退出而残留的租约，并把清理
动作写日志（供 lifespan 启动钩子调用）。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from chatchat.utils import build_logger

from .consumers import CHANNEL_LABELS, channel_priority, describe_consumers
from .context import (
    ModelCallSpec,
    held_lease_ids,
    mark_lease_held,
    unmark_lease_held,
)
from .errors import (
    ModelCapacityCancelledError,
    ModelCapacityDuplicateError,
    ModelCapacityQueueTimeoutError,
    ModelCapacityReentrancyError,
)

logger = build_logger(__name__)

#: 容量上限。上游约束是"同一时间只能处理一个生成请求"，默认 1；
#: 只有上游真的放开并发时才允许通过环境变量调大。
ENV_CAPACITY_LIMIT = "MODEL_CAPACITY_LIMIT"
DEFAULT_CAPACITY_LIMIT = 1
#: 排队等待容量的最长秒数；超时后出队并抛 ModelCapacityQueueTimeoutError
ENV_QUEUE_TIMEOUT_SECONDS = "MODEL_CAPACITY_QUEUE_TIMEOUT_SECONDS"
DEFAULT_QUEUE_TIMEOUT_SECONDS = 120.0
#: 释放/取消/超时历史保留条数（观测用，不落库）
ENV_HISTORY_SIZE = "MODEL_CAPACITY_HISTORY_SIZE"
DEFAULT_HISTORY_SIZE = 200

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_RELEASED = "released"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"

#: 释放原因（机器可读码 → 中文说明）。流式断线与用户取消都走
#: ``stream_disconnected``：从闸门视角看都是"调用方不再消费这个流"。
RELEASE_REASON_LABELS: Dict[str, str] = {
    "completed": "正常完成",
    "error": "调用异常退出",
    "stream_completed": "流式正常结束",
    "stream_disconnected": "流式被取消或连接断开",
    "stream_error": "流式过程中异常退出",
    "queue_timeout": "排队超时出队",
    "cancelled": "排队中被取消",
    "service_restart": "服务重启清理旧租约",
    "process_shutdown": "进程退出清理",
}


def env_capacity_limit() -> int:
    try:
        return max(1, int(os.environ.get(ENV_CAPACITY_LIMIT, DEFAULT_CAPACITY_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_LIMIT


def env_queue_timeout_seconds() -> float:
    try:
        value = float(os.environ.get(ENV_QUEUE_TIMEOUT_SECONDS, DEFAULT_QUEUE_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_QUEUE_TIMEOUT_SECONDS


def _iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


@dataclass
class CapacityLease:
    """一次实际模型调用的容量租约（也是队列里的一项）。"""

    lease_id: str
    seq: int
    spec: ModelCallSpec
    call_kind: str  # generate / stream / agenerate / astream / manual
    state: str = STATE_QUEUED
    enqueued_at: float = field(default_factory=time.time)
    acquired_at: Optional[float] = None
    released_at: Optional[float] = None
    last_heartbeat_at: Optional[float] = None
    release_reason: str = ""
    thread_name: str = field(default_factory=lambda: threading.current_thread().name)
    cancel_requested: bool = False
    cancel_requested_at: Optional[float] = None
    dedup_key: Optional[Tuple[str, str, str]] = None
    #: 异步等待者的事件循环与 future（不参与 to_dict）
    wake_loop: Optional[asyncio.AbstractEventLoop] = None
    wake_future: Optional["asyncio.Future"] = None
    #: 所属闸门，用于 heartbeat()
    _gate: Optional["ModelCapacityGate"] = field(default=None, repr=False)

    @property
    def wait_seconds(self) -> Optional[float]:
        if self.acquired_at is None:
            return None
        return round(self.acquired_at - self.enqueued_at, 3)

    @property
    def hold_seconds(self) -> Optional[float]:
        if self.acquired_at is None:
            return None
        end = self.released_at or time.time()
        return round(end - self.acquired_at, 3)

    def to_dict(self) -> Dict[str, Any]:
        """运行时占用证据：§8.2 要求的最小字段集。"""
        spec = self.spec
        return {
            "lease_id": self.lease_id,
            "queue_seq": self.seq,
            "state": self.state,
            "consumer_type": spec.consumer_type,
            "consumer_label": _consumer_label(spec.consumer_type),
            "channel": spec.channel,
            "channel_label": CHANNEL_LABELS.get(spec.channel, spec.channel),
            "channel_priority": channel_priority(spec.channel),
            "business_object": spec.business_object,
            "user_id": spec.user_id,
            "request_id": spec.request_id,
            "model_name": spec.model_name,
            "call_kind": self.call_kind,
            "enqueued_at": _iso(self.enqueued_at),
            "acquired_at": _iso(self.acquired_at),
            "released_at": _iso(self.released_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "wait_seconds": self.wait_seconds,
            "hold_seconds": self.hold_seconds,
            "release_reason": self.release_reason,
            "release_reason_label": RELEASE_REASON_LABELS.get(
                self.release_reason, self.release_reason
            ),
            "cancel_requested": self.cancel_requested,
            "thread": self.thread_name,
        }

    def heartbeat(self) -> None:
        """流式调用每收到一片调一次，证明"这次调用还活着"。"""
        gate = self._gate
        if gate is not None:
            gate.heartbeat(self)


def _consumer_label(consumer_type: str) -> str:
    from .consumers import lookup_consumer

    spec = lookup_consumer(consumer_type)
    return spec.label if spec else consumer_type


class ModelCapacityGate:
    """进程内生成模型容量闸门 + 占用证据注册表。"""

    def __init__(
        self,
        limit: Optional[int] = None,
        *,
        queue_timeout_seconds: Optional[float] = None,
        history_size: Optional[int] = None,
    ):
        self._limit = max(1, int(limit)) if limit is not None else env_capacity_limit()
        self._queue_timeout_seconds = (
            float(queue_timeout_seconds)
            if queue_timeout_seconds is not None
            else env_queue_timeout_seconds()
        )
        if history_size is None:
            try:
                history_size = int(os.environ.get(ENV_HISTORY_SIZE, DEFAULT_HISTORY_SIZE))
            except (TypeError, ValueError):
                history_size = DEFAULT_HISTORY_SIZE
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._waiting: List[CapacityLease] = []
        self._active: Dict[str, CapacityLease] = {}
        #: 重复请求抑制：(消费者, 业务对象, 请求编号) → 正在排队或运行的租约
        self._by_key: Dict[Tuple[str, str, str], CapacityLease] = {}
        self._history: Deque[CapacityLease] = deque(maxlen=max(1, history_size))
        self._seq = 0
        # —— 进程生命周期内的累计证据 ——
        self._max_active_observed = 0
        self._total_enqueued = 0
        self._total_acquired = 0
        self._total_waited = 0
        self._total_timeouts = 0
        self._total_cancels = 0
        self._total_duplicates = 0

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def max_active_observed(self) -> int:
        """本次进程生命周期内出现过的最大占用值（§16.1B 第 7 条的验收依据）。"""
        return self._max_active_observed

    @property
    def queue_timeout_seconds(self) -> float:
        return self._queue_timeout_seconds

    # ------------------------------------------------------------------
    # 取租约
    # ------------------------------------------------------------------

    def check_reentrancy(self, spec: ModelCallSpec, call_kind: str) -> None:
        """外层已持租约时再取一次必然自锁：立刻失败。

        上下文里残留但已经释放的租约编号会被清掉，不会把后续调用永久毒死
        （流式生成器被丢弃时，生成器体的 finally 可能晚于下一次调用才跑）。
        """
        held = held_lease_ids()
        if not held:
            return
        with self._lock:
            outer = [self._active[lease_id] for lease_id in held if lease_id in self._active]
        if not outer:
            from .context import clear_held_lease_ids

            clear_held_lease_ids()
            return
        first = outer[0]
        raise ModelCapacityReentrancyError(
            f"同一调用上下文里重复取得模型容量租约会自锁：当前上下文已持有 "
            f"{len(outer)} 次调用的租约（{first.spec.describe()}，"
            f"租约 {first.lease_id}，已持有 {first.hold_seconds or 0} 秒），"
            f"现在又要为「{spec.describe()}」取一次容量。"
            f"路由、整轮对话、整次 Agent Run 和整个预审运行都不允许先持租约，"
            f"容量只在每次实际模型调用内部取得（设计方案 §8.2）。",
            outer_lease=first.to_dict(),
        )

    def acquire(
        self,
        spec: ModelCallSpec,
        *,
        call_kind: str = "generate",
        timeout: Optional[float] = None,
        dedup: bool = True,
        mark_held: bool = True,
    ) -> CapacityLease:
        """同步取得一次调用的容量租约（阻塞当前线程）。

        ``mark_held`` 让调用方决定"是否在调用方上下文里登记已持有"：
        异步路径把取租约放进线程池执行时必须在协程自己的上下文里登记，
        因此传 ``False`` 之后再手动 ``mark_lease_held``。
        """
        lease = self._enqueue(spec, call_kind=call_kind, dedup=dedup)
        deadline = time.monotonic() + self._resolve_timeout(timeout)
        with self._cond:
            while True:
                if lease.state == STATE_CANCELLED:
                    raise ModelCapacityCancelledError(
                        f"排队中的模型调用已被取消：{lease.spec.describe()}",
                        lease=lease.to_dict(),
                    )
                if self._try_activate(lease):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    ahead = self._position_locked(lease) - 1
                    occupied = f"{len(self._active)}/{self._limit}"
                    waited = round(time.time() - lease.enqueued_at, 1)
                    limit_seconds = round(self._resolve_timeout(timeout), 1)
                    self._expire_locked(lease)
                    raise ModelCapacityQueueTimeoutError(
                        f"等待模型容量超时（已等待 {waited} 秒，上限 {limit_seconds} 秒）："
                        f"{lease.spec.describe()}。当前占用 {occupied}，"
                        f"前方还有 {ahead} 个任务。",
                        lease=lease.to_dict(),
                    )
                self._total_waited += 1
                self._cond.wait(remaining)
        if mark_held:
            mark_lease_held(lease.lease_id)
        return lease

    async def acquire_async(
        self,
        spec: ModelCallSpec,
        *,
        call_kind: str = "agenerate",
        timeout: Optional[float] = None,
        dedup: bool = True,
        mark_held: bool = True,
    ) -> CapacityLease:
        """异步取得租约：等待期间不阻塞事件循环。

        正在流式生成的调用与排队中的调用很可能在同一个事件循环线程上；同步
        等待会让前者无法继续收片，两边互相等死。这里等的是调用方事件循环上的
        future，由释放方 ``call_soon_threadsafe`` 唤醒。
        """
        loop = asyncio.get_running_loop()
        lease = self._enqueue(spec, call_kind=call_kind, dedup=dedup)
        deadline = time.monotonic() + self._resolve_timeout(timeout)
        while True:
            waiter: "asyncio.Future" = loop.create_future()
            with self._cond:
                if lease.state == STATE_CANCELLED:
                    raise ModelCapacityCancelledError(
                        f"排队中的模型调用已被取消：{lease.spec.describe()}",
                        lease=lease.to_dict(),
                    )
                if self._try_activate(lease):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    ahead = self._position_locked(lease) - 1
                    occupied = f"{len(self._active)}/{self._limit}"
                    waited = round(time.time() - lease.enqueued_at, 1)
                    limit_seconds = round(self._resolve_timeout(timeout), 1)
                    self._expire_locked(lease)
                    raise ModelCapacityQueueTimeoutError(
                        f"等待模型容量超时（已等待 {waited} 秒，上限 {limit_seconds} 秒）："
                        f"{lease.spec.describe()}。当前占用 {occupied}，"
                        f"前方还有 {ahead} 个任务。",
                        lease=lease.to_dict(),
                    )
                self._total_waited += 1
                lease.wake_loop = loop
                lease.wake_future = waiter
            try:
                await asyncio.wait_for(waiter, remaining)
            except asyncio.TimeoutError:
                with self._cond:
                    self._expire_locked(lease)
                raise ModelCapacityQueueTimeoutError(
                    f"等待模型容量超时（上限 {round(self._resolve_timeout(timeout), 1)} 秒）："
                    f"{lease.spec.describe()}",
                    lease=lease.to_dict(),
                ) from None
            except asyncio.CancelledError:
                # 等待方自己被取消（例如前端断开）：按取消出队，不留队列项
                with self._cond:
                    self._cancel_locked(lease, reason="cancelled")
                raise ModelCapacityCancelledError(
                    f"排队中的模型调用被取消：{lease.spec.describe()}",
                    lease=lease.to_dict(),
                ) from None
            finally:
                with self._cond:
                    if lease.wake_future is waiter:
                        lease.wake_future = None
                        lease.wake_loop = None
        if mark_held:
            mark_lease_held(lease.lease_id)
        return lease

    def _resolve_timeout(self, timeout: Optional[float]) -> float:
        if timeout is None:
            return self._queue_timeout_seconds
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            return self._queue_timeout_seconds
        return value if value > 0 else self._queue_timeout_seconds

    def _enqueue(
        self, spec: ModelCallSpec, *, call_kind: str, dedup: bool
    ) -> CapacityLease:
        resolved = spec.resolved()
        key = resolved.dedup_key() if dedup else None
        with self._cond:
            if key is not None:
                existing = self._by_key.get(key)
                if existing is not None:
                    self._total_duplicates += 1
                    raise ModelCapacityDuplicateError(
                        f"同一请求已在{'运行中' if existing.state == STATE_RUNNING else '排队中'}，"
                        f"不重复入队：{existing.spec.describe()}（租约 {existing.lease_id}，"
                        f"排队序号 {existing.seq}）",
                        lease=existing.to_dict(),
                    )
            self._seq += 1
            lease = CapacityLease(
                lease_id=f"MC-{self._seq}-{uuid.uuid4().hex[:8]}",
                seq=self._seq,
                spec=resolved,
                call_kind=call_kind,
                state=STATE_QUEUED,
                enqueued_at=time.time(),
                dedup_key=key,
                _gate=self,
            )
            if key is not None:
                self._by_key[key] = lease
            self._waiting.append(lease)
            self._total_enqueued += 1
            return lease

    def _ordered_waiting_locked(self) -> List[CapacityLease]:
        """按 (通道优先级, 排队序号) 排序的等待清单：同通道 FIFO，
        高优先级可以排在尚未开始的低优先级之前。"""
        return sorted(
            self._waiting, key=lambda item: (channel_priority(item.spec.channel), item.seq)
        )

    def _try_activate(self, lease: CapacityLease) -> bool:
        """只有"当前最靠前的等待者 + 还有容量"才允许开始，持锁调用。"""
        if len(self._active) >= self._limit:
            return False
        ordered = self._ordered_waiting_locked()
        if not ordered or ordered[0] is not lease:
            return False
        self._waiting.remove(lease)
        lease.state = STATE_RUNNING
        lease.acquired_at = time.time()
        lease.last_heartbeat_at = lease.acquired_at
        self._active[lease.lease_id] = lease
        self._total_acquired += 1
        if len(self._active) > self._max_active_observed:
            self._max_active_observed = len(self._active)
        return True

    def _position_locked(self, lease: CapacityLease) -> int:
        ordered = self._ordered_waiting_locked()
        for index, item in enumerate(ordered):
            if item is lease:
                return index + 1
        return 0

    # ------------------------------------------------------------------
    # 释放 / 取消 / 超时
    # ------------------------------------------------------------------

    def release(self, lease: CapacityLease, reason: str = "completed") -> bool:
        """释放租约；重复释放是幂等的（第一次的原因生效）。

        调用方必须在 ``finally`` 里调用它，覆盖正常返回、异常、超时、
        流式结束、用户取消与连接断开（``GeneratorExit``）全部路径。
        """
        unmark_lease_held(lease.lease_id)
        with self._cond:
            if lease.state != STATE_RUNNING:
                return False
            self._active.pop(lease.lease_id, None)
            self._forget_key_locked(lease)
            lease.state = STATE_RELEASED
            lease.released_at = time.time()
            lease.release_reason = reason
            self._history.append(lease)
            self._wake_waiting_locked()
            return True

    def heartbeat(self, lease: CapacityLease) -> None:
        with self._cond:
            if lease.state == STATE_RUNNING:
                lease.last_heartbeat_at = time.time()

    def cancel(
        self,
        *,
        lease_id: Optional[str] = None,
        request_id: Optional[str] = None,
        business_object: Optional[str] = None,
        consumer_type: Optional[str] = None,
        reason: str = "cancelled",
    ) -> Dict[str, Any]:
        """取消尚未开始的排队调用；已在运行的调用只标记 ``cancel_requested``。

        容量上限为 1 且不能中断已经发出的生成（§8.2「不能打断已开始的流式
        生成」），所以运行中的调用只能请求它自己收尾。
        """
        if not any((lease_id, request_id, business_object, consumer_type)):
            return {"cancelled": [], "cancel_requested": [], "reason": "未指定筛选条件"}
        with self._cond:
            targets = [
                item
                for item in list(self._waiting) + list(self._active.values())
                if self._matches(
                    item,
                    lease_id=lease_id,
                    request_id=request_id,
                    business_object=business_object,
                    consumer_type=consumer_type,
                )
            ]
            cancelled: List[Dict[str, Any]] = []
            requested: List[Dict[str, Any]] = []
            for item in targets:
                if item.state == STATE_QUEUED:
                    self._cancel_locked(item, reason="cancelled")
                    cancelled.append(item.to_dict())
                elif item.state == STATE_RUNNING:
                    item.cancel_requested = True
                    item.cancel_requested_at = time.time()
                    requested.append(item.to_dict())
            self._wake_waiting_locked()
            return {
                "cancelled": cancelled,
                "cancel_requested": requested,
                "reason": "已出队" if cancelled else "运行中的调用只能请求自行收尾",
            }

    @staticmethod
    def _matches(
        lease: CapacityLease,
        *,
        lease_id: Optional[str],
        request_id: Optional[str],
        business_object: Optional[str],
        consumer_type: Optional[str],
    ) -> bool:
        if lease_id:
            return lease.lease_id == lease_id
        if request_id and lease.spec.request_id != request_id:
            return False
        if business_object and lease.spec.business_object != business_object:
            return False
        if consumer_type and lease.spec.consumer_type != consumer_type:
            return False
        return bool(request_id or business_object or consumer_type)

    def _cancel_locked(self, lease: CapacityLease, *, reason: str) -> None:
        if lease.state not in (STATE_QUEUED, STATE_RUNNING):
            return
        if lease in self._waiting:
            self._waiting.remove(lease)
        self._active.pop(lease.lease_id, None)
        self._forget_key_locked(lease)
        lease.state = STATE_CANCELLED
        lease.released_at = time.time()
        lease.release_reason = reason
        self._total_cancels += 1
        self._history.append(lease)

    def _expire_locked(self, lease: CapacityLease) -> None:
        if lease.state not in (STATE_QUEUED, STATE_RUNNING):
            return
        if lease in self._waiting:
            self._waiting.remove(lease)
        self._active.pop(lease.lease_id, None)
        self._forget_key_locked(lease)
        lease.state = STATE_EXPIRED
        lease.released_at = time.time()
        lease.release_reason = "queue_timeout"
        self._total_timeouts += 1
        self._history.append(lease)

    def _forget_key_locked(self, lease: CapacityLease) -> None:
        if lease.dedup_key and self._by_key.get(lease.dedup_key) is lease:
            self._by_key.pop(lease.dedup_key, None)

    def _wake_waiting_locked(self) -> None:
        """唤醒等待者：同步等待者用 Condition，异步等待者投递到各自事件循环。"""
        for item in self._waiting:
            waiter = item.wake_future
            if waiter is None or waiter.done():
                continue
            loop = item.wake_loop
            if loop is None or loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(_settle_waiter, waiter)
            except RuntimeError:  # 事件循环正在关闭
                pass
        self._cond.notify_all()

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """运行时占用证据（§8.2 最后一段、§16.1B 第 7 条）。"""
        with self._cond:
            waiting = self._ordered_waiting_locked()
            return {
                "capacity_limit": self._limit,
                "active_count": len(self._active),
                "max_active_observed": self._max_active_observed,
                "queue_length": len(self._waiting),
                "queue_timeout_seconds": self._queue_timeout_seconds,
                "holders": [item.to_dict() for item in self._active.values()],
                "waiting": [
                    {**item.to_dict(), "position": index + 1}
                    for index, item in enumerate(waiting)
                ],
                "recent_releases": [
                    item.to_dict() for item in reversed(self._history)
                ],
                "counters": {
                    "enqueued": self._total_enqueued,
                    "acquired": self._total_acquired,
                    "wait_rounds": self._total_waited,
                    "queue_timeouts": self._total_timeouts,
                    "cancelled": self._total_cancels,
                    "duplicate_suppressed": self._total_duplicates,
                },
                "channels": [
                    {
                        "channel": channel,
                        "label": label,
                        "priority": channel_priority(channel),
                        "waiting": sum(
                            1
                            for item in self._waiting
                            if item.spec.channel == channel
                        ),
                        "active": sum(
                            1
                            for item in self._active.values()
                            if item.spec.channel == channel
                        ),
                    }
                    for channel, label in CHANNEL_LABELS.items()
                ],
                "consumers": describe_consumers(),
                "observed_at": _iso(time.time()),
            }

    def active_leases(self) -> List[Dict[str, Any]]:
        with self._cond:
            return [item.to_dict() for item in self._active.values()]

    def active_held_leases(self) -> List[CapacityLease]:
        """当前上下文持有、且仍在运行的租约对象。

        ``capacity_mode="external"`` 的模型在发请求前用它确认"业务层确实替我
        取到了容量"。上下文里残留但已释放的租约编号不算（流式生成器被丢弃时
        生成器体的 ``finally`` 可能晚于下一次调用才跑）。
        """
        held = held_lease_ids()
        if not held:
            return []
        with self._cond:
            return [self._active[lease_id] for lease_id in held if lease_id in self._active]

    def reset_stale_leases(self, *, reason: str = "service_restart") -> Dict[str, Any]:
        """清理本进程内残留的租约（启动钩子调用）。

        租约是进程内状态，进程重启后天然不会显示为占用；但同一进程里如果
        有调用被硬杀死（``os._exit``、C 扩展崩溃）或被丢弃的生成器没跑到
        ``finally``，租约会永远占着唯一容量。这里把它们收敛成"已释放"，
        写审计日志，让队列继续跑（§8.2 最后一段、§12.4 最后一段）。
        """
        with self._cond:
            stale = list(self._active.values()) + list(self._waiting)
            for item in stale:
                if item.state == STATE_RUNNING:
                    self._active.pop(item.lease_id, None)
                elif item in self._waiting:
                    self._waiting.remove(item)
                self._forget_key_locked(item)
                item.state = STATE_RELEASED
                item.released_at = time.time()
                item.release_reason = reason
                self._history.append(item)
            self._wake_waiting_locked()
            cleared = [item.to_dict() for item in stale]
        if cleared:
            logger.warning(
                "模型容量闸门启动清理：释放 {} 个残留租约（原因 {}）：{}",
                len(cleared),
                reason,
                "; ".join(
                    f"{item['consumer_type']}/{item['business_object'] or '-'}"
                    f"/{item['lease_id']}"
                    for item in cleared
                ),
            )
        else:
            logger.info("模型容量闸门启动清理：没有残留租约需要释放")
        return {"cleared": len(cleared), "leases": cleared, "reason": reason}

    def shutdown(self, *, reason: str = "process_shutdown") -> Dict[str, Any]:
        """进程退出前把仍在持有的租约标记释放，避免观测里出现"永远运行中"。"""
        return self.reset_stale_leases(reason=reason)

    def reset_counters(self) -> None:
        """测试用：清空累计计数与历史，保留正在持有的租约（应为空）。"""
        with self._cond:
            self._history.clear()
            self._max_active_observed = len(self._active)
            self._total_enqueued = 0
            self._total_acquired = 0
            self._total_waited = 0
            self._total_timeouts = 0
            self._total_cancels = 0
            self._total_duplicates = 0
            self._seq = 0


def _settle_waiter(waiter: "asyncio.Future") -> None:
    if not waiter.done():
        waiter.set_result(None)


# ---------------------------------------------------------------------------
# 进程内单例与启动/退出钩子
# ---------------------------------------------------------------------------

_GATE: Optional[ModelCapacityGate] = None
_GATE_LOCK = threading.Lock()


def get_gate() -> ModelCapacityGate:
    global _GATE
    if _GATE is None:
        with _GATE_LOCK:
            if _GATE is None:
                _GATE = ModelCapacityGate()
    return _GATE


def set_gate(gate: Optional[ModelCapacityGate]) -> None:
    """替换单例（测试隔离用；生产代码不要调）。"""
    global _GATE
    _GATE = gate


def reset_stale_leases(*, reason: str = "service_restart") -> Dict[str, Any]:
    """释放单例闸门里残留的租约（lifespan 启动钩子调用，写审计日志）。"""
    return get_gate().reset_stale_leases(reason=reason)


def initialize_model_capacity() -> Dict[str, Any]:
    """lifespan 启动钩子：记录闸门配置并清理残留租约（§15.0B 第 4/6 条）。

    持久化任务（预审、论文子任务、Agent Run）的运行状态收敛由各业务模块在
    自己的启动检查里完成；这里只保证容量闸门本身不会带着旧租约启动。
    """
    gate = get_gate()
    cleared = gate.reset_stale_leases(reason="service_restart")
    logger.info(
        "模型容量闸门已启用：容量上限 {}，排队超时 {} 秒，通道优先级 {}",
        gate.limit,
        gate.queue_timeout_seconds,
        {channel: channel_priority(channel) for channel, label in CHANNEL_LABELS.items()},
    )
    return {"capacity_limit": gate.limit, "cleared_stale_leases": cleared}


def shutdown_model_capacity() -> Dict[str, Any]:
    """lifespan 关闭钩子。"""
    if _GATE is None:
        return {"cleared": 0}
    return _GATE.shutdown()


__all__ = [
    "CapacityLease",
    "ModelCapacityGate",
    "get_gate",
    "set_gate",
    "reset_stale_leases",
    "initialize_model_capacity",
    "shutdown_model_capacity",
    "env_capacity_limit",
    "env_queue_timeout_seconds",
    "RELEASE_REASON_LABELS",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_RELEASED",
    "STATE_CANCELLED",
    "STATE_EXPIRED",
]
