"""智能路由接通 MCP（离线）。

现场（2026-10-12）：智能路由模式下 MCP 工具永远用不上，三处不一致叠加：

1. ``chat_routes`` 的 auto_agent 分支只读全局 ``AUTO_ROUTE_USE_MCP``
   （默认 False），请求里带 ``use_mcp: true`` 也被忽略；
2. 意图路由看不见 MCP 候选（MCP 工具名必须连上服务才知道，连接表不缓存
   工具清单），需要 MCP 的请求在 ``_resolve_execution`` 被判
   「无可用候选工具」退回直接回答；
3. 只有多步骤分支把 ``use_mcp`` 传下去，单工具分支写死 ``use_mcp=False``。

修法：候选清单允许保留名 ``mcp``（仅本次请求开启 MCP 时有效），执行分支
在建立 MCP 连接后把它换成真实工具；MCP 开关按请求走，全局配置只作强制
开启的逃生口。

全部离线：假 http_client / 假工具 / 打桩 agent_run_chat，不连 MCP 服务、
LLM、FastJev、数据库或网络。
"""
import asyncio
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# 在导入 chat 相关模块之前打桩 kb_chat：其导入链（search_local_knowledgebase）
# 在模块加载时读取真实知识库数据库，离线测试无法提供。与 test_intent_routing /
# test_smart_conversation 保持一致。
# ---------------------------------------------------------------------------
if "chatchat.server.chat.kb_chat" not in sys.modules:
    _kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
    _kb_stub.kb_chat = lambda *args, **kwargs: None
    sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.intent_router.providers import (
    FastJevIntentProvider,
    LlmIntentProvider,
    RouteContext,
)
from chatchat.server.intent_router.schemas import (
    COMPLEXITY_MULTI,
    COMPLEXITY_SINGLE,
    INTENT_COMPLEX,
    INTENT_SINGLE_TOOL,
    MCP_CANDIDATE,
    PATH_AGENT_RUN,
    RouteError,
    RouteResult,
    validate_route_result,
)
import chatchat.server.api_server.chat_routes as chat_routes_mod
import chatchat.server.agent_run.service as agent_service
import chatchat.server.intent_router as intent_router_pkg
from chatchat.server.smart_conversation import graph as G


def _tool(name: str, description: str = "假工具"):
    return types.SimpleNamespace(name=name, description=description)


def _route(intent: str, complexity: str, candidates):
    return RouteResult(
        path=PATH_AGENT_RUN,
        intent=intent,
        complexity=complexity,
        candidate_tools=list(candidates),
        confidence=0.9,
        provider="test",
        source="test",
    )


# ---------------------------------------------------------------------------
# 1. 候选校验：保留名 mcp 只在开启 MCP 时有效
# ---------------------------------------------------------------------------


class TestValidateMcpCandidate:
    def test_mcp_candidate_survives_when_mcp_is_enabled(self):
        result = validate_route_result(
            _route(INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]),
            {"search_internet": _tool("search_internet")},
            mcp_enabled=True,
        )
        assert result.candidate_tools == [MCP_CANDIDATE]

    def test_mcp_candidate_is_dropped_when_mcp_is_disabled(self):
        # 关掉开关时路由器不该还能声明要用 MCP：剔除后没有候选 → 非法结果，
        # 由路由器统一回退直接回答。
        with pytest.raises(RouteError):
            validate_route_result(
                _route(INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]),
                {"search_internet": _tool("search_internet")},
                mcp_enabled=False,
            )

    def test_unknown_tool_names_are_still_rejected(self):
        with pytest.raises(RouteError):
            validate_route_result(
                _route(INTENT_SINGLE_TOOL, COMPLEXITY_SINGLE, ["browser_navigate"]),
                {"search_internet": _tool("search_internet")},
                mcp_enabled=True,
            )


# ---------------------------------------------------------------------------
# 2. 执行分支判定
# ---------------------------------------------------------------------------


class TestResolveExecution:
    def test_mcp_only_complex_request_goes_to_multi_step_agent(self):
        route, mode = G._resolve_execution(
            _route(INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]),
            {"calculate": _tool("calculate")},
            mcp_enabled=True,
        )
        assert mode == G.EXEC_MULTI_STEP_AGENT
        # 没有被降级：path 仍是 agent_run，也没有回退原因
        assert route.path == PATH_AGENT_RUN
        assert route.fallback_reason == ""

    def test_mcp_only_single_request_goes_to_single_tool_task(self):
        _, mode = G._resolve_execution(
            _route(INTENT_SINGLE_TOOL, COMPLEXITY_SINGLE, [MCP_CANDIDATE]),
            {"calculate": _tool("calculate")},
            mcp_enabled=True,
        )
        assert mode == G.EXEC_SINGLE_TOOL_TASK

    def test_mcp_only_request_still_demotes_when_mcp_is_disabled(self):
        route, mode = G._resolve_execution(
            _route(INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]),
            {"calculate": _tool("calculate")},
            mcp_enabled=False,
        )
        assert mode == G.EXEC_DIRECT_ANSWER
        assert route.fallback_reason == "无可用候选工具"

    def test_platform_tools_are_kept_and_mcp_placeholder_is_not_a_tool(self):
        valid, mcp_requested = G._split_candidates(
            _route(INTENT_COMPLEX, COMPLEXITY_MULTI, ["calculate", MCP_CANDIDATE]),
            {"calculate": _tool("calculate")},
            mcp_enabled=True,
        )
        assert valid == ["calculate"]
        assert mcp_requested is True

    def test_split_candidates_ignores_mcp_when_disabled(self):
        valid, mcp_requested = G._split_candidates(
            _route(INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]),
            {},
            mcp_enabled=False,
        )
        assert valid == []
        assert mcp_requested is False


