"""单 Agent 任务闭环离线测试（任务 011）。

全部使用临时 SQLite 数据库（迁移到 v7）+ 确定性假规划器/假工具，不连接真实
模型、外部服务、MCP 服务或用户数据库；用户身份一律来自服务端认证上下文。

覆盖任务文档 §9 要求：
- 无需工具时直接生成最终答案；
- 同一运行顺序执行两个以上工具步骤，观察结果可供后续决策使用；
- 知识库工具作为普通 Agent 工具被选择和记录（mock 实际检索）；
- 工具报错后重新规划或明确失败，数据库中留下失败步骤；
- 达到最大步数 / 总超时时不再继续调用工具；
- 取消后不开始新步骤，运行状态为 cancelled；
- 运行中每步逐步持久化，完成 / 失败 / 请求中断均收敛到终态；
- 用户 A 无法查看或取消用户 B 的运行（含未登录 401、必须改密 403）；
- /chat/chat/completions 显式开启 agent_run 时接入新闭环（未开启时不变）。

回归补充（自动 Agent 规划偶发"计划为空"修复）：
- 规划器首次输出无效（仅 summary 的空计划 / 非 JSON）时一次性纠正重试，
  重试后仍无效则保持可控失败，且模型最多调用两次；
- 规划上下文中失败/取消的 Agent 轮被替换为中性占位，避免小模型把历史
  里的失败叙述复述进后续规划；
- 规划器工具描述设总量上限：小工具集输出不变，超大工具集截断并附说明。
"""
import sys
import types
import asyncio
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
from chatchat.server.db.repository.message_repository import update_message
from chatchat.server.db.repository import agent_run_repository as agent_repo
from chatchat.server.agent_run.planner import LLMPlanner
from chatchat.server.agent_run.runner import AgentRunner
from chatchat.server.agent_run import schema as S
from chatchat.server.memory import context_package, usage_manifest
import chatchat.server.agent_run.service as agent_service
import chatchat.server.api_server.chat_routes as chat_routes_mod
from chatchat.server.api_server.agent_run_routes import agent_run_router
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router


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


class FakeLLM:
    """按脚本返回原始文本的假 LLM，模拟小模型规划输出（离线、确定性）。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # 每次调用收到的 user prompt

    def invoke(self, messages):
        self.calls.append(messages[-1]["content"])
        if not self._responses:
            raise AssertionError("假 LLM 脚本耗尽")
        return self._responses.pop(0)


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


def _drain(gen):
    async def _run():
        return [ev async for ev in gen]

    return asyncio.run(_run())


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


def _make_runner(db, planner, tools, goal, **kw):
    return AgentRunner(
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
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    """设置一个 >=32 字节的测试密钥（非真实密钥）。"""
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "agent-run-unit-test-secret-0000000000",
    )
    return "agent-run-unit-test-secret-0000000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：建全表、迁移到 v7，注册模型元数据。"""
    path = tmp_path / "agent_run_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    assert current_version(engine.connect()) == 22
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


# ---------------------------------------------------------------------------
# 1. 执行闭环（运行器层，假规划器 + 假工具）
# ---------------------------------------------------------------------------


