"""统一模型容量闸门离线测试（里程碑0B · 重新设计方案 §8.2、§16.1B；
AI预审方案 §12、P-10）。

全部使用**假 ChatModel 子类**：只替换真正发请求的四个汇聚点
（``_generate_without_capacity`` 等），闸门、队列、租约与包装逻辑跑的都是
生产实现。不联网、不调用真实模型、不读用户数据库。

覆盖 §16.1B 放行标准与 P-10 用例：

1. 并发压力下运行时最大占用值始终为 1（普通 / 流式 / 异步 / 异步流式）；
2. 流式在请求发出时持有，正常结束、用户取消、连接断开（GeneratorExit）、
   异常退出都在 finally 释放；模型对象构造不持容量；
3. 三条通道：同通道 FIFO，高优先级排在尚未开始的低优先级之前，
   后台批任务按子任务释放，前台聊天能插到下一个子任务前面；
4. 排队可见（位置、等待数、持有者）、可取消、可超时、重复请求抑制；
5. 外层持租约、内层再取 → ModelCapacityReentrancyError，不自锁；
6. 意图路由完成后释放，回答重新排队（用真实 IntentRouter + LlmIntentProvider）；
7. 绕开闸门的 ChatOpenAI 构造点会被扫描发现；embedding 不被塞进生成闸门；
8. 残留租约可被 reset_stale_leases 收敛；观测端点要求登录。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

import chatchat.server.model_capacity as mc
from chatchat.server.model_capacity import GatedChatOpenAI, ModelCapacityGate
from chatchat.server.model_capacity import audit as capacity_audit
from chatchat.server.model_capacity.context import clear_held_lease_ids
from chatchat.server.model_capacity.gate import (
    RELEASE_REASON_LABELS,
    env_capacity_limit,
    env_queue_timeout_seconds,
)

# ---------------------------------------------------------------------------
# 夹具与假模型
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate():
    """每个测试一套全新的闸门单例，避免占用计数互相污染。"""
    previous = mc.get_gate()
    fresh = ModelCapacityGate(limit=1, queue_timeout_seconds=5.0, history_size=50)
    mc.set_gate(fresh)
    clear_held_lease_ids()
    try:
        yield fresh
    finally:
        mc.set_gate(previous)
        clear_held_lease_ids()


class _Tracker:
    """记录"真正在发请求"的重叠情况：闸门之外再证一次并发为 1。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.violations = 0
        self.calls = 0
        self.gate_max_seen = 0
        self.kinds: List[str] = []

    def enter(self, kind: str, status: Dict[str, Any]) -> None:
        with self.lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            if self.running > 1:
                self.violations += 1
            self.gate_max_seen = max(self.gate_max_seen, status["active_count"])
            self.kinds.append(kind)

    def exit(self) -> None:
        with self.lock:
            self.running -= 1

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return {
                "calls": self.calls,
                "max_running": self.max_running,
                "violations": self.violations,
                "gate_max_seen": self.gate_max_seen,
            }


class _FakeChatModel(GatedChatOpenAI):
    """生产闸门 + 假请求体。四个 ``*_without_capacity`` 钩子替代真实网络调用。"""

    tracker: Any = None
    delay: float = 0.0
    chunks: int = 3
    content: str = "假回答"
    fail_at: str = ""

    def _enter(self, kind: str) -> None:
        if self.tracker is not None:
            self.tracker.enter(kind, mc.get_gate().status())

    def _exit(self) -> None:
        if self.tracker is not None:
            self.tracker.exit()

    def _generate_without_capacity(self, *args: Any, **kwargs: Any) -> ChatResult:
        self._enter("generate")
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_at == "generate":
                raise RuntimeError("假模型调用失败")
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=self.content))]
            )
        finally:
            self._exit()

    def _stream_without_capacity(self, *args: Any, **kwargs: Any):
        self._enter("stream")
        try:
            for index in range(self.chunks):
                if self.delay:
                    time.sleep(self.delay)
                if self.fail_at == "stream_mid" and index == 1:
                    raise RuntimeError("假模型流式中途失败")
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content=f"片{index}")
                )
        finally:
            self._exit()

    async def _agenerate_without_capacity(self, *args: Any, **kwargs: Any) -> ChatResult:
        self._enter("agenerate")
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_at == "agenerate":
                raise RuntimeError("假异步模型调用失败")
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=self.content))]
            )
        finally:
            self._exit()

    async def _astream_without_capacity(self, *args: Any, **kwargs: Any):
        self._enter("astream")
        try:
            for index in range(self.chunks):
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield ChatGenerationChunk(message=AIMessageChunk(content=f"片{index}"))
        finally:
            self._exit()


def _model(
    tracker: Optional[_Tracker] = None,
    *,
    consumer: str = mc.CONSUMER_SMART_CHAT,
    streaming: bool = False,
    delay: float = 0.0,
    chunks: int = 3,
    content: str = "假回答",
    fail_at: str = "",
    business_object: str = "",
    request_id: str = "",
) -> _FakeChatModel:
    return _FakeChatModel(
        model_name="fake-gated-model",
        openai_api_key="EMPTY",
        openai_api_base="http://127.0.0.1:9/v1",  # 永不真正访问（请求体已被替换）
        streaming=streaming,
        capacity_consumer=consumer,
        capacity_business_object=business_object,
        capacity_request_id=request_id,
        tracker=tracker,
        delay=delay,
        chunks=chunks,
        content=content,
        fail_at=fail_at,
    )


