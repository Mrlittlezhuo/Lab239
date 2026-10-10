"""证据闭环串联离线测试（任务 018 阶段 E）。

覆盖 ``read_selected_papers -> extract_evidence -> wait_for_evidence_review
-> analyze_coverage_and_conflicts -> build_comparison -> generate_report
-> wait_for_report_review -> finalize`` 这一段：

- 检索完成但未纳入任何论文：产物阶段如实收尾（不猜、不调模型、写明原因）；
- 纳入论文后基于同一运行继续：读原文→提取证据→挂起等待证据复核；
- 确认复核→覆盖分析→比较/冲突→报告→挂起等待报告复核→再确认→completed；
- 重放安全：重复执行不重复联网检索（来源调用次数不变）、不重复入库、
  不重复出版本（内容未变时报告不新增版本）；
- 复核门禁中取消 → cancelled，且不能再打开产物阶段（409）；
- 报告生成失败 → failed 且错误如实记录，修好生成器后 resume 重试可完成，
  已持久化的证据/比较不丢失；
- 跨用户不可触发（服务层 LookupError / API 404）、状态不允许 409；
- 各阶段写入关联 Agent Run 的步骤时间线；Phoenix 业务 span 关联
  研究运行/项目/Agent Run ID。

全部注入假计划器/假来源/假原文提供者/假提取器/假比较与报告生成器与
临时 checkpoint 存储，不连接真实模型、ArXiv、Phoenix、外部网络或用户数据。
"""
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from chatchat.server.agent_run.runtime import checkpoint as ckpt_mod
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import (
    MigrationError,
    MigrationRegistry,
    apply_migrations,
    current_version,
)
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.models.research_model import (  # noqa: F401
    ComparisonUnitModel,
    EvidenceCardModel,
    PaperRecordModel,
    ProjectPaperModel,
    ResearchConflictModel,
    ResearchReportModel,
    ResearchRunModel,
)
from chatchat.server.db.repository import agent_run_repository
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service as auth_service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.api_server.research_routes import research_router
from chatchat.server.research import runtime as research_runtime
from chatchat.server.research import service as research_service
from chatchat.server.research.comparison import ComparisonDraft, ConflictDraft
from chatchat.server.research.evidence import parse_evidence_payload
from chatchat.server.research.fulltext import (
    FullTextError,
    FullTextProvider,
    units_from_documents,
)
from chatchat.server.research.papers import PaperCandidate
from chatchat.server.research.plan import ResearchPlan, SubQuestion
from chatchat.server.research.report import parse_report_payload