# ---------------------------------------------------------------------------
# 3. route 节点将开关传给分支判定
# ---------------------------------------------------------------------------


class TestRouteNodePassesMcpFlag:
    def test_mcp_only_route_selects_multi_step_agent(self, monkeypatch):
        monkeypatch.setattr(
            intent_router_pkg,
            "route_auto_agent",
            lambda query, **kwargs: _route(
                INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]
            ),
        )
        state = {
            "query": "打开浏览器看看官网今天的公告",
            "available_tools": {"calculate": _tool("calculate")},
            "auto_use_mcp": True,
        }
        out = asyncio.run(G.route_node(state))
        assert out["execution_mode"] == G.EXEC_MULTI_STEP_AGENT
        assert out["execution_mode_label"] == "多步骤 Agent"


# ---------------------------------------------------------------------------
# 4. 分支节点把 MCP 候选换成 use_mcp=True
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_agent(monkeypatch):
    """打桩 agent_run_chat 与工具配置装配，记录实际传给运行时的参数。"""
    calls = []

    async def fake_agent_run_chat(**kwargs):
        calls.append(kwargs)
        return {"object": "chat.completion", "content": "ok"}

    monkeypatch.setattr(agent_service, "agent_run_chat", fake_agent_run_chat)
    monkeypatch.setattr(
        chat_routes_mod,
        "get_tool_config",
        lambda name: {"configured": name},
    )
    return calls


class TestBranchNodesWithMcp:
    def test_single_tool_task_loads_mcp_when_router_asked_for_it(
        self, _stub_agent
    ):
        state = {
            "query": "打开浏览器查一下公告",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "session": None,
            "owner_id": "u1",
            "stream": False,
            "auto_use_mcp": True,
            "available_tools": {"calculate": _tool("calculate")},
            "route_result": _route(
                INTENT_SINGLE_TOOL, COMPLEXITY_SINGLE, [MCP_CANDIDATE]
            ),
        }
        asyncio.run(G.single_tool_task_node(state))

        kwargs = _stub_agent[0]
        # 平台工具一个都没装配（候选只有保留名），改为加载 MCP 工具
        assert kwargs["tool_config"] == {}
        assert kwargs["use_mcp"] is True
        assert kwargs["max_steps"] == G.ONE_STEP_MAX_STEPS

    def test_single_tool_task_keeps_platform_tool_and_mcp(self, _stub_agent):
        state = {
            "query": "算一下再打开浏览器",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "session": None,
            "owner_id": "u1",
            "stream": False,
            "auto_use_mcp": True,
            "available_tools": {"calculate": _tool("calculate")},
            "route_result": _route(
                INTENT_SINGLE_TOOL, COMPLEXITY_SINGLE, ["calculate", MCP_CANDIDATE]
            ),
        }
        asyncio.run(G.single_tool_task_node(state))

        kwargs = _stub_agent[0]
        assert list(kwargs["tool_config"]) == ["calculate"]
        assert kwargs["use_mcp"] is True

    def test_single_tool_task_without_mcp_stays_tool_only(self, _stub_agent):
        state = {
            "query": "算一下 1+1",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "session": None,
            "owner_id": "u1",
            "stream": False,
            "auto_use_mcp": True,
            "available_tools": {"calculate": _tool("calculate")},
            "route_result": _route(
                INTENT_SINGLE_TOOL, COMPLEXITY_SINGLE, ["calculate"]
            ),
        }
        asyncio.run(G.single_tool_task_node(state))

        kwargs = _stub_agent[0]
        assert list(kwargs["tool_config"]) == ["calculate"]
        # 路由器没要求 MCP，一步任务就不额外挂一堆未知 MCP 工具
        assert kwargs["use_mcp"] is False

    def test_multi_step_task_with_mcp_only_does_not_raise(self, _stub_agent):
        state = {
            "query": "打开浏览器并汇总公告",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "session": None,
            "owner_id": "u1",
            "stream": False,
            "auto_use_mcp": True,
            "available_tools": {},
            "route_result": _route(
                INTENT_COMPLEX, COMPLEXITY_MULTI, [MCP_CANDIDATE]
            ),
        }
        asyncio.run(G.multi_step_agent_node(state))

        kwargs = _stub_agent[0]
        assert kwargs["tool_config"] == {}
        assert kwargs["use_mcp"] is True

    def test_multi_step_task_without_any_candidate_still_raises(
        self, _stub_agent
    ):
        state = {
            "query": "做点什么",
            "conversation_id": "conv-1",
            "message_id": "msg-1",
            "session": None,
            "owner_id": "u1",
            "stream": False,
            "auto_use_mcp": False,
            "available_tools": {},
            "route_result": _route(INTENT_COMPLEX, COMPLEXITY_MULTI, []),
        }
        with pytest.raises(ValueError, match="多步骤任务无可用候选工具"):
            asyncio.run(G.multi_step_agent_node(state))


