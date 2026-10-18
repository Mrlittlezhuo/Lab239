"""证据覆盖、比较与冲突离线测试（任务 018 阶段 B）。

覆盖：

- v10 迁移：新建 ``comparison_unit``/``research_conflict``，为
  ``research_project`` 加比较维度、为 ``research_run`` 加轮次列；
  v9 存量库升级保留旧证据、重复升级幂等；
- 比较维度：默认维度、用户自定义（去重/上限/非法 JSON 回落）；
- 比较单元：必须引用本项目真实存在的证据；引用不存在或跨论文的 id 被
  拒绝；重复生成幂等；内容变化时版本递增；
- 冲突记录：两侧都必须引用真实证据、不得引用同一张卡片；重复检测幂等；
  人工裁决写入结论；
- 覆盖检查：covered/thin/missing 判定、缺失维度、被驳回证据不计入、
  建议复用任务 017 计划里的检索式、结果可重现；
- 补充检索：复用任务 017 运行入口创建可见新运行，轮次有上限（超限 409）；
- 所有者隔离：跨用户无法读写比较、冲突与补充检索入口（API 404）。

全部使用临时 SQLite + 假生成器，不连接真实模型、外部网络或用户数据。
"""
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import MigrationRegistry, apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.research_model import (  # noqa: F401
    ComparisonUnitModel,
    EvidenceCardModel,
    PaperRecordModel,
    ResearchConflictModel,
    ResearchProjectModel,
    ResearchReportModel,
    ResearchRunModel,
)
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.api_server.research_routes import research_router
from chatchat.server.research import comparison_service
from chatchat.server.research.comparison import (
    ComparisonGenerator,
    EvidenceRef,
    parse_comparison_payload,
    parse_conflict_payload,
)
from chatchat.server.research.coverage import COVERED, MISSING, THIN, build_coverage


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "research-compare-unit-test-secret-00000"
    )
    return "research-compare-unit-test-secret-00000"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "compare_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 18
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def _mk_user(db, username):
    S = db["Session"]
    s = S()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username.title(),
            password="password12345678",
            role="user",
        )
        u.must_change_password = False
        s.commit()
        return {"id": u.id, "username": u.username, "auth_version": u.auth_version}
    finally:
        s.close()


def _headers(db, username):
    S = db["Session"]
    s = S()
    try:
        u = s.query(UserModel).filter(UserModel.username == username).one_or_none()
        if u is None:
            u = _mk_user(db, username)
            s = S()
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


def _registry_upto(last_version):
    registry = MigrationRegistry()
    from chatchat.server.db.migrate import migrations as migrations_mod

    for version, name, upgrade in migrations_mod._MIGRATIONS[:last_version]:
        registry.register(version, name, upgrade)
    return registry


PLAN = {
    "summary": "RAG 综述",
    "sub_questions": [
        {"question": "RAG 用哪些评价指标？", "queries": ["RAG evaluation metrics", "open-domain QA metrics"]},
        {"question": "RAG 的主要局限是什么？", "queries": ["retrieval augmented generation limitations"]},
    ],
    "sources": ["arxiv", "local_kb"],
}


def _mk_project(db, owner_id, plan_json=""):
    with _session(db) as s:
        project = repo.create_project(
            s, owner_id, name="RAG 综述", research_question="RAG 的指标与局限？"
        )
        run = repo.create_run(s, owner_id, project.id)
        if plan_json:
            repo.set_plan(s, run.id, owner_id, plan_json)
        s.commit()
        return project.id, run.id