# ---------------------------------------------------------------------------
# fixtures（与任务 017 工作流测试一致：临时库 + 临时 checkpoint + 假 HTTP 客户端）
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "research-artifact-loop-unit-test-secret",
    )
    return "research-artifact-loop-unit-test-secret"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "artifact_loop.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 12
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@pytest.fixture
async def ckpt(tmp_path):
    store = await ckpt_mod.initialize_agent_checkpoint_store(
        str(tmp_path / "artifact_ckpt.sqlite")
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
        u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def users(temp_db):
    return {"alice": _mk_user(temp_db, "alice"), "bob": _mk_user(temp_db, "bob")}


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

    import fastapi.dependencies.utils
    import fastapi.routing

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


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakePlanner:
    def __init__(self, queries=("wireless localization",)):
        self.queries = list(queries)
        self.calls = 0

    def generate(self, research_question, scope, available_sources):
        self.calls += 1
        return ResearchPlan(
            summary="计划摘要",
            sub_questions=[SubQuestion("RAG 用哪些评测指标？", list(self.queries))],
            sources=sorted(available_sources),
        )


def _paper(title, arxiv_id=None, year=2023):
    return PaperCandidate(
        title=title, source="", arxiv_id=arxiv_id, year=year,
        abstract="We evaluate retrieval-augmented generation on open QA.",
    )


class FakeSource:
    def __init__(self, name, results=()):
        self.name = name
        self.results = list(results)
        self.calls = []

    async def search(self, query, limit=10):
        self.calls.append(query)
        out = []
        for r in self.results:
            c = _paper(r.title, arxiv_id=r.arxiv_id, year=r.year)
            c.source = self.name
            out.append(c)
        return out


PAGE1 = (
    "Introduction\n"
    "Retrieval-augmented generation combines a retriever with a generator. "
    "The retriever fetches passages that condition the generator."
)
PAGE2 = (
    "3 Evaluation Metrics\n"
    "We report exact match and F1 on three open-domain QA datasets. "
    "Exact match measures whether the answer string matches the gold answer."
)


def _docs():
    return [
        type("Doc", (), {"page_content": PAGE1, "metadata": {"page": 0}}),
        type("Doc", (), {"page_content": PAGE2, "metadata": {"page": 1}}),
    ]


class FakePdfProvider(FullTextProvider):
    name = "pdf"

    def __init__(self, docs=None, error=None):
        self.docs = docs if docs is not None else _docs()
        self.error = error
        self.calls = 0

    def fetch(self, paper):
        self.calls += 1
        if self.error:
            raise FullTextError(self.error)
        return units_from_documents(self.docs, source_kind="pdf")


EVIDENCE_PAYLOAD = {
    "evidence": [
        {
            "evidence_type": "metric",
            "quote": "We report exact match and F1 on three open-domain QA datasets.",
            "summary": "在三个开放域 QA 数据集上报告 exact match 与 F1。",
        }
    ]
}


class FakeExtractor:
    model_name = "fake-extract-model"

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else EVIDENCE_PAYLOAD
        self.calls = 0

    def extract(self, *, question, sub_question, paper, units):
        self.calls += 1
        return parse_evidence_payload(self.payload, units)


class FakeComparisonGenerator:
    """按传入的真实证据构造比较与冲突（引用全部可反查）。"""

    model_name = "fake-compare-model"

    def __init__(self):
        self.compare_calls = 0
        self.conflict_calls = 0

    def generate_comparisons(self, *, question, dimensions, evidence):
        self.compare_calls += 1
        drafts = [
            ComparisonDraft(
                paper_id=ref.paper_id,
                dimension_id="metric",
                content=f"{ref.paper_title} 以 exact match 为主指标",
                evidence_ids=[ref.evidence_id],
            )
            for ref in evidence
        ]
        return drafts, []

    def detect_conflicts(self, *, question, evidence):
        self.conflict_calls += 1
        if len(evidence) < 2:
            return [], []
        left, right = evidence[0], evidence[1]
        return [
            ConflictDraft(
                topic="指标口径不一致",
                description="两篇论文对主指标的口径不同。",
                side_a=[left.evidence_id],
                side_b=[right.evidence_id],
                dimension_id="metric",
                sub_question_id="sq1",
            )
        ], []


class FakeReportGenerator:
    model_name = "fake-report-model"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def generate(self, *, question, scope, coverage, comparisons, conflicts, evidence):
        self.calls += 1
        if self.fail:
            raise RuntimeError("报告模型不可用")
        payload = {
            "title": "RAG 指标综述报告",
            "overview": "两篇论文的指标口径不同。",
            "conclusions": [
                {
                    "text": "有论文以 exact match 为主指标",
                    "evidence_ids": [e.evidence_id for e in evidence[:1]],
                },
                {"text": "整体趋势仍需验证", "evidence_ids": []},
            ],
            "gaps": ["缺少中文数据集上的评测"],
            "next_steps": ["补充检索中文评测"],
        }
        return parse_report_payload(payload, evidence)


def _fakes(source, extractor=None, provider=None, comparison=None, report=None):
    """服务层调用参数（每个入口都传同一批假件，便于断言调用次数）。"""
    return {
        "planner": FakePlanner(),
        "sources": {"alpha": source},
        "extractor": extractor if extractor is not None else FakeExtractor(),
        "unit_providers": [provider] if provider is not None else [FakePdfProvider()],
        "comparison_generator": comparison if comparison is not None
        else FakeComparisonGenerator(),
        "report_generator": report if report is not None else FakeReportGenerator(),
    }


def _wire(monkeypatch, db):
    monkeypatch.setattr(research_service, "_session_factory", db["Session"])


def _make_project_run(db, owner="alice"):
    with _session(db) as s:
        project = repo.create_project(s, owner, "RAG 指标综述", "RAG 用哪些评测指标？")
        run = repo.create_run(s, owner, project.id)
        return project.id, run.id


async def _retrieve_to_completed(db, rid, source):
    """任务 017 段：生成计划→确认→检索入库→（无纳入论文）产物收尾。"""
    planner = FakePlanner()
    await research_service.generate_research_plan(
        rid, "alice", planner=planner, sources={"alpha": source}
    )
    return await research_service.confirm_and_execute_research_run(
        rid, "alice", planner=planner, sources={"alpha": source}
    )


def _include_all_papers(db, pid, owner="alice"):
    with _session(db) as s:
        ids = [pp.id for pp, _ in repo.list_project_papers(s, pid, owner)]
        for pp_id in ids:
            repo.set_screening(s, pp_id, owner, "included", "人工纳入")
        return ids


async def _drive_artifacts(rid, kwargs):
    """产物闭环：artifacts → 证据复核确认 → 报告复核确认，返回终态摘要。"""
    first = await research_service.run_research_artifacts(rid, "alice", **kwargs)
    assert first["status"] == "awaiting_confirmation"
    assert first["stage"] == "evidence_review"
    mid = await research_service.confirm_and_execute_research_run(rid, "alice", **kwargs)
    assert mid["status"] == "awaiting_confirmation"
    assert mid["stage"] == "report_review"
    return await research_service.confirm_and_execute_research_run(rid, "alice", **kwargs)


# ---------------------------------------------------------------------------
# 0. v12 迁移：research_run 阶段约束扩展
# ---------------------------------------------------------------------------


def _registry_upto(last_version):
    registry = MigrationRegistry()
    from chatchat.server.db.migrate import migrations as migrations_mod

    for version, name, upgrade in migrations_mod._MIGRATIONS[:last_version]:
        registry.register(version, name, upgrade)
    return registry


_INSERT_OLD_RUN = (
    "INSERT INTO research_run (id, project_id, owner_id, plan_json, status, stage, "
    "agent_run_id, sources_json, summary_json, error, create_time, start_time, "
    "end_time, update_time, parent_run_id, round_no) VALUES "
    "('r-old', 'p-old', 'alice', '', 'running', 'persist', NULL, '[]', "
    "'{\"new_papers\": 2}', NULL, CURRENT_TIMESTAMP, NULL, NULL, "
    "CURRENT_TIMESTAMP, NULL, 2)"
)


class TestV12Migration:
    """证据闭环阶段需要放宽 ``research_run.stage`` CHECK（v12 重建表）。"""

    def test_v11_db_upgrades_preserving_rows_and_indexes(self, tmp_path):
        path = tmp_path / "v11_to_v12.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(11), conn)
            assert current_version(conn) == 11
            conn.exec_driver_sql(_INSERT_OLD_RUN)
            conn.commit()
            # 旧约束确实拒绝新阶段（这正是必须追加 v12 的原因）
            with pytest.raises(IntegrityError):
                conn.exec_driver_sql(
                    "UPDATE research_run SET stage = 'evidence_review' WHERE id = 'r-old'"
                )
            conn.rollback()

        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [12]
            assert current_version(conn) == 12
            row = conn.exec_driver_sql(
                "SELECT id, status, stage, summary_json, round_no FROM research_run"
            ).first()
            assert row == ("r-old", "running", "persist", '{"new_papers": 2}', 2)
            conn.exec_driver_sql(
                "UPDATE research_run SET stage = 'evidence_review' WHERE id = 'r-old'"
            )
            conn.commit()
            names = {
                r[0]
                for r in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'research_run'"
                )
            }
            assert {"ix_research_run_project", "ix_research_run_owner_create"} <= names
            # 函数级幂等：已扩展的约束不再重建
            from chatchat.server.db.migrate.migrations import _v12_research_run_stage

            _v12_research_run_stage(conn)
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM research_run").scalar() == 1

        with engine.connect() as conn:
            assert apply_migrations(build_registry(), conn).applied == []
        engine.dispose()

    def test_unexpected_column_aborts_rebuild_and_keeps_data(self, tmp_path):
        """遇到预期之外的列必须失败回滚，绝不静默丢列或丢行。"""
        path = tmp_path / "v11_extra.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(11), conn)
            conn.exec_driver_sql(_INSERT_OLD_RUN)
            conn.exec_driver_sql("ALTER TABLE research_run ADD COLUMN mystery VARCHAR(10)")
            conn.commit()
        with engine.connect() as conn:
            with pytest.raises(MigrationError):
                apply_migrations(build_registry(), conn)
            assert current_version(conn) == 11
            row = conn.exec_driver_sql(
                "SELECT id, stage, mystery FROM research_run"
            ).first()
            assert row == ("r-old", "persist", None)
        engine.dispose()


