"""论文研究 LangGraph 工作流离线测试（任务 017 阶段 C）。

覆盖：
- 真实多节点图结构（非单节点包装）；
- 计划生成后挂起等待确认（确认前不调用任何来源）；
- 确认前编辑计划，确认后按编辑后的检索式检索；
- 确认执行 → completed：摘要统计、论文入库、Agent Run 关联；
- 单来源失败继续（source_errors>0），全部来源失败 → failed；
- 挂起时取消 → 确认 409；检索中取消 → cancelled 且不入库；
- 崩溃恢复：来源抛 BaseException → failed，resume 从 checkpoint
  继续且不重复创建论文；全部失败后重试同样不重调计划模型；
- completed 后 resume 幂等；
- Phoenix 业务 span 关联研究运行/项目 ID；关闭时可观测性无副作用；
- API 表面：状态映射（400/404/409）、计划编辑校验、取消。

全部注入假计划器/假来源与临时 checkpoint 存储，不连接真实模型、
ArXiv、Phoenix、外部网络或用户数据。
"""
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.agent_run.runtime import checkpoint as ckpt_mod
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.models.research_model import (  # noqa: F401
    PaperRecordModel,
    ProjectPaperModel,
    ResearchRunModel,
)
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service as auth_service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.api_server.research_routes import research_router
from chatchat.server.research import runtime as research_runtime
from chatchat.server.research import service as research_service
from chatchat.server.research.graph import build_research_graph
from chatchat.server.research.plan import ResearchPlan, ResearchPlanError, SubQuestion
from chatchat.server.research.papers import PaperCandidate
from chatchat.server.research.runtime import ResearchRuntime
from chatchat.server.research.sources import SourceError


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "research-workflow-unit-test-secret-00000",
    )
    return "research-workflow-unit-test-secret-00000"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "workflow_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 13
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@pytest.fixture
async def ckpt(tmp_path):
    """临时 checkpoint 存储（与测试同一事件循环）。"""
    store = await ckpt_mod.initialize_agent_checkpoint_store(
        str(tmp_path / "research_ckpt.sqlite")
    )
    yield store
    await ckpt_mod.shutdown_agent_checkpoint_store()


def _mk_user(db, username):
    s = db["Session"]()
    try:
        u = auth_service.create_user(
            s, username=username, display_name=username.title(),
            password="password12345678", role="user",
        )
        u.must_change_password = False
        s.commit()
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(db, username):
    s = db["Session"]()
    try:
        u = s.query(UserModel).filter(UserModel.username == username).one_or_none()
        if u is None:
            s.close()
            _mk_user(db, username)
            s = db["Session"]()
            u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def users(temp_db):
    return {
        "alice": _mk_user(temp_db, "alice"),
        "bob": _mk_user(temp_db, "bob"),
    }