def _spec(
    consumer: str = mc.CONSUMER_SMART_CHAT,
    *,
    obj: str = "conv-1",
    user: str = "u-1",
    req: str = "",
    channel: str = "",
) -> mc.ModelCallSpec:
    return mc.ModelCallSpec(
        consumer_type=consumer,
        channel=channel,
        business_object=obj,
        user_id=user,
        request_id=req,
        model_name="fake-gated-model",
    )


def _wait_until(predicate, timeout: float = 3.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(message or "等待条件超时")


def _latest_release(gate: ModelCapacityGate) -> Dict[str, Any]:
    """最近一次释放记录（``recent_releases`` 按时间倒序，第 0 条最新）。"""
    releases = gate.status()["recent_releases"]
    assert releases, "没有释放记录"
    return releases[0]


# ---------------------------------------------------------------------------
# 1. 实际占用永不超过 1
# ---------------------------------------------------------------------------


def test_concurrent_model_calls_never_exceed_one_occupancy(gate):
    """8 线程 × 2 次调用的并发压力：运行时最大占用值与真实重叠都为 1。"""
    tracker = _Tracker()
    model = _model(tracker, streaming=True, delay=0.01, chunks=2)

    def worker(index: int) -> None:
        for round_index in range(2):
            model.invoke(f"问题 {index}-{round_index}")
            list(model.stream(f"流式问题 {index}-{round_index}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    status = gate.status()
    snap = tracker.snapshot()
    assert snap["calls"] == 32, snap
    assert snap["violations"] == 0, f"出现并发重叠：{snap}"
    assert snap["max_running"] == 1, snap
    assert status["max_active_observed"] == 1, status
    assert snap["gate_max_seen"] == 1, snap
    assert status["active_count"] == 0
    assert status["queue_length"] == 0
    assert status["capacity_limit"] == 1


def test_async_and_stream_paths_are_all_gated(gate):
    """invoke / stream / ainvoke / astream 四条路径都取放租约。"""
    tracker = _Tracker()
    sync_model = _model(tracker, streaming=False)
    stream_model = _model(tracker, streaming=True, delay=0.005)
    async_model = _model(tracker, streaming=False, delay=0.01)
    astream_model = _model(tracker, streaming=True, delay=0.01)

    sync_model.invoke("普通调用")
    assert _latest_release(gate)["call_kind"] == "generate"
    list(stream_model.stream("流式调用"))
    assert _latest_release(gate)["call_kind"] == "stream"

    async def scenario() -> None:
        await async_model.ainvoke("异步调用")
        assert gate.status()["recent_releases"][0]["call_kind"] == "agenerate"
        async for _ in astream_model.astream("异步流式"):
            pass
        assert gate.status()["recent_releases"][0]["call_kind"] == "astream"

    asyncio.run(scenario())

    status = gate.status()
    assert status["max_active_observed"] == 1, status
    assert status["counters"]["acquired"] == 4, status
    assert status["active_count"] == 0
    assert tracker.snapshot()["violations"] == 0


# ---------------------------------------------------------------------------
# 2. 流式持有到结束 / 取消 / 断线 / 异常；构造不持容量
# ---------------------------------------------------------------------------


def test_stream_holds_lease_from_request_until_disconnect(gate):
    """流式：请求发出才取租约；调用方关闭生成器（取消/断线）后释放。"""
    model = _model(_Tracker(), streaming=True, delay=0.01, chunks=5)

    generator = model.stream("开始生成")
    assert gate.status()["active_count"] == 0, "只创建流不取容量"

    first = next(generator)
    assert first.content == "片0"
    status = gate.status()
    assert status["active_count"] == 1, "流式进行中必须持有容量"
    holder = status["holders"][0]
    assert holder["call_kind"] == "stream"
    assert holder["last_heartbeat_at"], "流式每片要刷新心跳"

    generator.close()  # 用户取消 / 客户端断开
    status = gate.status()
    assert status["active_count"] == 0
    assert status["recent_releases"][0]["release_reason"] == "stream_disconnected"
    assert status["recent_releases"][0]["release_reason_label"] == RELEASE_REASON_LABELS[
        "stream_disconnected"
    ]


def test_stream_releases_on_normal_end_and_on_error(gate):
    """流式正常结束 → stream_completed；中途异常 → stream_error 且已释放。"""
    ok_model = _model(_Tracker(), streaming=True, chunks=3)
    chunks = list(ok_model.stream("正常"))
    # langchain 会在末尾追加一个空的分片（用量元数据），按拼接后的正文核对
    assert "".join(chunk.content for chunk in chunks) == "片0片1片2"
    assert _latest_release(gate)["release_reason"] == "stream_completed"
    assert gate.status()["active_count"] == 0

    bad_model = _model(_Tracker(), streaming=True, chunks=3, fail_at="stream_mid")
    with pytest.raises(RuntimeError):
        list(bad_model.stream("中途失败"))
    status = gate.status()
    assert status["active_count"] == 0, "流式异常也必须释放容量"
    assert status["recent_releases"][0]["release_reason"] == "stream_error"


def test_async_stream_releases_on_close_and_on_task_cancel(gate):
    """异步流式：aclose（断线）与任务取消（用户取消）都释放租约。"""

    async def scenario() -> None:
        model = _model(_Tracker(), streaming=True, delay=0.02, chunks=5)
        agen = model.astream("断线")
        first = await agen.__anext__()
        assert first.content == "片0"
        assert gate.status()["active_count"] == 1
        await agen.aclose()
        assert gate.status()["active_count"] == 0
        assert gate.status()["recent_releases"][0]["release_reason"] == (
            "stream_disconnected"
        )

        model2 = _model(_Tracker(), streaming=True, delay=0.05, chunks=5)

        async def consume() -> None:
            async for _ in model2.astream("取消"):
                pass

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.08)
        assert gate.status()["active_count"] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    status = gate.status()
    assert status["active_count"] == 0, "取消后不能留租约"
    assert status["recent_releases"][0]["release_reason"] == "stream_disconnected"
    assert status["max_active_observed"] == 1


def test_async_queueing_does_not_block_the_event_loop(gate):
    """异步排队不能阻塞事件循环：否则正在流式生成的请求收不到下一片，
    排队方与它会在同一个循环线程上互相等死。"""
    holder = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="streaming-1"))
    ticks: List[int] = []

    async def scenario() -> None:
        async def ticker() -> None:
            for index in range(20):
                await asyncio.sleep(0.01)
                ticks.append(index)

        async def waiter() -> Any:
            return await gate.acquire_async(
                _spec(mc.CONSUMER_MENTOR_REVIEW, obj="submission-1", req="review-1"),
                timeout=10,
            )

        ticking = asyncio.ensure_future(ticker())
        waiting = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.12)
        assert not waiting.done(), "排队中的异步调用不该已经拿到容量"
        assert len(ticks) >= 5, f"事件循环被排队阻塞了：{ticks}"
        assert gate.status()["waiting"][0]["request_id"] == "review-1"

        gate.release(holder, "completed")
        lease = await waiting
        assert gate.status()["active_count"] == 1
        gate.release(lease, "completed")
        await ticking

    asyncio.run(scenario())
    status = gate.status()
    assert status["max_active_observed"] == 1
    assert status["active_count"] == 0
    assert status["counters"]["acquired"] == 2


