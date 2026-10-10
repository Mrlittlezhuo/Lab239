"""智能对话总图离线测试（任务 015）。

全部使用临时 SQLite 数据库（迁移到 v7）+ 确定性假工具 / 假规划器 / 脚本化
route_auto_agent 替身，不连接真实 LLM、FastJev、MCP、知识库、外部网络或用户
数据库；用户身份一律来自服务端认证上下文。

覆盖任务文档 §9 要求：
- 总图包含独立路由节点、四类执行节点以及条件边（真实 LangGraph 状态图）；
- 普通请求走直接回答，不创建 AgentRun；
- 知识库请求只获得知识库工具，最多执行一个工具步骤；
- 单工具请求只获得匹配工具，最多执行一个工具步骤；
- 复杂任务进入任务 014 的多步骤 LangGraph Agent（复用 checkpoint 恢复能力）；
- 路由异常、低置信度回退、未知工具与无有效候选时回退直接回答；
- 工具或 Agent 已开始后的失败不会再触发第二条执行路径；
- 流式响应事件顺序正确（首条为路由 / 执行分支事件），非流式字段兼容；
- 一次请求只创建一条消息并写回一次最终回答；
- auto_agent、手动 agent_run 与未携带新参数的旧请求继续可用；
- 用户 A 不能使用用户 B 的会话（智能对话路径同样校验会话所有者）。
"""
import sys
import types
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# 在导入 chat 相关模块之前打桩 kb_chat：其导入链（search_local_knowledgebase）
# 在模块加载时读取真实知识库数据库，离线测试无法提供。与
# test_intent_routing / test_agent_run 保持一致。
# ---------------------------------------------------------------------------
if "chatchat.server.chat.kb_chat" not in sys.modules:
    _kb_stub = types.ModuleType("chatchat.server.chat.kb_chat")
    _kb_stub.kb_chat = lambda *args, **kwargs: None
    sys.modules["chatchat.server.chat.kb_chat"] = _kb_stub

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.user_model import UserModel, OpenAIFileModel  # noqa: F401
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.knowledge_base_model import KnowledgeBaseModel  # noqa: F401
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel  # noqa: F401
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.agent_step_model import AgentStepModel  # noqa: F401
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.db.repository import create_conversation
from chatchat.server.agent_run import schema as S
import chatchat.server.agent_run.service as agent_service
import chatchat.server.api_server.chat_routes as chat_routes_mod
import chatchat.server.intent_router as intent_router_pkg
from chatchat.server.api_server.agent_run_routes import agent_run_router
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.intent_router import (
    INTENT_COMPLEX,
    INTENT_GENERAL,
    INTENT_KB_SEARCH,
    INTENT_SINGLE_TOOL,
    PATH_AGENT_RUN,
    PATH_DIRECT,
    RouteResult,
    rule_fast_path,
)
from chatchat.server.intent_router.schemas import (
    COMPLEXITY_DIRECT,
    COMPLEXITY_MULTI,
    COMPLEXITY_SINGLE,
)
from chatchat.server.smart_conversation.graph import (
    EXEC_DIRECT_ANSWER,
    EXEC_KNOWLEDGE_TASK,
    EXEC_MULTI_STEP_AGENT,
    EXEC_SINGLE_TOOL_TASK,
    EXECUTION_MODE_LABELS,
    KB_TOOL_NAME,
    ONE_STEP_MAX_STEPS,
    build_smart_conversation_graph,
    get_smart_conversation_graph,
)
from chatchat.server.smart_conversation.service import run_smart_conversation

# 复用 test_intent_routing 的确定性测试替身与数据库助手，避免重复实现
from test_intent_routing import (  # noqa: E402
    CapturingPlanner,
    FakeTool,
    ScriptedPlanner,
    _headers,
    _kb_result,
    _mk_user,
    _run_row,
    _session,
)


# ---------------------------------------------------------------------------
# fixtures（与 test_intent_routing / test_agent_run 保持一致）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_sse_app_status():
    """隔离 sse-starlette 的进程级全局状态。

    ``AppStatus.should_exit_event`` 是类级全局 anyio.Event，会在第一次流式
    响应时绑定当时的事件循环。本文件与 test_intent_routing 等文件各自用
    ``asyncio.run`` 创建新事件循环，若不在测试前后重置，先运行的流式测试会
    把该 Event 泄漏给后续测试文件，导致 "bound to a different event loop"。
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "smart-conv-unit-test-secret-00000000",
    )
    return "smart-conv-unit-test-secret-00000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：建全表、迁移到 v7，注册模型元数据。"""
    path = tmp_path / "smart_conv_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    assert current_version(engine.connect()) == 12
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine, "owner_id": None}
    engine.dispose()


