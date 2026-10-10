"""LangGraph 运行时离线测试（任务 014 §11/§12 验收点）。

全部使用临时 SQLite 数据库（迁移到 v7）+ 临时持久化 checkpoint 文件
（``AsyncSqliteSaver``）+ 确定性假规划器/假工具，不连接真实模型、外部
服务、MCP 服务或用户数据库；用户身份一律来自服务端认证上下文。

覆盖任务文档 §11/§12：
- 生产图为真实多节点状态图（prepare_plan / gate / step_start /
  step_run / decide + 条件边），不是把旧循环包成单节点的"形式迁移"；
- 运行时配置默认 langgraph、未知值回退 legacy（回退开关保持可用）；
- 闭环：直接回答 / 多步计划顺序执行 / 重规划 / 异步工具 / 工具失败
  重规划 / 明确失败 / 非法计划 / 非法判断，均为可控终态；
- 有界与收敛：最大步数、总超时、取消（pending 与 running）、请求中断，
  均不留永久 ``running``；
- 断点恢复（持久 checkpoint）：边界中断后已完成工具步骤绝不重跑；
  步骤停留在 running 时可控失败；终态运行重复恢复幂等；无 checkpoint
  的旧记录给出明确状态；并发恢复串行化且不重复执行；
- 持久化与 SSE：Run/Step 仍是用户可见轨迹来源，新运行 checkpoint 的
  ``thread_id`` 即稳定 ``run_id``；服务层写回源消息；流式输出
  plan/step/终态事件；
- 权限：恢复的跨用户拒绝（404）、未登录 401、必须改密 403；运行时为
  legacy 时恢复返回 409 明确状态；
- 同一脚本在两种运行时下端到端结果一致（离线对比）。
"""
import sys
import types
import asyncio
import json
import time
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
from chatchat.server.db.repository.message_repository import add_message
from chatchat.server.db.repository import agent_run_repository as agent_repo
from chatchat.server.agent_run import schema as S
import chatchat.server.agent_run.service as agent_service
from chatchat.server.agent_run.runtime import (
    LegacyRuntime,
    LangGraphRuntime,
    create_agent_runtime,
    runtime_name,
)
from chatchat.server.agent_run.runtime import checkpoint as ckpt_mod
from chatchat.server.agent_run.runtime.graph import build_agent_graph
from chatchat.server.api_server.agent_run_routes import agent_run_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.settings import Settings

# 运行时层直接建运行使用的测试属主（运行时只要求非空，不做用户表校验）
OWNER = "lg-owner"


# ---------------------------------------------------------------------------
# 测试替身：确定性假工具 / 假规划器
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


class AsyncOnlyTool:
    """只提供异步调用的假工具，模拟 MCP 等异步工具。"""

    def __init__(self, name, result):
        self.name = name
        self.description = "async-only tool"
        self.args = {}
        self.result = result
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(dict(args or {}))
        return self.result


class SlowTool(FakeTool):
    """耗时（同步 sleep）的假工具，用于触发总超时门禁。"""

    def __init__(self, name, sleep_seconds=1.2):
        super().__init__(name, "慢速工具（mock）")
        self.sleep_seconds = sleep_seconds

    def invoke(self, args):
        self.calls.append(dict(args or {}))
        time.sleep(self.sleep_seconds)
        return "慢步骤完成"


class HangTool:
    """永不返回的异步工具：模拟运行中断于工具执行中途（进程死亡）。"""

    def __init__(self, name="search_local_knowledgebase"):
        self.name = name
        self.description = "hang tool"
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(dict(args or {}))
        await asyncio.Event().wait()


class ScriptedPlanner:
    """按脚本返回计划/判断的假规划器；脚本耗尽或脚本项为异常时抛出。"""

    def __init__(self, plan, decisions=()):
        self._plan = plan
        self._decisions = list(decisions)
        self.plan_calls = 0
        self.decide_calls = []  # 每次 decide 收到的 StepResult 列表

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


def _kb_result(args):
    return f"观察：{args.get('query', '')} 的相关内容"


def _types(events):
    return [ev["type"] for ev in events]


async def _collect(gen):
    return [ev async for ev in gen]


async def _consume_until(gen, pred, limit=100):
    """消费事件流直到 pred(event) 成立；生成器保持挂起（模拟中断点）。"""
    events = []
    for _ in range(limit):
        try:
            ev = await gen.__anext__()
        except StopAsyncIteration:
            raise AssertionError(f"事件流提前结束: {events}")
        events.append(ev)
        if pred(ev):
            return events
    raise AssertionError(f"超过事件上限仍未满足条件: {events}")


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


def _run_row(db, run_id):
    with _session(db) as s:
        run = agent_repo.get_owned_run(s, run_id, db["owner_id"])
        if run is None:
            return None
        return {
            "status": run.status,
            "final_result": run.final_result,
            "error": run.error,
            "plan_text": run.plan_text,
            "current_step": run.current_step,
            "max_steps": run.max_steps,
        }


def _step_rows(db, run_id):
    with _session(db) as s:
        steps = agent_repo.get_owned_steps(s, run_id, db["owner_id"])
        return [
            {
                "seq": st.seq,
                "type": st.type,
                "status": st.status,
                "tool_name": st.tool_name,
                "tool_input": st.tool_input,
                "output_summary": st.output_summary,
                "error_summary": st.error_summary,
                "start_time": st.start_time,
                "end_time": st.end_time,
            }
            for st in steps
        ]


def _make_rt(db, store, planner, tools, goal, **kw):
    """经统一工厂构造运行时（默认 langgraph；可用 forced/existing_run_id 覆盖）。"""
    return create_agent_runtime(
        session_factory=lambda: db["Session"](),
        owner_id=db["owner_id"],
        planner=planner,
        tools=tools,
        goal=goal,
        conversation_id=kw.get("conversation_id"),
        source_message_id=kw.get("source_message_id"),
        max_steps=kw.get("max_steps", 8),
        timeout_seconds=kw.get("timeout_seconds", 30),
        model_name="fake-llm",
        checkpointer=store,
        existing_run_id=kw.get("existing_run_id"),
        forced=kw.get("forced", "langgraph"),
    )


