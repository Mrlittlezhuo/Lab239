"""把容量闸门挂到最终 ChatModel 调用层（§8.2「在最终 ChatModel 调用层统一
包装所有同步、异步、普通和流式调用」）。

为什么重写这四个方法就够：langchain-core 的 ``invoke`` / ``stream`` /
``ainvoke`` / ``astream`` / ``batch`` 最终都汇聚到 ``_generate``、``_stream``、
``_agenerate``、``_astream``。``bind_tools``、``with_structured_output``、
Agent 规划循环、工具内部再次调用模型，走的都是同一个模型对象的这四个方法，
因此每一次实际调用都会独立排队——这正是"租约最小单位是一次实际模型调用"
的含义，也是"意图路由结束后先释放、回答再重新排队"不需要业务层配合的原因。

租约的取得与释放位置：

- 普通调用：进入 ``_generate`` / ``_agenerate`` 时取得，返回、异常或超时后在
  ``finally`` 里释放；
- 流式调用：在生成器体第一次被推进（请求真正发出）时取得，每收到一片刷新
  心跳，直到流正常结束、调用方取消、连接断开（``GeneratorExit``）或异常退出
  后在 ``finally`` 里释放；
- 模型对象构造、缓存与复用期间不持有任何容量（构造函数里只填字段）。

异步路径用 ``acquire_async``：等待发生在调用方自己的事件循环上，不阻塞循环
线程。否则"正在流式生成的请求"和"排队中的异步调用"会在同一个事件循环线程
上互相等死——前者收不到下一片就永远不释放，后者永远排不到。
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator, Optional

from langchain_chatchat.chat_models import ChatPlatformAI as ChatPlatformAIBase
from langchain_openai.chat_models import ChatOpenAI as ChatOpenAIBase
from langchain_openai.llms import OpenAI as OpenAIBase

from chatchat.utils import build_logger

from .consumers import normalize_channel
from .context import EMPTY_SPEC, ModelCallSpec, held_lease_ids, resolve_call_spec
from .errors import ModelCapacityLeaseNotHeldError
from .gate import CapacityLease, get_gate

logger = build_logger(__name__)

#: 构造期声明用的字段名（get_ChatOpenAI 通过它们把身份烘进模型对象）
CAPACITY_KWARGS = (
    "capacity_consumer",
    "capacity_channel",
    "capacity_business_object",
    "capacity_user_id",
    "capacity_request_id",
    "capacity_mode",
)

#: ``capacity_mode`` 的两种取值。
#: ``acquire``（默认，空串等同）：模型自己在四个汇聚点取放租约。
#: ``external``：租约由调用方（``lease_adapter.CapacityLeaseAdapter``）在业务
#: 代码里取放，模型只确认"当前上下文确实持着租约"再发请求。用于一次业务
#: 调用要按批次多次取放容量的场景（AI 静态预审按 AC 批次排队，§12.2.1）：
#: 租约身份只有业务层知道（消费者类型、业务对象、发起人），而闸门上限为 1
#: 且租约不可重入，模型再取一次必然自锁，所以这里改成"必须已持有"检查。
CAPACITY_MODE_ACQUIRE = "acquire"
CAPACITY_MODE_EXTERNAL = "external"

#: get_ChatOpenAI 也接受的短别名，业务层写起来更顺手
CAPACITY_KWARG_ALIASES = {
    "consumer": "capacity_consumer",
    "channel": "capacity_channel",
    "business_object": "capacity_business_object",
    "user_id": "capacity_user_id",
    "request_id": "capacity_request_id",
}


def pop_model_call_kwargs(kwargs: dict) -> dict:
    """从 ``**kwargs`` 里取走容量声明参数，返回构造模型对象时用的字段。

    ``get_ChatOpenAI`` 的 ``**kwargs`` 会原样传给 ``ChatOpenAI``，进而传给
    openai SDK；容量声明必须在这里摘掉，否则会变成非法的请求参数。
    """
    picked: dict = {}
    for alias, field in CAPACITY_KWARG_ALIASES.items():
        if alias in kwargs:
            picked[field] = kwargs.pop(alias)
    for field in CAPACITY_KWARGS:
        if field in kwargs:
            picked[field] = kwargs.pop(field)
    channel = normalize_channel(picked.get("capacity_channel"))
    if "capacity_channel" in picked and not channel:
        logger.warning(
            "容量声明里的通道 {} 无法识别，按登记表推导通道", picked.get("capacity_channel")
        )
    picked["capacity_channel"] = channel or ""
    return picked


class ModelCapacityCallMixin:
    """四个汇聚点的闸门包装；混入任意 langchain ChatModel / LLM 即可。"""

    # —— 构造期声明（由子类以 pydantic 字段声明，这里只按属性读取）——

    def _capacity_instance_spec(self) -> ModelCallSpec:
        return ModelCallSpec(
            consumer_type=str(getattr(self, "capacity_consumer", "") or ""),
            channel=str(getattr(self, "capacity_channel", "") or ""),
            business_object=str(getattr(self, "capacity_business_object", "") or ""),
            user_id=str(getattr(self, "capacity_user_id", "") or ""),
            request_id=str(getattr(self, "capacity_request_id", "") or ""),
            model_name=str(
                getattr(self, "model_name", "") or getattr(self, "model", "") or ""
            ),
        )

    def _capacity_spec(self, call_kind: str) -> ModelCallSpec:
        """调用期声明优先于构造期声明，再按登记表补通道。"""
        return resolve_call_spec(self._capacity_instance_spec())

    # —— 取放租约 ——

    def _capacity_acquire(self, call_kind: str) -> CapacityLease:
        gate = get_gate()
        spec = self._capacity_spec(call_kind)
        gate.check_reentrancy(spec, call_kind)
        return gate.acquire(spec, call_kind=call_kind)

    async def _capacity_acquire_async(self, call_kind: str) -> CapacityLease:
        gate = get_gate()
        spec = self._capacity_spec(call_kind)
        gate.check_reentrancy(spec, call_kind)
        return await gate.acquire_async(spec, call_kind=call_kind)

    # —— external 模式：租约由业务层持有，模型只确认确实持有 ——

    def _capacity_is_external(self) -> bool:
        return (
            str(getattr(self, "capacity_mode", "") or "").strip().lower()
            == CAPACITY_MODE_EXTERNAL
        )

    def _capacity_require_external_lease(self, call_kind: str) -> None:
        """``capacity_mode="external"`` 的前置检查：没有租约就不许发请求。

        没有这一步，"由业务层取容量"会退化成"谁都没取容量"：预审之类的调用
        绕过闸门直接打模型，§16.1B 的"最多一个并发生成"当场失效。
        """
        held = held_lease_ids()
        if not held:
            raise ModelCapacityLeaseNotHeldError(
                f"模型声明了 capacity_mode={CAPACITY_MODE_EXTERNAL}，"
                f"但当前上下文没有持有容量租约，拒绝发起 {call_kind} 调用。"
                f"请先用 model_capacity.CapacityLeaseAdapter.acquire(...) 取得租约，"
                f"并在 finally 里 release（AI 预审按 AC 批次取放，§12.2.1）。"
            )
        if not get_gate().active_held_leases():
            raise ModelCapacityLeaseNotHeldError(
                f"模型声明了 capacity_mode={CAPACITY_MODE_EXTERNAL}，"
                f"但上下文记录的租约 {'、'.join(held)} 都已释放，"
                f"拒绝发起 {call_kind} 调用。"
            )

    def _capacity_release(self, lease: CapacityLease, reason: str) -> None:
        try:
            released = get_gate().release(lease, reason)
            if released and lease.wait_seconds and lease.wait_seconds > 0.5:
                logger.info(
                    "模型调用排队 {:.1f} 秒后开始：{}（当前占用 {}/{}）",
                    lease.wait_seconds,
                    lease.spec.describe(),
                    len(get_gate().active_leases()),
                    get_gate().limit,
                )
        except Exception:  # 释放本身绝不能把业务异常盖掉
            logger.exception("释放模型容量租约失败（租约 {}）", lease.lease_id)

    # —— 四个汇聚点 ——

    def _generate(self, *args: Any, **kwargs: Any):
        """同步普通调用：真正发出请求前取得容量，返回/异常后释放。"""
        if self._capacity_is_external():
            self._capacity_require_external_lease("generate")
            return self._generate_without_capacity(*args, **kwargs)
        lease = self._capacity_acquire("generate")
        reason = "completed"
        try:
            return self._generate_without_capacity(*args, **kwargs)
        except BaseException:
            reason = "error"
            raise
        finally:
            self._capacity_release(lease, reason)

    def _generate_without_capacity(self, *args: Any, **kwargs: Any):
        """实际发请求的部分；测试可覆盖它以避免联网（闸门逻辑仍是生产实现）。"""
        return super()._generate(*args, **kwargs)  # type: ignore[misc]

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        """同步流式调用：请求发出时持有，断线/取消/异常都在 finally 释放。"""
        if self._capacity_is_external():
            self._capacity_require_external_lease("stream")
            yield from self._stream_without_capacity(*args, **kwargs)
            return
        lease = self._capacity_acquire("stream")
        reason = "stream_completed"
        try:
            for chunk in self._stream_without_capacity(*args, **kwargs):
                lease.heartbeat()
                yield chunk
        except GeneratorExit:
            # 调用方提前关闭生成器：用户取消、客户端断开、上游 break
            reason = "stream_disconnected"
            raise
        except BaseException:
            reason = "stream_error"
            raise
        finally:
            self._capacity_release(lease, reason)

    def _stream_without_capacity(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        return super()._stream(*args, **kwargs)  # type: ignore[misc]

    async def _agenerate(self, *args: Any, **kwargs: Any):
        """异步普通调用：等待容量不阻塞事件循环。"""
        if self._capacity_is_external():
            self._capacity_require_external_lease("agenerate")
            return await self._agenerate_without_capacity(*args, **kwargs)
        lease = await self._capacity_acquire_async("agenerate")
        reason = "completed"
        try:
            return await self._agenerate_without_capacity(*args, **kwargs)
        except BaseException:
            reason = "error"
            raise
        finally:
            self._capacity_release(lease, reason)

    async def _agenerate_without_capacity(self, *args: Any, **kwargs: Any):
        return await super()._agenerate(*args, **kwargs)  # type: ignore[misc]

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """异步流式调用：断线（GeneratorExit）与任务取消都释放租约。"""
        if self._capacity_is_external():
            self._capacity_require_external_lease("astream")
            async for chunk in self._astream_without_capacity(*args, **kwargs):
                yield chunk
            return
        lease = await self._capacity_acquire_async("astream")
        reason = "stream_completed"
        try:
            async for chunk in self._astream_without_capacity(*args, **kwargs):
                lease.heartbeat()
                yield chunk
        except GeneratorExit:
            reason = "stream_disconnected"
            raise
        except asyncio.CancelledError:
            reason = "stream_disconnected"
            raise
        except BaseException:
            reason = "stream_error"
            raise
        finally:
            # 释放是同步操作：异步生成器在 GeneratorExit 路径里不能 await，
            # 否则解释器会报 "async generator ignored GeneratorExit"
            self._capacity_release(lease, reason)

    async def _astream_without_capacity(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in super()._astream(*args, **kwargs):  # type: ignore[misc]
            yield chunk


class GatedChatOpenAI(ModelCapacityCallMixin, ChatOpenAIBase):  # type: ignore[misc]
    """所有生成模型的唯一构造类型：``get_ChatOpenAI`` 返回它。

    身份可以在构造期声明（``capacity_*`` 字段），也可以在调用期用
    ``model_call_context`` 声明；两者都不代表持有容量。
    """

    capacity_consumer: str = ""
    capacity_channel: str = ""
    capacity_business_object: str = ""
    capacity_user_id: str = ""
    capacity_request_id: str = ""
    #: "" / "acquire"：自己取放租约；"external"：租约由业务层持有（AI 预审等）
    capacity_mode: str = ""


class GatedOpenAI(ModelCapacityCallMixin, OpenAIBase):  # type: ignore[misc]
    """``langchain_openai.llms.OpenAI``（补全式接口）的同类包装。

    ``get_OpenAI`` 返回本类，闸门行为与 ``GatedChatOpenAI`` 一致：一次实际
    调用一次租约，流式在首个分片取得、断线或异常时在 ``finally`` 释放。
    """

    capacity_consumer: str = ""
    capacity_channel: str = ""
    capacity_business_object: str = ""
    capacity_user_id: str = ""
    capacity_request_id: str = ""
    #: "" / "acquire"：自己取放租约；"external"：租约由业务层持有（AI 预审等）
    capacity_mode: str = ""


class GatedChatPlatformAI(ModelCapacityCallMixin, ChatPlatformAIBase):  # type: ignore[misc]
    """``langchain_chatchat`` 平台模型（action_model 路径）的同类包装。

    平台模型是主聊天路径实际使用的生成模型，必须与 ``ChatOpenAI`` 走同一个
    闸门，否则"最多一个并发"只是局部约束。类名以 ``Gated`` 开头，因此不会被
    ``audit`` 的构造点扫描当成未覆盖调用。
    """

    capacity_consumer: str = ""
    capacity_channel: str = ""
    capacity_business_object: str = ""
    capacity_user_id: str = ""
    capacity_request_id: str = ""
    #: "" / "acquire"：自己取放租约；"external"：租约由业务层持有（AI 预审等）
    capacity_mode: str = ""


# ---------------------------------------------------------------------------
# 不走 ChatModel 的生成调用（例如直接用 openai SDK 的兼容接口）用这个入口
# ---------------------------------------------------------------------------


def _manual_spec(
    consumer: Optional[str],
    channel: Optional[str],
    business_object: Optional[str],
    user_id: Optional[str],
    request_id: Optional[str],
    model_name: Optional[str],
    note: Optional[str],
) -> ModelCallSpec:
    declared = ModelCallSpec(
        consumer_type=(consumer or "").strip(),
        channel=normalize_channel(channel) or (channel or "").strip(),
        business_object=business_object or "",
        user_id=user_id or "",
        request_id=request_id or "",
        model_name=model_name or "",
        note=note or "",
    )
    return declared.merge(EMPTY_SPEC).resolved()


@contextmanager
def gated_call(
    consumer: Optional[str] = None,
    *,
    channel: Optional[str] = None,
    business_object: Optional[str] = None,
    user_id: Optional[str] = None,
    request_id: Optional[str] = None,
    model_name: Optional[str] = None,
    note: Optional[str] = None,
    call_kind: str = "manual",
    timeout: Optional[float] = None,
    dedup: bool = True,
) -> Iterator[CapacityLease]:
    """手动取得**一次**调用的租约（同步）。

    只给不走 ``GatedChatOpenAI`` 的生成调用用（例如直接用 openai SDK 打兼容
    接口）。已经经过闸门的模型不要再套这一层——外层持租约、内层再取一次会
    直接抛 ``ModelCapacityReentrancyError``（这是刻意的自锁保护）。
    """
    spec = _manual_spec(
        consumer, channel, business_object, user_id, request_id, model_name, note
    )
    gate = get_gate()
    gate.check_reentrancy(spec, call_kind)
    lease = gate.acquire(spec, call_kind=call_kind, timeout=timeout, dedup=dedup)
    reason = "completed"
    try:
        yield lease
    except GeneratorExit:
        reason = "stream_disconnected"
        raise
    except BaseException:
        reason = "error"
        raise
    finally:
        get_gate().release(lease, reason)


class _AsyncGatedCall:
    """``gated_call`` 的异步版本（``async with``）。"""

    def __init__(
        self,
        consumer: Optional[str],
        channel: Optional[str],
        business_object: Optional[str],
        user_id: Optional[str],
        request_id: Optional[str],
        model_name: Optional[str],
        note: Optional[str],
        call_kind: str,
        timeout: Optional[float],
        dedup: bool,
    ):
        self._spec = _manual_spec(
            consumer, channel, business_object, user_id, request_id, model_name, note
        )
        self._call_kind = call_kind
        self._timeout = timeout
        self._dedup = dedup
        self._lease: Optional[CapacityLease] = None

    async def __aenter__(self) -> CapacityLease:
        gate = get_gate()
        gate.check_reentrancy(self._spec, self._call_kind)
        self._lease = await gate.acquire_async(
            self._spec,
            call_kind=self._call_kind,
            timeout=self._timeout,
            dedup=self._dedup,
        )
        return self._lease

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        lease = self._lease
        if lease is None:
            return False
        reason = "completed" if exc_type is None else "error"
        if exc_type is asyncio.CancelledError:
            reason = "stream_disconnected"
        get_gate().release(lease, reason)
        return False


def agated_call(
    consumer: Optional[str] = None,
    *,
    channel: Optional[str] = None,
    business_object: Optional[str] = None,
    user_id: Optional[str] = None,
    request_id: Optional[str] = None,
    model_name: Optional[str] = None,
    note: Optional[str] = None,
    call_kind: str = "manual",
    timeout: Optional[float] = None,
    dedup: bool = True,
) -> _AsyncGatedCall:
    """手动取得一次调用的租约（异步；等待不阻塞事件循环）。"""
    return _AsyncGatedCall(
        consumer,
        channel,
        business_object,
        user_id,
        request_id,
        model_name,
        note,
        call_kind,
        timeout,
        dedup,
    )


__all__ = [
    "ModelCapacityCallMixin",
    "GatedChatOpenAI",
    "GatedOpenAI",
    "gated_call",
    "agated_call",
    "pop_model_call_kwargs",
    "CAPACITY_KWARGS",
    "CAPACITY_KWARG_ALIASES",
]