class TestRunnerDirectAnswer:
    def test_direct_answer_without_tools(self, temp_db, users):
        """无需工具：直接生成最终答案，一步 answer 记录，运行 completed。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(summary="无需工具", steps=[], direct_answer="答案是 42")
        )
        runner = _make_runner(
            temp_db, planner, tools={}, goal="1+1 等于几？"
        )
        events = _drain(runner.run())
        types = [ev["type"] for ev in events]
        assert types == [
            "run_created", "run_created", "plan_ready", "run_completed"
        ]
        assert planner.plan_calls == 1
        assert runner.status == "completed"
        assert runner.final_result == "答案是 42"

        row = _run_row(temp_db, runner.run_id)
        assert row is not None
        assert row["status"] == "completed"
        assert row["final_result"] == "答案是 42"
        assert row["plan_text"]
        steps = _step_rows(temp_db, runner.run_id)
        assert len(steps) == 1
        assert steps[0]["type"] == "answer"
        assert steps[0]["status"] == "completed"
        assert steps[0]["output_summary"] == "答案是 42"


class TestRunnerMultiStep:
    def test_async_only_tool_is_supported(self, temp_db, users):
        """只提供 ainvoke 的工具可被执行，为 MCP 工具保留兼容。"""
        temp_db["owner_id"] = users["alice"]["id"]
        tool = AsyncOnlyTool("async_search", "异步检索结果")
        planner = ScriptedPlanner(
            S.Plan(
                summary="异步检索",
                steps=[S.PlanStep("执行异步检索", "async_search", {"q": "Mem0"})],
            ),
            decisions=[S.Decision(action="answer", answer="已完成异步检索")],
        )
        runner = _make_runner(
            temp_db,
            planner,
            tools={"async_search": tool},
            goal="使用异步工具检索",
        )

        events = _drain(runner.run())

        assert events[-1]["type"] == "run_completed"
        assert tool.calls == [{"q": "Mem0"}]
        assert _step_rows(temp_db, runner.run_id)[0]["output_summary"] == "异步检索结果"

    def test_initial_multi_step_plan_runs_all_steps_before_decision(
        self, temp_db, users
    ):
        """初始计划的多个既定步骤必须全部执行后才进入完成判断。"""
        temp_db["owner_id"] = users["alice"]["id"]
        tool = FakeTool(
            "search_local_knowledgebase",
            result=lambda args: f"{args['query']} 的结果",
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="分别检索两个主题后比较",
                steps=[
                    S.PlanStep(
                        "检索 Mem0",
                        "search_local_knowledgebase",
                        {"query": "Mem0"},
                    ),
                    S.PlanStep(
                        "检索传统 RAG",
                        "search_local_knowledgebase",
                        {"query": "传统 RAG"},
                    ),
                ],
            ),
            decisions=[S.Decision(action="answer", answer="两者比较结论")],
        )
        runner = _make_runner(
            temp_db,
            planner,
            tools={"search_local_knowledgebase": tool},
            goal="比较 Mem0 和传统 RAG",
        )

        events = _drain(runner.run())

        assert events[-1]["type"] == "run_completed"
        assert [call["query"] for call in tool.calls] == ["Mem0", "传统 RAG"]
        assert len(planner.decide_calls) == 1
        assert [r.output for r in planner.decide_calls[0]] == [
            "Mem0 的结果",
            "传统 RAG 的结果",
        ]

    def test_multi_step_tool_run_observations_feed_later_decisions(
        self, temp_db, users
    ):
        """同一运行连续执行三个工具步骤；每次 decide 都能看到此前观察结果。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool(
            "search_local_knowledgebase",
            "本地知识库检索（mock）",
            result=_kb_result,
        )
        planner = ScriptedPlanner(
            S.Plan(
                summary="先检索 Mem0，再逐步补充检索并最终比较",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 相关内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    ),
                ],
            ),
            decisions=[
                # 每一步执行后都会判断；观察到 Mem0 结果后补充第 2 步
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索传统 RAG 相关内容",
                            "search_local_knowledgebase",
                            {"database": "kb", "query": "传统 RAG"},
                        )
                    ],
                ),
                # 观察到前两步结果后补充第 3 步
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "基于前两次检索观察，核对关键差异",
                            "search_local_knowledgebase",
                            {"database": "kb", "query": "Mem0 vs 传统 RAG"},
                        )
                    ],
                ),
                S.Decision(
                    action="answer",
                    answer="Mem0 与传统 RAG 的比较结论（综合三步检索观察）",
                ),
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="比较知识库中 Mem0 与传统 RAG 的相关内容",
        )
        events = _drain(runner.run())
        types = [ev["type"] for ev in events]
        assert types == [
            "run_created", "run_created", "plan_ready",
            "step_started", "step_finished", "replanned",
            "step_started", "step_finished", "replanned",
            "step_started", "step_finished", "run_completed",
        ]
        assert runner.status == "completed"

        # 观察结果确实进入后续决策：第 2 次 decide 收到前两步的完整输出
        assert len(planner.decide_calls) == 3
        first_decide_results = planner.decide_calls[0]
        assert len(first_decide_results) == 1
        assert first_decide_results[0].output == "观察：Mem0 的相关内容"
        second_decide_results = planner.decide_calls[1]
        assert [r.output for r in second_decide_results] == [
            "观察：Mem0 的相关内容", "观察：传统 RAG 的相关内容"
        ]
        # 工具按顺序被调用三次
        assert [c["query"] for c in kb.calls] == [
            "Mem0", "传统 RAG", "Mem0 vs 传统 RAG"
        ]

        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "completed"
        assert row["final_result"] == (
            "Mem0 与传统 RAG 的比较结论（综合三步检索观察）"
        )
        steps = _step_rows(temp_db, runner.run_id)
        assert [st["seq"] for st in steps] == [1, 2, 3, 4]
        assert [st["type"] for st in steps] == [
            "tool_call", "tool_call", "tool_call", "answer"
        ]
        assert all(st["status"] == "completed" for st in steps)
        assert steps[0]["output_summary"] == "观察：Mem0 的相关内容"
        assert steps[3]["output_summary"] == runner.final_result

    def test_kb_tool_recorded_as_regular_agent_tool(self, temp_db, users):
        """知识库工具作为普通 Agent 工具被选择并记录（mock 实际检索）。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", "本地知识库检索（mock）")
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索知识库",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 相关内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="基于检索结果的回答")],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="检索知识库中 Mem0 的相关内容",
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_completed"
        assert kb.calls == [{"database": "kb", "query": "Mem0"}]

        steps = _step_rows(temp_db, runner.run_id)
        tool_steps = [st for st in steps if st["type"] == "tool_call"]
        assert len(tool_steps) == 1
        assert tool_steps[0]["tool_name"] == "search_local_knowledgebase"
        assert "Mem0" in tool_steps[0]["tool_input"]
        assert tool_steps[0]["output_summary"] == kb.result
        # 轨迹不包含隐藏思维链：只持久化计划/工具行为/摘要/状态
        row = _run_row(temp_db, runner.run_id)
        assert "思维链" not in (row["plan_text"] or "")


class TestRunnerFailure:
    def test_tool_error_replans_then_succeeds(self, temp_db, users):
        """工具报错 → 留下失败步骤 → 重新规划 → 成功完成。"""
        temp_db["owner_id"] = users["alice"]["id"]
        state = {"n": 0}

        def flaky(args):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("向量库连接失败")
            return "第二次检索成功：传统 RAG 的观察"

        kb = FakeTool("search_local_knowledgebase", result=flaky)
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索后回答",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 相关内容", "search_local_knowledgebase",
                        {"query": "Mem0"},
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "重试：检索传统 RAG 相关内容",
                            "search_local_knowledgebase",
                            {"query": "传统 RAG"},
                        )
                    ],
                ),
                S.Decision(action="answer", answer="重试后得到的回答"),
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="检索并回答",
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_completed"
        assert runner.status == "completed"

        # 第一次 decide 看到失败观察
        first_results = planner.decide_calls[0]
        assert first_results[0].error == "向量库连接失败"

        steps = _step_rows(temp_db, runner.run_id)
        assert steps[0]["status"] == "failed"
        assert "向量库连接失败" in steps[0]["error_summary"]
        assert steps[1]["status"] == "completed"
        assert steps[1]["output_summary"] == "第二次检索成功：传统 RAG 的观察"
        assert steps[-1]["type"] == "answer"

    def test_tool_error_then_explicit_failure(self, temp_db, users):
        """工具持续报错 → 判断器明确失败，数据库留下失败步骤。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", raises=RuntimeError("工具不可用"))
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索 B", "search_local_knowledgebase", {"query": "B"}
                        )
                    ],
                ),
                S.Decision(action="fail", reason="工具持续不可用，无法完成"),
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="检索并回答",
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_failed"
        assert runner.status == "failed"
        assert "工具持续不可用" in runner.error

        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "failed"
        assert "工具持续不可用" in row["error"]
        steps = _step_rows(temp_db, runner.run_id)
        assert len(steps) == 2
        assert all(st["status"] == "failed" for st in steps)
        assert all("工具不可用" in st["error_summary"] for st in steps)

    def test_invalid_plan_output_fails_controlled(self, temp_db, users):
        """规划输出无效：可控失败，不无限循环，不留 running。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(plan=S.PlanError("输出不是 JSON"))
        kb = FakeTool("search_local_knowledgebase")
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="任意任务",
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_failed"
        assert planner.plan_calls == 1
        assert planner.decide_calls == []
        assert kb.calls == []

        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "failed"
        assert "计划生成失败" in row["error"]
        assert _step_rows(temp_db, runner.run_id) == []

    def test_invalid_decision_output_fails_controlled(self, temp_db, users):
        """判断输出无效：可控失败，已执行步骤保留在数据库中。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", result="观察 X")
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索 X", "search_local_knowledgebase", {"query": "X"}
                    )
                ],
            ),
            decisions=[S.PlanError("判断输出无效")],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="任意任务",
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_failed"
        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "failed"
        assert "执行判断失败" in row["error"]
        steps = _step_rows(temp_db, runner.run_id)
        assert len(steps) == 1
        assert steps[0]["status"] == "completed"

    def test_max_steps_halts_no_further_tool_calls(self, temp_db, users):
        """达到最大步数：不再调用工具，收敛为 failed。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", result=_kb_result)
        planner = ScriptedPlanner(
            S.Plan(
                summary="两步检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    ),
                    S.PlanStep(
                        "检索 B", "search_local_knowledgebase", {"query": "B"}
                    ),
                ],
            ),
            decisions=[
                # 每一步执行后都会判断；连续补充新步骤，
                # 使运行在第 3 步开始前被步数上限拦截
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索 C", "search_local_knowledgebase", {"query": "C"}
                        )
                    ],
                ),
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索 D", "search_local_knowledgebase", {"query": "D"}
                        )
                    ],
                ),
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="超出步数上限的任务", max_steps=2,
        )
        events = _drain(runner.run())
        assert [ev["type"] for ev in events][-1] == "run_failed"
        assert "最大步数" in runner.error
        # 只有两步工具调用；第 3 步（replan 补充）未执行
        assert [c["query"] for c in kb.calls] == ["A", "B"]

        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "failed"
        assert row["max_steps"] == 2
        steps = _step_rows(temp_db, runner.run_id)
        assert [st["seq"] for st in steps] == [1, 2]
        assert all(st["status"] == "completed" for st in steps)

    def test_timeout_halts_no_further_tool_calls(self, temp_db, users):
        """总超时达到：停止继续执行，收敛为 failed。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", result="耗时观察")
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索 B", "search_local_knowledgebase", {"query": "B"}
                        )
                    ],
                )
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="超时任务", max_steps=8, timeout_seconds=1,
        )
        # 让第一步"耗时"：用会 sleep 的假工具替换
        import time

        class SlowTool(FakeTool):
            def invoke(self, args):
                self.calls.append(dict(args or {}))
                time.sleep(1.2)
                return "耗时观察"

        slow = SlowTool("search_local_knowledgebase")
        runner.tools = {"search_local_knowledgebase": slow}
        _drain(runner.run())
        assert [c["query"] for c in slow.calls] == ["A"]
        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "failed"
        assert "超时" in row["error"]