def test_error_and_timeout_release_lease(gate):
    """普通调用异常 → error；排队超时 → 出队并抛明确异常，不留队列项。"""
    bad = _model(_Tracker(), streaming=False, fail_at="generate")
    with pytest.raises(RuntimeError):
        bad.invoke("会失败")
    status = gate.status()
    assert status["active_count"] == 0
    assert status["recent_releases"][0]["release_reason"] == "error"

    holder = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="answer-1"))
    with pytest.raises(mc.ModelCapacityQueueTimeoutError) as exc:
        gate.acquire(_spec(mc.CONSUMER_MENTOR_REVIEW, req="review-1"), timeout=0.05)
    status = gate.status()
    assert status["queue_length"] == 0, "超时后必须出队"
    assert status["counters"]["queue_timeouts"] == 1
    assert status["recent_releases"][0]["release_reason"] == "queue_timeout"
    assert "超时" in str(exc.value)
    assert exc.value.lease is not None

    # 超时的那次调用已经离开队列与去重表：同一请求编号可以重新排队
    with pytest.raises(mc.ModelCapacityQueueTimeoutError):
        gate.acquire(_spec(mc.CONSUMER_MENTOR_REVIEW, req="review-1"), timeout=0.05)
    assert gate.status()["counters"]["duplicate_suppressed"] == 0

    gate.release(holder, "completed")
    assert gate.status()["active_count"] == 0


def test_model_construction_does_not_hold_capacity(gate):
    """模型对象构造、缓存、复用期间不得持有容量。"""
    models = [_model(_Tracker(), consumer=mc.CONSUMER_KB_CHAT) for _ in range(5)]
    status = gate.status()
    assert status["active_count"] == 0
    assert status["max_active_observed"] == 0
    assert status["queue_length"] == 0
    assert len(models) == 5


# ---------------------------------------------------------------------------
# 3. 三条通道：优先级、FIFO、后台按子任务释放
# ---------------------------------------------------------------------------


def test_channel_priority_and_same_channel_fifo(gate):
    """先入队的后台任务，会被后到的前台交互与短任务插到前面。"""
    holder = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="running-1"))
    order: List[str] = []
    lock = threading.Lock()

    def run(consumer: str, req: str) -> None:
        try:
            lease = gate.acquire(_spec(consumer, req=req), timeout=10)
        except mc.ModelCapacityError:
            return
        with lock:
            order.append(consumer)
        time.sleep(0.02)
        gate.release(lease, "completed")

    plan = [
        (mc.CONSUMER_PAPER_EXTRACT, "bg-1"),
        (mc.CONSUMER_PAPER_COMPARE, "bg-2"),
        (mc.CONSUMER_MENTOR_REVIEW, "ut-1"),
        (mc.CONSUMER_KB_CHAT, "fg-1"),
        (mc.CONSUMER_KB_CHAT, "fg-2"),
    ]
    threads = []
    for index, (consumer, req) in enumerate(plan):
        thread = threading.Thread(target=run, args=(consumer, req))
        thread.start()
        threads.append(thread)
        _wait_until(
            lambda expected=index + 1: gate.status()["queue_length"] == expected,
            message=f"第 {index + 1} 个请求没有按预期入队",
        )

    # 排队可见：位置与前方任务数按 (通道优先级, 排队序号) 排
    waiting = gate.status()["waiting"]
    assert [item["position"] for item in waiting] == [1, 2, 3, 4, 5]
    assert [item["consumer_type"] for item in waiting] == [
        mc.CONSUMER_KB_CHAT,
        mc.CONSUMER_KB_CHAT,
        mc.CONSUMER_MENTOR_REVIEW,
        mc.CONSUMER_PAPER_EXTRACT,
        mc.CONSUMER_PAPER_COMPARE,
    ]
    assert waiting[0]["channel"] == mc.CHANNEL_INTERACTIVE
    assert waiting[2]["channel"] == mc.CHANNEL_USER_TASK
    assert waiting[3]["channel"] == mc.CHANNEL_BACKGROUND

    gate.release(holder, "completed")
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()

    # 前台按进入顺序（fg-1 先于 fg-2），短任务在后台之前，后台保持原顺序
    assert order == [
        mc.CONSUMER_KB_CHAT,
        mc.CONSUMER_KB_CHAT,
        mc.CONSUMER_MENTOR_REVIEW,
        mc.CONSUMER_PAPER_EXTRACT,
        mc.CONSUMER_PAPER_COMPARE,
    ], order
    assert gate.status()["max_active_observed"] == 1


