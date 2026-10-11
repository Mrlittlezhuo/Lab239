"""工具名不存在时，Agent 不再空转到步数上限。

现场（2026-10-11 15:39 的日志）：智能聊天里让 Agent 重试一次搜索，意图路由回退到
直接回答 —— 这一轮一个工具都没有；但模型照着上一轮的格式又写了 `<navigate_page>`，
解析器把它当成工具调用，执行器在工具表里找不到，只回一句
`navigate_page is not a valid tool, try one of []`。模型看不懂这句，原样重发，
连发 15 次才撞到 langchain 的 `max_iterations`：日志里 15 条 agent_action、
0 条 tool_start/tool_end，15 次模型调用，回答是一串"好的，我再尝试一次"。

全部离线：不起 MCP server、不调模型、不碰数据库。
"""
from typing import Any, List

import pytest
from langchain_classic.agents.agent import RunnableAgent
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool

from langchain_chatchat.agents.all_tools_agent import PlatformToolsAgentExecutor
from langchain_chatchat.agents.output_parsers import MCPToolAction
from langchain_chatchat.agent_toolkits.mcp_kit.tools import MCPStructuredTool

NAVIGATE = "navigate_page"
SERVER = "chrome-devtools"


def _mcp_tool(name: str = NAVIGATE, server_name: str = SERVER, calls: list = None) -> MCPStructuredTool:
    async def _open_page(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return "已打开页面"

    return MCPStructuredTool(
        name=name,
        description="打开一个 URL",
        server_name=server_name,
        args_schema={"url": {"type": "string"}},
        coroutine=_open_page,
    )


class SearchInternet(BaseTool):
    """真存在的平台工具：用来确认修复没有把正常调用一起拦掉。"""

    name: str = "search_internet"
    description: str = "联网搜索"

    def _run(self, query: str = "", **kwargs: Any) -> str:
        return f"搜索结果：{query}"

    async def _arun(self, query: str = "", **kwargs: Any) -> str:
        return self._run(query)


def _executor(actions: List[Any], tools=(), mcp_tools=()) -> PlatformToolsAgentExecutor:
    """按预先准备好的 action 顺序吐出的假 Agent。

    守卫失效时假 Agent 会被取 15 次并抛 StopIteration，测试直接炸，不会静默通过。
    """
    remaining = iter(actions)

    async def _next(_inputs):
        return next(remaining)

    agent = RunnableAgent(runnable=RunnableLambda(_next), input_keys=["input"])
    return PlatformToolsAgentExecutor(
        agent=agent,
        tools=list(tools),
        mcp_tools=list(mcp_tools),
        return_intermediate_steps=True,
    )


def _action(tool: str, tool_input: dict) -> AgentAction:
    return AgentAction(tool=tool, tool_input=tool_input, log="好的，我再尝试一次。")


class TestMissingToolReason:
    def test_tool_in_map_has_no_reason(self):
        executor = _executor([], tools=[SearchInternet()])

        assert executor._missing_tool_reason(_action("search_internet", {}), {"search_internet": SearchInternet()}) is None

    @pytest.mark.parametrize("available,expected_tail", [
        ({}, "本轮可用工具：无"),
        ({"search_internet": SearchInternet()}, "本轮可用工具：search_internet"),
    ])
    def test_missing_platform_tool_names_tool_and_available_list(self, available, expected_tail):
        executor = _executor([])

        reason = executor._missing_tool_reason(_action(NAVIGATE, {"url": "https://x"}), available)

        assert reason == f"工具「{NAVIGATE}」本轮不可用（{expected_tail}），已停止重试"

    def test_mcp_tool_matched_by_name_and_server_has_no_reason(self):
        executor = _executor([], mcp_tools=[_mcp_tool()])
        action = MCPToolAction(server_name=SERVER, tool=NAVIGATE, tool_input={"url": "https://x"}, log="")

        assert executor._missing_tool_reason(action, {}) is None

    def test_mcp_server_name_mismatch_is_reported_with_the_server(self):
        # 工具名对得上、server_name 对不上：查找要求两个字段同时匹配，这也是最常见的
        # 一种"模型写的 server_name 跟连接记录不一致"
        executor = _executor([], mcp_tools=[_mcp_tool(server_name="other-server")])
        action = MCPToolAction(server_name=SERVER, tool=NAVIGATE, tool_input={"url": "https://x"}, log="")

        reason = executor._missing_tool_reason(action, {})

        assert reason == f"MCP 工具「{NAVIGATE}」（服务「{SERVER}」）本轮没有加载成功，已停止重试"

    def test_mcp_tool_with_no_server_connected_is_reported(self):
        executor = _executor([])
        action = MCPToolAction(server_name=SERVER, tool=NAVIGATE, tool_input={"url": "https://x"}, log="")

        assert executor._missing_tool_reason(action, {}) is not None


class TestStopOnMissingTool:
    def test_returns_agent_finish_carrying_the_reason(self):
        executor = _executor([])
        action = _action(NAVIGATE, {"url": "https://x"})
        step = executor._perform_agent_action({}, {}, action)

        # 循环里拿到的是 _consume_next_step 转换后的 (action, observation) 二元组
        stopped = executor._stop_on_missing_tool([(step.action, step.observation)], {})

        assert isinstance(stopped, AgentFinish)
        assert "已停止重试" in stopped.return_values["output"]

    def test_returns_none_when_every_tool_exists(self):
        tool = SearchInternet()
        tool_map = {"search_internet": tool}
        executor = _executor([], tools=[tool])
        step = executor._perform_agent_action(
            tool_map, {"search_internet": "red"}, _action("search_internet", {"query": "x"})
        )

        assert step.observation == "搜索结果：x"
        assert executor._stop_on_missing_tool([(step.action, step.observation)], tool_map) is None


class TestAgentLoop:
    async def test_missing_tool_stops_after_one_step(self):
        # 复现 15:39：本轮没有任何工具，模型反复要 navigate_page
        executor = _executor([_action(NAVIGATE, {"url": "https://x"})] * 15)

        out = await executor.ainvoke({"input": "再试一次", "chat_history": []})

        assert len(out["intermediate_steps"]) == 1
        assert "已停止重试" in out["output"]
        # 观察结果与最终回答一致：下一轮的 scratchpad 里看到的也是这句中文
        assert out["intermediate_steps"][0][1] == out["output"]

    async def test_missing_mcp_tool_stops_after_one_step(self):
        executor = _executor(
            [MCPToolAction(server_name=SERVER, tool=NAVIGATE, tool_input={"url": "https://x"}, log="")] * 15,
            mcp_tools=[_mcp_tool(server_name="other-server")],
        )

        out = await executor.ainvoke({"input": "再试一次", "chat_history": []})

        assert len(out["intermediate_steps"]) == 1
        assert f"服务「{SERVER}」" in out["output"]

    async def test_existing_platform_tool_still_runs(self):
        tool = SearchInternet()
        executor = _executor(
            [
                _action("search_internet", {"query": "苏州大学"}),
                AgentFinish(return_values={"output": "这是回答"}, log="这是回答"),
            ],
            tools=[tool],
        )

        out = await executor.ainvoke({"input": "搜一下苏州大学", "chat_history": []})

        assert out["output"] == "这是回答"
        assert out["intermediate_steps"][0][1] == "搜索结果：苏州大学"

    async def test_matched_mcp_tool_still_runs(self):
        calls: list = []
        mcp_tool = _mcp_tool(calls=calls)
        executor = _executor(
            [
                MCPToolAction(server_name=SERVER, tool=NAVIGATE, tool_input={"url": "https://x"}, log=""),
                AgentFinish(return_values={"output": "页面已打开"}, log="页面已打开"),
            ],
            mcp_tools=[mcp_tool],
        )

        out = await executor.ainvoke({"input": "打开这个页面", "chat_history": []})

        assert out["output"] == "页面已打开"
        assert out["intermediate_steps"][0][1] == "已打开页面"
        assert calls == [{"url": "https://x"}]