def _mk_paper_with_evidence(db, owner_id, project_id, title, *, evidence_type="metric",
                            level="fulltext", verified=True, review_status="pending",
                            quote="Exact match measures the answer string."):
    """建论文 + 纳入 + 一张证据卡片，返回 (paper_id, evidence_id)。"""
    with _session(db) as s:
        paper, _ = repo.upsert_paper(
            s, {"title": title, "authors": "A. Author", "year": 2023, "abstract": "abs", "source": "arxiv"}
        )
        pp = repo.link_project_paper(s, project_id, owner_id, paper.id, source="arxiv")
        pp.screening_status = "included"
        card, _action = repo.upsert_evidence(
            s,
            project_id=project_id,
            owner_id=owner_id,
            paper_id=paper.id,
            sub_question_id="sq1",
            evidence_type=evidence_type,
            quote=quote,
            summary=f"{title} 的{evidence_type}证据",
            level=level,
            source_kind="pdf",
        )
        if review_status != "pending":
            repo.review_evidence(s, card.id, owner_id, review_status=review_status)
        if not verified:
            repo.set_evidence_verified(s, card.id, owner_id, False)
        s.commit()
        return paper.id, card.id


# ---------------------------------------------------------------------------
# 1. v10 迁移
# ---------------------------------------------------------------------------