@pytest.fixture
def app(temp_db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(research_router)

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

        def patch(self, url, **kwargs):
            return self.request("PATCH", url, **kwargs)

        def put(self, url, **kwargs):
            return self.request("PUT", url, **kwargs)

        def delete(self, url, **kwargs):
            return self.request("DELETE", url, **kwargs)

    return Client()


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakePlanner:
    def __init__(self, queries=("wireless localization",), fail=False):
        self.queries = list(queries)
        self.fail = fail
        self.calls = 0

    def generate(self, research_question, scope, available_sources):
        self.calls += 1
        if self.fail:
            raise ResearchPlanError("模拟模型输出无效")
        return ResearchPlan(
            summary="计划摘要",
            sub_questions=[SubQuestion("如何提升精度？", list(self.queries))],
            sources=sorted(available_sources),
        )


def _paper(title, arxiv_id=None, doi=None, year=2023, abstract=""):
    return PaperCandidate(
        title=title, source="", arxiv_id=arxiv_id, doi=doi, year=year,
        abstract=abstract,
    )


class FakeSource:
    def __init__(self, name, results=(), error=None, crash=None, on_search=None):
        self.name = name
        self.results = list(results)
        self.error = error
        self.crash = crash
        self.on_search = on_search
        self.calls = []

    async def search(self, query, limit=10):
        self.calls.append(query)
        if self.crash is not None:
            raise self.crash
        if self.error is not None:
            raise SourceError(self.error)
        if self.on_search is not None:
            self.on_search(query)
        out = []
        for r in self.results:
            c = _paper(r.title, arxiv_id=r.arxiv_id, doi=r.doi, year=r.year,
                       abstract=r.abstract)
            c.source = self.name
            out.append(c)
        return out


def _wire_service(monkeypatch, db):
    """让服务层使用临时数据库。"""
    monkeypatch.setattr(research_service, "_session_factory", db["Session"])


def _make_project_run(db, owner="alice"):
    with _session(db) as s:
        project = repo.create_project(s, owner, "定位研究", "WiFi 指纹定位精度")
        run = repo.create_run(s, owner, project.id)
        return project.id, run.id


# ---------------------------------------------------------------------------
# 1. 图结构：真实多节点
# ---------------------------------------------------------------------------


class TestGraphShape:
    def test_graph_has_six_real_nodes(self, temp_db):
        rt = ResearchRuntime(
            temp_db["Session"], "alice", "run-x",
            planner=FakePlanner(), sources={},
        )
        compiled = build_research_graph(rt, None)
        nodes = set(compiled.get_graph().nodes)
        for expected in (
            "prepare_plan", "wait_for_confirmation", "retrieve_sources",
            "normalize_and_deduplicate", "screen_candidates", "persist_results",
        ):
            assert expected in nodes
        assert len(nodes) > 3  # 不是单节点包装


# ---------------------------------------------------------------------------
# 2. 计划生成 → 挂起等待确认（确认前不检索）
# ---------------------------------------------------------------------------


class TestPlanGate:
    async def test_plan_generated_then_pauses_without_source_calls(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        planner = FakePlanner()
        src = FakeSource("alpha", results=[_paper("A", arxiv_id="2301.11111")])
        summary = await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": src}
        )
        assert summary["status"] == "awaiting_confirmation"
        assert summary["plan"]["sub_questions"][0]["queries"] == ["wireless localization"]
        assert planner.calls == 1
        assert src.calls == []  # 确认前绝不检索

        # 重复生成幂等：不再调模型
        again = await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": src}
        )
        assert again["status"] == "awaiting_confirmation"
        assert planner.calls == 1

    async def test_invalid_plan_fails_controlled(self, temp_db, ckpt, monkeypatch):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        summary = await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(fail=True),
            sources={"alpha": FakeSource("alpha")},
        )
        assert summary["status"] == "failed"
        assert "计划生成失败" in (summary["error"] or "")

    async def test_plan_editable_before_confirmation_and_used(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        planner = FakePlanner(queries=("original query",))
        alpha = FakeSource("alpha", results=[_paper("A", arxiv_id="2301.11111")])
        await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": alpha}
        )
        edited = {
            "summary": "改过的计划",
            "sub_questions": [{"question": "q", "queries": ["edited query"]}],
            "sources": ["alpha"],
        }
        with _session(temp_db) as s:
            repo.update_plan(s, rid, "alice", json.dumps(edited, ensure_ascii=False))
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=planner, sources={"alpha": alpha}
        )
        assert summary["status"] == "completed"
        assert alpha.calls == ["edited query"]  # 用编辑后的检索式，不用旧的
        assert "original query" not in alpha.calls


# ---------------------------------------------------------------------------
# 3. 确认执行：完成、入库、Agent Run 关联
# ---------------------------------------------------------------------------


