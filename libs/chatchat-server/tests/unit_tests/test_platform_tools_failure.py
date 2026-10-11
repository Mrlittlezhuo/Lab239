"""手动模式（``chat()`` 路径）收尾的失败可读性回归测试，全部离线。

工具报错时（例如 chrome-devtools MCP 连不上 Chrome）AgentExecutor 不会产出最终
回答，``callback.outputs`` 保持空字典 —— 它只在 ``on_chain_end`` 带
``intermediate_steps`` 的正常收尾里赋值。旧代码收尾时直接
``self.callback.outputs["output"]``，抛出的 ``KeyError('output')`` 把真正的失败
原因盖成一个看不懂的键名（日志里只剩 ``error in chat: 'output'``），而且
``history`` 只写进半轮，``chat.py`` 里的 ``history[-1]`` 会取到用户自己的提问。
"""
import asyncio
import json

import pytest

from langchain_chatchat.agents.platform_tools.base import PlatformToolsRunnable
from langchain_chatchat.callbacks.agent_callback_handler import (
    AgentExecutorAsyncIteratorCallbackHandler,
    AgentStatus,
)

CHROME_ERROR = ("Could not connect to Chrome. Check if Chrome is running. "
                "Cause: Could not find DevToolsActivePort")


class _ReplayExecutor:
    """把预先准备好的回调帧推进队列，不真的跑 AgentExecutor。

    ``outputs`` 用来模拟 ``on_chain_end`` 对 ``callback.outputs`` 的赋值。
    ``consumed`` 是消费侧的收件列表：每推一帧就等它长出对应的那一条再返回，
    否则 ``done`` 会和队列同时就绪，``aiter`` 可能先取到 ``done`` 把帧丢掉
    （真实链路里帧是陆续产生的，收尾时队列早就空了，不会这样）。
    """

    def __init__(self, frames, callback, outputs=None, consumed=None):
        self._frames = frames
        self._callback = callback
        self._outputs = outputs
        self._consumed = consumed

    async def ainvoke(self, inputs, config=None, **kwargs):
        for index, frame in enumerate(self._frames, start=1):
            self._callback.queue.put_nowait(json.dumps(frame))
            while self._consumed is not None and len(self._consumed) < index:
                await asyncio.sleep(0)
        if self._outputs is not None:
            self._callback.outputs = dict(self._outputs)
        return {}


def _runnable(frames, outputs=None, consumed=None):
    callback = AgentExecutorAsyncIteratorCallbackHandler()
    return PlatformToolsRunnable.model_construct(
        agent_executor=_ReplayExecutor(frames, callback, outputs, consumed),
        agent_type="platform-knowledge-mode",
        callback=callback,
        intermediate_steps=[],
        history=[],
        mcp_connections=None,
    )


async def _drain(runnable, out):
    async for item in runnable.invoke("帮我看看小红书"):
        out.append(item)


def _tool_error_frame(key="tool_output"):
    return {
        "run_id": "r1",
        "status": AgentStatus.error,
        key: CHROME_ERROR,
        "is_error": True,
    }


def _finish_frame(answer="最终回答"):
    return {
        "run_id": "r2",
        "status": AgentStatus.agent_finish,
        "finish": {
            "return_values": {"output": answer},
            "log": f"Final Answer: {answer}",
        },
    }


class TestFailedTurn:
    async def test_real_tool_error_replaces_the_bare_keyerror(self):
        """报错原文必须出现在异常里，不能只剩一个 'output'。"""
        out = []
        runnable = _runnable([_tool_error_frame()], consumed=out)
        with pytest.raises(RuntimeError) as exc:
            await _drain(runnable, out)

        assert CHROME_ERROR in str(exc.value)
        assert "'output'" not in str(exc.value)

    async def test_error_chunk_is_still_streamed_before_the_raise(self):
        """错误帧本身要先发给前端，收尾异常只是补一句说明。"""
        out = []
        runnable = _runnable([_tool_error_frame()], consumed=out)
        with pytest.raises(RuntimeError):
            await _drain(runnable, out)

        assert [item.status for item in out] == [AgentStatus.error]

    async def test_history_is_left_untouched_when_the_turn_failed(self):
        """半轮 history 会让 chat.py 的 history[-1] 取到用户自己的提问。"""
        out = []
        runnable = _runnable([_tool_error_frame()], consumed=out)
        with pytest.raises(RuntimeError):
            await _drain(runnable, out)

        assert runnable.history == []

    @pytest.mark.parametrize("key", ["tool_output", "error", "text"])
    async def test_error_text_is_read_from_whichever_key_the_callback_used(
            self, key):
        """on_tool_error 用 tool_output，on_chain_error 用 error，
        on_llm_error 用 text。"""
        out = []
        runnable = _runnable([_tool_error_frame(key)], consumed=out)
        with pytest.raises(RuntimeError) as exc:
            await _drain(runnable, out)

        assert CHROME_ERROR in str(exc.value)

    async def test_failure_without_an_error_frame_still_raises_cleanly(self):
        out = []
        runnable = _runnable([], consumed=out)
        with pytest.raises(RuntimeError) as exc:
            await _drain(runnable, out)

        assert "'output'" not in str(exc.value)


class TestSuccessfulTurn:
    async def test_agent_finish_output_is_written_to_history(self):
        """on_agent_finish 不写 callback.outputs，回答要从收尾帧里留一份。"""
        out = []
        runnable = _runnable([_finish_frame()], consumed=out)
        await _drain(runnable, out)

        assert [item.status for item in out] == [AgentStatus.agent_finish]
        assert runnable.history == [
            {"role": "user", "content": "帮我看看小红书"},
            {"role": "assistant", "content": "最终回答"},
        ]

    async def test_chain_end_outputs_take_precedence(self):
        frames = [{
            "run_id": "r3",
            "status": AgentStatus.chain_end,
            "outputs": {"output": "链式回答"},
            "parent_run_id": None,
            "tags": [],
        }]
        out = []
        runnable = _runnable(frames, outputs={
            "output": "链式回答",
            "intermediate_steps": [],
        }, consumed=out)
        await _drain(runnable, out)

        assert runnable.history[-1]["content"] == "链式回答"