@pytest.fixture
def users(temp_db):
    """创建 alice/bob 两个普通用户（均已改密）。"""
    return {
        "alice": _mk_user(temp_db, "alice"),
        "bob": _mk_user(temp_db, "bob"),
    }


@pytest.fixture
def app(temp_db):
    """FastAPI 应用：认证 + Agent 运行 + chat 路由，get_db 指向临时库。"""
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(agent_run_router)
    a.include_router(chat_router)

    async def override_get_db():
        s = temp_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_get_db] = override_get_db
    return a


@pytest.fixture
def client(app, monkeypatch):
    # 当前 Starlette/TestClient 与环境中的 anyio 组合会在请求线程中卡住。
    # 直接使用 httpx 的 ASGI transport，仍然完整经过 FastAPI 路由与依赖。
    async def direct_call(func, *args, **kwargs):
        return func(*args, **kwargs)

    import fastapi.routing
    import fastapi.dependencies.utils

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", direct_call)
    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", direct_call)

    class Client:
        def request(self, method, url, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as async_client:
                    return await async_client.request(method, url, **kwargs)

            return asyncio.run(send())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    return Client()


def _parse_sse_events(text: str):
    """把 SSE 文本解析为 JSON 事件列表（忽略注释 / ping 行）。"""
    events = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# 1. 总图结构：独立路由节点 + 四类执行节点 + 条件边
# ---------------------------------------------------------------------------


class TestMasterGraphStructure:
    def test_graph_has_route_four_branches_and_normalize(self):
        g = build_smart_conversation_graph()
        dg = g.get_graph()
        node_names = set(dg.nodes.keys())
        assert {"route", "direct_answer", "knowledge_task",
                "single_tool_task", "multi_step_agent",
                "normalize_result"} <= node_names

        edge_keys = {(e.source, e.target) for e in dg.edges}
        # START → route；route → 四类执行分支（条件边）
        assert ("__start__", "route") in edge_keys
        for branch in ("direct_answer", "knowledge_task",
                       "single_tool_task", "multi_step_agent"):
            assert ("route", branch) in edge_keys, f"缺少条件边 route→{branch}"
        # 四类执行分支 → normalize_result → END
        for branch in ("direct_answer", "knowledge_task",
                       "single_tool_task", "multi_step_agent"):
            assert (branch, "normalize_result") in edge_keys
        assert ("normalize_result", "__end__") in edge_keys

        # 条件边必须真实存在（route 的出边为条件边，而非单一包装节点）
        conditional_from_route = [
            e for e in dg.edges if e.source == "route" and e.conditional
        ]
        assert len(conditional_from_route) == 4

    def test_graph_compiled_without_persistent_checkpointer(self):
        """总图是短生命周期编排层：不挂载第二套持久 checkpoint。"""
        g = build_smart_conversation_graph()
        assert getattr(g, "checkpointer", None) is None
        assert getattr(g, "_checkpointer", None) is None

    def test_process_wide_graph_is_shared(self):
        assert get_smart_conversation_graph() is get_smart_conversation_graph()

    def test_one_step_bound_constants(self):
        assert ONE_STEP_MAX_STEPS == 1
        assert KB_TOOL_NAME == "search_local_knowledgebase"
        assert set(EXECUTION_MODE_LABELS) == {
            EXEC_DIRECT_ANSWER, EXEC_KNOWLEDGE_TASK,
            EXEC_SINGLE_TOOL_TASK, EXEC_MULTI_STEP_AGENT,
        }


# ---------------------------------------------------------------------------
# 2. 智能对话分发（/chat/chat/completions + extra.auto_agent）
# ---------------------------------------------------------------------------


class TestSmartConversationDispatch:
    def _patch_smart(self, monkeypatch, temp_db, planner, route, extra_tools=()):
        """把服务层接到临时库与假规划器/假工具，并用脚本化 route_auto_agent
        替身模拟路由结果（不触真实 LLM / FastJev / 网络）。"""
        monkeypatch.setattr(
            chat_routes_mod, "get_OpenAIClient", lambda **k: None
        )
        tools = {
            "search_local_knowledgebase": FakeTool(
                "search_local_knowledgebase", "本地知识库检索（mock）",
                result=_kb_result,
            ),
            "calculate": FakeTool("calculate", "计算器（mock）", result="42"),
            "weather_check": FakeTool(
                "weather_check", "天气查询（mock）", result="晴 25 度"
            ),
        }
        for name in extra_tools:
            tools.setdefault(
                name, FakeTool(name, f"mock {name}", result=f"{name} 结果")
            )

        def _get_tool(name=None):
            if name is None:
                return tools
            return tools.get(name)

        monkeypatch.setattr(agent_service, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool", _get_tool)
        monkeypatch.setattr(
            chat_routes_mod, "get_tool_config", lambda name: {}
        )
        monkeypatch.setattr(
            agent_service, "_build_planner",
            lambda model_name=None: planner,
        )
        monkeypatch.setattr(
            agent_service, "_session_factory",
            lambda: temp_db["Session"](),
        )
        monkeypatch.setattr(agent_service, "_build_history", lambda *a, **k: [])
        monkeypatch.setattr(
            agent_service, "_build_memory_messages", lambda *a, **k: []
        )

        calls = []

        def fake_route(query, *, available_tools, history=None,
                       use_mcp=False, router=None):
            calls.append(
                {
                    "query": query,
                    "tools": dict(available_tools or {}),
                    "use_mcp": use_mcp,
                }
            )
            if isinstance(route, BaseException):
                raise route
            return route

        monkeypatch.setattr(intent_router_pkg, "route_auto_agent", fake_route)
        return {"tools": tools, "route_calls": calls}

    def test_plain_request_direct_answer_no_agent_run(
        self, monkeypatch, app, client, temp_db, users
    ):
        """普通请求 → 直接回答分支：复用普通聊天，不创建 AgentRun。"""
        temp_db["owner_id"] = users["alice"]["id"]
        route = rule_fast_path("你好！")
        assert route is not None
        self._patch_smart(
            monkeypatch, temp_db,
            ScriptedPlanner(S.Plan(direct_answer="不应执行")), route,
        )
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "你好！有什么可以帮你的？"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "问候"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "你好！"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "你好！有什么可以帮你的？"
        assert data["conversation_id"] == conv
        # 统一输出字段：route_result（兼容 012 字段）+ execution_mode 扩展
        rr = data["route_result"]
        assert rr["path"] == PATH_DIRECT
        assert rr["path_label"] == "直接回答"
        assert rr["intent_label"] == "普通对话"
        assert data["execution_mode"] == EXEC_DIRECT_ANSWER
        assert data["execution_mode_label"] == EXECUTION_MODE_LABELS[EXEC_DIRECT_ANSWER]
        # 未创建任何 Agent 运行
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        # 直接回答不装配任何工具，身份来自服务端认证上下文
        assert called["kwargs"]["tool_config"] == {}
        assert called["kwargs"]["use_mcp"] is False
        assert called["kwargs"]["owner_id"] == users["alice"]["id"]

    def test_kb_task_kb_tool_only_and_one_step_limit(
        self, monkeypatch, app, client, temp_db, users
    ):
        """知识库请求 → 知识库任务分支：只提供知识库工具，且最多一个工具步骤。

        规划器故意给出两个工具步骤（知识库 + 计算器）：一步上限使第二步
        （计算器）不再执行，运行以明确的失败状态收敛，而不是静默换路径。
        """
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="先检索知识库，再计算",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    ),
                    S.PlanStep("计算", "calculate", {"expr": "1+1"}),
                ],
            ),
            decisions=[S.Decision(action="answer", answer="知识库最终回答")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=[KB_TOOL_NAME],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        patched = self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "知识库问题"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "知识库中 Mem0 的资料有哪些？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["execution_mode"] == EXEC_KNOWLEDGE_TASK
        assert data["conversation_id"] == conv
        run = data["agent_run"]
        # 规划器只拿到知识库工具（calculate / weather_check 不在其中）
        assert set(planner.last_ctx.tools) == {KB_TOOL_NAME}
        # 一步上限：知识库工具执行一次，计划中的第二个工具步骤不再执行
        kb = patched["tools"][KB_TOOL_NAME]
        calc = patched["tools"]["calculate"]
        assert kb.calls == [{"database": "kb", "query": "Mem0"}]
        assert calc.calls == []
        # 明确失败状态（达到一步上限），而非静默切换路径
        assert run["status"] == "failed"
        # 运行持久化，且只创建了一个 Agent 运行
        with _session(temp_db) as s:
            runs = s.query(AgentRunModel).all()
        assert len(runs) == 1
        assert _run_row(temp_db, run["run_id"])["status"] == "failed"

    def test_kb_task_happy_path_completed_one_message_one_writeback(
        self, monkeypatch, app, client, temp_db, users
    ):
        """知识库一步闭环：运行完成、一条用户消息、最终回答只写回一次。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="检索知识库",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="知识库最终回答")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=[KB_TOOL_NAME],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "知识库问题"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "知识库中 Mem0 的资料有哪些？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        run = data["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "知识库最终回答"
        assert data["execution_mode"] == EXEC_KNOWLEDGE_TASK
        # 非流式统一字段兼容：conversation_id / route_result / execution_mode
        assert data["conversation_id"] == conv
        assert data["route_result"]["path"] == PATH_AGENT_RUN
        # 一次请求只创建一条用户消息，最终回答写回该消息（不新增消息行）
        with _session(temp_db) as s:
            msgs = (
                s.query(MessageModel)
                .filter_by(conversation_id=conv)
                .all()
            )
        assert len(msgs) == 1
        assert msgs[0].chat_type == "agent_chat"
        assert msgs[0].query == "知识库中 Mem0 的资料有哪些？"
        assert msgs[0].response == "知识库最终回答"
        assert msgs[0].meta_data["agent_run_id"] == run["run_id"]

    def test_single_tool_task_matching_tool_only_and_one_step_limit(
        self, monkeypatch, app, client, temp_db, users
    ):
        """单工具请求 → 单工具任务分支：只给路由选中的工具，最多一个工具步骤。"""
        temp_db["owner_id"] = users["alice"]["id"]
        # 规划器故意在单工具步骤之后再排一个知识库步骤：一步上限使其不执行
        planner = CapturingPlanner(
            S.Plan(
                summary="查天气后查知识库",
                steps=[
                    S.PlanStep("查询天气", "weather_check", {"city": "上海"}),
                    S.PlanStep(
                        "检索知识库", "search_local_knowledgebase",
                        {"database": "kb", "query": "x"},
                    ),
                ],
            ),
            decisions=[S.Decision(action="answer", answer="上海今天晴天")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_SINGLE_TOOL,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["weather_check"],
            confidence=0.9,
            summary="单工具任务",
            provider="llm",
            source="llm",
        )
        patched = self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "天气"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "上海今天天气如何？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["execution_mode"] == EXEC_SINGLE_TOOL_TASK
        # 规划器只拿到路由选中的单个工具
        assert set(planner.last_ctx.tools) == {"weather_check"}
        weather = patched["tools"]["weather_check"]
        kb = patched["tools"][KB_TOOL_NAME]
        assert weather.calls == [{"city": "上海"}]
        assert kb.calls == []
        # 一步上限后的明确失败状态
        assert data["agent_run"]["status"] == "failed"

    def test_single_tool_task_happy_path_completed(
        self, monkeypatch, app, client, temp_db, users
    ):
        """单工具一步闭环完成：只调用匹配工具，运行完成。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="查天气",
                steps=[S.PlanStep("查询天气", "weather_check", {"city": "上海"})],
            ),
            decisions=[S.Decision(action="answer", answer="上海今天晴天")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_SINGLE_TOOL,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["weather_check"],
            confidence=0.9,
            summary="单工具任务",
            provider="llm",
            source="llm",
        )
        patched = self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "天气"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "上海今天天气如何？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        run = data["agent_run"]
        assert data["execution_mode"] == EXEC_SINGLE_TOOL_TASK
        assert run["status"] == "completed"
        assert run["final_result"] == "上海今天晴天"
        assert set(planner.last_ctx.tools) == {"weather_check"}
        assert patched["tools"]["weather_check"].calls == [{"city": "上海"}]
        assert patched["tools"][KB_TOOL_NAME].calls == []

    def test_complex_task_runs_multi_step_agent_runtime(
        self, monkeypatch, app, client, temp_db, users
    ):
        """复杂请求 → 多步骤 Agent 分支：复用任务 014 的多步执行与重规划。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="先检索知识库",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[S.PlanStep("计算", "calculate", {"expr": "6*7"})],
                ),
                S.Decision(action="answer", answer="复杂任务最终回答"),
            ],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_COMPLEX,
            complexity=COMPLEXITY_MULTI,
            candidate_tools=[KB_TOOL_NAME, "calculate"],
            confidence=0.9,
            summary="复杂任务",
            provider="llm",
            source="llm",
        )
        patched = self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "复杂任务"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "对比 Mem0 与 RAG 并计算 6*7"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["execution_mode"] == EXEC_MULTI_STEP_AGENT
        run = data["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "复杂任务最终回答"
        # 规划器拿到全部有效候选工具，两个工具步骤按序执行
        assert set(planner.last_ctx.tools) == {KB_TOOL_NAME, "calculate"}
        assert patched["tools"][KB_TOOL_NAME].calls == [
            {"database": "kb", "query": "Mem0"}
        ]
        assert patched["tools"]["calculate"].calls == [{"expr": "6*7"}]
        step_tools = [s["tool_name"] for s in run["steps"]]
        assert step_tools == [KB_TOOL_NAME, "calculate"]
        # 多步骤运行沿用任务 014 的 checkpoint 存储（断点恢复入口可用）
        run_id = run["run_id"]
        assert run_id
        assert _run_row(temp_db, run_id)["status"] == "completed"
        # MCP 默认不启用（自动路由配置）
        assert patched["route_calls"][0]["use_mcp"] is False

    def test_route_exception_falls_back_to_direct(
        self, monkeypatch, app, client, temp_db, users
    ):
        """路由调用异常 → 受控回退直接回答，不创建 AgentRun、不执行工具。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(steps=[S.PlanStep("计算", "calculate", {"expr": "1"})]),
            decisions=[S.Decision(action="answer", answer="不应执行")],
        )
        self._patch_smart(
            monkeypatch, temp_db, planner,
            RuntimeError("路由服务异常"),
        )
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "直接回答：路由失败已回退"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "路由异常"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "帮我查一下资料"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "直接回答：路由失败已回退"
        assert data["execution_mode"] == EXEC_DIRECT_ANSWER
        rr = data["route_result"]
        assert rr["path"] == PATH_DIRECT
        assert rr["source"] == "fallback"
        assert "路由意外异常" in rr["fallback_reason"]
        # 未创建 Agent 运行、未执行任何工具
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        assert planner.plan_calls == 0

    def test_low_confidence_demoted_route_goes_direct(
        self, monkeypatch, app, client, temp_db, users
    ):
        """低置信度降级路由（012 IntentRouter 已降级为 direct）→ 直接回答分支。"""
        temp_db["owner_id"] = users["alice"]["id"]
        route = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_DIRECT,
            candidate_tools=[],
            confidence=0.3,
            summary="判断置信度不足，已按普通对话回答",
            provider="llm",
            source="fallback",
            fallback_reason="置信度 0.3 低于阈值 0.5",
        )
        planner = ScriptedPlanner(
            S.Plan(steps=[S.PlanStep("检索", KB_TOOL_NAME, {"query": "x"})]),
            decisions=[S.Decision(action="answer", answer="不应执行")],
        )
        self._patch_smart(monkeypatch, temp_db, planner, route)
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "直接回答"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "低置信度"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "可能是知识库的问题"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["execution_mode"] == EXEC_DIRECT_ANSWER
        assert data["route_result"]["source"] == "fallback"
        assert planner.plan_calls == 0
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []

    def test_invalid_candidate_tools_fallback_before_execution(
        self, monkeypatch, app, client, temp_db, users
    ):
        """候选工具未知 / 无有效候选 → 工具执行开始前受控回退直接回答。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(steps=[S.PlanStep("计算", "calculate", {"expr": "1"})]),
            decisions=[S.Decision(action="answer", answer="不应执行")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_SINGLE_TOOL,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["ghost_tool"],
            confidence=0.9,
            summary="未知工具",
            provider="llm",
            source="llm",
        )
        patched = self._patch_smart(monkeypatch, temp_db, planner, route)
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "直接回答：无可用候选工具"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "未知工具"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "帮我用某个神秘工具做事"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["execution_mode"] == EXEC_DIRECT_ANSWER
        assert data["route_result"]["source"] == "fallback"
        assert "无可用候选工具" in data["route_result"]["fallback_reason"]
        # 工具执行开始前回退：未创建 AgentRun、未执行任何工具
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        assert patched["tools"]["calculate"].calls == []
        assert planner.plan_calls == 0

    def test_tool_failure_after_start_does_not_switch_path(
        self, monkeypatch, app, client, temp_db, users
    ):
        """工具执行开始后失败 → 明确失败状态，不再触发第二条执行路径。

        知识库工具执行一次后抛错；规划器尝试重规划追加计算器步骤，但一步
        上限阻止了新的工具执行——计算器从未被调用，也不存在第二个 Agent 运行。
        """
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool(
            KB_TOOL_NAME, "本地知识库检索（mock）",
            raises=RuntimeError("知识库服务异常"),
        )
        calc = FakeTool("calculate", "计算器（mock）", result="42")
        weather = FakeTool("weather_check", "天气查询（mock）", result="晴")
        tools = {
            KB_TOOL_NAME: kb, "calculate": calc, "weather_check": weather,
        }

        def _get_tool(name=None):
            if name is None:
                return tools
            return tools.get(name)

        planner = ScriptedPlanner(
            S.Plan(
                summary="检索知识库",
                steps=[
                    S.PlanStep(
                        "检索", KB_TOOL_NAME,
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[S.PlanStep("计算", "calculate", {"expr": "1+1"})],
                ),
                S.Decision(action="answer", answer="不应执行"),
            ],
        )

        monkeypatch.setattr(chat_routes_mod, "get_OpenAIClient", lambda **k: None)
        monkeypatch.setattr(agent_service, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool_config", lambda name: {})
        monkeypatch.setattr(agent_service, "_build_planner", lambda model_name=None: planner)
        monkeypatch.setattr(
            agent_service, "_session_factory", lambda: temp_db["Session"]()
        )
        monkeypatch.setattr(agent_service, "_build_history", lambda *a, **k: [])
        monkeypatch.setattr(agent_service, "_build_memory_messages", lambda *a, **k: [])

        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=[KB_TOOL_NAME],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        monkeypatch.setattr(intent_router_pkg, "route_auto_agent",
                           lambda q, **k: route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "工具失败"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "知识库中 Mem0 的资料有哪些？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        run = data["agent_run"]
        # 明确失败状态，而非静默切换到直接回答 / 其他工具
        assert data["execution_mode"] == EXEC_KNOWLEDGE_TASK
        assert run["status"] == "failed"
        # 知识库工具只执行了一次；重规划追加的计算器步骤从未执行
        assert len(kb.calls) == 1
        assert calc.calls == []
        # 只有这一个 Agent 运行（没有第二条执行路径）
        with _session(temp_db) as s:
            runs = s.query(AgentRunModel).all()
            msgs = s.query(MessageModel).filter_by(conversation_id=conv).all()
        assert len(runs) == 1
        assert len(msgs) == 1
        # 最终回答（失败说明）只写回一次
        assert msgs[0].response
        assert msgs[0].meta_data["agent_run_id"] == run["run_id"]

    def test_streaming_route_event_first_then_branch_events(
        self, monkeypatch, app, client, temp_db, users
    ):
        """流式：首条事件为路由 / 执行分支事件，其后透传 Agent 事件。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="检索知识库",
                steps=[
                    S.PlanStep(
                        "检索", KB_TOOL_NAME,
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="知识库最终回答")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=[KB_TOOL_NAME],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        self._patch_smart(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "知识库问题"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "知识库中 Mem0 的资料有哪些？"}],
                "stream": True,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        events = _parse_sse_events(r.text)
        assert events, "未解析到任何 SSE 事件"
        # 首条事件：路由 / 执行分支（RouteResult）
        first = events[0]
        assert first["class_name"] == "RouteResult"
        assert first["route_result"]["intent"] == INTENT_KB_SEARCH
        assert first["execution_mode"] == EXEC_KNOWLEDGE_TASK
        assert first["conversation_id"] == conv
        # 其后为所选分支的 Agent 事件，且顺序正确
        agent_events = [e.get("agent_event") for e in events[1:]
                        if e.get("class_name") == "AgentRunEvent"]
        assert "run_created" in agent_events
        assert agent_events.index("run_created") < agent_events.index("plan_ready")
        assert agent_events.index("plan_ready") < agent_events.index("run_completed")
        assert agent_events[-1] == "run_completed"
        completed = next(
            e for e in events[1:]
            if e.get("agent_event") == "run_completed"
        )
        assert completed["agent_final_result"] == "知识库最终回答"
        # Agent 事件透传所选分支的 run_id
        assert completed["run_id"]