class TestRunnerCancelAndAbort:
    def test_cancel_pending_before_planning(self, temp_db, users):
        """pending 阶段取消：不规划、不执行，直接 cancelled。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase")
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    )
                ],
            ),
            decisions=[S.Decision(action="answer", answer="回答")],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="取消任务",
        )
        async def scenario():
            gen = runner.run()
            first = await gen.__anext__()  # run_created (pending)
            assert first["type"] == "run_created"
            with _session(temp_db) as s:
                agent_repo.cancel_run(s, runner.run_id, temp_db["owner_id"])
            rest = [ev async for ev in gen]
            return first, rest

        first, rest = asyncio.run(scenario())
        assert [ev["type"] for ev in rest] == ["run_cancelled"]
        assert planner.plan_calls == 0
        assert kb.calls == []
        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "cancelled"
        assert _step_rows(temp_db, runner.run_id) == []

    def test_cancel_stops_new_steps_and_persists_progress(self, temp_db, users):
        """运行中取消：当前步骤完成后不开始新步骤，已执行步骤逐步入库。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", result="第 1 步观察")
        planner = ScriptedPlanner(
            S.Plan(
                summary="两步检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    )
                ],
            ),
            decisions=[
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索 B", "search_local_knowledgebase", {"query": "B"}
                        )
                    ],
                )
            ],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="运行中取消",
        )

        async def scenario():
            gen = runner.run()
            ev = await gen.__anext__()  # run_created (pending)
            ev = await gen.__anext__()  # run_created (running)
            ev = await gen.__anext__()  # plan_ready
            ev = await gen.__anext__()  # step_started (A)
            # 在 A 执行完成前发起取消（其他会话/连接）
            with _session(temp_db) as s:
                agent_repo.cancel_run(s, runner.run_id, temp_db["owner_id"])
            ev = await gen.__anext__()  # step_finished (A 已完成)
            # 逐步持久化：运行未结束，A 的结果已在数据库中
            with _session(temp_db) as s:
                rows = agent_repo.get_owned_steps(
                    s, runner.run_id, temp_db["owner_id"]
                )
                mid_run = [
                    {"seq": st.seq, "status": st.status,
                     "output_summary": st.output_summary}
                    for st in rows
                ]
            ev = await gen.__anext__()  # replanned (B 只是被提出)
            ev = await gen.__anext__()  # 门禁发现 cancelled -> run_cancelled
            return mid_run

        mid_run = asyncio.run(scenario())
        assert runner.status == "cancelled"
        assert mid_run == [
            {"seq": 1, "status": "completed", "output_summary": "第 1 步观察"}
        ]
        # 取消后没有开始新步骤：B 从未执行
        assert [c["query"] for c in kb.calls] == ["A"]

        row = _run_row(temp_db, runner.run_id)
        assert row["status"] == "cancelled"
        steps = _step_rows(temp_db, runner.run_id)
        assert [st["seq"] for st in steps] == [1]
        assert steps[0]["status"] == "completed"

    def test_request_abort_converges_to_terminal(self, temp_db, users):
        """请求中断（消费者关闭）：运行收敛为 failed，不留永久 running。"""
        temp_db["owner_id"] = users["alice"]["id"]
        kb = FakeTool("search_local_knowledgebase", result="观察")
        planner = ScriptedPlanner(
            S.Plan(
                summary="检索",
                steps=[
                    S.PlanStep(
                        "检索 A", "search_local_knowledgebase", {"query": "A"}
                    ),
                    S.PlanStep(
                        "检索 B", "search_local_knowledgebase", {"query": "B"}
                    ),
                ],
            ),
            decisions=[S.Decision(action="answer", answer="回答")],
        )
        runner = _make_runner(
            temp_db, planner, tools={"search_local_knowledgebase": kb},
            goal="中断任务",
        )

        async def scenario():
            gen = runner.run()
            await gen.__anext__()  # run_created (pending)
            await gen.__anext__()  # run_created (running)
            await gen.__anext__()  # plan_ready
            await gen.__anext__()  # step_started (A)
            await gen.aclose()  # 客户端断连 -> GeneratorExit
            return True

        asyncio.run(scenario())
        row = _run_row(temp_db, runner.run_id)
        assert row is not None
        assert row["status"] in agent_repo.RUN_TERMINAL_STATUSES
        # 数据库中不存在任何 pending/running 的运行
        with _session(temp_db) as s:
            active = s.query(AgentRunModel).filter(
                AgentRunModel.owner_id == temp_db["owner_id"],
                AgentRunModel.status.in_(agent_repo.RUN_ACTIVE_STATUSES),
            ).all()
        assert active == []


