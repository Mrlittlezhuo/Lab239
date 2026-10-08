"""意图路由与自动 Agent 离线测试（任务 012）。

全部使用临时 SQLite 数据库（迁移到 v7）+ 确定性假工具 / 假规划器 / 假路由
provider，不连接真实模型、FastJev、外部服务、MCP 服务或用户数据库；用户
身份一律来自服务端认证上下文。

覆盖任务文档 §9 要求：
- 问候 / 闲聊 → 直接回答（普通聊天流程），不创建无意义 Agent Run；
- 知识库类问题 → Agent Run 且候选工具含 search_local_knowledgebase；
- 明确单工具请求 → 只把匹配候选工具交给规划器；
- 检索 / 比较 / 汇总 → 多步 Agent Run（含重规划）；
- 未知工具 / 非法结构 / provider 异常 / 超时 / 低置信度 → 受控回退普通
  对话，不阻断用户对话；
- 自动模式无需手动选择工具即可工作；普通对话模式不触发自动路由；
- 手动调试模式（工具多选 / Agent Run 开关）行为保持不变；
- 自动路由出的 Agent Run 沿用任务 011 的时间线 / 取消接口与所有者隔离。
"""
import sys
import time
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
# 在模块加载时读取真实知识库数据库，离线测试无法提供。本测试中的知识库工具
# 一律使用确定性假工具（mock 检索）。
# ---------------------------------------------------------------------------
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
    IntentProvider,
    IntentRouter,
    LlmIntentProvider,
    RouteContext,
    RouteError,
    RouteResult,
    rule_fast_path,
    route_auto_agent,
    validate_route_result,
)
from chatchat.server.intent_router.schemas import (
    COMPLEXITY_DIRECT,
    COMPLEXITY_MULTI,
    COMPLEXITY_SINGLE,
)


# ---------------------------------------------------------------------------
# 测试替身：假工具 / 假规划器 / 假路由 provider
# ---------------------------------------------------------------------------


class FakeTool:
    """确定性假工具：记录调用参数，返回预设文本或抛出异常。"""

    def __init__(self, name, description="", result="", raises=None):
        self.name = name
        self.description = description
        self.args = {}
        self.result = result
        self.raises = raises
        self.calls = []

    def invoke(self, args):
        self.calls.append(dict(args or {}))
        if self.raises is not None:
            if isinstance(self.raises, BaseException):
                raise self.raises
            raise self.raises()
        if callable(self.result):
            return self.result(dict(args or {}))
        return self.result


class ScriptedPlanner:
    """按脚本返回计划/判断的假规划器；脚本耗尽或脚本项为异常时抛出。"""

    def __init__(self, plan, decisions=()):
        self._plan = plan
        self._decisions = list(decisions)
        self.plan_calls = 0
        self.decide_calls = []

    def plan(self, ctx):
        self.plan_calls += 1
        if isinstance(self._plan, BaseException):
            raise self._plan
        return self._plan

    def decide(self, ctx, results):
        self.decide_calls.append(list(results))
        if not self._decisions:
            raise S.PlanError("脚本未提供判断结果")
        d = self._decisions.pop(0)
        if isinstance(d, BaseException):
            raise d
        return d


class CapturingPlanner(ScriptedPlanner):
    """额外记录规划上下文，用于断言规划器拿到的工具集合（候选工具收窄）。"""

    def __init__(self, plan, decisions=()):
        super().__init__(plan, decisions)
        self.last_ctx = None

    def plan(self, ctx):
        self.last_ctx = ctx
        return super().plan(ctx)


def _kb_result(args):
    return f"观察：{args.get('query', '')} 的相关内容"


class FakeLLM:
    """假 LLM：invoke 返回预设文本（或抛出异常），记录收到的消息。"""

    def __init__(self, raw="{}", raises=None):
        self.raw = raw
        self.raises = raises
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append([(m["role"], m["content"]) for m in messages])
        if self.raises is not None:
            if isinstance(self.raises, BaseException):
                raise self.raises
            raise self.raises()

        class _Resp:
            def __init__(self, content):
                self.content = content

        return _Resp(self.raw)