def test_background_batch_releases_capacity_per_subtask(gate):
    """论文批任务每完成一次调用就释放；期间到达的前台聊天排到下一个子任务前。"""
    tracker = _Tracker()
    batch_model = _model(
        tracker,
        consumer=mc.CONSUMER_PAPER_EXTRACT,
        streaming=False,
        delay=0.15,
        business_object="paper-batch",
    )
    done: List[str] = []

    def batch() -> None:
        # 每个子任务一次实际调用：取一次容量、释放一次，不连续霸占唯一模型
        for index in range(3):
            batch_model.invoke(f"提取论文 {index}")
            done.append(f"subtask-{index}")

    thread = threading.Thread(target=batch)
    thread.start()
    _wait_until(
        lambda: gate.status()["active_count"] == 1,
        message="第一个子任务没有开始占用容量",
    )

    chat_done: List[str] = []

    def chat() -> None:
        lease = gate.acquire(
            _spec(mc.CONSUMER_KB_CHAT, obj="conv-9", req="chat-1"), timeout=10
        )
        chat_done.append("chat")
        gate.release(lease, "completed")

    chat_thread = threading.Thread(target=chat)
    chat_thread.start()
    _wait_until(
        lambda: gate.status()["queue_length"] == 1,
        message="前台聊天没有进入等待队列",
    )
    waiting = gate.status()["waiting"][0]
    assert waiting["channel"] == mc.CHANNEL_INTERACTIVE
    assert waiting["position"] == 1
    assert waiting["request_id"] == "chat-1"

    # 子任务释放后，前台聊天排在尚未开始的第二个子任务之前
    _wait_until(lambda: len(chat_done) == 1, message="前台聊天没有排到容量")
    assert len(done) == 1, f"前台聊天应在第二个子任务之前完成：{done}"

    thread.join(timeout=20)
    chat_thread.join(timeout=20)
    assert not thread.is_alive() and not chat_thread.is_alive()
    assert len(done) == 3, "批任务没有被拆成可释放的子任务"
    assert chat_done == ["chat"]
    assert gate.status()["max_active_observed"] == 1
    assert tracker.snapshot()["violations"] == 0


# ---------------------------------------------------------------------------
# 4. 排队可见 / 可取消 / 重复请求抑制
# ---------------------------------------------------------------------------


def test_duplicate_request_is_suppressed(gate):
    """同一 (消费者, 业务对象, 请求编号) 已在排队或运行时不重复入队。"""
    # 占容量的是另一次调用（不同请求编号），否则等待方自己就会被判为重复
    holder = gate.acquire(
        _spec(mc.CONSUMER_SMART_CHAT, obj="conv-1", req="answer-1")
    )
    outcome: Dict[str, Any] = {}

    def waiter() -> None:
        try:
            lease = gate.acquire(
                _spec(mc.CONSUMER_AUTO_REVIEW, obj="submission-1", req="run-1"),
                timeout=10,
            )
        except mc.ModelCapacityError as exc:
            outcome["error"] = exc
            return
        outcome["lease"] = lease
        gate.release(lease, "completed")

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_until(lambda: gate.status()["queue_length"] == 1)

    with pytest.raises(mc.ModelCapacityDuplicateError) as exc:
        gate.acquire(
            _spec(mc.CONSUMER_AUTO_REVIEW, obj="submission-1", req="run-1"),
            timeout=0.2,
        )
    status = gate.status()
    assert status["queue_length"] == 1, "重复请求不得再入队"
    assert status["counters"]["duplicate_suppressed"] == 1
    assert "不重复入队" in str(exc.value)

    gate.release(holder, "completed")
    thread.join(timeout=20)
    assert "lease" in outcome, outcome
    assert gate.status()["active_count"] == 0