# ---------------------------------------------------------------------------
# 2. 所有者隔离（Agent 运行 API）
# ---------------------------------------------------------------------------


class TestAgentRunApiIsolation:
    def _seed_run(self, temp_db, owner_id, conversation_id, status="running"):
        with _session(temp_db) as s:
            run = agent_repo.create_run(
                s, owner_id, "测试目标",
                conversation_id=conversation_id, max_steps=8,
            )
            run_id = run.id
            if status != "pending":
                agent_repo.start_run(s, run_id, owner_id)
            step = agent_repo.add_step(
                s, run_id, owner_id, 1,
                stype="tool_call", goal="检索",
                tool_name="search_local_knowledgebase",
            )
            if step is not None:
                agent_repo.update_step(
                    s, step.id, owner_id, status="completed",
                    output_summary="ok",
                )
            if status == "completed":
                agent_repo.finalize_run(
                    s, run_id, owner_id, "completed", final_result="完成"
                )
        return run_id

    def test_owner_isolation_list_detail_cancel(self, app, client, temp_db, users):
        """用户 A 无法查看或取消用户 B 的运行；未登录 401、必须改密 403。"""
        with _session(temp_db) as s:
            conv_alice = create_conversation(
                s, users["alice"]["id"], "agent_chat", "alice 会话"
            )
        run_id = self._seed_run(temp_db, users["alice"]["id"], conv_alice)
        h_alice = _headers(temp_db, "alice")
        h_bob = _headers(temp_db, "bob")

        # alice 可见并可操作
        r = client.get("/agent-runs", headers=h_alice)
        assert r.status_code == 200
        body = r.json()
        assert len(body["runs"]) == 1
        assert body["runs"][0]["run_id"] == run_id
        assert body["runs"][0]["status"] == "running"

        r = client.get(f"/agent-runs/{run_id}", headers=h_alice)
        assert r.status_code == 200
        detail = r.json()
        assert detail["run"]["run_id"] == run_id
        assert len(detail["steps"]) == 1
        assert detail["steps"][0]["tool_name"] == "search_local_knowledgebase"

        # bob 无法查看（404，不区分不存在/非本人）
        r = client.get("/agent-runs", headers=h_bob)
        assert r.status_code == 200
        assert r.json() == {"runs": []}
        r = client.get(f"/agent-runs/{run_id}", headers=h_bob)
        assert r.status_code == 404
        r = client.post(f"/agent-runs/{run_id}/cancel", headers=h_bob)
        assert r.status_code == 404

        # alice 取消成功；bob 取消不影响（仍是 404）
        r = client.post(f"/agent-runs/{run_id}/cancel", headers=h_alice)
        assert r.status_code == 200
        assert r.json()["run"]["status"] == "cancelled"
        r = client.post(f"/agent-runs/{run_id}/cancel", headers=h_bob)
        assert r.status_code == 404

        # 终态取消保持原状
        r = client.post(f"/agent-runs/{run_id}/cancel", headers=h_alice)
        assert r.status_code == 200
        assert r.json()["run"]["status"] == "cancelled"

        # 不存在的运行 404
        r = client.get("/agent-runs/does-not-exist", headers=h_alice)
        assert r.status_code == 404
        r = client.post("/agent-runs/does-not-exist/cancel", headers=h_alice)
        assert r.status_code == 404

        # 未登录 401
        assert client.get("/agent-runs").status_code == 401
        assert client.get(f"/agent-runs/{run_id}").status_code == 401
        assert client.post(f"/agent-runs/{run_id}/cancel").status_code == 401

        # 必须改密用户 403
        must_change = _mk_user(temp_db, "carol", must_change_password=True)
        h_carol = _headers(temp_db, "carol")
        assert client.get("/agent-runs", headers=h_carol).status_code == 403
        assert client.post(f"/agent-runs/{run_id}/cancel", headers=h_carol).status_code == 403

    def test_list_by_foreign_conversation_returns_empty(
        self, app, client, temp_db, users
    ):
        """按他人会话过滤：返回空列表，不泄露运行。"""
        with _session(temp_db) as s:
            conv_alice = create_conversation(
                s, users["alice"]["id"], "agent_chat", "alice 会话"
            )
            conv_bob = create_conversation(
                s, users["bob"]["id"], "agent_chat", "bob 会话"
            )
        self._seed_run(temp_db, users["alice"]["id"], conv_alice)
        self._seed_run(temp_db, users["bob"]["id"], conv_bob)
        h_alice = _headers(temp_db, "alice")
        h_bob = _headers(temp_db, "bob")

        r = client.get(
            f"/agent-runs?conversation_id={conv_bob}", headers=h_alice
        )
        assert r.status_code == 200
        assert r.json() == {"runs": []}

        r = client.get(
            f"/agent-runs?conversation_id={conv_bob}", headers=h_bob
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["runs"]) == 1
        assert body["runs"][0]["conversation_id"] == conv_bob