class FixedRouteProvider(IntentProvider):
    """返回预设 RouteResult（或抛出异常）的路由 provider 替身。"""

    name = "fixed"

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def route(self, ctx):
        self.calls.append(ctx)
        if self.raises is not None:
            if isinstance(self.raises, BaseException):
                raise self.raises
            raise self.raises()
        if self.result is None:
            raise RuntimeError("fixed provider 未提供结果")
        return self.result


class SlowProvider(IntentProvider):
    """超过超时阈值才返回的 provider，用于验证路由超时回退。"""

    name = "slow"

    def __init__(self, seconds):
        self.seconds = seconds

    def route(self, ctx):
        time.sleep(self.seconds)
        return RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["t"],
            confidence=0.9,
        )


class BoomRouter:
    """鸭子类型路由器：route() 恒定抛错，用于验证 route_auto_agent 外层回退。"""

    provider_name = "boom"

    def route(self, ctx):
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# fixtures（与 test_agent_run.py 保持一致：临时库 + httpx ASGI client）
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "intent-route-unit-test-secret-00000000",
    )
    return "intent-route-unit-test-secret-00000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：建全表、迁移到 v7，注册模型元数据。"""
    path = tmp_path / "intent_route_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    assert current_version(engine.connect()) == 7
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine, "owner_id": None}
    engine.dispose()


def _mk_user(db, username, role="user", must_change_password=False):
    """创建用户并返回其非敏感字段。"""
    S = db["Session"]
    s = S()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username.title(),
            password="password12345678",
            role=role,
        )
        u.must_change_password = must_change_password
        s.commit()
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(db, username):
    """生成当前登录用户的 Bearer token 请求头。"""
    S = db["Session"]
    s = S()
    try:
        u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


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


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


def _run_row(db, run_id):
    from chatchat.server.db.repository import agent_run_repository as agent_repo

    with _session(db) as s:
        run = agent_repo.get_owned_run(s, run_id, db["owner_id"])
        if run is None:
            return None
        return {"status": run.status, "final_result": run.final_result}


# ---------------------------------------------------------------------------
# 1. 确定性快速路径 rule_fast_path
# ---------------------------------------------------------------------------


class TestRuleFastPath:
    def test_greeting_chinese_direct(self):
        r = rule_fast_path("你好！")
        assert r is not None
        assert r.path == PATH_DIRECT
        assert r.intent == INTENT_GENERAL
        assert r.complexity == COMPLEXITY_DIRECT
        assert r.source == "rules"
        assert r.confidence == 1.0
        assert r.candidate_tools == []

    def test_greeting_english_direct(self):
        assert rule_fast_path("Hello").path == PATH_DIRECT
        assert rule_fast_path("hi").path == PATH_DIRECT

    def test_simple_reply_direct(self):
        assert rule_fast_path("谢谢").path == PATH_DIRECT
        assert rule_fast_path("好的").path == PATH_DIRECT

    def test_pure_symbols_direct(self):
        r = rule_fast_path("!!？？")
        assert r is not None
        assert r.path == PATH_DIRECT
        assert "纯符号" in r.summary

    def test_empty_returns_none(self):
        assert rule_fast_path("") is None
        assert rule_fast_path("   ") is None
        assert rule_fast_path(None) is None

    def test_real_question_returns_none(self):
        assert (
            rule_fast_path("请帮我查一下 Mem0 的记忆方案有哪些优缺点？") is None
        )

    def test_long_mixed_text_returns_none(self):
        q = "你好，请帮我详细分析 Mem0 与传统 RAG 的优缺点差异"
        assert rule_fast_path(q) is None

    def test_greeting_plus_other_words_returns_none(self):
        assert rule_fast_path("hi there") is None


# ---------------------------------------------------------------------------
# 2. 结构化路由结果校验 validate_route_result
# ---------------------------------------------------------------------------