# ---------------------------------------------------------------------------
# 3. 兼容性：手动 agent_run / 旧请求 / 会话隔离
# ---------------------------------------------------------------------------


class TestSmartConversationCompat:
    def test_manual_agent_run_flag_bypasses_smart_graph(
        self, monkeypatch, app, client, temp_db, users
    ):
        """手动调试路径（extra.agent_run）保持任务 011 行为，不经过总图路由。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="计算",
                steps=[S.PlanStep("计算", "calculate", {"expr": "6*7"})],
            ),
            decisions=[S.Decision(action="answer", answer="手动路径结果")],
        )
        tools = {
            "calculate": FakeTool("calculate", "计算器（mock）", result="42"),
        }

        def _get_tool(name=None):
            if name is None:
                return tools
            return tools.get(name)

        monkeypatch.setattr(chat_routes_mod, "get_OpenAIClient", lambda **k: None)
        monkeypatch.setattr(agent_service, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool_config", lambda name: {})
        monkeypatch.setattr(agent_service, "_build_planner", lambda model_name=None: planner)
        monkeypatch.setattr(
            agent_service, "_session_factory", lambda: temp_db["Session"]()
        )
        monkeypatch.setattr(agent_service, "_build_history", lambda *a, **k: [])
        monkeypatch.setattr(agent_service, "_build_memory_messages", lambda *a, **k: [])

        route_calls = []

        def fake_route(query, *, available_tools, history=None,
                       use_mcp=False, router=None):
            route_calls.append(query)
            return rule_fast_path(query)

        monkeypatch.setattr(intent_router_pkg, "route_auto_agent", fake_route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "手动调试"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "帮我算 6*7"}],
                "stream": False,
                "conversation_id": conv,
                "agent_run": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["agent_run"]["status"] == "completed"
        # 手动路径不调用意图路由（不经过总图），响应无 execution_mode 字段
        assert route_calls == []
        assert "execution_mode" not in data

    def test_legacy_request_without_new_params_plain_chat(
        self, monkeypatch, app, client, temp_db, users
    ):
        """未携带 auto_agent / agent_run 的旧请求 → 普通聊天，手动工具透传。"""
        temp_db["owner_id"] = users["alice"]["id"]
        tools = {
            KB_TOOL_NAME: FakeTool(KB_TOOL_NAME, "本地知识库检索（mock）"),
        }

        def _get_tool(name=None):
            if name is None:
                return tools
            return tools.get(name)

        monkeypatch.setattr(chat_routes_mod, "get_OpenAIClient", lambda **k: None)
        monkeypatch.setattr(chat_routes_mod, "get_tool", _get_tool)
        monkeypatch.setattr(chat_routes_mod, "get_tool_config", lambda name: {})
        route_calls = []

        def fake_route(query, *, available_tools, history=None,
                       use_mcp=False, router=None):
            route_calls.append(query)

        monkeypatch.setattr(intent_router_pkg, "route_auto_agent", fake_route)

        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "普通聊天回答"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "旧客户端"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "你好"}],
                "stream": False,
                "conversation_id": conv,
                "tools": [KB_TOOL_NAME],
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "普通聊天回答"
        assert data["conversation_id"] == conv
        # 旧请求不触发自动路由，手动工具配置原样透传给普通聊天
        assert route_calls == []
        assert called["kwargs"]["tool_config"] == {KB_TOOL_NAME: {}}
        assert "execution_mode" not in data

    def test_foreign_conversation_auto_mode_rejected(
        self, monkeypatch, app, client, temp_db, users
    ):
        """用户 B 不能进入用户 A 的会话发起智能对话（在路由之前 404）。"""
        temp_db["owner_id"] = users["alice"]["id"]
        route_calls = []

        def fake_route(query, *, available_tools, history=None,
                       use_mcp=False, router=None):
            route_calls.append(query)
            return rule_fast_path(query)

        monkeypatch.setattr(intent_router_pkg, "route_auto_agent", fake_route)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "alice 的会话"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "bob"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "你好"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 404
        # 会话所有者校验先于路由与任何执行分支
        assert route_calls == []
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