# ---------------------------------------------------------------------------
# 1. 没有纳入论文时如实收尾
# ---------------------------------------------------------------------------


class TestNoIncludedPapers:
    async def test_artifacts_stop_honestly_without_models(self, temp_db, ckpt, monkeypatch):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00001")])
        done = await _retrieve_to_completed(temp_db, rid, src)
        assert done["status"] == "completed"

        extractor = FakeExtractor()
        provider = FakePdfProvider()
        summary = await research_service.run_research_artifacts(
            rid, "alice", **_fakes(src, extractor=extractor, provider=provider)
        )
        assert summary["status"] == "completed"
        assert summary["stage"] == "done"
        note = summary["summary"].get("artifact_note") or ""
        assert "没有已纳入的论文" in note
        assert extractor.calls == 0  # 不猜、不自动纳入，绝不调用提取模型
        assert provider.calls == 0
        with _session(temp_db) as s:
            assert repo.list_evidence(s, pid, "alice") == []
            assert s.query(ResearchReportModel).count() == 0


# ---------------------------------------------------------------------------
# 2. 完整闭环：两个人工确认点
# ---------------------------------------------------------------------------


class TestArtifactLoop:
    async def test_loop_pauses_at_both_reviews_then_completes(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[
            _paper("Paper A", arxiv_id="2301.00001"),
            _paper("Paper B", arxiv_id="2301.00002"),
        ])
        assert (await _retrieve_to_completed(temp_db, rid, src))["status"] == "completed"
        assert src.calls == ["wireless localization"]
        _include_all_papers(temp_db, pid)

        extractor, provider = FakeExtractor(), FakePdfProvider()
        comparison, report = FakeComparisonGenerator(), FakeReportGenerator()
        kwargs = _fakes(src, extractor=extractor, provider=provider,
                        comparison=comparison, report=report)

        first = await research_service.run_research_artifacts(rid, "alice", **kwargs)
        assert first["status"] == "awaiting_confirmation"
        assert first["stage"] == "evidence_review"
        assert src.calls == ["wireless localization"]  # 不重复联网检索
        assert provider.calls == 2 and extractor.calls == 2
        with _session(temp_db) as s:
            cards = repo.list_evidence(s, pid, "alice")
            assert len(cards) == 2
            assert all(c.quote_verified and c.level == "fulltext" for c in cards)
            # 证据复核前不产出比较与报告
            assert s.query(ComparisonUnitModel).count() == 0
            assert s.query(ResearchReportModel).count() == 0

        mid = await research_service.confirm_and_execute_research_run(rid, "alice", **kwargs)
        assert mid["status"] == "awaiting_confirmation"
        assert mid["stage"] == "report_review"
        with _session(temp_db) as s:
            assert s.query(ComparisonUnitModel).count() == 2
            assert s.query(ResearchConflictModel).count() == 1
            reports = s.query(ResearchReportModel).all()
            assert len(reports) == 1 and reports[0].version == 1
            assert "exact match" in reports[0].content

        final = await research_service.confirm_and_execute_research_run(rid, "alice", **kwargs)
        assert final["status"] == "completed"
        assert final["stage"] == "done"
        artifacts = final["summary"]["artifacts"]
        assert artifacts["evidence"] == 2
        assert artifacts["comparisons"] == 2
        assert artifacts["conflicts"] == 1
        assert artifacts["reports"] == 1
        assert report.calls == 1
        with _session(temp_db) as s:
            run = repo.get_owned_run(s, rid, "alice")
            assert run.status == "completed" and run.stage == "done"
            agent = s.get(AgentRunModel, run.agent_run_id)
            assert agent is not None and agent.status == "completed"

    async def test_agent_run_steps_record_each_phase(self, temp_db, ckpt, monkeypatch):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00003")])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)
        final = await _drive_artifacts(rid, _fakes(src))
        assert final["status"] == "completed"

        with _session(temp_db) as s:
            run = repo.get_owned_run(s, rid, "alice")
            steps = agent_run_repository.get_owned_steps(s, run.agent_run_id, "alice")
            tools = {st.tool_name for st in steps}
        for expected in (
            "research.read_selected_papers",
            "research.extract_evidence",
            "research.wait_for_evidence_review",
            "research.analyze_coverage_and_conflicts",
            "research.build_comparison",
            "research.generate_report",
            "research.wait_for_report_review",
        ):
            assert expected in tools

    async def test_business_span_carries_run_project_and_agent_run(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00004")])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)

        recorded = []

        def fake_start(name, attributes=None):
            recorded.append((name, dict(attributes or {})))
            return None

        monkeypatch.setattr(research_runtime, "start_business_span", fake_start)
        monkeypatch.setattr(research_runtime, "set_span_attributes", lambda *a, **k: None)
        monkeypatch.setattr(research_runtime, "finish_span", lambda *a, **k: None)

        summary = await research_service.run_research_artifacts(rid, "alice", **_fakes(src))
        assert summary["status"] == "awaiting_confirmation"
        spans = [attrs for name, attrs in recorded if name == "research_run"]
        assert any(
            a.get("chatchat.research_run_id") == rid
            and a.get("chatchat.research_project_id") == pid
            and a.get("chatchat.research_phase") == "execute"
            and a.get("chatchat.agent_run_id")
            for a in spans
        )


# ---------------------------------------------------------------------------
# 3. 重放安全：不重复检索、不重复入库、不出版本噪声
# ---------------------------------------------------------------------------


class TestReplaySafety:
    async def test_second_artifact_run_does_not_duplicate_products(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[
            _paper("Paper A", arxiv_id="2301.00005"),
            _paper("Paper B", arxiv_id="2301.00006"),
        ])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)

        extractor, provider = FakeExtractor(), FakePdfProvider()
        comparison, report = FakeComparisonGenerator(), FakeReportGenerator()
        kwargs = _fakes(src, extractor=extractor, provider=provider,
                        comparison=comparison, report=report)
        assert (await _drive_artifacts(rid, kwargs))["status"] == "completed"
        first_evidence = extractor.calls
        first_reports = None
        with _session(temp_db) as s:
            first_reports = s.query(ResearchReportModel).count()
            assert first_reports == 1

        # 用户再次点击“生成证据与报告”：产物不重复，报告不新增噪声版本
        again = await _drive_artifacts(rid, kwargs)
        assert again["status"] == "completed"
        assert src.calls == ["wireless localization"]  # 全程只检索一次
        with _session(temp_db) as s:
            assert len(repo.list_project_papers(s, pid, "alice")) == 2
            assert s.query(PaperRecordModel).count() == 2
            assert len(repo.list_evidence(s, pid, "alice")) == 2
            assert s.query(ComparisonUnitModel).count() == 2
            assert s.query(ResearchConflictModel).count() == 1
            assert s.query(ResearchReportModel).count() == first_reports
        assert extractor.calls == first_evidence + 2  # 重跑幂等：unchanged，不新增卡片