def test_waiting_request_can_be_cancelled_once(gate):
    """排队中取消：等待方收到取消异常、队列清空，重复取消不再产生新动作。"""
    holder = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="answer-1"))
    outcome: Dict[str, Any] = {}

    def waiter() -> None:
        try:
            lease = gate.acquire(
                _spec(mc.CONSUMER_STUDENT_CHECK, obj="task-1", req="check-1"),
                timeout=10,
            )
        except mc.ModelCapacityCancelledError as exc:
            outcome["cancelled"] = exc
            return
        except mc.ModelCapacityError as exc:  # pragma: no cover - 不应发生
            outcome["other"] = exc
            return
        outcome["lease"] = lease

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_until(lambda: gate.status()["queue_length"] == 1)
    waiting = gate.status()["waiting"][0]
    assert waiting["consumer_type"] == mc.CONSUMER_STUDENT_CHECK
    assert waiting["position"] == 1
    assert waiting["request_id"] == "check-1"

    result = gate.cancel(request_id="check-1")
    assert len(result["cancelled"]) == 1
    thread.join(timeout=20)
    assert isinstance(outcome.get("cancelled"), mc.ModelCapacityCancelledError)
    status = gate.status()
    assert status["queue_length"] == 0
    assert status["counters"]["cancelled"] == 1
    assert status["recent_releases"][0]["release_reason"] == "cancelled"

    # 再取消一次：没有可取消对象，不报错也不重复动作
    again = gate.cancel(request_id="check-1")
    assert again["cancelled"] == []
    assert again["cancel_requested"] == []

    # 运行中的调用不能被打断，只能标记 cancel_requested
    running = gate.cancel(request_id="answer-1")
    assert running["cancelled"] == []
    assert len(running["cancel_requested"]) == 1
    assert gate.status()["holders"][0]["cancel_requested"] is True
    assert gate.status()["active_count"] == 1
    gate.release(holder, "completed")
    assert gate.status()["active_count"] == 0


def test_queue_timeout_is_configurable_by_env(monkeypatch):
    """排队超时秒数来自环境变量；容量上限默认 1。"""
    monkeypatch.setenv("MODEL_CAPACITY_QUEUE_TIMEOUT_SECONDS", "7.5")
    assert env_queue_timeout_seconds() == 7.5
    assert ModelCapacityGate().queue_timeout_seconds == 7.5
    monkeypatch.setenv("MODEL_CAPACITY_QUEUE_TIMEOUT_SECONDS", "abc")
    assert env_queue_timeout_seconds() == 120.0
    assert env_capacity_limit() == 1
    monkeypatch.setenv("MODEL_CAPACITY_LIMIT", "3")
    assert env_capacity_limit() == 3
    assert ModelCapacityGate().limit == 3


# ---------------------------------------------------------------------------
# 5. 自锁保护：外层持租约、内层再取
# ---------------------------------------------------------------------------


def test_nested_lease_raises_reentrancy_instead_of_deadlocking(gate):
    """外层（整轮对话 / 整次 Agent Run / 整个预审运行）先持租约时内层立刻失败。"""
    model = _model(_Tracker(), streaming=False)

    with mc.gated_call(
        mc.CONSUMER_SMART_CHAT,
        business_object="conv-1",
        user_id="u-1",
        request_id="turn-1",
    ) as outer:
        assert gate.status()["active_count"] == 1
        assert gate.status()["holders"][0]["lease_id"] == outer.lease_id

        with pytest.raises(mc.ModelCapacityReentrancyError) as exc:
            model.invoke("内层再取一次容量")
        assert "自锁" in str(exc.value)
        assert exc.value.outer_lease is not None
        assert gate.status()["active_count"] == 1, "内层没有偷偷占住第二个槽"

        with pytest.raises(mc.ModelCapacityReentrancyError):
            with mc.gated_call(mc.CONSUMER_AGENT_TOOL, request_id="tool-1"):
                pass

        with pytest.raises(mc.ModelCapacityReentrancyError):
            list(model.stream("流式内层"))

    # 外层释放后上下文没有被污染：后续调用正常排队
    model.invoke("下一轮")
    status = gate.status()
    assert status["active_count"] == 0
    assert status["max_active_observed"] == 1
    assert status["counters"]["acquired"] == 2


def test_agent_tool_second_call_queues_independently(gate):
    """Agent 规划与工具内部调用是两次独立调用：各自取放，不自锁。"""
    tracker = _Tracker()
    planner = _model(tracker, consumer=mc.CONSUMER_AGENT_PLAN, streaming=False)
    tool = _model(tracker, consumer=mc.CONSUMER_AGENT_TOOL, streaming=False)
    final = _model(tracker, consumer=mc.CONSUMER_AGENT_FINAL_ANSWER, streaming=False)

    planner.invoke("规划")
    assert gate.status()["active_count"] == 0, "规划调用结束必须已释放"
    tool.invoke("text2sql 内部调用")
    final.invoke("最终回答")

    status = gate.status()
    assert status["counters"]["acquired"] == 3
    assert status["max_active_observed"] == 1
    assert [item["consumer_type"] for item in status["recent_releases"]] == [
        mc.CONSUMER_AGENT_FINAL_ANSWER,
        mc.CONSUMER_AGENT_TOOL,
        mc.CONSUMER_AGENT_PLAN,
    ]
    assert tracker.snapshot()["violations"] == 0


def test_call_context_declares_identity_without_holding_capacity(gate):
    """model_call_context 只声明身份：期间每次调用各自排队，业务对象可见。"""
    with mc.model_call_context(
        mc.CONSUMER_AGENT_PLAN,
        business_object="agent-run-7",
        user_id="u-9",
        request_id="step-2",
    ):
        assert gate.status()["active_count"] == 0, "声明不取容量"
        # 模型对象本身没有声明消费者：靠调用期声明排队
        planner = _model(_Tracker(), consumer="", streaming=False)
        planner.invoke("规划")
        status = gate.status()
        assert status["recent_releases"][0]["consumer_type"] == mc.CONSUMER_AGENT_PLAN
        assert status["recent_releases"][0]["business_object"] == "agent-run-7"
        assert status["recent_releases"][0]["user_id"] == "u-9"
        assert status["recent_releases"][0]["request_id"] == "step-2"
        assert status["recent_releases"][0]["channel"] == mc.CHANNEL_INTERACTIVE
    assert gate.status()["active_count"] == 0