class TestValidateRouteResult:
    def test_unknown_tools_dropped_confidence_clamped(self):
        r = validate_route_result(
            {
                "path": "agent_run",
                "intent": "kb_search",
                "candidate_tools": [
                    "search_local_knowledgebase",
                    "ghost",
                    "search_local_knowledgebase",
                ],
                "confidence": 2.5,
                "summary": "x" * 300,
            },
            {"search_local_knowledgebase": object()},
        )
        assert r.path == PATH_AGENT_RUN
        assert r.candidate_tools == ["search_local_knowledgebase"]
        assert r.confidence == 1.0
        assert len(r.summary) == 120

    def test_direct_clears_tools(self):
        r = validate_route_result(
            {"path": "direct", "intent": "general", "candidate_tools": ["a"]},
            {"a": 1},
        )
        assert r.path == PATH_DIRECT
        assert r.candidate_tools == []
        assert r.complexity == COMPLEXITY_DIRECT

    def test_invalid_path_raises(self):
        with pytest.raises(RouteError):
            validate_route_result(
                {"path": "teleport", "intent": "general"}, {"a": 1}
            )

    def test_agent_run_without_valid_tools_raises(self):
        with pytest.raises(RouteError):
            validate_route_result(
                {
                    "path": "agent_run",
                    "intent": "kb_search",
                    "candidate_tools": ["ghost"],
                },
                {"search_local_knowledgebase": 1},
            )

    def test_unknown_intent_falls_back_to_direct(self):
        r = validate_route_result(
            {"path": "agent_run", "intent": "weird", "candidate_tools": ["a"]},
            {"a": 1},
        )
        assert r.path == PATH_DIRECT
        assert r.intent == "unknown"
        assert r.candidate_tools == []

    def test_non_mapping_input_raises(self):
        with pytest.raises(RouteError):
            validate_route_result("not a mapping", {"a": 1})

    def test_accepts_route_result_instance(self):
        rr = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["a"],
            confidence=0.7,
        )
        r = validate_route_result(rr, {"a": 1})
        assert r.path == PATH_AGENT_RUN
        assert r.candidate_tools == ["a"]
        assert r.confidence == 0.7

    def test_agent_run_complexity_normalized(self):
        r = validate_route_result(
            {
                "path": "agent_run",
                "intent": "complex",
                "candidate_tools": ["a", "b"],
                "complexity": "weird",
            },
            {"a": 1, "b": 2},
        )
        assert r.complexity == COMPLEXITY_MULTI


# ---------------------------------------------------------------------------
# 3. LlmIntentProvider：现有 LLM 判断 + 解析/调用失败处理
# ---------------------------------------------------------------------------


class TestLlmIntentProvider:
    def test_valid_json_output(self):
        llm = FakeLLM(
            raw=json.dumps(
                {
                    "path": "agent_run",
                    "intent": "kb_search",
                    "complexity": "single",
                    "candidate_tools": ["search_local_knowledgebase"],
                    "confidence": 0.9,
                    "summary": "知识库检索",
                },
                ensure_ascii=False,
            )
        )
        p = LlmIntentProvider(llm)
        r = p.route(
            RouteContext(
                query="查一下知识库",
                available_tools={"search_local_knowledgebase": object()},
            )
        )
        assert r.path == PATH_AGENT_RUN
        assert r.intent == INTENT_KB_SEARCH
        assert r.candidate_tools == ["search_local_knowledgebase"]
        assert llm.calls[0][0][0] == "system"
        # system prompt 携带当前可用工具清单（来自实时注册表，不维护副本）
        assert "search_local_knowledgebase" in llm.calls[0][0][1]

    def test_json_in_code_fence_is_parsed(self):
        llm = FakeLLM(
            raw='```json\n{"path": "direct", "intent": "general"}\n```'
        )
        r = LlmIntentProvider(llm).route(
            RouteContext(query="你好呀", available_tools={})
        )
        assert r.path == PATH_DIRECT

    def test_invalid_json_raises_route_error(self):
        p = LlmIntentProvider(FakeLLM(raw="这不是 JSON"))
        with pytest.raises(RouteError):
            p.route(RouteContext(query="q", available_tools={"a": 1}))

    def test_llm_call_failure_raises_route_error(self):
        p = LlmIntentProvider(FakeLLM(raises=RuntimeError("模型不可用")))
        with pytest.raises(RouteError):
            p.route(RouteContext(query="q", available_tools={}))

    def test_system_prompt_notes_empty_toolset(self):
        llm = FakeLLM(raw='{"path": "direct", "intent": "general"}')
        p = LlmIntentProvider(llm)
        p.route(RouteContext(query="q", available_tools={}))
        assert "当前没有可用工具" in llm.calls[0][0][1]