# ---------------------------------------------------------------------------
# 5. FastJev provider：把保留候选名作为可选项交给分类器
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeHttpClient:
    """记录请求载荷并返回脚本化的 FastJev 答案。"""

    def __init__(self, answers):
        self.answers = answers
        self.payloads = []

    def post(self, url, json, timeout):
        self.payloads.append(json)
        return _FakeResponse({"answers": self.answers})


def _jev_answers(intent: str, tool: str, score: float = 0.8):
    return {
        "intent": {"choice": intent, "probabilities": {intent: 0.6}},
        "primary_tool": {"choice": tool, "probabilities": {tool: score}},
        "secondary_tool": {"choice": "none", "probabilities": {"none": 0.9}},
    }


class TestFastJevMcpCandidate:
    def test_mcp_candidate_is_offered_only_when_enabled(self):
        client = _FakeHttpClient(_jev_answers("complex", MCP_CANDIDATE))
        provider = FastJevIntentProvider(
            "http://127.0.0.1:8811", "fake-model", http_client=client
        )
        ctx = RouteContext(
            query="打开浏览器看看官网公告",
            available_tools={"search_internet": _tool("search_internet")},
            use_mcp=True,
        )
        result = provider.route(ctx)

        criteria = client.payloads[0]["questions"]["primary_tool"]["criteria"]
        assert MCP_CANDIDATE in criteria
        assert result.candidate_tools == [MCP_CANDIDATE]
        assert result.is_agent_run

    def test_mcp_candidate_is_not_offered_when_disabled(self):
        # 关闭开关时分类器看不见 mcp，选了它也不算有效候选 → 结果非法，
        # 由路由器回退直接回答（保持旧行为，不会误开 MCP）。
        client = _FakeHttpClient(_jev_answers("complex", MCP_CANDIDATE))
        provider = FastJevIntentProvider(
            "http://127.0.0.1:8811", "fake-model", http_client=client
        )
        ctx = RouteContext(
            query="打开浏览器看看官网公告",
            available_tools={"search_internet": _tool("search_internet")},
            use_mcp=False,
        )
        with pytest.raises(RouteError):
            provider.route(ctx)

        criteria = client.payloads[0]["questions"]["primary_tool"]["criteria"]
        assert MCP_CANDIDATE not in criteria

    def test_mcp_only_candidate_still_gets_tool_head_confidence(self):
        client = _FakeHttpClient(_jev_answers("complex", MCP_CANDIDATE, 0.95))
        provider = FastJevIntentProvider(
            "http://127.0.0.1:8811",
            "fake-model",
            http_client=client,
            promotion_threshold=0.9,
        )
        ctx = RouteContext(
            query="打开浏览器看看官网公告",
            available_tools={"search_internet": _tool("search_internet")},
            use_mcp=True,
        )
        result = provider.route(ctx)
        # 意图头只有 0.6，工具头托底到 0.95，否则会被低置信度护栏退回直接回答
        assert result.confidence == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# 6. LLM provider 提示词：明确 MCP 场景怎么写候选
# ---------------------------------------------------------------------------


class TestLlmPromptMcpRule:
    def test_prompt_names_the_reserved_candidate_when_mcp_is_on(self):
        provider = LlmIntentProvider(llm=object())
        prompt = provider._system_prompt(
            RouteContext(
                query="打开浏览器",
                available_tools={"calculate": _tool("calculate")},
                use_mcp=True,
            )
        )
        assert MCP_CANDIDATE in prompt
        assert "禁止编造具体 MCP 工具名" in prompt

    def test_prompt_has_no_mcp_rule_when_mcp_is_off(self):
        provider = LlmIntentProvider(llm=object())
        prompt = provider._system_prompt(
            RouteContext(
                query="算一下 1+1",
                available_tools={"calculate": _tool("calculate")},
                use_mcp=False,
            )
        )
        assert MCP_CANDIDATE not in prompt