# ---------------------------------------------------------------------------
# 3. 接入已认证的对话流程（/chat/chat/completions + agent_run 开关）
# ---------------------------------------------------------------------------


class TestChatCompletionsAgentRun:
    def _patch_service(self, monkeypatch, temp_db, planner):
        """把服务层接到临时库与假规划器/假工具（不触模型、记忆与网络）。"""
        # 测试模型名不在本机 model_settings.yaml 里：不打桩的话，路由的模型
        # 校验会先返回 400，测试结果就取决于 CHATCHAT_ROOT 指向哪里。
        monkeypatch.setattr(
            chat_routes_mod, "get_model_info", lambda **k: {"platform_name": "test"}
        )
        kb = FakeTool(
            "search_local_knowledgebase", "本地知识库检索（mock）",
            result=_kb_result,
        )
        registry = {"search_local_knowledgebase": kb}
        monkeypatch.setattr(
            agent_service, "get_tool",
            lambda name=None: registry.get(name) if name else registry,
        )
        # chat_routes 在路由层也会用字符串工具名做 dict 转换与取配置，
        # 一并打桩，避免触达真实 get_tool（其导入链读取真实知识库数据库）。
        monkeypatch.setattr(
            chat_routes_mod, "get_tool",
            lambda name=None: registry.get(name) if name else registry,
        )
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
        # 第四期起 Agent 分支的历史、记忆注入与 manifest 都来自统一上下文构建器
        monkeypatch.setattr(
            agent_service,
            "_build_package",
            lambda *a, **k: context_package.ContextPackage(),
        )
        monkeypatch.setattr(agent_service, "_auto_extract", lambda *a, **k: None)
        return kb

    def test_agent_run_flag_runs_closed_loop(self, monkeypatch, app, client,
                                             temp_db, users):
        """extra.agent_run=true 时走新闭环：多步工具 + 持久化 + 写回消息。"""
        temp_db["owner_id"] = users["alice"]["id"]
        planner = ScriptedPlanner(
            S.Plan(
                summary="先检索 Mem0，观察后再检索传统 RAG 并比较",
                steps=[
                    S.PlanStep(
                        "检索 Mem0 相关内容", "search_local_knowledgebase",
                        {"database": "kb", "query": "Mem0"},
                    ),
                ],
            ),
            decisions=[
                # 第 1 步执行后判断：补充第 2 步检索
                S.Decision(
                    action="replan",
                    steps=[
                        S.PlanStep(
                            "检索传统 RAG 相关内容",
                            "search_local_knowledgebase",
                            {"database": "kb", "query": "传统 RAG"},
                        )
                    ],
                ),
                S.Decision(
                    action="answer",
                    answer="Mem0 与传统 RAG 的比较（最终回答）",
                ),
            ],
        )
        kb = self._patch_service(monkeypatch, temp_db, planner)

        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "比较 Mem0 与传统 RAG"
            )
        h_alice = _headers(temp_db, "alice")
        r = client.post(
            "/chat/chat/completions",
            headers=h_alice,
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "比较知识库中 Mem0 与传统 RAG 的相关内容"}],
                "stream": False,
                "conversation_id": conv,
                "tools": ["search_local_knowledgebase"],
                "agent_run": True,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["conversation_id"] == conv
        run = data["agent_run"]
        assert run["status"] == "completed"
        assert run["final_result"] == "Mem0 与传统 RAG 的比较（最终回答）"
        assert [st["tool_name"] for st in run["steps"] if st["type"] == "tool_call"] == [
            "search_local_knowledgebase", "search_local_knowledgebase"
        ]
        assert kb.calls == [
            {"database": "kb", "query": "Mem0"},
            {"database": "kb", "query": "传统 RAG"},
        ]
        # 运行与步骤已持久化（owner 为 alice）
        assert run["run_id"]
        row = _run_row(temp_db, run["run_id"])
        assert row["status"] == "completed"
        assert len(_step_rows(temp_db, run["run_id"])) == 3  # 2 工具 + 1 答案
        # 最终回答写回来源消息
        with _session(temp_db) as s:
            # message 表无 owner_id 列，归属经会话推导（conv 属 alice）
            msgs = (
                s.query(MessageModel)
                .filter_by(conversation_id=conv)
                .all()
            )
        assert len(msgs) == 1
        assert msgs[0].response == "Mem0 与传统 RAG 的比较（最终回答）"
        assert msgs[0].meta_data["agent_run_id"] == run["run_id"]

    def test_agent_run_flag_rejects_foreign_conversation(
        self, monkeypatch, app, client, temp_db, users
    ):
        """他人会话 + agent_run：模型调用前 404，不创建运行。"""
        planner = ScriptedPlanner(S.Plan(direct_answer="不应执行"))
        self._patch_service(monkeypatch, temp_db, planner)
        with _session(temp_db) as s:
            conv_bob = create_conversation(
                s, users["bob"]["id"], "agent_chat", "bob 会话"
            )
        h_alice = _headers(temp_db, "alice")
        r = client.post(
            "/chat/chat/completions",
            headers=h_alice,
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "越权请求"}],
                "stream": False,
                "conversation_id": conv_bob,
                "agent_run": True,
            },
        )
        assert r.status_code == 404
        assert planner.plan_calls == 0
        with _session(temp_db) as s:
            runs = s.query(AgentRunModel).all()
        assert runs == []

    def test_without_agent_run_flag_unchanged_path(self, monkeypatch, app,
                                                   client, temp_db, users):
        """未开启 agent_run：走原有 chat 路径（本测试桩断言其被调用）。"""
        kb = self._patch_service(
            monkeypatch, temp_db,
            ScriptedPlanner(S.Plan(direct_answer="不应执行")),
        )
        called = {}

        async def fake_chat(**kwargs):
            called["kwargs"] = kwargs
            return {"object": "chat.completion", "content": "原路径回答"}

        monkeypatch.setattr(chat_routes_mod, "chat", fake_chat)
        with _session(temp_db) as s:
            conv = create_conversation(
                s, users["alice"]["id"], "agent_chat", "普通会话"
            )
        h_alice = _headers(temp_db, "alice")
        r = client.post(
            "/chat/chat/completions",
            headers=h_alice,
            json={
                "model": "fake-llm",
                "messages": [{"role": "user", "content": "普通问题"}],
                "stream": False,
                "conversation_id": conv,
                "tools": ["search_local_knowledgebase"],
            },
        )
        assert r.status_code == 200
        assert r.json()["content"] == "原路径回答"
        assert called["kwargs"]["conversation_id"] == conv
        assert called["kwargs"]["owner_id"] == users["alice"]["id"]
        # 未创建 Agent 运行
        with _session(temp_db) as s:
            runs = s.query(AgentRunModel).all()
        assert runs == []