# ---------------------------------------------------------------------------
# 4. IntentRouter：异常 / 超时 / 低置信度统一回退
# ---------------------------------------------------------------------------


class TestIntentRouterFallbacks:
    def test_fast_path_short_circuits_provider(self):
        p = FixedRouteProvider(raises=RuntimeError("不应被调用"))
        r = IntentRouter(p).route(RouteContext(query="你好"))
        assert r.path == PATH_DIRECT
        assert r.source == "rules"
        assert p.calls == []

    def test_no_provider_falls_back(self):
        r = IntentRouter(None).route(RouteContext(query="分析一下"))
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "未配置" in r.fallback_reason

    def test_provider_exception_falls_back(self):
        r = IntentRouter(
            FixedRouteProvider(raises=RuntimeError("boom"))
        ).route(RouteContext(query="分析一下"))
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "路由异常" in r.fallback_reason

    def test_provider_timeout_falls_back(self):
        r = IntentRouter(
            SlowProvider(3), timeout_seconds=1
        ).route(RouteContext(query="分析一下"))
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "超时" in r.fallback_reason

    def test_low_confidence_agent_run_downgraded(self):
        fixed = FixedRouteProvider(
            RouteResult(
                path=PATH_AGENT_RUN,
                intent=INTENT_KB_SEARCH,
                complexity=COMPLEXITY_SINGLE,
                candidate_tools=["a"],
                confidence=0.2,
            )
        )
        r = IntentRouter(fixed, min_confidence=0.5).route(
            RouteContext(query="查知识库", available_tools={"a": 1})
        )
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "置信度" in r.fallback_reason

    def test_high_confidence_agent_run_kept(self):
        fixed = FixedRouteProvider(
            RouteResult(
                path=PATH_AGENT_RUN,
                intent=INTENT_KB_SEARCH,
                complexity=COMPLEXITY_SINGLE,
                candidate_tools=["a"],
                confidence=0.9,
            )
        )
        r = IntentRouter(fixed, min_confidence=0.5).route(
            RouteContext(query="查知识库", available_tools={"a": 1})
        )
        assert r.path == PATH_AGENT_RUN
        assert r.candidate_tools == ["a"]
        assert r.provider == "fixed"

    def test_non_routeresult_from_provider_falls_back(self):
        class DictProvider(FixedRouteProvider):
            name = "dict"

            def route(self, ctx):
                return {"path": "direct"}

        r = IntentRouter(DictProvider()).route(
            RouteContext(query="q", available_tools={})
        )
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"

    def test_custom_provider_unknown_tool_falls_back(self):
        """所有 provider 都必须经过统一工具校验，含后续 FastJev。"""
        provider = FixedRouteProvider(
            RouteResult(
                path=PATH_AGENT_RUN,
                intent=INTENT_SINGLE_TOOL,
                complexity=COMPLEXITY_SINGLE,
                candidate_tools=["ghost_tool"],
                confidence=0.9,
            )
        )
        r = IntentRouter(provider).route(
            RouteContext(query="调用工具", available_tools={"real_tool": object()})
        )
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "没有有效候选工具" in r.fallback_reason


class TestRouteAutoAgentEntry:
    def test_fast_path_without_provider(self):
        r = route_auto_agent("你好", available_tools={}, router=IntentRouter(None))
        assert r.path == PATH_DIRECT
        assert r.source == "rules"

    def test_broken_router_outer_fallback(self):
        r = route_auto_agent(
            "复杂问题", available_tools={"a": 1}, router=BoomRouter()
        )
        assert r.path == PATH_DIRECT
        assert r.source == "fallback"
        assert "意外异常" in r.fallback_reason