class TestConfirmExecute:
    async def test_full_flow_confirm_then_execute(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        alpha = FakeSource("alpha", results=[
            _paper("Wireless Fingerprinting", arxiv_id="2301.11111"),
        ])
        beta = FakeSource("beta", results=[
            _paper("Wireless Fingerprinting copy", arxiv_id="2301.11111"),
            _paper("RSSI Calibration", doi="10.5/rss"),
        ])
        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(),
            sources={"alpha": alpha, "beta": beta},
        )
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=FakePlanner(),
            sources={"alpha": alpha, "beta": beta},
        )
        assert summary["status"] == "completed"
        assert alpha.calls == ["wireless localization"]
        assert beta.calls == ["wireless localization"]
        stats = summary["summary"]
        assert stats["merged_count"] == 1  # 同 arXiv ID 合并一条
        assert stats["candidate_count"] == 2
        assert stats["new_papers"] == 2
        assert stats["source_hits"] == {"alpha": 1, "beta": 2}
        assert stats["source_errors"] == 0

        with _session(temp_db) as s:
            run = repo.get_owned_run(s, rid, "alice")
            assert run.status == "completed" and run.stage == "done"
            papers = repo.list_project_papers(s, pid, "alice")
            assert len(papers) == 2
            assert all(pp.screening_status == "pending_review" for pp, _ in papers)
            # Agent Run 关联：一条 completed 的 agent_run 行
            assert run.agent_run_id
            ar = s.get(AgentRunModel, run.agent_run_id)
            assert ar is not None and ar.status == "completed"

    async def test_confirm_requires_awaiting_state(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        with pytest.raises(repo.ResearchConflict):
            await research_service.confirm_and_execute_research_run(
                rid, "alice", planner=FakePlanner(),
                sources={"alpha": FakeSource("alpha")},
            )


# ---------------------------------------------------------------------------
# 4. 来源失败隔离
# ---------------------------------------------------------------------------


class TestSourceFailures:
    async def test_partial_failure_continues(self, temp_db, ckpt, monkeypatch):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        good = FakeSource("alpha", results=[_paper("Good", arxiv_id="2301.22222")])
        bad = FakeSource("beta", error="ArXiv 不可用")
        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(), sources={"alpha": good, "beta": bad}
        )
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": good, "beta": bad}
        )
        assert summary["status"] == "completed"
        assert summary["summary"]["source_errors"] >= 1
        with _session(temp_db) as s:
            assert len(repo.list_project_papers(s, pid, "alice")) == 1

    async def test_all_sources_fail_marks_failed(self, temp_db, ckpt, monkeypatch):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        bad1 = FakeSource("alpha", error="网络不可用")
        bad2 = FakeSource("beta", error="依赖缺失")
        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(), sources={"alpha": bad1, "beta": bad2}
        )
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": bad1, "beta": bad2}
        )
        assert summary["status"] == "failed"
        assert "所有来源均失败" in (summary["error"] or "")
        with _session(temp_db) as s:
            assert repo.list_project_papers(s, pid, "alice") == []

    async def test_retry_after_all_failed_reuses_saved_plan(
        self, temp_db, ckpt, monkeypatch
    ):
        """全部失败 → failed；修好来源后 resume 重试：不重调计划模型、
        从已持久化计划继续并完成。"""
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        planner = FakePlanner()
        bad = FakeSource("alpha", error="网络不可用")
        await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": bad}
        )
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=planner, sources={"alpha": bad}
        )
        assert summary["status"] == "failed"
        assert planner.calls == 1

        fixed = FakeSource("alpha", results=[_paper("Recovered", arxiv_id="2301.33333")])
        retry = await research_service.resume_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": fixed}
        )
        assert retry["status"] == "completed"
        assert fixed.calls == ["wireless localization"]
        with _session(temp_db) as s:
            assert len(repo.list_project_papers(s, pid, "alice")) == 1


# ---------------------------------------------------------------------------
# 5. 取消语义
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_cancel_while_awaiting_then_confirm_conflicts(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("A", arxiv_id="2301.44444")])
        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(), sources={"alpha": src}
        )
        cancelled = research_service.cancel_research_run(rid, "alice")
        assert cancelled["status"] == "cancelled"
        with pytest.raises(repo.ResearchConflict):
            await research_service.confirm_and_execute_research_run(
                rid, "alice", planner=FakePlanner(), sources={"alpha": src}
            )
        assert src.calls == []  # 取消后绝不检索

    async def test_cancel_during_retrieval_persists_nothing(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)

        def cancel_after_first(query):
            with _session(temp_db) as s:
                repo.cancel_run(s, rid, "alice")

        src = FakeSource(
            "alpha",
            results=[_paper("A", arxiv_id="2301.55555")],
            on_search=cancel_after_first,
        )
        planner = FakePlanner(queries=("q one", "q two"))
        await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": src}
        )
        summary = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=planner, sources={"alpha": src}
        )
        assert summary["status"] == "cancelled"
        assert src.calls == ["q one"]  # 取消后停止后续检索
        with _session(temp_db) as s:
            assert repo.list_project_papers(s, pid, "alice") == []
            assert repo.get_owned_run(s, rid, "alice").status == "cancelled"