# ---------------------------------------------------------------------------
# 4. 规划偶发无效输出的纠正重试 / 历史去污 / 工具描述预算
# ---------------------------------------------------------------------------


class TestLLMPlannerRetry:
    """生产规划器（LLMPlanner）对偶发无效输出的有限次纠正重试。"""

    @staticmethod
    def _ctx():
        tools = {
            "search_internet": FakeTool("search_internet", "联网搜索"),
        }
        return S.PlanContext(goal="联网搜索 X", tools=tools, max_steps=8)

    def test_summary_only_output_recovered_by_retry(self):
        """首次仅输出 summary（空计划）→ 纠正重试后得到有效计划。"""
        llm = FakeLLM([
            '{"summary": "无法规划", "steps": [], "direct_answer": ""}',
            '{"summary": "联网搜索", "steps": [{"goal": "搜索 X", '
            '"tool": "search_internet", "args": {"query": "X"}}], '
            '"direct_answer": ""}',
        ])
        planner = LLMPlanner(llm)
        plan = planner.plan(self._ctx())
        assert len(plan.steps) == 1
        assert plan.steps[0].tool == "search_internet"
        assert plan.direct_answer == ""
        assert len(llm.calls) == 2
        # 重试时携带明确的纠正提示，而不是简单重复原提示词
        assert llm.calls[1].startswith(llm.calls[0])
        assert "纠正" in llm.calls[1]
        assert "不可同时为空" in llm.calls[1]

    def test_persistently_invalid_output_still_fails(self):
        """重试后仍无效：保持可控失败，模型最多调用两次（不无限重试）。"""
        llm = FakeLLM([
            '{"summary": "无法规划", "steps": [], "direct_answer": ""}',
            "这不是一个 JSON 对象",
        ])
        planner = LLMPlanner(llm)
        with pytest.raises(S.PlanError):
            planner.plan(self._ctx())
        assert len(llm.calls) == 2

    def test_valid_first_output_no_retry(self):
        """首次输出合法：不触发重试。"""
        llm = FakeLLM([
            '{"summary": "直接回答", "steps": [], "direct_answer": "答案是 42"}',
        ])
        planner = LLMPlanner(llm)
        plan = planner.plan(self._ctx())
        assert plan.direct_answer == "答案是 42"
        assert len(llm.calls) == 1


