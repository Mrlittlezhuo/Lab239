"""上一轮消息 metadata 缺少 intermediate_steps 时的安全降级（回归测试）。

背景：Agent Run 轮持久化 ``{"agent_run_id", "agent_status"}``、kb 轮持久化
引用元数据、普通轮流被中断时只有默认空 metadata——这些 metadata 都不是
None 却没有 ``intermediate_steps`` 键。旧代码直接 ``loads(None)``，在
下一轮直接回答路径抛 TypeError（"the JSON object must be str, bytes or
bytearray, not NoneType"），整轮回答失败。
"""
from __future__ import annotations

from types import SimpleNamespace

from langchain_core.agents import AgentAction
from langchain_core.load import dumps

from chatchat.server.chat.chat import _load_intermediate_steps, _round_metadata


class TestLoadIntermediateSteps:
    def test_agent_run_metadata_without_intermediate_steps(self):
        """Agent Run 轮只写 agent_run_id/agent_status，不应触发 loads(None)。"""
        meta = {"agent_run_id": "run-001", "agent_status": "completed"}
        assert _load_intermediate_steps(meta) == []

    def test_empty_default_metadata(self):
        """add_message 默认 metadata={}（非 None），应安全降级为空。"""
        assert _load_intermediate_steps({}) == []

    def test_none_metadata(self):
        assert _load_intermediate_steps(None) == []

    def test_non_dict_metadata(self):
        assert _load_intermediate_steps("not-a-dict") == []

    def test_valid_serialized_steps_roundtrip(self):
        """普通轮/工具轮持久化的合法序列化结果可以正常解析回。"""
        serialized = dumps([], pretty=True)
        meta = {"intermediate_steps": serialized}
        assert _load_intermediate_steps(meta) == []

    def test_empty_string_intermediate_steps(self):
        assert _load_intermediate_steps({"intermediate_steps": ""}) == []

    def test_corrupted_intermediate_steps_degrades(self):
        """损坏的序列化内容只降级为空，不抛异常阻断回答。"""
        meta = {"intermediate_steps": "{这不是合法的JSON"}
        assert _load_intermediate_steps(meta) == []


# 失败轮（工具不存在 / 撞到 max_iterations / 超时）以前也无条件写
# intermediate_steps，下一轮被当作"上次这么做是成功的"回放给模型，
# 于是它照着上一串"好的，我再试一次"继续空转。
_FAILED_STEPS = [
    (
        AgentAction(
            tool="navigate_page",
            tool_input={"url": "https://example.com"},
            log="我来打开页面",
        ),
        "工具「navigate_page」本轮不可用（本轮可用工具：无），已停止重试",
    )
]


class TestRoundMetadata:
    def test_failed_round_does_not_persist_tool_context(self):
        executor = SimpleNamespace(intermediate_steps=_FAILED_STEPS)

        assert _round_metadata(executor, round_failed=True) == {}

    def test_failed_round_starts_clean_next_round(self):
        executor = SimpleNamespace(intermediate_steps=_FAILED_STEPS)

        assert _load_intermediate_steps(_round_metadata(executor, True)) == []

    def test_successful_round_still_persists_tool_context(self):
        executor = SimpleNamespace(
            intermediate_steps=[
                (
                    AgentAction(tool="calculate", tool_input={"text": "1+1"}, log="算一下"),
                    "2",
                )
            ]
        )

        meta = _round_metadata(executor, round_failed=False)

        assert len(_load_intermediate_steps(meta)) == 1

    def test_empty_steps_round_writes_empty_list(self):
        executor = SimpleNamespace(intermediate_steps=[])

        meta = _round_metadata(executor, round_failed=False)

        assert _load_intermediate_steps(meta) == []