class TestV10Migration:
    def test_creates_tables_and_columns(self, temp_db):
        with temp_db["engine"].connect() as conn:
            tables = set(
                conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).scalars()
            )
            assert {"comparison_unit", "research_conflict"} <= tables
            proj_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(research_project)")}
            assert "dimensions_json" in proj_cols
            run_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(research_run)")}
            assert {"parent_run_id", "round_no"} <= run_cols
            cu_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(comparison_unit)")}
            assert {"evidence_ids_json", "dimension_id"} <= cu_cols

    def test_v9_db_upgrades_preserving_evidence(self, tmp_path):
        """v9 存量库升级到 v10：只加表加列，旧证据行完整保留。"""
        path = tmp_path / "v9_to_v10.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(9), conn)
            conn.execute(
                text(
                    "INSERT INTO evidence_card (id, project_id, paper_id, owner_id, content, "
                    "locator_json, version, quote, dedup_key, create_time, update_time) VALUES "
                    "('e1', 'p1', 'pa1', 'u1', '旧证据', '', 2, '旧原文', 'k1', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [10, 11, 12, 13, 14, 15, 16, 17, 18]
            row = conn.execute(
                text("SELECT content, quote, version FROM evidence_card WHERE id='e1'")
            ).fetchone()
            assert row == ("旧证据", "旧原文", 2)
            assert conn.execute(text("SELECT COUNT(*) FROM comparison_unit")).scalar() == 0
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. 比较维度
# ---------------------------------------------------------------------------


class TestDimensions:
    def test_defaults_when_unset(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            dims = repo.get_project_dimensions(s, project_id, owner)
            assert [d["id"] for d in dims] == [d["id"] for d in repo.DEFAULT_DIMENSIONS]

    def test_user_can_customize(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            dims = repo.set_project_dimensions(
                s,
                project_id,
                owner,
                [{"id": "dataset", "label": "数据集"}, {"id": "cost", "label": "推理成本"},
                 {"id": "cost", "label": "重复项"}],
            )
            assert [d["id"] for d in dims] == ["dataset", "cost"]
            assert repo.get_project_dimensions(s, project_id, owner) == dims

    def test_invalid_json_falls_back_to_default(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            project = repo.get_owned_project(s, project_id, owner)
            project.dimensions_json = "{不是 JSON"
            s.commit()
            assert [d["id"] for d in repo.get_project_dimensions(s, project_id, owner)] == [
                d["id"] for d in repo.DEFAULT_DIMENSIONS
            ]


# ---------------------------------------------------------------------------
# 3. 比较单元：必须可追溯到证据
# ---------------------------------------------------------------------------


class TestComparisonUnits:
    def test_unit_without_evidence_is_rejected(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            paper_id, _card = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            result = repo.replace_comparison_units(
                s,
                project_id=project_id,
                owner_id=owner,
                units=[
                    {"paper_id": paper_id, "dimension_id": "metric", "content": "无依据的总结", "evidence_ids": []},
                    {"paper_id": paper_id, "dimension_id": "method", "content": "引用不存在的证据",
                     "evidence_ids": ["does-not-exist"]},
                ],
            )
            assert result["written"] == 0
            assert len(result["rejected"]) == 2
            assert repo.list_comparison_units(s, project_id, owner) == []

    def test_unit_is_idempotent_and_versioned(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            paper_id, card_id = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            unit = {
                "paper_id": paper_id,
                "dimension_id": "metric",
                "content": "使用 exact match 与 F1",
                "evidence_ids": [card_id],
            }
            first = repo.replace_comparison_units(
                s, project_id=project_id, owner_id=owner, units=[unit]
            )
            assert first["written"] == 1
            again = repo.replace_comparison_units(
                s, project_id=project_id, owner_id=owner, units=[unit]
            )
            assert again["written"] == 0 and again["skipped"] == 1
            changed = dict(unit, content="使用 exact match、F1 与召回率")
            repo.replace_comparison_units(s, project_id=project_id, owner_id=owner, units=[changed])
            units = repo.list_comparison_units(s, project_id, owner)
            assert len(units) == 1 and units[0].version == 2
            assert json.loads(units[0].evidence_ids_json) == [card_id]

    def test_foreign_evidence_reference_dropped(self, temp_db):
        """Bob 的证据 id 不能出现在 Alice 的比较单元里。"""
        alice = _mk_user(temp_db, "alice")["id"]
        bob = _mk_user(temp_db, "bob")["id"]
        alice_project, _ = _mk_project(temp_db, alice)
        bob_project, _ = _mk_project(temp_db, bob)
        alice_paper, _ = _mk_paper_with_evidence(temp_db, alice, alice_project, "Paper A")
        _bob_paper, bob_card = _mk_paper_with_evidence(temp_db, bob, bob_project, "Paper B")
        with _session(temp_db) as s:
            result = repo.replace_comparison_units(
                s,
                project_id=alice_project,
                owner_id=alice,
                units=[
                    {
                        "paper_id": alice_paper,
                        "dimension_id": "metric",
                        "content": "混入他人证据",
                        "evidence_ids": [bob_card],
                    }
                ],
            )
            assert result["written"] == 0
            assert any("没有引用本项目内真实存在的证据卡片" in r for r in result["rejected"])

    def test_delete_project_cascades_comparisons_and_conflicts(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            paper_a, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            paper_b, card_b = _mk_paper_with_evidence(
                temp_db, owner, project_id, "Paper B", quote="F1 is the primary metric."
            )
            repo.replace_comparison_units(
                s,
                project_id=project_id,
                owner_id=owner,
                units=[{"paper_id": paper_a, "dimension_id": "metric", "content": "x", "evidence_ids": [card_a]}],
            )
            repo.record_conflict(
                s,
                project_id=project_id,
                owner_id=owner,
                topic="指标口径不一致",
                side_a=[{"evidence_id": card_a}],
                side_b=[{"evidence_id": card_b}],
            )
            repo.delete_owned_project(s, project_id, owner)
            assert s.query(ComparisonUnitModel).count() == 0
            assert s.query(ResearchConflictModel).count() == 0
            assert s.query(PaperRecordModel).count() == 2


# ---------------------------------------------------------------------------
# 4. 冲突记录
# ---------------------------------------------------------------------------


class TestConflicts:
    def test_both_sides_must_reference_evidence(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            paper_a, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            with pytest.raises(ValueError):
                repo.record_conflict(
                    s,
                    project_id=project_id,
                    owner_id=owner,
                    topic="无依据冲突",
                    side_a=[{"evidence_id": card_a}],
                    side_b=[{"evidence_id": "nope"}],
                )
            with pytest.raises(ValueError):
                repo.record_conflict(
                    s,
                    project_id=project_id,
                    owner_id=owner,
                    topic="同一证据",
                    side_a=[{"evidence_id": card_a}],
                    side_b=[{"evidence_id": card_a}],
                )

    def test_conflict_is_idempotent_and_resolvable(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            _pa, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            _pb, card_b = _mk_paper_with_evidence(
                temp_db, owner, project_id, "Paper B", quote="F1 is the primary metric."
            )
            conflict, action = repo.record_conflict(
                s,
                project_id=project_id,
                owner_id=owner,
                topic="指标口径不一致",
                description="A 用 exact match，B 用 F1",
                dimension_id="metric",
                sub_question_id="sq1",
                side_a=[{"evidence_id": card_a}],
                side_b=[{"evidence_id": card_b}],
            )
            assert action == "created"
            # A/B 互换不产生第二条
            again, action2 = repo.record_conflict(
                s,
                project_id=project_id,
                owner_id=owner,
                topic="指标口径不一致",
                dimension_id="metric",
                sub_question_id="sq1",
                side_a=[{"evidence_id": card_b}],
                side_b=[{"evidence_id": card_a}],
            )
            assert action2 == "unchanged" and again.id == conflict.id
            assert len(repo.list_conflicts(s, project_id, owner)) == 1

            resolved = repo.resolve_conflict(s, conflict.id, owner, "两篇口径不同，按任务定义取 exact match")
            assert resolved.status == "resolved" and resolved.version == 2
            assert repo.list_conflicts(s, project_id, owner, status_filter="open") == []

    def test_conflict_sides_keep_quote_snapshots(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _ = _mk_project(temp_db, owner)
            paper_a, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
            paper_b, card_b = _mk_paper_with_evidence(
                temp_db, owner, project_id, "Paper B", quote="F1 is the primary metric."
            )
            conflict, _ = repo.record_conflict(
                s,
                project_id=project_id,
                owner_id=owner,
                topic="指标",
                side_a=[{"evidence_id": card_a}],
                side_b=[{"evidence_id": card_b}],
            )
            side_a = json.loads(conflict.side_a_json)
            side_b = json.loads(conflict.side_b_json)
            assert side_a[0]["paper_id"] == paper_a
            assert side_a[0]["quote"] == "Exact match measures the answer string."
            assert side_b[0]["paper_id"] == paper_b


# ---------------------------------------------------------------------------
# 5. 解析层：无效引用被丢弃
# ---------------------------------------------------------------------------


def _refs():
    return [
        EvidenceRef("e1", "p1", "Paper A", "metric", "fulltext", "A 的指标", "Exact match ...", "sq1"),
        EvidenceRef("e2", "p2", "Paper B", "metric", "abstract", "B 的指标", "F1 ...", "sq1"),
    ]


class TestComparisonParsing:
    def test_drops_unknown_and_cross_paper_references(self):
        raw = {
            "comparisons": [
                {"paper_id": "p1", "dimension_id": "metric", "content": "ok", "evidence_ids": ["e1"]},
                {"paper_id": "p1", "dimension_id": "method", "content": "引用别人的证据", "evidence_ids": ["e2"]},
                {"paper_id": "p2", "dimension_id": "metric", "content": "引用不存在的 id", "evidence_ids": ["zzz"]},
            ]
        }
        drafts, rejected = parse_comparison_payload(raw, _refs())
        assert len(drafts) == 1 and drafts[0].evidence_ids == ["e1"]
        assert len(rejected) == 2

    def test_conflict_sides_must_not_overlap(self):
        raw = {
            "conflicts": [
                {"topic": "重叠", "side_a": ["e1"], "side_b": ["e1"]},
                {"topic": "缺侧", "side_a": ["e1"], "side_b": ["nope"]},
                {"topic": "ok", "side_a": ["e1"], "side_b": ["e2"], "dimension_id": "metric"},
            ]
        }
        drafts, rejected = parse_conflict_payload(raw, _refs())
        assert len(drafts) == 1 and drafts[0].side_b == ["e2"]
        assert len(rejected) == 2

    def test_generator_rejects_unparseable_output(self):
        class BadLLM:
            def invoke(self, messages):
                return type("R", (), {"content": "抱歉，我无法输出 JSON"})()

        generator = ComparisonGenerator(BadLLM(), model_name="fake")
        from chatchat.server.research.comparison import ComparisonError

        with pytest.raises(ComparisonError):
            generator.generate_comparisons(
                question="q", dimensions=[{"id": "metric"}], evidence=_refs()
            )


# ---------------------------------------------------------------------------
# 6. 覆盖检查（纯计算，可重现）
# ---------------------------------------------------------------------------


class _Card:
    """覆盖函数的最小输入替身。"""

    def __init__(self, paper_id, sub_question_id, evidence_type, level="fulltext",
                 verified=True, review_status="pending"):
        self.paper_id = paper_id
        self.sub_question_id = sub_question_id
        self.evidence_type = evidence_type
        self.level = level
        self.quote_verified = verified
        self.review_status = review_status


class TestCoverage:
    def test_classification_and_missing_dimensions(self):
        evidence = [
            _Card("p1", "sq1", "metric"),
            _Card("p2", "sq1", "metric"),
            _Card("p1", "sq2", "limitation", level="abstract"),
        ]
        result = build_coverage(
            plan=PLAN,
            dimensions=list(repo.DEFAULT_DIMENSIONS),
            evidence=evidence,
            conflicts=[],
            included_paper_ids=["p1", "p2", "p3"],
        )
        by_id = {row["id"]: row for row in result["sub_questions"]}
        assert by_id["sq1"]["status"] == COVERED
        assert by_id["sq2"]["status"] == THIN  # 仅一篇 + 摘要级
        assert [d["id"] for d in result["missing_dimensions"]] == [
            "objective", "method", "dataset", "result", "conclusion"
        ]
        assert result["papers_without_evidence"] == ["p3"]
        assert result["totals"]["covered"] == 1 and result["totals"]["thin"] == 1

    def test_unverified_only_is_thin(self):
        result = build_coverage(
            plan=PLAN,
            dimensions=list(repo.DEFAULT_DIMENSIONS),
            evidence=[_Card("p1", "sq1", "metric", verified=False), _Card("p2", "sq1", "metric", verified=False)],
            included_paper_ids=["p1", "p2"],
        )
        by_id = {row["id"]: row for row in result["sub_questions"]}
        assert by_id["sq1"]["status"] == THIN

    def test_rejected_evidence_does_not_count(self):
        result = build_coverage(
            plan=PLAN,
            dimensions=list(repo.DEFAULT_DIMENSIONS),
            evidence=[
                _Card("p1", "sq1", "metric", review_status="rejected"),
                _Card("p2", "sq1", "metric", review_status="rejected"),
            ],
            included_paper_ids=["p1", "p2"],
        )
        by_id = {row["id"]: row for row in result["sub_questions"]}
        assert by_id["sq1"]["status"] == MISSING
        assert result["totals"]["evidence"] == 0

    def test_suggestions_reuse_plan_queries(self):
        result = build_coverage(
            plan=PLAN,
            dimensions=list(repo.DEFAULT_DIMENSIONS),
            evidence=[_Card("p1", "sq1", "metric")],
            included_paper_ids=["p1"],
        )
        suggestions = {s["sub_question_id"]: s for s in result["suggestions"]}
        assert set(suggestions) == {"sq1", "sq2"}
        assert suggestions["sq1"]["queries"] == ["RAG evaluation metrics", "open-domain QA metrics"]
        assert suggestions["sq2"]["queries"] == ["retrieval augmented generation limitations"]
        assert all(s["requires_confirmation"] for s in result["suggestions"])

    def test_same_input_same_output(self):
        evidence = [_Card("p1", "sq1", "metric"), _Card("p2", "sq2", "limitation")]
        args = dict(
            plan=PLAN,
            dimensions=list(repo.DEFAULT_DIMENSIONS),
            evidence=evidence,
            conflicts=[],
            included_paper_ids=["p1", "p2"],
        )
        assert json.dumps(build_coverage(**args), sort_keys=True) == json.dumps(
            build_coverage(**args), sort_keys=True
        )


# ---------------------------------------------------------------------------
# 7. API：比较、冲突、覆盖、补充检索
# ---------------------------------------------------------------------------


class FakeGenerator:
    model_name = "fake-compare-model"

    def __init__(self, comparisons=None, conflicts=None):
        self.comparisons = comparisons or []
        self.conflicts = conflicts or []

    def generate_comparisons(self, *, question, dimensions, evidence):
        return list(self.comparisons), ["示例拒绝：某条结论没有证据"]

    def detect_conflicts(self, *, question, evidence):
        return list(self.conflicts), []


class TestComparisonApi:
    def _seed(self, temp_db, users):
        owner = users["alice"]["id"]
        project_id, run_id = _mk_project(temp_db, owner, json.dumps(PLAN, ensure_ascii=False))
        paper_a, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
        paper_b, card_b = _mk_paper_with_evidence(
            temp_db, owner, project_id, "Paper B", quote="F1 is the primary metric."
        )
        return project_id, run_id, (paper_a, paper_b), (card_a, card_b)

    def test_dimensions_roundtrip(self, client, temp_db, users):
        headers = _headers(temp_db, "alice")
        project_id, _ = _mk_project(temp_db, users["alice"]["id"])
        got = client.get(f"/research/projects/{project_id}/dimensions", headers=headers)
        assert [d["id"] for d in got.json()["dimensions"]] == [d["id"] for d in repo.DEFAULT_DIMENSIONS]
        put = client.put(
            f"/research/projects/{project_id}/dimensions",
            headers=headers,
            json={"dimensions": [{"id": "metric", "label": "评价指标"}, {"id": "cost", "label": "成本"}]},
        )
        assert [d["id"] for d in put.json()["dimensions"]] == ["metric", "cost"]

    def test_generate_comparisons_and_conflicts_traceable(self, client, temp_db, users, monkeypatch):
        project_id, run_id, (paper_a, paper_b), (card_a, card_b) = self._seed(temp_db, users)
        from chatchat.server.research.comparison import ComparisonDraft, ConflictDraft

        monkeypatch.setattr(
            comparison_service,
            "build_default_generator",
            lambda model_name=None: FakeGenerator(
                comparisons=[
                    ComparisonDraft(paper_a, "metric", "A 用 exact match", [card_a]),
                    ComparisonDraft(paper_b, "metric", "B 用 F1", [card_b]),
                    ComparisonDraft(paper_a, "method", "无依据", []),
                ],
                conflicts=[ConflictDraft("指标口径不一致", "A 与 B 口径不同", [card_a], [card_b], "metric", "sq1")],
            ),
        )
        headers = _headers(temp_db, "alice")
        resp = client.post(
            f"/research/projects/{project_id}/comparisons",
            headers=headers,
            json={"run_id": run_id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["written"] == 2 and body["conflicts"] == 1
        assert any("没有证据" in r for r in body["rejected"])

        matrix = client.get(f"/research/projects/{project_id}/comparisons", headers=headers).json()
        assert matrix["total"] == 2
        assert all(item["evidence_ids"] for item in matrix["comparisons"])
        assert all(item["paper_title"] for item in matrix["comparisons"])

        conflicts = client.get(f"/research/projects/{project_id}/conflicts", headers=headers).json()
        assert conflicts["total"] == 1
        conflict = conflicts["conflicts"][0]
        assert conflict["side_a"][0]["evidence_id"] == card_a
        assert conflict["side_b"][0]["paper_id"] == paper_b

        resolved = client.patch(
            f"/research/conflicts/{conflict['conflict_id']}",
            headers=headers,
            json={"resolution": "按任务定义取 exact match"},
        )
        assert resolved.json()["status"] == "resolved"

        # 重新生成：幂等，不产生第二条冲突
        again = client.post(
            f"/research/projects/{project_id}/comparisons", headers=headers, json={"run_id": run_id}
        ).json()
        assert again["written"] == 0 and again["skipped"] == 2 and again["conflicts"] == 0

    def test_coverage_endpoint(self, client, temp_db, users, monkeypatch):
        project_id, run_id, _papers, _cards = self._seed(temp_db, users)
        from chatchat.server.research.comparison import ComparisonDraft

        monkeypatch.setattr(
            comparison_service,
            "build_default_generator",
            lambda model_name=None: FakeGenerator(
                comparisons=[ComparisonDraft(_papers[0], "metric", "A", [_cards[0]])]
            ),
        )
        headers = _headers(temp_db, "alice")
        cov = client.get(f"/research/projects/{project_id}/coverage", headers=headers)
        assert cov.status_code == 200, cov.text
        body = cov.json()
        by_id = {row["id"]: row for row in body["sub_questions"]}
        assert by_id["sq1"]["evidence_count"] == 2 and by_id["sq1"]["status"] == COVERED
        assert by_id["sq2"]["evidence_count"] == 0 and by_id["sq2"]["status"] == MISSING
        assert body["suggestions"] and body["suggestions"][0]["queries"]

    def test_supplement_run_is_bounded(self, client, temp_db, users):
        headers = _headers(temp_db, "alice")
        project_id, run_id = _mk_project(temp_db, users["alice"]["id"])
        current = run_id
        for expected_round in range(2, repo.MAX_RESEARCH_ROUNDS + 1):
            resp = client.post(f"/research/runs/{current}/supplement", headers=headers)
            assert resp.status_code == 200, resp.text
            assert resp.json()["round_no"] == expected_round
            current = resp.json()["run_id"]
        over = client.post(f"/research/runs/{current}/supplement", headers=headers)
        assert over.status_code == 409
        assert "最大轮次" in over.json()["detail"]
        with _session(temp_db) as s:
            runs = repo.list_runs(s, users["alice"]["id"], project_id=project_id, limit=50)
            assert len(runs) == repo.MAX_RESEARCH_ROUNDS
            assert {r.round_no for r in runs} == set(range(1, repo.MAX_RESEARCH_ROUNDS + 1))

    def test_cross_user_cannot_read_or_write(self, temp_db, users, client):
        owner = users["alice"]["id"]
        project_id, run_id = _mk_project(temp_db, owner)
        paper_a, card_a = _mk_paper_with_evidence(temp_db, owner, project_id, "Paper A")
        _paper_b, card_b = _mk_paper_with_evidence(
            temp_db, owner, project_id, "Paper B", quote="F1 is the primary metric."
        )
        with _session(temp_db) as s:
            conflict, _ = repo.record_conflict(
                s,
                project_id=project_id,
                owner_id=owner,
                topic="指标",
                side_a=[{"evidence_id": card_a}],
                side_b=[{"evidence_id": card_b}],
            )
        bob = _headers(temp_db, "bob")
        assert client.get(f"/research/projects/{project_id}/coverage", headers=bob).status_code == 404
        assert client.get(f"/research/projects/{project_id}/comparisons", headers=bob).status_code == 404
        assert client.get(f"/research/projects/{project_id}/conflicts", headers=bob).status_code == 404
        assert client.get(f"/research/projects/{project_id}/dimensions", headers=bob).status_code == 404
        assert (
            client.post(f"/research/runs/{run_id}/supplement", headers=bob).status_code == 404
        )
        assert (
            client.patch(
                f"/research/conflicts/{conflict.id}", headers=bob, json={"resolution": "越权"}
            ).status_code
            == 404
        )
        assert client.delete(f"/research/conflicts/{conflict.id}", headers=bob).status_code == 404
        # Alice 的记录仍在
        alice = _headers(temp_db, "alice")
        assert client.get(f"/research/projects/{project_id}/conflicts", headers=alice).json()["total"] == 1