# ---------------------------------------------------------------------------
# 6. 崩溃恢复与幂等
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    async def test_crash_then_resume_no_duplicate_papers(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        crash = FakeSource("alpha", crash=KeyboardInterrupt("模拟进程崩溃"))
        planner = FakePlanner()
        await research_service.generate_research_plan(
            rid, "alice", planner=planner, sources={"alpha": crash}
        )
        with pytest.raises(KeyboardInterrupt):
            await research_service.confirm_and_execute_research_run(
                rid, "alice", planner=planner, sources={"alpha": crash}
            )
        with _session(temp_db) as s:
            assert repo.get_owned_run(s, rid, "alice").status == "failed"

        fixed = FakeSource("alpha", results=[
            _paper("Crash Paper", arxiv_id="2301.66666"),
        ])
        retry = await research_service.resume_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": fixed}
        )
        assert retry["status"] == "completed"
        with _session(temp_db) as s:
            papers = repo.list_project_papers(s, pid, "alice")
            assert len(papers) == 1
            assert s.query(PaperRecordModel).count() == 1  # 重放不重复建论文

    async def test_resume_on_completed_is_idempotent(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Once", arxiv_id="2301.77777")])
        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(), sources={"alpha": src}
        )
        first = await research_service.confirm_and_execute_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": src}
        )
        assert first["status"] == "completed"
        again = await research_service.resume_research_run(
            rid, "alice", planner=FakePlanner(), sources={"alpha": src}
        )
        assert again["status"] == "completed"
        with _session(temp_db) as s:
            assert s.query(PaperRecordModel).count() == 1
            assert s.query(ProjectPaperModel).count() == 1

    async def test_second_run_same_project_no_duplicates(
        self, temp_db, ckpt, monkeypatch
    ):
        """同一项目第二次运行命中同一批论文：规范记录与关系都不重复。"""
        _wire_service(monkeypatch, temp_db)
        pid, rid1 = _make_project_run(temp_db)
        results = [_paper("Stable", arxiv_id="2301.88888", doi="10.8/stable")]
        await research_service.generate_research_plan(
            rid1, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha", results=results)},
        )
        await research_service.confirm_and_execute_research_run(
            rid1, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha", results=results)},
        )
        with _session(temp_db) as s:
            rid2 = repo.create_run(s, "alice", pid).id
        await research_service.generate_research_plan(
            rid2, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha", results=results)},
        )
        await research_service.confirm_and_execute_research_run(
            rid2, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha", results=results)},
        )
        with _session(temp_db) as s:
            assert s.query(PaperRecordModel).count() == 1
            assert s.query(ProjectPaperModel).count() == 1


# ---------------------------------------------------------------------------
# 7. Phoenix/可观测性关联
# ---------------------------------------------------------------------------