class TestToolsDescriptionBudget:
    """规划器工具描述的总量预算（防止 MCP 等工具集膨胀撑爆提示词）。"""

    def test_small_toolset_output_unchanged(self):
        tools = {
            "search_internet": FakeTool("search_internet", "联网搜索"),
            "search_local_knowledgebase": FakeTool(
                "search_local_knowledgebase", "本地知识库检索"
            ),
        }
        text = S.format_tools_description(tools)
        assert text == (
            "- search_internet：联网搜索\n"
            "- search_local_knowledgebase：本地知识库检索"
        )

    def test_oversized_toolset_truncated_with_note(self):
        tools = {
            f"tool_{i:02d}": FakeTool(f"tool_{i:02d}", "描述" * 100)
            for i in range(30)
        }
        text = S.format_tools_description(tools)
        assert len(text) <= 3200  # 总预算 3000 + 省略说明
        assert "未列出" in text
        # 字典序靠前的工具保留
        assert text.startswith("- tool_00：描述")
        # 靠后的工具被省略
        assert "- tool_29" not in text

    def test_empty_toolset_placeholder(self):
        assert S.format_tools_description({}) == "（无可用工具）"


class TestBuildHistoryNeutralization:
    """规划历史去污：失败/取消的 Agent 轮替换为中性占位；半截回答如实标记。"""

    def _patch_recent_messages(self, monkeypatch, oldest_first):
        """把统一上下文构建器换成"返回这几条消息"的替身。

        第四期起 Agent 规划历史也来自 ContextPackage 的最近原文窗口（升序），
        不再由 filter_message 倒序返回（设计方案 §18.2.2）。
        """
        rows = [
            types.SimpleNamespace(
                id=f"m{i}",
                query=item["query"],
                response=item["response"],
                meta_data=item.get("meta") or {},
            )
            for i, item in enumerate(oldest_first)
        ]
        monkeypatch.setattr(
            agent_service.context_package,
            "build_context_package",
            lambda *a, **k: context_package.ContextPackage(recent_messages=rows),
        )

    def test_failed_and_cancelled_turns_are_neutralized(self, monkeypatch):
        """历史中的失败/取消轮不再把错误文本带给规划器。"""
        oldest_first = [
            {"query": "什么是 MTP", "response": "MTP 是多token预测技术。"},
            {"query": "帮我联网搜索 X",
             "response": "（Agent 运行失败：计划生成失败（模型输出无效）："
                         "计划为空：既没有步骤也没有 direct_answer）"},
            {"query": "继续处理", "response": "（Agent 运行已取消）"},
        ]
        self._patch_recent_messages(monkeypatch, oldest_first)
        history = agent_service._build_history(
            object(), "conv-1", "owner-1", None
        )
        # 时间正序：普通轮原样保留
        assert history[0] == {"role": "user", "content": "什么是 MTP"}
        assert history[1] == {
            "role": "assistant", "content": "MTP 是多token预测技术。"
        }
        # 失败/取消轮：原始错误细节不再保留，替换为中性占位
        assert "计划为空" not in history[3]["content"]
        assert "模型输出无效" not in history[3]["content"]
        assert "不可用" in history[3]["content"]
        assert "不可用" in history[5]["content"]

    def test_normal_history_unchanged(self, monkeypatch):
        """普通轮（非 Agent 结果文本）保持原样。"""
        oldest_first = [
            {"query": "Q1", "response": "A1"},
            {"query": "Q2", "response": "A2"},
        ]
        self._patch_recent_messages(monkeypatch, oldest_first)
        history = agent_service._build_history(
            object(), "conv-1", "owner-1", None
        )
        assert history == [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
        ]

    def test_partial_answer_is_marked_as_unfinished(self, monkeypatch):
        """停止/中断留下的半截回答进规划历史，但要说清它没写完（§11.3 / §18.3.9）。

        不带标记时，规划器会把半截说法当成已经说完的结论，接着往下推或直接
        复述。用户下一轮说"继续"仍然要求这段可见，所以是标记而不是删除。
        """
        oldest_first = [
            {
                "query": "写一段长文",
                "response": "只写了一半的回答",
                "meta": {"generation_status": "cancelled"},
            },
            {
                "query": "接着写",
                "response": "这次写完了",
                "meta": {"generation_status": "completed"},
            },
        ]
        self._patch_recent_messages(monkeypatch, oldest_first)
        history = agent_service._build_history(object(), "conv-1", "owner-1", None)
        assert history[1]["content"] == (
            f"{usage_manifest.INCOMPLETE_MARKER} 只写了一半的回答"
        )
        assert history[3]["content"] == "这次写完了"

    def test_neutralized_agent_failure_is_not_marked_twice(self, monkeypatch):
        """已经换成中性失败说明的轮次不再叠加 ``[未完成回答]`` 标记。"""
        oldest_first = [
            {
                "query": "帮我联网搜索 X",
                "response": "（Agent 运行已取消）",
                "meta": {"generation_status": "cancelled"},
            },
        ]
        self._patch_recent_messages(monkeypatch, oldest_first)
        history = agent_service._build_history(object(), "conv-1", "owner-1", None)
        content = history[1]["content"]
        assert usage_manifest.INCOMPLETE_MARKER not in content
        assert "不可用" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-o", "addopts=", "-p", "no:cacheprovider"])