# ---------------------------------------------------------------------------
# 5. /chat/chat/completions 自动 Agent 模式（extra.auto_agent）
# ---------------------------------------------------------------------------


class TestChatCompletionsAutoAgent:
    def _patch_auto(self, monkeypatch, temp_db, planner, route, extra_tools=()):
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

    def test_auto_greeting_direct_answer_no_agent_run(
        self, monkeypatch, app, client, temp_db, users
    ):
        """问候 → 直接回答：复用普通聊天、零工具，不创建无意义 Agent Run。"""
        temp_db["owner_id"] = users["alice"]["id"]
        route = rule_fast_path("你好！")
        assert route is not None
        self._patch_auto(
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
        rr = data["route_result"]
        assert rr["path"] == PATH_DIRECT
        assert rr["path_label"] == "直接回答"
        assert rr["intent_label"] == "普通对话"
        # 未创建任何 Agent 运行
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        # 直接回答不装配任何工具，身份来自服务端认证上下文
        assert called["kwargs"]["tool_config"] == {}
        assert called["kwargs"]["use_mcp"] is False
        assert called["kwargs"]["owner_id"] == users["alice"]["id"]

    def test_auto_kb_question_runs_agent_with_kb_tool_only(
        self, monkeypatch, app, client, temp_db, users
    ):
        """知识库问题 → Agent Run：无需手动选工具，只给候选工具，闭环完成。"""
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
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.95,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        patched = self._patch_auto(monkeypatch, temp_db, planner, route)

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
        assert data["conversation_id"] == conv
        rr = data["route_result"]
        assert rr["path"] == PATH_AGENT_RUN
        assert rr["path_label"] == "Agent 执行"
        assert rr["intent"] == INTENT_KB_SEARCH
        assert rr["candidate_tools"] == ["search_local_knowledgebase"]
        # 自动模式默认不启用 MCP
        assert patched["route_calls"][0]["use_mcp"] is False
        # 知识库经现有本地 KB 工具在 Agent Run 内完成
        kb = patched["tools"]["search_local_knowledgebase"]
        assert kb.calls == [{"database": "kb", "query": "Mem0"}]
        # 规划器只拿到候选工具（calculate / weather_check 不在其中）
        assert set(planner.last_ctx.tools) == {"search_local_knowledgebase"}
        # 运行持久化（owner 为 alice）且最终回答写回消息
        assert run["run_id"]
        assert _run_row(temp_db, run["run_id"])["status"] == "completed"
        with _session(temp_db) as s:
            msgs = (
                s.query(MessageModel)
                .filter_by(conversation_id=conv)
                .all()
            )
        assert msgs[0].response == "知识库最终回答"
        assert msgs[0].meta_data["agent_run_id"] == run["run_id"]

    def test_auto_single_tool_selects_matching_candidate_only(
        self, monkeypatch, app, client, temp_db, users
    ):
        """明确单工具请求 → 只把匹配的候选工具交给 Agent Run。"""
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
            summary="天气查询",
            provider="llm",
            source="llm",
        )
        patched = self._patch_auto(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "天气"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "上海今天天气怎么样？"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        run = data["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "上海今天晴天"
        assert data["route_result"]["intent"] == INTENT_SINGLE_TOOL
        weather = patched["tools"]["weather_check"]
        assert weather.calls == [{"city": "上海"}]
        # 知识库等无关工具未被调用，也不在规划器工具集合中
        assert patched["tools"]["search_local_knowledgebase"].calls == []
        assert set(planner.last_ctx.tools) == {"weather_check"}

    def test_auto_complex_request_runs_multi_step(
        self, monkeypatch, app, client, temp_db, users
    ):
        """检索 / 比较 / 汇总 → 多步 Agent Run（观察后重规划）。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = CapturingPlanner(
            S.Plan(
                summary="先检索，再计算对比指标",
                steps=[
                    S.PlanStep(
                        "检索 Mem0", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "计算对比指标", "calculate",
                            {"expression": "90 - 80"},
                        )
                    ],
                ),
                S.Decision(action="answer", answer="检索与计算汇总（最终）"),
            ],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_COMPLEX,
            complexity=COMPLEXITY_MULTI,
            candidate_tools=["search_local_knowledgebase", "calculate"],
            confidence=0.9,
            summary="检索并汇总",
            provider="llm",
            source="llm",
        )
        self._patch_auto(monkeypatch, temp_db, planner, route)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "汇总"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "检索知识库并汇总对比结果"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        run = data["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "检索与计算汇总（最终）"
        assert [
            st["tool_name"] for st in run["steps"] if st["type"] == "tool_call"
        ] == ["search_local_knowledgebase", "calculate"]
        assert data["route_result"]["intent"] == INTENT_COMPLEX
        # 规划器拿到的是候选工具集合（含重规划所需工具）
        assert set(planner.last_ctx.tools) == {
            "search_local_knowledgebase", "calculate"
        }

    def test_auto_route_fallback_returns_to_normal_chat(
        self, monkeypatch, app, client, temp_db, users
    ):
        """路由结果已回退（超时 / 异常 / 低置信度）→ 普通聊天，不阻断对话。"""
        temp_db["owner_id"] = users["alice"]["id"]
        fallback = RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            confidence=None,
            summary="自动判断未通过，已按普通对话回答",
            provider="llm",
            source="fallback",
            fallback_reason="路由超时（超过 20 秒）",
        )
        self._patch_auto(
            monkeypatch, temp_db,
            ScriptedPlanner(S.Plan(direct_answer="不应执行")), fallback,
        )
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "回退后的普通回答"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "回退"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "帮我查一下知识库里的内容"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["content"] == "回退后的普通回答"
        rr = data["route_result"]
        assert rr["path"] == PATH_DIRECT
        assert rr["source"] == "fallback"
        assert rr["fallback_reason"] == "路由超时（超过 20 秒）"
        # 未创建 Agent 运行；回退路径零工具
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        assert called["kwargs"]["tool_config"] == {}

    def test_normal_chat_mode_never_auto_routes(self, monkeypatch, app,
                                                client, temp_db, users):
        """普通对话模式：不做自动路由、不自动调用工具，原路径行为不变。"""
        temp_db["owner_id"] = users["alice"]["id"]
        patched = self._patch_auto(
            monkeypatch, temp_db,
            ScriptedPlanner(S.Plan(direct_answer="不应执行")),
            RouteResult(provider="unused"),
        )
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "普通回答"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "普通会话"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "普通问题"}],
                "stream": False,
                "conversation_id": conv,
                "tools": ["search_local_knowledgebase"],
            },
        )
        assert r.status_code == 200
        assert r.json()["content"] == "普通回答"
        # 未触发任何自动路由
        assert patched["route_calls"] == []
        # 未创建 Agent 运行；手动 tools 仍按原路径透传
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []
        assert called["kwargs"]["tool_config"] == {
            "search_local_knowledgebase": {}
        }

    def test_manual_debug_mode_keeps_manual_agent_run(
        self, monkeypatch, app, client, temp_db, users
    ):
        """手动调试模式（agent_run + 手动工具）优先于自动路由，行为不变。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索", "search_local_knowledgebase",
                        {"database": "kb", "query": "x"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="手动调试回答")],
        )
        patched = self._patch_auto(
            monkeypatch, temp_db, planner, RouteResult(provider="unused")
        )
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "手动调试"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "手动调试"}],
                "stream": False,
                "conversation_id": conv,
                "tools": ["search_local_knowledgebase"],
                "agent_run": True,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["agent_run"]["status"] == "completed"
        assert data["agent_run"]["final_result"] == "手动调试回答"
        # 手动路径优先：自动路由未被调用，无 route_result 扩展字段
        assert patched["route_calls"] == []
        assert "route_result" not in data

    def test_auto_agent_rejects_foreign_conversation(
        self, monkeypatch, app, client, temp_db, users
    ):
        """他人会话 + auto_agent：模型调用前 404，不路由、不创建运行。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        patched = self._patch_auto(
            monkeypatch, temp_db, planner, RouteResult()
        )
        with _session(temp_db) as s:
            conv_bob = create_conversation(
                s, users["bob"]["id"], "agent_chat", "bob 会话"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "越权请求"}],
                "stream": False,
                "conversation_id": conv_bob,
                "auto_agent": True,
            },
        )
        assert r.status_code == 404
        assert patched["route_calls"] == []
        assert planner.plan_calls == 0
        with _session(temp_db) as s:
            assert s.query(AgentRunModel).all() == []

    def test_auto_agent_run_owner_isolation_and_cancel_reuse(
        self, monkeypatch, app, client, temp_db, users
    ):
        """自动路由出的 Agent Run 沿用任务 011 时间线/取消接口与所有者隔离。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="自动 Agent 回答")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.9,
            provider="llm",
            source="llm",
        )
        self._patch_auto(monkeypatch, temp_db, planner, route)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "自动 Agent"
            )
        h_alice = _headers(temp_db, "alice")
        h_bob = _headers(temp_db, "bob")

        r = client.post(
            "/chat/chat/completions",
            headers=h_alice,
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "查一下知识库"}],
                "stream": False,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        run_id = r.json()["agent_run"]["run_id"]
        assert run_id

        # bob 无法查看/取消 alice 的运行（404，不区分不存在/非本人）
        assert client.get(f"/agent-runs/{run_id}", headers=h_bob).status_code == 404
        assert client.post(f"/agent-runs/{run_id}/cancel",
                          headers=h_bob).status_code == 404
        r = client.get("/agent-runs", headers=h_bob)
        assert r.json() == {"runs": []}

        # alice 可查看详情并取消
        r = client.get(f"/agent-runs/{run_id}", headers=h_alice)
        assert r.status_code == 200
        assert r.json()["run"]["run_id"] == run_id
        assert len(r.json()["steps"]) == 2  # 1 工具 + 1 答案
        r = client.post(f"/agent-runs/{run_id}/cancel", headers=h_alice)
        assert r.status_code == 200
        # 已完成的运行处于终态：取消接口可用但保持原状（与任务 011 一致）
        assert r.json()["run"]["status"] == "completed"

        # 未登录 401
        assert client.get("/agent-runs").status_code == 401

    def test_auto_agent_stream_emits_route_result_first(
        self, monkeypatch, app, client, temp_db, users
    ):
        """流式自动 Agent：SSE 首条为 RouteResult，随后沿用 AgentRunEvent 事件。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="流式自动回答")],
        )
        route = RouteResult(
            path=PATH_AGENT_RUN,
            intent=INTENT_KB_SEARCH,
            complexity=COMPLEXITY_SINGLE,
            candidate_tools=["search_local_knowledgebase"],
            confidence=0.9,
            summary="知识库检索",
            provider="llm",
            source="llm",
        )
        self._patch_auto(monkeypatch, temp_db, planner, route)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "流式自动"
            )
        r = client.post(
            "/chat/chat/completions",
            headers=_headers(temp_db, "alice"),
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "查一下知识库"}],
                "stream": True,
                "conversation_id": conv,
                "auto_agent": True,
            },
        )
        assert r.status_code == 200
        chunks = []
        for line in r.text.splitlines():
            if line.startswith("data: "):
                chunks.append(json.loads(line[6:]))
        assert chunks, "SSE 流为空"
        first = chunks[0]
        assert first["class_name"] == "RouteResult"
        assert first["route_result"]["path"] == PATH_AGENT_RUN
        assert first["route_result"]["intent_label"] == "知识库检索"
        assert first["conversation_id"] == conv
        rest = chunks[1:]
        assert rest, "缺少 Agent 运行事件"
        assert all(c.get("class_name") == "AgentRunEvent" for c in rest)
        events = [c.get("agent_event") for c in rest]
        assert "run_completed" in events


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-o", "addopts=", "-p", "no:cacheprovider"])