# ---------------------------------------------------------------------------
# 4. 取消与状态机
# ---------------------------------------------------------------------------


class TestGatesAndStateMachine:
    async def test_cancel_at_evidence_gate_blocks_reopen(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00007")])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)

        kwargs = _fakes(src)
        first = await research_service.run_research_artifacts(rid, "alice", **kwargs)
        assert first["status"] == "awaiting_confirmation"

        cancelled = research_service.cancel_research_run(rid, "alice")
        assert cancelled["status"] == "cancelled"
        with pytest.raises(repo.ResearchConflict):
            await research_service.run_research_artifacts(rid, "alice", **kwargs)
        with pytest.raises(repo.ResearchConflict):
            await research_service.confirm_and_execute_research_run(rid, "alice", **kwargs)
        with _session(temp_db) as s:
            assert repo.get_owned_run(s, rid, "alice").status == "cancelled"

    async def test_artifacts_require_completed_run(self, temp_db, ckpt, monkeypatch):
        _wire(monkeypatch, temp_db)
        _, rid = _make_project_run(temp_db)
        with pytest.raises(repo.ResearchConflict):
            await research_service.run_research_artifacts(rid, "alice", **_fakes(FakeSource("alpha")))

    async def test_cross_user_cannot_trigger_artifacts(self, temp_db, ckpt, monkeypatch):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        with pytest.raises(LookupError):
            await research_service.run_research_artifacts(rid, "bob", **_fakes(FakeSource("alpha")))


# ---------------------------------------------------------------------------
# 5. 失败与恢复
# ---------------------------------------------------------------------------


class TestFailureAndRecovery:
    async def test_report_failure_is_recorded_and_retry_completes(
        self, temp_db, ckpt, monkeypatch
    ):
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00008")])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)

        broken = _fakes(src, report=FakeReportGenerator(fail=True))
        await research_service.run_research_artifacts(rid, "alice", **broken)
        failed = await research_service.confirm_and_execute_research_run(rid, "alice", **broken)
        assert failed["status"] == "failed"
        assert "报告生成失败" in (failed["error"] or "")
        with _session(temp_db) as s:
            # 之前阶段的产物保留：证据与比较不因为报告失败而丢失
            assert len(repo.list_evidence(s, pid, "alice")) == 1
            assert s.query(ComparisonUnitModel).count() == 1
            assert s.query(ResearchReportModel).count() == 0

        fixed = _fakes(src)
        resumed = await research_service.resume_research_run(rid, "alice", **fixed)
        # 证据已复核过：重放不再要求重复复核，直接补出报告并挂起报告复核
        assert resumed["status"] == "awaiting_confirmation"
        assert resumed["stage"] == "report_review"
        done = await research_service.confirm_and_execute_research_run(rid, "alice", **fixed)
        assert done["status"] == "completed"
        with _session(temp_db) as s:
            assert s.query(ResearchReportModel).count() == 1
            assert len(repo.list_evidence(s, pid, "alice")) == 1

    async def test_fulltext_provider_failure_degrades_without_faking(
        self, temp_db, ckpt, monkeypatch
    ):
        """原文取不到：降级为摘要级证据，绝不编造页码。"""
        _wire(monkeypatch, temp_db)
        pid, rid = _make_project_run(temp_db)
        src = FakeSource("alpha", results=[_paper("Paper A", arxiv_id="2301.00009")])
        await _retrieve_to_completed(temp_db, rid, src)
        _include_all_papers(temp_db, pid)

        provider = FakePdfProvider(error="PDF 下载失败")
        # 摘要级证据只能引用摘要里真实出现过的句子
        payload = {
            "evidence": [
                {
                    "evidence_type": "metric",
                    "quote": "We evaluate retrieval-augmented generation on open QA.",
                    "summary": "在开放域 QA 上评测 RAG。",
                }
            ]
        }
        kwargs = _fakes(src, provider=provider, extractor=FakeExtractor(payload))
        first = await research_service.run_research_artifacts(rid, "alice", **kwargs)
        assert first["status"] == "awaiting_confirmation"
        with _session(temp_db) as s:
            cards = repo.list_evidence(s, pid, "alice")
            assert cards, "取不到全文仍应给出摘要级证据"
            assert all(c.level == "abstract" for c in cards)
            for card in cards:
                locator = json.loads(card.locator_json or "{}")
                assert locator.get("page") is None, "摘要级证据绝不编造页码"
            papers = repo.list_project_papers(s, pid, "alice")
            assert all(pp.fulltext_state == "abstract" for pp, _ in papers)


# ---------------------------------------------------------------------------
# 6. API 表面
# ---------------------------------------------------------------------------


class TestApiSurface:
    def _project_run(self, client, headers):
        pid = client.post("/research/projects", headers=headers, json={
            "name": "api", "research_question": "q"}).json()["project_id"]
        rid = client.post(f"/research/projects/{pid}/runs", headers=headers).json()["run_id"]
        return pid, rid

    def test_artifacts_route_maps_conflict(self, client, temp_db, monkeypatch, users):
        _wire(monkeypatch, temp_db)
        headers = _headers(temp_db, "alice")
        _, rid = self._project_run(client, headers)

        async def fake_artifacts(run_id, owner_id, **kwargs):
            raise repo.ResearchConflict("当前状态 pending 不能重新打开生成产物")

        monkeypatch.setattr(research_service, "run_research_artifacts", fake_artifacts)
        resp = client.post(f"/research/runs/{rid}/artifacts", headers=headers)
        assert resp.status_code == 409
        assert "不能重新打开" in resp.json()["detail"]

    def test_artifacts_route_is_owner_scoped(self, client, temp_db, monkeypatch, users):
        _wire(monkeypatch, temp_db)
        alice = _headers(temp_db, "alice")
        bob = _headers(temp_db, "bob")
        _, rid = self._project_run(client, alice)
        # bob 触发：404（不区分不存在与无权，避免枚举）
        resp = client.post(f"/research/runs/{rid}/artifacts", headers=bob)
        assert resp.status_code == 404
        # 未登录：401/403
        anon = client.post(f"/research/runs/{rid}/artifacts")
        assert anon.status_code in (401, 403)