class TestTracingAssociation:
    async def test_business_span_carries_research_ids(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        recorded = []

        def fake_start(name, attributes=None):
            recorded.append((name, dict(attributes or {})))
            return None

        monkeypatch.setattr(research_runtime, "start_business_span", fake_start)
        monkeypatch.setattr(research_runtime, "set_span_attributes", lambda *a, **k: None)
        monkeypatch.setattr(research_runtime, "finish_span", lambda *a, **k: None)

        await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha")},
        )
        assert ("research_run", {
            "chatchat.research_run_id": rid,
            "chatchat.research_project_id": pid,
            "chatchat.research_phase": "plan",
        }) in recorded

    async def test_observability_disabled_is_noop(self, temp_db, ckpt, monkeypatch):
        """默认关闭可观测性时，真实 start_business_span 不影响执行。"""
        _wire_service(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        summary = await research_service.generate_research_plan(
            rid, "alice", planner=FakePlanner(),
            sources={"alpha": FakeSource("alpha")},
        )
        assert summary["status"] == "awaiting_confirmation"


# ---------------------------------------------------------------------------
# 8. API 表面（服务层打桩，验证路由与状态映射）
# ---------------------------------------------------------------------------


class TestApiSurface:
    def _project_run(self, client, h):
        pid = client.post("/research/projects", headers=h, json={
            "name": "api", "research_question": "q"}).json()["project_id"]
        rid = client.post(f"/research/projects/{pid}/runs", headers=h).json()["run_id"]
        return pid, rid

    def test_generate_plan_route(self, client, temp_db, monkeypatch):
        h = _headers(temp_db, "alice")
        pid, rid = self._project_run(client, h)

        seen = {}

        async def fake_generate(run_id, owner_id, **kw):
            seen["owner_id"] = owner_id
            return {"run_id": run_id, "status": "awaiting_confirmation"}

        monkeypatch.setattr(
            research_service, "generate_research_plan", fake_generate)
        r = client.post(f"/research/runs/{rid}/plan", headers=h)
        assert r.status_code == 200
        assert r.json()["status"] == "awaiting_confirmation"
        # owner 只来自服务端认证上下文
        assert seen["owner_id"]

    def test_update_plan_route_validates_structure(
        self, client, temp_db, monkeypatch, users
    ):
        h = _headers(temp_db, "alice")
        alice_id = users["alice"]["id"]
        pid, rid = self._project_run(client, h)
        monkeypatch.setattr(
            "chatchat.server.research.sources.build_default_sources",
            lambda: {"alpha": None, "local_kb": None},
        )
        with _session(temp_db) as s:
            repo.set_plan(s, rid, alice_id, "{}")  # -> awaiting_confirmation
        bad = client.put(f"/research/runs/{rid}/plan", headers=h, json={
            "plan": {"sub_questions": [{"question": "q", "queries": ["x"]}],
                     "sources": ["unknown_source"]}})
        assert bad.status_code == 400
        good = client.put(f"/research/runs/{rid}/plan", headers=h, json={
            "plan": {"summary": "s",
                     "sub_questions": [{"question": "q", "queries": ["x"]}],
                     "sources": ["alpha"]}})
        assert good.status_code == 200
        assert good.json()["plan"]["sub_questions"][0]["queries"] == ["x"]
        # 确认后不可编辑
        with _session(temp_db) as s:
            repo.confirm_run(s, rid, alice_id)
        late = client.put(f"/research/runs/{rid}/plan", headers=h, json={
            "plan": {"sub_questions": [{"question": "q", "queries": ["y"]}],
                     "sources": ["alpha"]}})
        assert late.status_code == 409

    def test_confirm_route_conflict_mapping(self, client, temp_db, monkeypatch):
        h = _headers(temp_db, "alice")
        pid, rid = self._project_run(client, h)

        async def fake_confirm(run_id, owner_id, **kw):
            raise repo.ResearchConflict("当前状态 pending 不可确认")

        monkeypatch.setattr(
            research_service, "confirm_and_execute_research_run", fake_confirm)
        r = client.post(f"/research/runs/{rid}/confirm", headers=h)
        assert r.status_code == 409

    def test_cancel_route_uses_service(self, client, temp_db, monkeypatch):
        _wire_service(monkeypatch, temp_db)
        h = _headers(temp_db, "alice")
        pid, rid = self._project_run(client, h)
        r = client.post(f"/research/runs/{rid}/cancel", headers=h)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_run_detail_lists_plan_and_stats(self, client, temp_db, monkeypatch, users):
        h = _headers(temp_db, "alice")
        alice_id = users["alice"]["id"]
        pid, rid = self._project_run(client, h)
        with _session(temp_db) as s:
            repo.set_plan(s, rid, alice_id, '{"summary": "x", "sub_questions": [], "sources": []}')
            repo.set_source_stats(s, rid, alice_id, [{"source": "alpha", "query": "q",
                                                      "hits": 2, "error": None}])
        detail = client.get(f"/research/runs/{rid}", headers=h)
        assert detail.status_code == 200
        body = detail.json()
        assert body["plan"]["summary"] == "x"
        assert body["source_stats"][0]["hits"] == 2