# ---------------------------------------------------------------------------
# 6. 意图路由释放后回答重新排队（真实路由器 + 假模型）
# ---------------------------------------------------------------------------

_ROUTE_JSON = json.dumps(
    {
        "path": "direct",
        "intent": "general",
        "complexity": "direct",
        "candidate_tools": [],
        "confidence": 0.9,
        "summary": "普通对话",
    }
)


def test_intent_route_releases_capacity_then_answer_requeues(gate):
    """P-10 第 3 步：路由结束可见占用为 0，回答作为另一次调用重新排队。"""
    from chatchat.server.intent_router.providers import LlmIntentProvider, RouteContext
    from chatchat.server.intent_router.router import IntentRouter

    tracker = _Tracker()
    route_model = _model(
        tracker,
        consumer=mc.CONSUMER_INTENT_ROUTE,
        streaming=False,
        content=_ROUTE_JSON,
        delay=0.01,
    )
    router = IntentRouter(LlmIntentProvider(route_model), timeout_seconds=5)
    result = router.route(RouteContext(query="什么是量子计算", available_tools={}))
    assert result.path == "direct"
    assert result.source == "llm"

    status = gate.status()
    assert status["active_count"] == 0, "意图路由结束后必须已释放容量"
    assert status["max_active_observed"] == 1
    assert status["recent_releases"][0]["consumer_type"] == mc.CONSUMER_INTENT_ROUTE
    assert status["recent_releases"][0]["channel"] == mc.CHANNEL_INTERACTIVE

    answer_model = _model(tracker, consumer=mc.CONSUMER_SMART_CHAT, streaming=True)
    list(answer_model.stream("回答"))
    status = gate.status()
    assert status["max_active_observed"] == 1, "回答重新排队后仍不超过 1"
    assert [item["consumer_type"] for item in status["recent_releases"]] == [
        mc.CONSUMER_SMART_CHAT,
        mc.CONSUMER_INTENT_ROUTE,
    ]


def test_build_router_declares_intent_route_consumer(gate, monkeypatch):
    """build_router 必须把路由调用登记为 intent_route，否则排不进前台通道。"""
    import chatchat.server.utils as utils_module
    from chatchat.server.intent_router.router import build_router

    captured: Dict[str, Any] = {}

    def fake_get_ChatOpenAI(*args: Any, **kwargs: Any) -> _FakeChatModel:
        captured.update(kwargs)
        return _model(streaming=False, content=_ROUTE_JSON)

    monkeypatch.setattr(utils_module, "get_ChatOpenAI", fake_get_ChatOpenAI)
    router = build_router()
    assert captured.get("consumer") == mc.CONSUMER_INTENT_ROUTE, captured
    assert router.provider is not None
    assert gate.status()["active_count"] == 0, "构建路由器不占容量"


def test_get_ChatOpenAI_returns_gated_model_and_pops_capacity_kwargs():
    """get_ChatOpenAI 返回闸门模型，且容量声明不会漏进 openai 请求参数。"""
    from chatchat.server.utils import get_ChatOpenAI

    model = get_ChatOpenAI(
        consumer=mc.CONSUMER_KB_SUMMARY,
        business_object="doc-42",
        user_id="u-3",
        request_id="req-42",
    )
    assert isinstance(model, GatedChatOpenAI)
    assert model.capacity_consumer == mc.CONSUMER_KB_SUMMARY
    assert model.capacity_business_object == "doc-42"
    assert model.capacity_user_id == "u-3"
    assert model.capacity_request_id == "req-42"
    dumped = model.model_dump()
    for key in ("consumer", "business_object", "request_id"):
        assert key not in dumped
    assert model.capacity_channel == ""  # 通道由登记表推导，不写死在调用点


# ---------------------------------------------------------------------------
# 7. 运行时占用证据与启动清理
# ---------------------------------------------------------------------------


def test_status_reports_required_evidence_fields(gate):
    """§8.2 要求的最小字段集：占用数、上限、持有者身份、时间、心跳、释放原因。"""
    lease = gate.acquire(
        _spec(
            mc.CONSUMER_MENTOR_REVIEW,
            obj="submission-7",
            user="u-mentor",
            req="review-7",
        )
    )
    status = gate.status()
    assert status["capacity_limit"] == 1
    assert status["active_count"] == 1
    assert status["max_active_observed"] == 1
    holder = status["holders"][0]
    for key in (
        "lease_id",
        "queue_seq",
        "consumer_type",
        "consumer_label",
        "channel",
        "business_object",
        "user_id",
        "request_id",
        "model_name",
        "acquired_at",
        "last_heartbeat_at",
        "hold_seconds",
    ):
        assert key in holder, f"缺少占用证据字段：{key}"
    assert holder["consumer_type"] == mc.CONSUMER_MENTOR_REVIEW
    assert holder["channel"] == mc.CHANNEL_USER_TASK
    assert holder["queue_seq"] == 1
    assert {c["channel"] for c in status["channels"]} == {
        mc.CHANNEL_INTERACTIVE,
        mc.CHANNEL_USER_TASK,
        mc.CHANNEL_BACKGROUND,
    }
    assert status["consumers"], "登记表必须随状态一起可见"

    gate.heartbeat(lease)
    gate.release(lease, "completed")
    status = gate.status()
    released = status["recent_releases"][0]
    assert released["released_at"], "释放时间必须留痕"
    assert released["release_reason"] == "completed"
    assert released["release_reason_label"] == "正常完成"
    assert released["wait_seconds"] is not None