def _patch_service(monkeypatch, temp_db, planner):
    """把服务层接到临时库与假规划器/假工具（不触真实模型、记忆、网络）。"""
    kb = FakeTool(
        "search_local_knowledgebase", "本地知识库检索（mock）", result=_kb_result
    )
    registry = {"search_local_knowledgebase": kb}
    monkeypatch.setattr(
        agent_service, "get_tool",
        lambda name=None: registry.get(name) if name else registry,
    )
    monkeypatch.setattr(
        agent_service, "_build_planner", lambda model_name=None: planner
    )
    monkeypatch.setattr(agent_service, "_session_factory", lambda: temp_db["Session"]())
    monkeypatch.setattr(agent_service, "_build_history", lambda *a, **k: [])
    monkeypatch.setattr(agent_service, "_build_memory_messages", lambda *a, **k: [])
    monkeypatch.setattr(
        agent_service.memory_service, "memory_system_messages", lambda *a, **k: []
    )
    return kb


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "agent-rt-lg-unit-test-secret-0000000000",
    )
    return "agent-rt-lg-unit-test-secret-0000000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：建全表、迁移到 v7，注册模型元数据。"""
    path = tmp_path / "agent_rt_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    assert current_version(engine.connect()) == 12
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine, "owner_id": None}
    engine.dispose()


@pytest.fixture
async def ckpt(tmp_path):
    """每测试一个临时持久化 checkpoint 存储（同一文件可再次初始化，模拟进程重启）。"""
    store = await ckpt_mod.initialize_agent_checkpoint_store(
        str(tmp_path / "agent_ckpt.sqlite")
    )
    yield store
    await ckpt_mod.shutdown_agent_checkpoint_store()


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
    """FastAPI 应用：认证 + Agent 运行路由，get_db 指向临时库。"""
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(agent_run_router)

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
        async def request(self, method, url, **kwargs):
            # 请求必须运行在当前事件循环内（pytest-asyncio 的循环上），
            # 不能在循环内再 asyncio.run 嵌套新循环
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as async_client:
                return await async_client.request(method, url, **kwargs)

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

        async def post(self, url, **kwargs):
            return await self.request("POST", url, **kwargs)

    return Client()


# ---------------------------------------------------------------------------
# 1. 图结构与运行时选择（014 §4/§6）
# ---------------------------------------------------------------------------


class TestLGGraphAndConfig:
    def test_graph_is_real_multinode_state_graph(self, ckpt, temp_db):
        """生产图是五个真实节点 + 条件边的状态图，不是单节点封装。"""
        rt = LangGraphRuntime(
            session_factory=lambda: temp_db["Session"](),
            owner_id=OWNER,
            planner=object(),
            tools={},
            goal="g",
            checkpointer=ckpt,
        )
        graph = build_agent_graph(rt, ckpt)
        g = graph.get_graph()
        assert {
            "prepare_plan", "gate", "step_start", "step_run", "decide",
        } <= set(g.nodes)
        # 入口边 + 五条条件边（含 END 分支）
        assert len(g.edges) >= 6

    def test_global_checkpoint_store_reopens_on_new_event_loop(
        self, monkeypatch, tmp_path
    ):
        """热重载/测试换用事件循环时，不复用旧循环的异步锁。"""
        path = tmp_path / "loop_reopen.sqlite"
        monkeypatch.setattr(ckpt_mod, "default_checkpoint_path", lambda: path)

        first = asyncio.run(ckpt_mod.initialize_agent_checkpoint_store())

        async def reopen():
            assert ckpt_mod.get_agent_checkpoint_store() is None
            return await ckpt_mod.initialize_agent_checkpoint_store()

        second = asyncio.run(reopen())
        assert second is not first
        asyncio.run(ckpt_mod.shutdown_agent_checkpoint_store())

    def test_runtime_config_default_and_unknown_fallback(self, monkeypatch):
        """默认 langgraph；未知值回退 legacy（回退开关保持可用）。"""
        monkeypatch.setattr(Settings.model_settings, "AGENT_RUNTIME", "langgraph")
        assert runtime_name() == "langgraph"
        rt = create_agent_runtime(
            lambda: None, "o", object(), {}, "goal",
            conversation_id=None, source_message_id=None, forced=None,
        )
        assert isinstance(rt, LangGraphRuntime)

        monkeypatch.setattr(Settings.model_settings, "AGENT_RUNTIME", "unknown-rt")
        assert runtime_name() == "legacy"
        rt = create_agent_runtime(lambda: None, "o", object(), {}, "goal")
        assert isinstance(rt, LegacyRuntime)

        # 显式 forced 到未知值同样按 legacy 回退
        rt = create_agent_runtime(
            lambda: None, "o", object(), {}, "goal", forced="unknown-rt"
        )
        assert isinstance(rt, LegacyRuntime)

    def test_runtime_input_guards(self):
        """owner_id 必填（服务端认证上下文）；legacy 回退不支持 existing_run_id。"""
        with pytest.raises(ValueError):
            create_agent_runtime(
                lambda: None, "", object(), {}, "goal", forced="langgraph"
            )
        with pytest.raises(ValueError):
            create_agent_runtime(
                lambda: None, "o", object(), {}, "goal",
                forced="legacy", existing_run_id="r1",
            )

    async def test_empty_goal_rejected(self):
        """空白 goal 被拒绝（不进入图）。"""
        rt = create_agent_runtime(
            lambda: None, "o", object(), {}, "   ", forced="langgraph"
        )
        with pytest.raises(ValueError):
            async for _ in rt.run():
                pass

# ---------------------------------------------------------------------------
# 2. 执行闭环（运行时层，假规划器 + 假工具）
# ---------------------------------------------------------------------------


