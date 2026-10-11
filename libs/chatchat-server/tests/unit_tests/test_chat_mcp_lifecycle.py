"""手动模式（chat() 路径）的 MCP 连接生命周期回归测试，全部离线。

覆盖六处修复（连接字典本身的构造见 ``test_mcp_connections.py``）：
- ``create_agent_executor`` 收到已连好的 ``mcp_client`` 时不再自己连（否则
  client 没人引用，每条消息漏一个 MCP 子进程，收尾还会炸 anyio cancel scope）；
- 非流式收尾遇到 ``{"error": ...}`` 帧时抛原始错误，而不是 ``KeyError('choices')``；
- ``_load_intermediate_steps`` 用显式 ``allowed_objects`` 白名单，上一轮的
  action/observation 能读回来（多轮工具上下文不再每轮清零），名单外的类仍被拒；
- 读回来的上下文不会被 ``L.extend(L)`` 每轮翻倍；
- 非流式收尾按与前端一致的规则聚合帧，正文只算一遍。
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import chatchat.server.chat.chat as chat_mod
from langchain_chatchat import ChatPlatformAI, PlatformToolsRunnable
from langchain_chatchat.callbacks.agent_callback_handler import AgentStatus


class TestPreconnectedClient:
    def test_client_is_used_and_no_second_connection_is_made(self, monkeypatch):
        """传了 mcp_client 就只取工具；再建一次连接就会漏进程，必须不发生。"""
        async def _must_not_connect(connections=None):
            raise AssertionError("create_mcp_client 不该被 create_agent_executor 调用")

        monkeypatch.setattr(PlatformToolsRunnable, "create_mcp_client",
                            staticmethod(_must_not_connect))

        tool = MagicMock(name="tool")
        client = MagicMock()
        client.get_tools.return_value = [tool]
        captured = {}

        class _Stop(Exception):
            pass

        def _registry(**kwargs):
            # 真正的 executor 需要 agent/tools 才能过 pydantic 校验，这里只关心
            # create_agent_executor 传了什么，拿到参数就停。
            captured.update(kwargs)
            raise _Stop()

        llm = ChatPlatformAI(model="stub", platform_name="platform", api_key="x",
                             temperature=0.5, max_tokens=100)
        llm.callbacks = []  # 真实链路里 create_models_chains 会先赋值
        with pytest.raises(_Stop):
            PlatformToolsRunnable.create_agent_executor(
                agent_type="platform-knowledge-mode",
                agents_registry=_registry,
                llm=llm,
                tools=[],
                mcp_client=client,
            )

        assert captured["mcp_tools"] == [tool]
        client.get_tools.assert_called_once_with()

    def test_no_mcp_means_no_tools(self, monkeypatch):
        async def _must_not_connect(connections=None):
            raise AssertionError("没配 MCP 时不该建连接")

        monkeypatch.setattr(PlatformToolsRunnable, "create_mcp_client",
                            staticmethod(_must_not_connect))

        captured = {}

        class _Stop(Exception):
            pass

        def _registry(**kwargs):
            # 真正的 executor 需要 agent/tools 才能过 pydantic 校验，这里只关心
            # create_agent_executor 传了什么，拿到参数就停。
            captured.update(kwargs)
            raise _Stop()

        llm = ChatPlatformAI(model="stub", platform_name="platform", api_key="x",
                             temperature=0.5, max_tokens=100)
        llm.callbacks = []  # 真实链路里 create_models_chains 会先赋值
        with pytest.raises(_Stop):
            PlatformToolsRunnable.create_agent_executor(
                agent_type="platform-knowledge-mode",
                agents_registry=_registry,
                llm=llm,
                tools=[],
            )
        assert captured["mcp_tools"] == []


class TestNonStreamErrorChunk:
    def test_original_error_surfaces_instead_of_keyerror(self, monkeypatch):
        """出错帧是 {"error": ...}，没有 choices；非流式收尾不能因此抛 KeyError。"""
        def _boom(**kwargs):
            raise RuntimeError("模型配置坏了")

        monkeypatch.setattr(chat_mod, "create_models_from_config", _boom)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat_mod.chat(
                query="你好", metadata={}, conversation_id="conv-1",
                message_id="msg-1",  # 已存在，避免补建消息时碰库
                history_len=0, stream=False, chat_model_config={}, tool_config={},
                use_mcp=False, max_tokens=None, session=None, owner_id=None,
            ))

        assert exc.value.status_code == 500
        assert "模型配置坏了" in exc.value.detail


class TestIntermediateStepsRoundTrip:
    """手动模式的多轮工具上下文：上一轮存的 action/observation 要能读回来。"""

    def test_mcp_action_and_observation_survive(self):
        from langchain_core.load import dumps

        from langchain_chatchat.agents.output_parsers.platform_knowledge_output_parsers import (
            MCPToolAction,
        )

        action = MCPToolAction(tool="navigate", tool_input={"url": "https://example.com"},
                               log="", server_name="chrome-devtools")
        steps = chat_mod._load_intermediate_steps({"intermediate_steps": dumps([(action, "Navigated")])})

        assert len(steps) == 1
        loaded, observation = steps[0]
        assert isinstance(loaded, MCPToolAction)
        assert loaded.server_name == "chrome-devtools"
        assert loaded.tool_input == {"url": "https://example.com"}
        assert observation == "Navigated"

    def test_tool_output_wrapper_survives(self):
        from langchain_core.agents import AgentAction
        from langchain_core.load import dumps

        from langchain_chatchat.agent_toolkits.all_tools.tool import BaseToolOutput

        action = AgentAction(tool="search_internet", tool_input={"q": "x"}, log="")
        out = BaseToolOutput(data={"results": [1, 2]}, format="json")
        steps = chat_mod._load_intermediate_steps({"intermediate_steps": dumps([(action, out)])})

        assert isinstance(steps[0][0], AgentAction)
        assert isinstance(steps[0][1], BaseToolOutput)
        assert steps[0][1].data == {"results": [1, 2]}

    def test_model_class_outside_allowlist_degrades_to_empty(self):
        """白名单必须真的在拦：名单外的类被拒后只降级为空，不能把整轮回答带崩。

        挑的是模型类本身 —— intermediate_steps 存在库里，如果哪天有人把
        allowed_objects 换成 'all'，这种带 base_url/headers 的类就会被真的
        实例化，反序列化变成一次外连。这里钉住它必须被拒。
        """
        from langchain_core.load import dumps

        model = ChatPlatformAI(model="stub", platform_name="platform", api_key="x",
                               temperature=0.5, max_tokens=100)
        raw = dumps([(model, "obs")])

        assert chat_mod._load_intermediate_steps({"intermediate_steps": raw}) == []

    def test_missing_or_broken_payload_degrades_to_empty(self):
        assert chat_mod._load_intermediate_steps({}) == []
        assert chat_mod._load_intermediate_steps(None) == []
        assert chat_mod._load_intermediate_steps({"intermediate_steps": "{ 不是 json"}) == []

    def test_context_does_not_double_across_turns(self):
        """executor 返回的就是我们传进去的那个 list，不能再 extend 一次。

        真实链路里 ``L.extend(L)`` 让上下文每轮翻倍：第一轮 2 次工具调用存成 4 步，
        第二轮 8 步。
        """
        from langchain_core.agents import AgentAction

        step = (AgentAction(tool="navigate", tool_input={"url": "x"}, log=""), "ok")
        carried = [step]  # executor 原地追加过本轮步骤后，返回的就是这个对象

        merged = PlatformToolsRunnable.merge_intermediate_steps(carried, carried)

        assert merged == [step]
        assert merged is carried

    def test_foreign_list_replaces_and_empty_keeps(self):
        from langchain_core.agents import AgentAction

        step = (AgentAction(tool="navigate", tool_input={"url": "x"}, log=""), "ok")
        carried = []

        # executor 若返回的是另一个列表（含上下文+本轮），直接采用，不重复拼接
        merged = PlatformToolsRunnable.merge_intermediate_steps(carried, [step])
        assert merged == [step]

        # 没开 return_intermediate_steps 时返回空列表，不能把已有上下文清掉
        assert PlatformToolsRunnable.merge_intermediate_steps([step], []) == [step]


class TestAggregateNonStreamFrames:
    """非流式收尾：同一段文字这条流会推好几遍，正文只能算一遍。"""

    @staticmethod
    def _frame(status, content="", tool_calls=None):
        return {
            "status": status,
            "model": "stub",
            "created": 1,
            "choices": [{"delta": {"content": content,
                                   "tool_calls": tool_calls or []}}],
        }

    def test_answer_counted_once(self):
        answer = "最近最火的是《XX》"
        content, _ = chat_mod._aggregate_agent_frames([
            self._frame(AgentStatus.llm_new_token, "最近最火的是"),
            self._frame(AgentStatus.llm_new_token, "《XX》"),
            self._frame(AgentStatus.llm_end, answer),          # 本轮全文回放
            self._frame(AgentStatus.agent_finish, answer),     # 最终答案
            self._frame(AgentStatus.chain_end, answer),        # 收尾块
        ])
        assert content == answer

    def test_scaffolding_never_becomes_the_answer(self):
        thought = "Thought: 调用工具\nAction: search_internet"
        content, _ = chat_mod._aggregate_agent_frames([
            self._frame(AgentStatus.llm_end, thought),
            self._frame(AgentStatus.agent_action, thought),
        ])
        assert content == ""

    def test_finish_falls_back_when_no_token_streamed(self):
        content, _ = chat_mod._aggregate_agent_frames([
            self._frame(AgentStatus.llm_new_token, ""),
            self._frame(AgentStatus.agent_finish, "推理模型只回收尾块"),
        ])
        assert content == "推理模型只回收尾块"

    def test_tool_calls_collected_from_tool_end(self):
        call = {"index": 0, "id": "r1", "type": "function",
                "function": {"name": "navigate", "arguments": {"url": "x"}}}
        _, tool_calls = chat_mod._aggregate_agent_frames([
            self._frame(AgentStatus.tool_start, "", [call]),
            self._frame(AgentStatus.tool_end, "", [call]),
        ])
        assert tool_calls == [call]

    def test_frames_without_status_are_treated_as_tokens(self):
        content, _ = chat_mod._aggregate_agent_frames([
            self._frame(None, "一段"),
            self._frame(None, "二段"),
        ])
        assert content == "一段二段"