def test_reset_stale_leases_clears_leftover_occupancy(gate):
    """残留租约（调用被硬杀死 / 生成器没跑到 finally）不会永久占住唯一容量。"""
    leaked = gate.acquire(_spec(mc.CONSUMER_PAPER_EXTRACT, obj="paper-1", req="p-1"))
    assert gate.status()["active_count"] == 1

    result = gate.reset_stale_leases(reason="service_restart")
    assert result["cleared"] == 1
    status = gate.status()
    assert status["active_count"] == 0, "旧租约不能继续显示为占用"
    assert status["queue_length"] == 0
    assert status["recent_releases"][0]["release_reason"] == "service_restart"
    assert status["recent_releases"][0]["release_reason_label"] == "服务重启清理旧租约"
    assert leaked.state == "released"

    # 清理后队列可用
    lease = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="after-restart"))
    assert gate.status()["active_count"] == 1
    gate.release(lease, "completed")

    initialized = mc.initialize_model_capacity()
    assert initialized["capacity_limit"] == 1
    assert initialized["cleared_stale_leases"]["cleared"] == 0


def test_stale_held_marker_does_not_poison_later_calls(gate):
    """上下文里残留的已释放租约标记会被清掉，不会误判成自锁。"""
    from chatchat.server.model_capacity.context import mark_lease_held

    lease = gate.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="turn-9"))
    gate.release(lease, "completed")
    mark_lease_held(lease.lease_id)  # 模拟生成器 finally 晚跑留下的残留标记

    model = _model(_Tracker(), streaming=False)
    model.invoke("下一轮照常排队")
    status = gate.status()
    assert status["active_count"] == 0
    assert status["counters"]["acquired"] == 2, "残留标记不能把后续调用挡在门外"
    assert status["max_active_observed"] == 1


# ---------------------------------------------------------------------------
# 8. 遗漏检查：绕开闸门的构造点 & embedding 边界
# ---------------------------------------------------------------------------


def test_no_unwrapped_chat_openai_construction_outside_gate():
    """除闸门模块自身外，源码里不得再出现 ``ChatOpenAI(``。"""
    found = mc.scan_generation_construction_points()
    offenders = [
        item
        for item in found
        if item["pattern"] == "ChatOpenAI("
        and not item["path"].startswith("server/model_capacity")
    ]
    assert offenders == [], f"绕开闸门的 ChatOpenAI 构造点：{offenders}"


def test_audit_flags_new_generation_sites_and_known_gaps_only():
    """新增未接闸门的生成调用点会让检查失败；当前已知缺口清单必须为空。

    平台模型（chat/chat.py 换成 ``GatedChatPlatformAI``）与补全式接口
    （``utils.get_OpenAI`` 换成 ``GatedOpenAI``）都已接进闸门，清单已清空。
    今后任何绕过闸门的生成模型构造点都会让本测试失败（§16.1B 第 5 条）——
    正确反应是接闸门，不是往清单里加条目。
    """
    report = mc.audit_generation_consumers()
    assert report["ok"] is True, report["ungated"]
    assert report["ungated"] == []
    assert {item["site"] for item in report["known_ungated"]} == set(
        capacity_audit.KNOWN_UNGATED_GENERATION_SITES
    ), "已知未接闸门清单与实际扫描结果不一致（新增缺口或清单过期）"
    assert capacity_audit.KNOWN_UNGATED_GENERATION_SITES == frozenset(), (
        "所有生成模型构造点都已接闸门；这里出现新条目说明有人绕过闸门直连模型"
    )
    # embedding 客户端属于独立资源：命中清单是预期，不算生成模型缺口
    assert {item["site"] for item in report["embedding_sites"]} == set(
        capacity_audit.EMBEDDING_SITES
    )
    assert report["stale_known_sites"] == []


def test_embedding_is_not_routed_through_generation_gate():
    """§16.1B 第 6 条：Embedding 是独立资源，不进生成模型队列。"""
    report = mc.embedding_paths_are_not_gated()
    assert report["ok"] is True, report["problems"]
    assert "LocalAIEmbeddings" in report["checked_classes"]

    from chatchat.server.localai_embeddings import LocalAIEmbeddings
    from chatchat.server.model_capacity.gated_model import ModelCapacityCallMixin

    assert not issubclass(LocalAIEmbeddings, ModelCapacityCallMixin)
    from chatchat.server.utils import get_Embeddings

    source = inspect.getsource(get_Embeddings)
    assert "model_capacity" not in source
    assert "gated_call" not in source
    # embedding 工厂返回的是 Embeddings，不是闸门包装的生成模型
    assert "GatedChatOpenAI" not in source


# ---------------------------------------------------------------------------
# 9. 观测端点
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    from chatchat.server.api_server.model_capacity_routes import model_capacity_router

    app = FastAPI()
    app.include_router(model_capacity_router)
    return app