class TestLGClosedLoop:
    async def test_direct_answer_no_tool_steps(self, ckpt, temp_db):
        """无需工具：直接回答，一步 answer 记录，不产生无意义工具步骤。"""
        temp_db["owner_id"] = OWNER
        planner = ScriptedPlanner(
            S.Plan(summary="无需工具", direct_answer="答案是 42")
        )
        rt = _make_rt(temp_db, ckpt, planner, {}, "1+1 等于几？")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready", "run_completed",
        ]
        assert planner.plan_calls == 1
        assert rt.status == "completed"
        assert rt.final_result == "答案是 42"

        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "completed"
        assert row["final_result"] == "答案是 42"
        assert row["plan_text"]
        steps = _step_rows(temp_db, rt.run_id)
        assert [s["type"] for s in steps] == ["answer"]
        assert steps[0]["status"] == "completed"
        assert steps[0]["output_summary"] == "答案是 42"

    async def test_multi_step_plan_executes_in_order(self, ckpt, temp_db):
        """初始多步计划全部顺序执行后才进入完成判断。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="分别检索两个主题后比较",
                steps=[
                    S.PlanStep("检索 Mem0", "search_local_knowledgebase", {"query": "Mem0"}),
                    S.PlanStep("检索传统 RAG", "search_local_knowledgebase", {"query": "传统 RAG"}),
                ],
            ),
            decisions=[S.Decision(action="answer", answer="两者比较结论")],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "比较 Mem0 和传统 RAG")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished",
            "step_started", "step_finished",
            "run_completed",
        ]
        assert [c["query"] for c in kb.calls] == ["Mem0", "传统 RAG"]
        assert len(planner.decide_calls) == 1
        assert [r.output for r in planner.decide_calls[0]] == [
            _kb_result({"query": "Mem0"}), _kb_result({"query": "传统 RAG"}),
        ]
        assert rt.final_result == "两者比较结论"

        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "completed"
        assert row["final_result"] == "两者比较结论"
        steps = _step_rows(temp_db, rt.run_id)
        assert [s["seq"] for s in steps] == [1, 2, 3]
        assert [s["type"] for s in steps] == ["tool_call", "tool_call", "answer"]
        assert all(s["status"] == "completed" for s in steps)
        assert steps[0]["output_summary"] == _kb_result({"query": "Mem0"})

    async def test_replan_chain_adds_steps(self, ckpt, temp_db):
        """重规划补充的步骤会追加执行并继续判断，直到完成。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后按判断补充",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"})]),
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 C", "search_local_knowledgebase", {"query": "C"})]),
                S.Decision(action="answer", answer="三步之后形成结论"),
            ],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "连续重规划")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished", "replanned",
            "step_started", "step_finished", "replanned",
            "step_started", "step_finished",
            "run_completed",
        ]
        assert [c["query"] for c in kb.calls] == ["A", "B", "C"]
        assert len(planner.decide_calls) == 3
        assert rt.final_result == "三步之后形成结论"

        steps = _step_rows(temp_db, rt.run_id)
        assert [s["seq"] for s in steps] == [1, 2, 3, 4]
        assert [s["type"] for s in steps] == ["tool_call", "tool_call", "tool_call", "answer"]
        assert all(s["status"] == "completed" for s in steps)

    async def test_async_tool_registered_and_invoked(self, ckpt, temp_db):
        """只提供 ainvoke 的工具（MCP 风格）可被执行并落观察。"""
        temp_db["owner_id"] = OWNER
        tool = AsyncOnlyTool("async_search", "异步检索结果")
        planner = ScriptedPlanner(
            S.Plan(
                summary="异步检索",
                steps=[S.PlanStep("执行异步检索", "async_search", {"q": "Mem0"})],
            ),
            decisions=[S.Decision(action="answer", answer="已完成异步检索")],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"async_search": tool}, "使用异步工具检索")
        events = await _collect(rt.run())
        assert events[-1]["type"] == "run_completed"
        assert tool.calls == [{"q": "Mem0"}]
        steps = _step_rows(temp_db, rt.run_id)
        assert steps[0]["output_summary"] == "异步检索结果"
        assert steps[0]["status"] == "completed"

    async def test_tool_failure_replans_then_succeeds(self, ckpt, temp_db):
        """工具报错后重规划改用其他工具，失败步骤留痕，运行仍可完成。"""
        temp_db["owner_id"] = OWNER
        bad = FakeTool("search_local_knowledgebase", raises=RuntimeError("向量库连接失败"))
        good = FakeTool("search_web", "网络检索（mock）", result=_kb_result)
        planner = ScriptedPlanner(
            S.Plan(
                summary="先查本地知识库",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("改用网络检索", "search_web", {"query": "A2"})]),
                S.Decision(action="answer", answer="重试后得到答案"),
            ],
        )
        tools = {"search_local_knowledgebase": bad, "search_web": good}
        rt = _make_rt(temp_db, ckpt, planner, tools, "容错检索")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished", "replanned",
            "step_started", "step_finished",
            "run_completed",
        ]
        assert bad.calls == [{"query": "A"}]
        assert good.calls == [{"query": "A2"}]

        steps = _step_rows(temp_db, rt.run_id)
        assert steps[0]["status"] == "failed"
        assert "向量库连接失败" in (steps[0]["error_summary"] or "")
        assert steps[1]["status"] == "completed"
        assert steps[1]["output_summary"] == _kb_result({"query": "A2"})
        assert steps[2]["type"] == "answer"
        assert _run_row(temp_db, rt.run_id)["status"] == "completed"

    async def test_tool_failure_then_explicit_fail(self, ckpt, temp_db):
        """工具持续失败且判断为 fail：可控失败，错误原因保留。"""
        temp_db["owner_id"] = OWNER
        bad = FakeTool("search_local_knowledgebase", raises=RuntimeError("向量库连接失败"))
        planner = ScriptedPlanner(
            S.Plan(
                summary="先查本地知识库",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("再查一次", "search_local_knowledgebase", {"query": "B"})]),
                S.Decision(action="fail", reason="工具持续不可用"),
            ],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": bad}, "必然失败")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished",
            "replanned",
            "step_started", "step_finished",
            "run_failed",
        ]
        assert events[-1]["status"] == "failed"
        assert "工具持续不可用" in (events[-1]["error"] or "")
        assert bad.calls == [{"query": "A"}, {"query": "B"}]
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        assert "工具持续不可用" in (row["error"] or "")
        steps = _step_rows(temp_db, rt.run_id)
        assert steps[0]["status"] == "failed"
        assert steps[1]["status"] == "failed"

    async def test_missing_tool_recorded_and_fails(self, ckpt, temp_db):
        """计划引用了注册表中不存在的工具：步骤留痕并可控失败。"""
        temp_db["owner_id"] = OWNER
        planner = ScriptedPlanner(
            S.Plan(
                summary="使用缺失工具",
                steps=[S.PlanStep("检索 A", "ghost_tool", {"query": "A"})],
            ),
            decisions=[S.Decision(action="fail", reason="缺少必要工具")],
        )
        rt = _make_rt(temp_db, ckpt, planner, {}, "缺失工具")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished",
            "run_failed",
        ]
        steps = _step_rows(temp_db, rt.run_id)
        assert steps[0]["tool_name"] == "ghost_tool"
        assert steps[0]["status"] == "failed"
        assert "ghost_tool" in (steps[0]["error_summary"] or "")
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        assert "缺少必要工具" in (row["error"] or "")

    async def test_invalid_plan_fails_without_steps(self, ckpt, temp_db):
        """规划输出非法：可控失败，不产生任何步骤。"""
        temp_db["owner_id"] = OWNER
        planner = ScriptedPlanner(S.PlanError("非法计划输出"))
        rt = _make_rt(temp_db, ckpt, planner, {}, "无法规划")
        events = await _collect(rt.run())
        assert _types(events) == ["run_created", "run_created", "run_failed"]
        assert "计划生成失败" in (events[-1]["error"] or "")
        assert planner.plan_calls == 1
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        assert _step_rows(temp_db, rt.run_id) == []

    async def test_invalid_decision_fails(self, ckpt, temp_db):
        """执行判断输出非法：已完成步骤保留，运行可控失败。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后判断",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[S.PlanError("非法判断输出")],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "判断失败")
        events = await _collect(rt.run())
        assert _types(events) == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished",
            "run_failed",
        ]
        assert "执行判断失败" in (events[-1]["error"] or "")
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        steps = _step_rows(temp_db, rt.run_id)
        assert steps[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# 3. 有界循环与收敛（最大步数 / 总超时 / 取消 / 请求中断）
# ---------------------------------------------------------------------------


class TestLGBoundedAndConvergence:
    async def test_max_steps_bounds_loop(self, ckpt, temp_db):
        """重规划无限补步时，最大步数门禁强制收敛为 failed。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="持续重规划",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"})]),
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 C", "search_local_knowledgebase", {"query": "C"})]),
            ],
        )
        rt = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": kb},
            "步数上限", max_steps=2,
        )
        events = await _collect(rt.run())
        assert "replanned" in _types(events)
        assert events[-1]["type"] == "run_failed"
        assert "最大步数" in (events[-1]["error"] or "")
        assert kb.calls == [{"query": "A"}, {"query": "B"}]
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        assert "最大步数" in (row["error"] or "")
        assert row["max_steps"] == 2
        steps = _step_rows(temp_db, rt.run_id)
        assert [s["seq"] for s in steps] == [1, 2]
        assert all(s["status"] == "completed" for s in steps)

    async def test_total_timeout_bounds_run(self, ckpt, temp_db):
        """超过总超时后，门禁停止执行且不再调用工具。"""
        temp_db["owner_id"] = OWNER
        slow = SlowTool("search_local_knowledgebase", sleep_seconds=1.2)
        planner = ScriptedPlanner(
            S.Plan(
                summary="慢检索",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"})]),
                S.Decision(action="answer", answer="不应到达"),
            ],
        )
        rt = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": slow},
            "超时上限", timeout_seconds=1,
        )
        events = await _collect(rt.run())
        assert events[-1]["type"] == "run_failed"
        assert "超时" in (events[-1]["error"] or "")
        assert slow.calls == [{"query": "A"}]
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "failed"
        assert "超时" in (row["error"] or "")

    async def test_cancel_pending_before_planning(self, ckpt, temp_db):
        """pending 阶段取消：不进入规划，直接终态 cancelled。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool("search_local_knowledgebase", result=_kb_result)
        planner = ScriptedPlanner(
            S.Plan(
                summary="不应被规划",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "规划前取消")

        async def scenario():
            gen = rt.run()
            first = await gen.__anext__()
            with _session(temp_db) as s:
                agent_repo.cancel_run(s, rt.run_id, temp_db["owner_id"])
            rest = await _collect(gen)
            return first, rest

        first, rest = await scenario()
        assert first["type"] == "run_created" and first["status"] == "pending"
        assert _types(rest) == ["run_cancelled"]
        assert planner.plan_calls == 0
        assert kb.calls == []
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "cancelled"
        assert _step_rows(temp_db, rt.run_id) == []

    async def test_cancel_during_run_prevents_new_steps(self, ckpt, temp_db):
        """运行中取消：不再开始新步骤，收敛为 cancelled。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="两步",
                steps=[S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"})]),
            ],
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "运行中取消")
        gen = rt.run()
        await _consume_until(gen, lambda e: e["type"] == "step_finished")
        with _session(temp_db) as s:
            agent_repo.cancel_run(s, rt.run_id, temp_db["owner_id"])
        rest = await _collect(gen)
        assert _types(rest) == ["replanned", "run_cancelled"]
        assert kb.calls == [{"query": "A"}]
        row = _run_row(temp_db, rt.run_id)
        assert row["status"] == "cancelled"
        steps = _step_rows(temp_db, rt.run_id)
        assert [s["seq"] for s in steps] == [1]
        assert steps[0]["status"] == "completed"

    async def test_request_abort_converges_to_terminal(self, ckpt, temp_db):
        """消费者关闭（请求中断）时尽力收敛终态，不留永久 running。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool("search_local_knowledgebase", result=_kb_result)
        planner = ScriptedPlanner(
            S.Plan(
                summary="两步",
                steps=[
                    S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"}),
                    S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"}),
                ],
            ),
        )
        rt = _make_rt(temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "请求中断")
        gen = rt.run()
        await _consume_until(gen, lambda e: e["type"] == "step_started")
        await gen.aclose()

        row = _run_row(temp_db, rt.run_id)
        assert row is not None
        assert row["status"] in agent_repo.RUN_TERMINAL_STATUSES
        with _session(temp_db) as s:
            active = [
                r.id for r in s.query(AgentRunModel)
                .filter(AgentRunModel.owner_id == temp_db["owner_id"])
                .all()
                if r.status in agent_repo.RUN_ACTIVE_STATUSES
            ]
        assert active == []


# ---------------------------------------------------------------------------
# 4. 断点恢复（持久 checkpoint）
# ---------------------------------------------------------------------------


async def _boundary_crash_setup(ckpt, temp_db):
    """运行到第 1 步完成后的边界 checkpoint 后"进程死亡"。

    事件流故意不关闭（不触发收敛），返回构造恢复运行时所需的全部对象。
    """
    kb = FakeTool(
        "search_local_knowledgebase",
        "本地知识库检索（mock）",
        result=_kb_result,
    )
    planner = ScriptedPlanner(
        S.Plan(
            summary="检索后回答",
            steps=[
                S.PlanStep(
                    "检索 Mem0 相关内容",
                    "search_local_knowledgebase", {"query": "Mem0"},
                )
            ],
        ),
        decisions=[
            S.Decision(action="replan", steps=[
                S.PlanStep(
                    "检索传统 RAG 相关内容",
                    "search_local_knowledgebase", {"query": "传统 RAG"},
                )
            ]),
            S.Decision(action="answer", answer="恢复后最终回答"),
        ],
    )
    rt1 = _make_rt(
        temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "边界中断任务"
    )
    gen = rt1.run()
    await _consume_until(gen, lambda e: e["type"] == "step_finished")
    return rt1, gen, planner, kb


class TestLGResume:
    async def test_resume_after_boundary_crash_no_tool_reexecution(
        self, ckpt, temp_db
    ):
        """边界中断后恢复：已完成工具步骤绝不重跑，从断点继续到完成。"""
        temp_db["owner_id"] = OWNER
        rt1, gen, planner, kb = await _boundary_crash_setup(ckpt, temp_db)
        run_id = rt1.run_id

        # 中断现场：运行仍为 running（收敛未发生）
        assert _run_row(temp_db, run_id)["status"] == "running"
        assert [c["query"] for c in kb.calls] == ["Mem0"]

        rt2 = _make_rt(
            temp_db, ckpt, planner,
            {"search_local_knowledgebase": kb}, "",
            existing_run_id=run_id,
        )
        events = await _collect(rt2.run())
        assert _types(events) == [
            "run_resumed", "replanned",
            "step_started", "step_finished",
            "run_completed",
        ]
        assert rt2.status == "completed"
        assert rt2.final_result == "恢复后最终回答"

        # 第 1 步工具未重跑；第 2 步恰好执行一次
        assert [c["query"] for c in kb.calls] == ["Mem0", "传统 RAG"]
        assert planner.plan_calls == 1  # 恢复不重新规划
        row = _run_row(temp_db, run_id)
        assert row["status"] == "completed"
        assert row["final_result"] == "恢复后最终回答"
        steps = _step_rows(temp_db, run_id)
        assert [s["seq"] for s in steps] == [1, 2, 3]
        assert [s["type"] for s in steps] == ["tool_call", "tool_call", "answer"]
        assert all(s["status"] == "completed" for s in steps)

    async def test_resume_of_completed_run_is_idempotent(self, ckpt, temp_db):
        """已 completed 的运行重复恢复：幂等空操作，只回报终态。"""
        temp_db["owner_id"] = OWNER
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})
                ],
            ),
            decisions=[S.Decision(action="answer", answer="已完成")],
        )
        rt1 = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "幂等恢复"
        )
        await _collect(rt1.run())
        assert rt1.status == "completed"
        plan_calls_before = planner.plan_calls
        kb_calls_before = list(kb.calls)

        rt2 = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "",
            existing_run_id=rt1.run_id,
        )
        events = await _collect(rt2.run())
        assert _types(events) == ["run_completed"]
        assert rt2.status == "completed"
        assert rt2.final_result == "已完成"
        assert planner.plan_calls == plan_calls_before
        assert kb.calls == kb_calls_before
        assert _run_row(temp_db, rt1.run_id)["status"] == "completed"

    async def test_resume_of_failed_run_is_idempotent(self, ckpt, temp_db):
        """已 failed 的运行重复恢复：幂等回报终态，不重新执行。"""
        temp_db["owner_id"] = OWNER
        planner = ScriptedPlanner(S.PlanError("非法计划输出"))
        rt1 = _make_rt(temp_db, ckpt, planner, {}, "必然失败")
        events = await _collect(rt1.run())
        assert _types(events)[-1] == "run_failed"

        rt2 = _make_rt(
            temp_db, ckpt, planner, {}, "", existing_run_id=rt1.run_id
        )
        events = await _collect(rt2.run())
        assert _types(events) == ["run_failed"]
        assert rt2.status == "failed"
        assert planner.plan_calls == 1  # 未重新规划
        assert _run_row(temp_db, rt1.run_id)["status"] == "failed"

    async def test_resume_legacy_record_without_checkpoint(self, ckpt, temp_db):
        """无 checkpoint 的旧（legacy 时期）记录：明确失败，绝不当作新图重放。"""
        temp_db["owner_id"] = OWNER
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        with _session(temp_db) as s:
            run = agent_repo.create_run(s, OWNER, "旧时期任务", max_steps=8)
            run_id = run.id
            agent_repo.start_run(s, run_id, OWNER)
            step = agent_repo.add_step(
                s, run_id, OWNER, 1,
                stype="tool_call", goal="检索",
                tool_name="search_local_knowledgebase",
            )
            if step is not None:
                agent_repo.update_step(
                    s, step.id, OWNER, status="completed",
                    output_summary="ok", end_time_now=True,
                )
        rt = _make_rt(
            temp_db, ckpt, planner, {}, "", existing_run_id=run_id
        )
        events = await _collect(rt.run())
        assert _types(events) == ["run_resumed", "run_failed"]
        assert "checkpoint" in (events[-1]["error"] or "")
        assert planner.plan_calls == 0
        row = _run_row(temp_db, run_id)
        assert row["status"] == "failed"

    async def test_resume_after_mid_tool_crash_controlled_failure(
        self, ckpt, temp_db, tmp_path
    ):
        """中断于工具执行中途（步骤行仍为 running）：恢复时可控失败，不重跑工具。"""
        temp_db["owner_id"] = OWNER
        hang = HangTool()
        planner = ScriptedPlanner(
            S.Plan(
                summary="慢检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="不应到达")],
        )
        rt1 = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": hang}, "步骤中途中断"
        )
        gen = rt1.run()
        # 停在 step_start 之后（步骤行已落库为 running）、工具尚未执行完
        await _consume_until(gen, lambda e: e["type"] == "step_started")
        run_id = rt1.run_id
        assert _run_row(temp_db, run_id)["status"] == "running"
        steps = _step_rows(temp_db, run_id)
        assert len(steps) == 1 and steps[0]["status"] == "running"

        # 模拟进程重启：在同一 checkpoint 文件上重新初始化存储
        store2 = await ckpt_mod.initialize_agent_checkpoint_store(
            str(tmp_path / "agent_ckpt.sqlite")
        )
        rt2 = _make_rt(
            temp_db, store2, planner, {"search_local_knowledgebase": hang}, "",
            existing_run_id=run_id,
        )
        events = await _collect(rt2.run())
        assert _types(events) == ["run_resumed", "run_failed"]
        assert "为避免重复执行工具已停止" in (events[-1]["error"] or "")
        # 工具从未被完整执行过；恢复也未触发新的工具调用
        assert hang.calls == []
        row = _run_row(temp_db, run_id)
        assert row["status"] == "failed"

    async def test_concurrent_resume_serialized_no_double_execution(
        self, ckpt, temp_db
    ):
        """同一运行的两个并发恢复：串行化后各自得到一致结果，步骤不重复执行。"""
        temp_db["owner_id"] = OWNER
        rt1, gen, planner, kb = await _boundary_crash_setup(ckpt, temp_db)
        run_id = rt1.run_id
        tools = {"search_local_knowledgebase": kb}

        rt_a = _make_rt(
            temp_db, ckpt, planner, tools, "", existing_run_id=run_id
        )
        rt_b = _make_rt(
            temp_db, ckpt, planner, tools, "", existing_run_id=run_id
        )
        gen_a = rt_a.run()
        gen_b = rt_b.run()
        ev_a = await gen_a.__anext__()
        ev_b = await gen_b.__anext__()
        assert ev_a["type"] == "run_resumed" and ev_b["type"] == "run_resumed"

        rest_a = await _collect(gen_a)  # 先拿到锁，完成恢复
        assert rest_a[-1]["type"] == "run_completed"
        rest_b = await _collect(gen_b)  # 等锁后发现已终态，幂等回报
        assert _types(rest_b) == ["run_completed"]
        assert rt_a.final_result == rt_b.final_result == "恢复后最终回答"
        # 每个步骤全程恰好执行一次（原始一次 + 恢复一次，B 步只由先者执行）
        assert [c["query"] for c in kb.calls] == ["Mem0", "传统 RAG"]
        steps = _step_rows(temp_db, run_id)
        assert [s["seq"] for s in steps] == [1, 2, 3]
        assert all(s["status"] == "completed" for s in steps)


# ---------------------------------------------------------------------------
# 5. 服务层持久化与 SSE（默认 langgraph 运行时）
# ---------------------------------------------------------------------------


class TestLGPersistenceAndSSE:
    async def test_service_chat_persists_checkpoint_and_message(
        self, monkeypatch, ckpt, temp_db, users
    ):
        """服务层新运行：checkpoint 持久化（thread_id == run_id）、最终答案写回源消息。"""
        alice = users["alice"]
        temp_db["owner_id"] = alice["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后回答",
                steps=[
                    S.PlanStep(
                        "检索 Mem0", "search_local_knowledgebase", {"query": "Mem0"}
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="服务层最终回答")],
        )
        kb = _patch_service(monkeypatch, temp_db, planner)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, alice["id"], "agent_chat", "服务层运行"
            )
            msg_id = add_message(
                s, conv, alice["id"], "agent_chat", "Mem0 是什么"
            )
        session = temp_db["Session"]()
        try:
            result = await agent_service.agent_run_chat(
                query="Mem0 是什么",
                conversation_id=conv,
                message_id=msg_id,
                session=session,
                owner_id=alice["id"],
                stream=False,
                tool_config={"search_local_knowledgebase": {}},
            )
        finally:
            session.close()

        run = result["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "服务层最终回答"
        assert kb.calls == [{"query": "Mem0"}]

        # 新运行的 checkpoint 已持久化，thread_id 即稳定的 run_id
        tup = await ckpt.aget_tuple(
            {"configurable": {"thread_id": run["run_id"]}}
        )
        assert tup is not None
        # thread_id 是 checkpoint 存储的键（在 tuple 的 config 里），
        # 序列化 checkpoint 负载本身不含该字段
        assert tup.config["configurable"]["thread_id"] == run["run_id"]

        # 最终答案写回源消息
        with _session(temp_db) as s:
            msgs = (
                s.query(MessageModel)
                .filter(MessageModel.conversation_id == conv)
                .all()
            )
        assert len(msgs) == 1
        assert msgs[0].response == "服务层最终回答"
        assert msgs[0].meta_data["agent_run_id"] == run["run_id"]
        assert msgs[0].meta_data["agent_status"] == "completed"

    async def test_service_stream_chat_emits_sse_events(
        self, monkeypatch, ckpt, temp_db, users
    ):
        """流式模式：SSE 依次输出 plan/step/终态事件，聚合字段与 legacy 对齐。"""
        alice = users["alice"]
        temp_db["owner_id"] = alice["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后回答",
                steps=[
                    S.PlanStep(
                        "检索 Mem0", "search_local_knowledgebase", {"query": "Mem0"}
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="流式最终回答")],
        )
        _patch_service(monkeypatch, temp_db, planner)
        with _session(temp_db) as s:
            conv = create_conversation(s, alice["id"], "agent_chat", "流式运行")
            msg_id = add_message(
                s, conv, alice["id"], "agent_chat", "流式查询"
            )
        session = temp_db["Session"]()
        try:
            resp = await agent_service.agent_run_chat(
                query="流式查询",
                conversation_id=conv,
                message_id=msg_id,
                session=session,
                owner_id=alice["id"],
                stream=True,
                tool_config={"search_local_knowledgebase": {}},
            )
        finally:
            session.close()

        payloads = [json.loads(str(item)) async for item in resp.body_iterator]
        agent_events = [
            p.get("agent_event") for p in payloads
            if p.get("class_name") == "AgentRunEvent"
        ]
        for t in ("run_created", "plan_ready", "step_started", "step_finished", "run_completed"):
            assert t in agent_events
        assert agent_events[-1] == "run_completed"

        last = payloads[-1]
        # OpenAI 兼容分块格式：content 位于 choices[0].delta（与 legacy 一致）
        assert last["choices"][0]["delta"]["content"] == "流式最终回答"
        assert last["run_id"]
        with _session(temp_db) as s:
            assert (
                agent_repo.run_status(s, last["run_id"], alice["id"])
                == "completed"
            )

    async def test_service_resume_completes_and_writes_back(
        self, monkeypatch, ckpt, temp_db, users
    ):
        """服务层恢复：经完整链路（归属校验/重建工具/断点恢复/写回消息）完成。"""
        alice = users["alice"]
        temp_db["owner_id"] = alice["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后回答",
                steps=[
                    S.PlanStep(
                        "检索 Mem0", "search_local_knowledgebase", {"query": "Mem0"}
                    )
                ],
            ),
            decisions=[
                S.Decision(action="replan", steps=[
                    S.PlanStep(
                        "检索传统 RAG",
                        "search_local_knowledgebase", {"query": "传统 RAG"},
                    )
                ]),
                S.Decision(action="answer", answer="恢复后最终回答"),
            ],
        )
        kb = _patch_service(monkeypatch, temp_db, planner)
        with _session(temp_db) as s:
            conv = create_conversation(s, alice["id"], "agent_chat", "恢复运行")
            msg_id = add_message(
                s, conv, alice["id"], "agent_chat", "恢复查询"
            )
        # 运行时层先跑到边界中断点（与生产同一路径，同一 checkpoint 存储）
        rt1 = _make_rt(
            temp_db, ckpt, planner, {"search_local_knowledgebase": kb}, "中断任务",
            conversation_id=conv, source_message_id=msg_id,
        )
        gen = rt1.run()
        await _consume_until(gen, lambda e: e["type"] == "step_finished")
        run_id = rt1.run_id

        result = await agent_service.agent_run_resume(
            run_id=run_id, owner_id=alice["id"], stream=False
        )
        run = result["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "恢复后最终回答"
        assert [c["query"] for c in kb.calls] == ["Mem0", "传统 RAG"]
        row = _run_row(temp_db, run_id)
        assert row["status"] == "completed"
        steps = _step_rows(temp_db, run_id)
        assert [s["seq"] for s in steps] == [1, 2, 3]
        assert all(s["status"] == "completed" for s in steps)

        with _session(temp_db) as s:
            msgs = (
                s.query(MessageModel)
                .filter(MessageModel.conversation_id == conv)
                .all()
            )
        assert msgs[0].response == "恢复后最终回答"
        assert msgs[0].meta_data["agent_run_id"] == run_id

    async def test_service_resume_access_denied(
        self, monkeypatch, ckpt, temp_db, users
    ):
        """恢复必须通过服务端认证上下文校验归属：跨用户抛 LookupError。"""
        alice, bob = users["alice"], users["bob"]
        temp_db["owner_id"] = alice["id"]
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        _patch_service(monkeypatch, temp_db, planner)
        with _session(temp_db) as s:
            conv = create_conversation(s, alice["id"], "agent_chat", "alice 会话")
            run = agent_repo.create_run(
                s, alice["id"], "alice 的任务", conversation_id=conv, max_steps=8
            )
            run_id = run.id
            agent_repo.start_run(s, run_id, alice["id"])

        # bob 恢复 alice 的运行：归属校验失败（404 的来源）
        with pytest.raises(LookupError):
            await agent_service.agent_run_resume(
                run_id=run_id, owner_id=bob["id"], stream=False
            )
        assert planner.plan_calls == 0
        assert _run_row(temp_db, run_id)["status"] == "running"

        # alice 恢复本人无 checkpoint 的记录：明确失败状态（可查看/取消）
        result = await agent_service.agent_run_resume(
            run_id=run_id, owner_id=alice["id"], stream=False
        )
        run = result["agent_run"]
        assert run["status"] == "failed"
        assert "checkpoint" in (run["error"] or "")
        assert _run_row(temp_db, run_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# 6. 恢复 API 的权限隔离（任务 014 §12）
# ---------------------------------------------------------------------------


class TestLGResumeAPI:
    async def _seed_runs(self, temp_db, users):
        """alice 一个已终态运行 + 一个 running 运行。"""
        alice = users["alice"]
        with _session(temp_db) as s:
            done = agent_repo.create_run(s, alice["id"], "已完成的运行", max_steps=8)
            done_id = done.id
            agent_repo.finalize_run(
                s, done_id, alice["id"], "completed", final_result="已完成"
            )
            act = agent_repo.create_run(s, alice["id"], "进行中的运行", max_steps=8)
            act_id = act.id
            agent_repo.start_run(s, act_id, alice["id"])
        return done_id, act_id

    async def test_resume_api_isolation(
        self, monkeypatch, ckpt, temp_db, users, client
    ):
        """恢复 API：跨用户 404 / 未登录 401 / 必须改密 403 / 本人终态幂等 200。"""
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        # 终态运行的恢复不触发模型/工具，但服务层仍会构建工具/planner，需离线化
        _patch_service(monkeypatch, temp_db, planner)
        done_id, _ = await self._seed_runs(temp_db, users)

        h_alice = _headers(temp_db, "alice")
        h_bob = _headers(temp_db, "bob")

        # 用户 B 恢复用户 A 的运行：404（与"不存在"不可区分）
        r = await client.post(f"/agent-runs/{done_id}/resume", headers=h_bob)
        assert r.status_code == 404
        # 未登录：401
        r = await client.post(f"/agent-runs/{done_id}/resume")
        assert r.status_code == 401
        # 必须改密：403
        must_change = _mk_user(temp_db, "carol", must_change_password=True)
        h_carol = _headers(temp_db, "carol")
        assert (
            await client.post(
                f"/agent-runs/{done_id}/resume", headers=h_carol
            )
        ).status_code == 403
        # 本人恢复终态运行：幂等 200，原样返回终态结果
        r = await client.post(f"/agent-runs/{done_id}/resume", headers=h_alice)
        assert r.status_code == 200
        body = r.json()
        assert body["agent_run"]["status"] == "completed"
        assert body["agent_run"]["final_result"] == "已完成"
        # 不存在的运行：404
        assert (
            await client.post(
                "/agent-runs/no-such-run/resume", headers=h_alice
            )
        ).status_code == 404

    async def test_resume_api_legacy_runtime_returns_conflict(
        self, monkeypatch, ckpt, temp_db, users, client
    ):
        """运行时为 legacy（迁移期回退）时恢复返回 409 明确状态。"""
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        _patch_service(monkeypatch, temp_db, planner)
        monkeypatch.setattr(Settings.model_settings, "AGENT_RUNTIME", "legacy")
        _, act_id = await self._seed_runs(temp_db, users)

        r = await client.post(
            f"/agent-runs/{act_id}/resume", headers=_headers(temp_db, "alice")
        )
        assert r.status_code == 409
        assert "legacy" in r.json()["detail"]
        # 运行保持原状（可查看/取消）——按所有者 alice 查询
        temp_db["owner_id"] = users["alice"]["id"]
        assert _run_row(temp_db, act_id)["status"] == "running"


# ---------------------------------------------------------------------------
# 7. 两种运行时离线对比（同一脚本，相同业务结果）
# ---------------------------------------------------------------------------


class TestRuntimeParity:
    def _script(self):
        plan = S.Plan(
            summary="检索后回答",
            steps=[
                S.PlanStep("检索 A", "search_local_knowledgebase", {"query": "A"})
            ],
        )
        decisions = [
            S.Decision(action="replan", steps=[
                S.PlanStep("检索 B", "search_local_knowledgebase", {"query": "B"})
            ]),
            S.Decision(action="answer", answer="一致性结论"),
        ]
        return plan, decisions

    async def test_both_runtimes_same_terminal_and_steps(self, ckpt, temp_db):
        """同一脚本在 langgraph（默认）与 legacy（回退）下端到端结果一致。"""
        temp_db["owner_id"] = OWNER
        plan, decisions = self._script()

        kb_lg = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        rt_lg = _make_rt(
            temp_db, ckpt,
            ScriptedPlanner(plan, list(decisions)),
            {"search_local_knowledgebase": kb_lg},
            "一致性任务", forced="langgraph",
        )
        ev_lg = await _collect(rt_lg.run())
        run_lg = _run_row(temp_db, rt_lg.run_id)
        steps_lg = _step_rows(temp_db, rt_lg.run_id)

        kb_lg2 = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        rt_legacy = _make_rt(
            temp_db, ckpt,
            ScriptedPlanner(plan, list(decisions)),
            {"search_local_knowledgebase": kb_lg2},
            "一致性任务", forced="legacy",
        )
        ev_legacy = await _collect(rt_legacy.run())
        run_legacy = _run_row(temp_db, rt_legacy.run_id)
        steps_legacy = _step_rows(temp_db, rt_legacy.run_id)

        # 终态与最终结果一致
        assert run_lg["status"] == run_legacy["status"] == "completed"
        assert run_lg["final_result"] == run_legacy["final_result"] == "一致性结论"
        # 工具调用顺序与内容一致
        assert kb_lg.calls == kb_lg2.calls == [{"query": "A"}, {"query": "B"}]
        # 步骤轨迹一致
        assert [(s["seq"], s["type"], s["status"]) for s in steps_lg] == \
            [(s["seq"], s["type"], s["status"]) for s in steps_legacy] == \
            [(1, "tool_call", "completed"),
             (2, "tool_call", "completed"),
             (3, "answer", "completed")]
        assert [s["output_summary"] for s in steps_lg] == \
            [s["output_summary"] for s in steps_legacy]
        # 事件主类型对齐（legacy 无 run_resumed，其余主事件一致）
        core = {
            "run_created", "plan_ready",
            "step_started", "step_finished", "replanned", "run_completed",
        }
        assert core <= set(_types(ev_lg)) and core <= set(_types(ev_legacy))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-o", "addopts=", "-p", "no:cacheprovider"])