def test_model_capacity_endpoint_requires_login(gate):
    app = _build_app()
    client = TestClient(app)
    for path in ("/server/model_capacity", "/server/model_capacity/status"):
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code)
    assert client.get("/server/model_capacity/consumers").status_code == 401


def test_model_capacity_endpoint_returns_runtime_evidence(gate):
    from chatchat.server.auth.deps import require_password_changed

    lease = gate.acquire(
        _spec(mc.CONSUMER_KB_CHAT, obj="conv-5", user="u-5", req="q-5")
    )
    app = _build_app()

    class _FakeUser:
        id = "u-admin"
        role = "admin"
        must_change_password = False

    app.dependency_overrides[require_password_changed] = lambda: _FakeUser()
    client = TestClient(app)
    response = client.get("/server/model_capacity")
    assert response.status_code == 200
    payload = response.json()
    assert payload["capacity_limit"] == 1
    assert payload["active_count"] == 1
    assert payload["max_active_observed"] == 1
    assert payload["holders"][0]["consumer_type"] == mc.CONSUMER_KB_CHAT
    assert payload["holders"][0]["request_id"] == "q-5"
    # 端点只读：不改变闸门状态
    assert gate.status()["active_count"] == 1

    alias = client.get("/server/model_capacity/status")
    assert alias.status_code == 200
    assert alias.json()["capacity_limit"] == 1
    consumers = client.get("/server/model_capacity/consumers").json()["consumers"]
    assert any(item["consumer_type"] == mc.CONSUMER_INTENT_ROUTE for item in consumers)

    gate.release(lease, "completed")


# ---------------------------------------------------------------------------
# 10. 消费者登记表
# ---------------------------------------------------------------------------


def test_consumer_registry_covers_documented_consumers():
    """登记表必须覆盖 §8.2 列出的全部生成调用，且通道归属正确。"""
    expected_channels = {
        mc.CONSUMER_SMART_CHAT: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_KB_CHAT: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_PROJECT_DISCUSSION: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_INTENT_ROUTE: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_AGENT_PLAN: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_AGENT_TOOL: mc.CHANNEL_INTERACTIVE,
        mc.CONSUMER_RESEARCH_DEFINITION: mc.CHANNEL_USER_TASK,
        mc.CONSUMER_RESEARCH_PLAN: mc.CHANNEL_USER_TASK,
        mc.CONSUMER_MENTOR_REVIEW: mc.CHANNEL_USER_TASK,
        mc.CONSUMER_STUDENT_CHECK: mc.CHANNEL_USER_TASK,
        mc.CONSUMER_AUTO_REVIEW: mc.CHANNEL_BACKGROUND,
        mc.CONSUMER_PAPER_EXTRACT: mc.CHANNEL_BACKGROUND,
        mc.CONSUMER_PAPER_COMPARE: mc.CHANNEL_BACKGROUND,
        mc.CONSUMER_PROGRESS_DRAFT: mc.CHANNEL_BACKGROUND,
        mc.CONSUMER_KB_SUMMARY: mc.CHANNEL_BACKGROUND,
        mc.CONSUMER_SESSION_SUMMARY: mc.CHANNEL_BACKGROUND,
    }
    for consumer, channel in expected_channels.items():
        assert mc.is_registered_consumer(consumer), consumer
        assert mc.channel_of(consumer) == channel, (consumer, channel)
    assert mc.channel_of("not_registered_at_all") == mc.DEFAULT_UNDECLARED_CHANNEL
    # 未登记回落前台通道：现有聊天构造点还没声明消费者，不能掉到后台
    assert mc.DEFAULT_UNDECLARED_CHANNEL == mc.CHANNEL_INTERACTIVE


def test_unregistered_consumer_falls_back_and_warns_once(gate):
    """未登记消费者按回落通道排队，并在 status 里以 undeclared 可见。"""
    lease = gate.acquire(_spec("brand_new_generation_call", req="x-1"))
    status = gate.status()
    assert status["holders"][0]["consumer_type"] == "brand_new_generation_call"
    assert status["holders"][0]["channel"] == mc.DEFAULT_UNDECLARED_CHANNEL
    gate.release(lease, "completed")
    assert gate.status()["recent_releases"][0]["consumer_label"] == (
        "brand_new_generation_call"
    )


def test_capacity_limit_greater_than_one_is_honoured_if_configured():
    """容量上限可配（默认 1）；调大后闸门按配置放行，不写死。"""
    wider = ModelCapacityGate(limit=2, queue_timeout_seconds=5.0)
    first = wider.acquire(_spec(mc.CONSUMER_SMART_CHAT, req="a"))
    second = wider.acquire(_spec(mc.CONSUMER_KB_CHAT, req="b"))
    assert wider.status()["active_count"] == 2
    assert wider.max_active_observed == 2
    third_outcome: Dict[str, Any] = {}

    def waiter() -> None:
        try:
            third_outcome["lease"] = wider.acquire(
                _spec(mc.CONSUMER_MENTOR_REVIEW, req="c"), timeout=10
            )
        except mc.ModelCapacityError as exc:
            third_outcome["error"] = exc

    thread = threading.Thread(target=waiter)
    thread.start()
    _wait_until(lambda: wider.status()["queue_length"] == 1)
    wider.release(first, "completed")
    thread.join(timeout=20)
    assert "lease" in third_outcome, third_outcome
    wider.release(second, "completed")
    wider.release(third_outcome["lease"], "completed")
    assert wider.status()["active_count"] == 0
