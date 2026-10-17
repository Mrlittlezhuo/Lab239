"""证据驱动研究闭环离线测试（任务 018 阶段 A）。

覆盖：

- v9 迁移：新建 ``paper_unit``、为 ``evidence_card``/``project_paper`` 补列；
  v8 存量库升级保留旧证据行、重复升级幂等；
- 原文单元：切分确定性、页码/章节定位、取不到全文时降级为摘要级单元
  （绝不伪造页码）、按内容哈希幂等重解析；
- 证据核对：quote 必须在给定原文中逐字出现，改写/臆造与非法类型被拒绝
  并如实返回原因；摘要级证据自动标注 ``level='abstract'``；
- 幂等与审核：同一原文重复提取不产生重复卡片；用户已审核的卡片在重跑时
  不被覆盖；编辑生成新版本；
- 所有者隔离：跨用户读写原文与证据一律不可见（API 404）；
- 删除项目级联清理私有原文单元与证据，不触碰公开 ``paper_record``。

全部使用临时 SQLite + 假原文 provider + 假 LLM，不连接真实模型、ArXiv、
外部网络或用户数据。
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
    EvidenceCardModel,
    PaperRecordModel,
    PaperUnitModel,
    ProjectPaperModel,
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
from chatchat.server.research import evidence_service
from chatchat.server.research import fulltext as fulltext_mod
from chatchat.server.research.evidence import (
    EvidenceExtractor,
    EvidenceError,
    find_quote,
    parse_evidence_payload,
)
from chatchat.server.research.fulltext import (
    FullTextError,
    FullTextProvider,
    PaperUnit,
    AbstractUnitProvider,
    collect_units,
    split_page_text,
    units_from_documents,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "research-evidence-unit-test-secret-0000"
    )
    return "research-evidence-unit-test-secret-0000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite：迁移到 v9 并建全表。"""
    path = tmp_path / "evidence_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 17
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


def _mk_project_and_paper(db, owner_id, title="Retrieval-Augmented Generation for QA"):
    """建项目 + 公开论文 + 项目内挂接，返回 (project_id, paper_id, project_paper_id)。"""
    with _session(db) as s:
        project = repo.create_project(
            s, owner_id, name="RAG 综述", research_question="RAG 有哪些评测指标？"
        )
        paper, _ = repo.upsert_paper(
            s,
            {
                "title": title,
                "authors": "A. Author; B. Backend",
                "year": 2023,
                "abstract": "We evaluate retrieval-augmented generation on open QA.",
                "url": "https://arxiv.org/pdf/2301.00001.pdf",
                "source": "arxiv",
            },
        )
        pp = repo.link_project_paper(s, project.id, owner_id, paper.id, source="arxiv")
        s.commit()
        return project.id, paper.id, pp.id


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


class FakeExtractor:
    """假提取器：按脚本返回模型风格输出，并记录收到的原文单元。"""

    model_name = "fake-extract-model"

    def __init__(self, payload):
        self.payload = payload
        self.seen_units = None

    def extract(self, *, question, sub_question, paper, units):
        self.seen_units = list(units)
        return parse_evidence_payload(self.payload, units)


# ---------------------------------------------------------------------------
# 1. v9 迁移
# ---------------------------------------------------------------------------


class TestV9Migration:
    def test_creates_paper_unit_and_new_columns(self, temp_db):
        with temp_db["engine"].connect() as conn:
            tables = set(
                conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).scalars()
            )
            assert "paper_unit" in tables
            ev_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(evidence_card)")}
            assert {
                "sub_question_id",
                "evidence_type",
                "quote",
                "unit_id",
                "quote_verified",
                "level",
                "review_status",
                "dedup_key",
            } <= ev_cols
            pp_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(project_paper)")}
            assert {"fulltext_state", "unit_count", "fulltext_hash"} <= pp_cols
            unit_cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(paper_unit)")}
            assert {"page", "section", "block_index", "content_hash"} <= unit_cols

    def test_v8_db_upgrades_preserving_existing_evidence(self, tmp_path):
        """v8 存量库升级：旧证据行完整保留，新列取默认值（不重建、不丢数据）。"""
        path = tmp_path / "v8_to_v9.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(8), conn)
            conn.execute(
                text(
                    "INSERT INTO evidence_card (id, project_id, paper_id, owner_id, content, "
                    "locator_json, version, create_time, update_time) VALUES "
                    "('e1', 'p1', 'pa1', 'u1', '旧证据内容', '{\"page\": 2}', 3, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [9, 10, 11, 12, 13, 14, 15, 16, 17]
            row = conn.execute(
                text(
                    "SELECT content, version, review_status, quote_verified, dedup_key, level "
                    "FROM evidence_card WHERE id='e1'"
                )
            ).fetchone()
            assert row == ("旧证据内容", 3, "pending", 0, "", "fulltext")
        with engine.connect() as conn:
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
        engine.dispose()

    def test_paper_unit_slot_unique(self, temp_db):
        """同一 (项目, 论文, kind, 槽位) 只能有一条：重跑不产生重复单元。"""
        with temp_db["engine"].connect() as conn:
            conn.execute(
                text(
                    "INSERT INTO paper_unit (id, project_id, paper_id, owner_id, unit_kind, "
                    "block_index, text, create_time, update_time) VALUES "
                    "('u1', 'p1', 'pa1', 'o1', 'block', 0, 'x', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        "INSERT INTO paper_unit (id, project_id, paper_id, owner_id, unit_kind, "
                        "block_index, text, create_time, update_time) VALUES "
                        "('u2', 'p1', 'pa1', 'o1', 'block', 0, 'y', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )


# ---------------------------------------------------------------------------
# 2. 原文单元与定位
# ---------------------------------------------------------------------------


class TestFullTextUnits:
    def test_split_is_deterministic_and_bounded(self):
        long_text = "Sentence one about retrieval. " * 200
        first = split_page_text(long_text, max_chars=300)
        second = split_page_text(long_text, max_chars=300)
        assert first == second and len(first) > 1
        assert all(len(block) <= 300 for block in first)
        assert "".join(first).replace(" ", "") == "".join(second).replace(" ", "")

    def test_page_and_section_locators(self):
        units = units_from_documents(_docs(), source_kind="pdf")
        assert len(units) >= 2
        # langchain 的 page 是 0 基，定位统一成 1 基
        assert units[0].page == 1
        assert units[-1].page == 2
        assert units[-1].section == "Evaluation Metrics"
        assert [u.block_index for u in units] == list(range(len(units)))

    def test_degrades_to_abstract_without_fake_page(self):
        """取不到全文 → 摘要级单元，page 必须为 None（绝不伪造页码）。"""
        paper = type("P", (), {"title": "X", "abstract": "Only an abstract here."})()
        result = collect_units(paper, providers=[FakePdfProvider(error="PDF 下载失败")])
        assert result.state == "abstract"
        assert result.units[0].unit_kind == "abstract"
        assert result.units[0].page is None
        assert "未取得全文" in result.detail

    def test_provider_failure_is_isolated(self):
        """单来源失败不影响其他来源；成功来源的单元被采用。"""
        paper = type("P", (), {"title": "X", "abstract": "abs"})()
        result = collect_units(
            paper, providers=[FakePdfProvider(error="boom"), FakePdfProvider()]
        )
        assert result.state == "fulltext"
        assert "boom" in result.detail

    def test_no_source_no_units(self):
        paper = type("P", (), {"title": "X", "abstract": ""})()
        result = collect_units(paper, providers=[FakePdfProvider(docs=[])])
        assert result.state == "none" and result.units == []

    def test_replace_units_is_idempotent(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            units = units_from_documents(_docs(), source_kind="pdf")
            first = repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[u.to_dict() for u in units],
            )
            assert first["changed"] is True and first["state"] == "fulltext"
            second = repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[u.to_dict() for u in units],
            )
            assert second["changed"] is False and second["created"] == 0
            rows = repo.list_paper_units(s, project_id, owner, paper_id)
            assert len(rows) == len(units)
            pp = repo.get_project_paper(s, project_id, paper_id, owner)
            assert pp.fulltext_state == "fulltext" and pp.unit_count == len(units)

    def test_replaced_units_do_not_duplicate(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[PaperUnit(text="旧内容 " * 30, block_index=0, source_kind="pdf").to_dict()],
            )
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[PaperUnit(text="新内容 " * 30, block_index=0, source_kind="pdf").to_dict()],
            )
            rows = repo.list_paper_units(s, project_id, owner, paper_id)
            assert len(rows) == 1 and rows[0].text.startswith("新内容")

    def test_abstract_units_never_carry_page(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[
                    {
                        "unit_kind": "abstract",
                        "page": 7,  # 即便调用方误传页码也必须被清空
                        "text": "Abstract text long enough to be kept as a unit.",
                        "block_index": 0,
                        "source_kind": "abstract",
                    }
                ],
            )
            rows = repo.list_paper_units(s, project_id, owner, paper_id)
            assert len(rows) == 1 and rows[0].page is None
            pp = repo.get_project_paper(s, project_id, paper_id, owner)
            assert pp.fulltext_state == "abstract"


# ---------------------------------------------------------------------------
# 3. 原文核对与解析
# ---------------------------------------------------------------------------


class TestQuoteVerification:
    def test_matches_across_whitespace(self):
        units = units_from_documents(_docs(), source_kind="pdf")
        quote = "We report exact match and F1\non three open-domain QA datasets."
        unit = find_quote(quote, units)
        assert unit is not None and unit.page == 2

    def test_rejects_paraphrase(self):
        units = units_from_documents(_docs(), source_kind="pdf")
        assert find_quote("The authors report accuracy on two datasets.", units) is None

    def test_rejects_too_short_quote(self):
        units = units_from_documents(_docs(), source_kind="pdf")
        assert find_quote("retriever", units) is None

    def test_payload_rejects_fabricated_and_bad_type(self):
        units = units_from_documents(_docs(), source_kind="pdf")
        raw = {
            "evidence": [
                {
                    "evidence_type": "metric",
                    "summary": "评测指标",
                    "quote": "We report exact match and F1 on three open-domain QA datasets.",
                    "unit_index": 1,
                },
                {
                    "evidence_type": "metric",
                    "summary": "模型编造的原文",
                    "quote": "The model achieved perfect scores on every benchmark ever.",
                },
                {
                    "evidence_type": "vibes",
                    "summary": "非法类型",
                    "quote": "Exact match measures whether the answer string matches the gold answer.",
                },
            ]
        }
        result = parse_evidence_payload(raw, units)
        assert len(result.drafts) == 1
        assert result.drafts[0].evidence_type == "metric"
        assert result.drafts[0].level == "fulltext"
        assert result.drafts[0].unit.page == 2
        assert len(result.rejected) == 2
        assert any("未在给定的论文原文中出现" in r for r in result.rejected)
        assert any("证据类型非法" in r for r in result.rejected)

    def test_abstract_level_labeling(self):
        units = AbstractUnitProvider().fetch(
            type("P", (), {"abstract": "We evaluate retrieval-augmented generation on open QA."})
        )
        raw = {
            "evidence": [
                {
                    "evidence_type": "objective",
                    "summary": "研究目标",
                    "quote": "We evaluate retrieval-augmented generation on open QA.",
                }
            ]
        }
        result = parse_evidence_payload(raw, units)
        assert len(result.drafts) == 1
        draft = result.drafts[0]
        assert draft.level == "abstract"
        assert draft.unit.page is None

    def test_invalid_structure_raises(self):
        with pytest.raises(EvidenceError):
            parse_evidence_payload(["not", "a", "dict"], [])

    def test_extractor_retries_once_on_bad_json(self):
        class FlakyLLM:
            def __init__(self):
                self.calls = 0

            def invoke(self, messages):
                self.calls += 1
                if self.calls == 1:
                    return type("R", (), {"content": "抱歉，我说不清楚"})()
                return type("R", (), {
                    "content": json.dumps(
                        {
                            "evidence": [
                                {
                                    "evidence_type": "result",
                                    "summary": "结果",
                                    "quote": "Exact match measures whether the answer string matches the gold answer.",
                                }
                            ]
                        }
                    )
                })()

        units = units_from_documents(_docs(), source_kind="pdf")
        extractor = EvidenceExtractor(FlakyLLM(), model_name="fake")
        result = extractor.extract(
            question="RAG 指标", sub_question="有哪些指标", paper=type("P", (), {"title": "T"})(), units=units
        )
        assert len(result.drafts) == 1
        assert result.drafts[0].unit.page == 2


# ---------------------------------------------------------------------------
# 4. 证据落库：幂等、审核、隔离
# ---------------------------------------------------------------------------


def _mk_evidence(s, project_id, owner, paper_id, quote, *, evidence_type="metric", summary="概括"):
    return repo.upsert_evidence(
        s,
        project_id=project_id,
        owner_id=owner,
        paper_id=paper_id,
        sub_question_id="sq1",
        evidence_type=evidence_type,
        quote=quote,
        summary=summary,
        level="fulltext",
        source_kind="pdf",
        extraction_model="fake",
    )


class TestEvidencePersistence:
    QUOTE = "We report exact match and F1 on three open-domain QA datasets."

    def test_upsert_is_idempotent(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            card, action = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            assert action == "created"
            again, action2 = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            assert action2 == "unchanged" and again.id == card.id
            assert len(repo.list_evidence(s, project_id, owner)) == 1

    def test_reviewed_card_is_not_overwritten(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            card, _ = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE, summary="原概括")
            repo.review_evidence(s, card.id, owner, review_status="confirmed", note="同意")
            _, action = _mk_evidence(
                s, project_id, owner, paper_id, self.QUOTE, summary="重跑想改的概括"
            )
            assert action == "kept_reviewed"
            fresh = repo.get_owned_evidence(s, card.id, owner)
            assert fresh.summary == "原概括" and fresh.review_status == "confirmed"

    def test_edit_creates_new_version(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            card, _ = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            edited = repo.review_evidence(
                s, card.id, owner, review_status="confirmed", summary="更准确的概括"
            )
            assert edited.review_status == "edited"  # 带修改的确认按编辑记账
            assert edited.version == 2
            assert edited.summary == "更准确的概括"

    def test_rejected_card_stays_for_audit(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            card, _ = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            repo.review_evidence(s, card.id, owner, review_status="rejected", note="不相关")
            assert len(repo.list_evidence(s, project_id, owner)) == 1
            assert repo.list_evidence(s, project_id, owner, include_rejected=False) == []

    def test_invalid_type_or_empty_quote_rejected(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            with pytest.raises(ValueError):
                repo.upsert_evidence(
                    s,
                    project_id=project_id,
                    owner_id=owner,
                    paper_id=paper_id,
                    sub_question_id="sq1",
                    evidence_type="vibes",
                    quote=self.QUOTE,
                )
            with pytest.raises(ValueError):
                repo.upsert_evidence(
                    s,
                    project_id=project_id,
                    owner_id=owner,
                    paper_id=paper_id,
                    sub_question_id="sq1",
                    evidence_type="metric",
                    quote="   ",
                )

    def test_cross_user_cannot_touch_evidence_or_units(self, temp_db):
        alice = _mk_user(temp_db, "alice")["id"]
        bob = _mk_user(temp_db, "bob")["id"]
        with _session(temp_db) as s:
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, alice)
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=alice,
                paper_id=paper_id,
                units=[u.to_dict() for u in units_from_documents(_docs(), source_kind="pdf")],
            )
            card, _ = _mk_evidence(s, project_id, alice, paper_id, self.QUOTE)
            # Bob 一律不可见
            assert repo.list_evidence(s, project_id, bob) == []
            assert repo.list_paper_units(s, project_id, bob, paper_id) == []
            assert repo.get_owned_evidence(s, card.id, bob) is None
            with pytest.raises(repo.ResearchResourceNotFound):
                repo.review_evidence(s, card.id, bob, review_status="confirmed")
            with pytest.raises(repo.ResearchResourceNotFound):
                repo.delete_evidence(s, card.id, bob)
            with pytest.raises(repo.ResearchResourceNotFound):
                repo.get_project_paper(s, project_id, paper_id, bob)

    def test_delete_project_cascades_units_and_evidence(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[u.to_dict() for u in units_from_documents(_docs(), source_kind="pdf")],
            )
            _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            repo.delete_owned_project(s, project_id, owner)
            assert s.query(PaperUnitModel).count() == 0
            assert s.query(EvidenceCardModel).count() == 0
            # 公开论文元数据不被删除
            assert s.query(PaperRecordModel).filter(PaperRecordModel.id == paper_id).one_or_none()

    def test_reverify_flags_stale_quotes(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, paper_id, _ = _mk_project_and_paper(temp_db, owner)
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[u.to_dict() for u in units_from_documents(_docs(), source_kind="pdf")],
            )
            card, _ = _mk_evidence(s, project_id, owner, paper_id, self.QUOTE)
            assert evidence_service.reverify_evidence(
                s, project_id=project_id, owner_id=owner
            ) == {"verified": 1, "unverified": 0, "total": 1}
            # 原文被换成别的论文内容后，证据不再可核对
            repo.replace_paper_units(
                s,
                project_id=project_id,
                owner_id=owner,
                paper_id=paper_id,
                units=[PaperUnit(text="完全不同的论文正文内容。" * 20, block_index=0).to_dict()],
            )
            stats = evidence_service.reverify_evidence(
                s, project_id=project_id, owner_id=owner
            )
            assert stats == {"verified": 0, "unverified": 1, "total": 1}
            assert repo.get_owned_evidence(s, card.id, owner).quote_verified == 0


# ---------------------------------------------------------------------------
# 5. 服务层 + API
# ---------------------------------------------------------------------------


class TestEvidenceApi:
    def test_fulltext_and_units_endpoints(self, client, temp_db, users, monkeypatch):
        provider = FakePdfProvider()
        monkeypatch.setattr(evidence_service, "default_providers", lambda kb_names=None: [provider])
        headers = _headers(temp_db, "alice")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])

        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/fulltext",
            headers=headers,
            json={},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "fulltext" and body["created"] >= 2
        assert provider.calls == 1

        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/fulltext",
            headers=headers,
            json={},
        )
        assert resp.json()["changed"] is False  # 幂等：内容未变不重写

        resp = client.get(f"/research/projects/{project_id}/papers/{paper_id}/units", headers=headers)
        units = resp.json()["units"]
        assert units and all(u["unit_kind"] == "block" for u in units)
        assert any(u["page"] == 2 and u["section"] == "Evaluation Metrics" for u in units)

    def test_extract_endpoint_persists_verified_evidence(self, client, temp_db, users, monkeypatch):
        monkeypatch.setattr(
            evidence_service, "default_providers", lambda kb_names=None: [FakePdfProvider()]
        )
        extractor = FakeExtractor(
            {
                "evidence": [
                    {
                        "evidence_type": "metric",
                        "summary": "评测指标",
                        "quote": "We report exact match and F1 on three open-domain QA datasets.",
                    },
                    {
                        "evidence_type": "result",
                        "summary": "编造",
                        "quote": "Perfect scores everywhere, guaranteed.",
                    },
                ]
            }
        )
        monkeypatch.setattr(
            evidence_service, "build_default_extractor", lambda model_name=None: extractor
        )
        headers = _headers(temp_db, "alice")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])
        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=headers,
            json={"sub_question_id": "sq1", "sub_question": "有哪些评测指标"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 1
        assert len(body["rejected"]) == 1 and "未在" in body["rejected"][0]

        resp = client.get(f"/research/projects/{project_id}/evidence", headers=headers)
        cards = resp.json()["evidence"]
        assert len(cards) == 1
        card = cards[0]
        assert card["quote_verified"] is True
        assert card["level"] == "fulltext"
        assert card["locator"]["page"] == 2
        assert card["paper_title"]

        # 重跑同一提取：幂等，不新增卡片
        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=headers,
            json={"sub_question_id": "sq1", "sub_question": "有哪些评测指标"},
        )
        assert resp.json()["unchanged"] == 1 and resp.json()["created"] == 0

    def test_review_edit_must_stay_verifiable(self, client, temp_db, users, monkeypatch):
        monkeypatch.setattr(
            evidence_service, "default_providers", lambda kb_names=None: [FakePdfProvider()]
        )
        extractor = FakeExtractor(
            {
                "evidence": [
                    {
                        "evidence_type": "metric",
                        "summary": "评测指标",
                        "quote": "We report exact match and F1 on three open-domain QA datasets.",
                    }
                ]
            }
        )
        monkeypatch.setattr(
            evidence_service, "build_default_extractor", lambda model_name=None: extractor
        )
        headers = _headers(temp_db, "alice")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])
        client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=headers,
            json={"sub_question_id": "sq1", "sub_question": "指标"},
        )
        card = client.get(f"/research/projects/{project_id}/evidence", headers=headers).json()["evidence"][0]

        bad = client.patch(
            f"/research/evidence/{card['evidence_id']}",
            headers=headers,
            json={"review_status": "confirmed", "quote": "这句原文根本不存在于论文里。"},
        )
        assert bad.status_code == 400 and "找不到" in bad.json()["detail"]

        ok = client.patch(
            f"/research/evidence/{card['evidence_id']}",
            headers=headers,
            json={
                "review_status": "confirmed",
                "quote": "Exact match measures whether the answer string matches the gold answer.",
                "note": "换成更完整的引用",
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["review_status"] == "edited" and ok.json()["version"] == 2

        deleted = client.delete(f"/research/evidence/{card['evidence_id']}", headers=headers)
        assert deleted.json()["deleted"] is True
        assert client.get(f"/research/projects/{project_id}/evidence", headers=headers).json()["evidence"] == []

    def test_cross_user_and_anonymous_access(self, client, temp_db, users, monkeypatch):
        monkeypatch.setattr(
            evidence_service, "default_providers", lambda kb_names=None: [FakePdfProvider()]
        )
        extractor = FakeExtractor(
            {
                "evidence": [
                    {
                        "evidence_type": "metric",
                        "summary": "评测指标",
                        "quote": "We report exact match and F1 on three open-domain QA datasets.",
                    }
                ]
            }
        )
        monkeypatch.setattr(
            evidence_service, "build_default_extractor", lambda model_name=None: extractor
        )
        alice_headers = _headers(temp_db, "alice")
        bob_headers = _headers(temp_db, "bob")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])
        client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=alice_headers,
            json={"sub_question_id": "sq1", "sub_question": "指标"},
        )
        card = client.get(
            f"/research/projects/{project_id}/evidence", headers=alice_headers
        ).json()["evidence"][0]

        # 未登录
        assert client.get(f"/research/projects/{project_id}/evidence").status_code == 401
        # Bob 看不到 Alice 的项目/原文/证据
        assert (
            client.get(f"/research/projects/{project_id}/evidence", headers=bob_headers).status_code
            == 404
        )
        assert (
            client.get(f"/research/projects/{project_id}/papers/{paper_id}/units", headers=bob_headers)
            .status_code
            == 404
        )
        assert (
            client.patch(
                f"/research/evidence/{card['evidence_id']}",
                headers=bob_headers,
                json={"review_status": "confirmed"},
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/research/evidence/{card['evidence_id']}", headers=bob_headers).status_code
            == 404
        )
        # Alice 的证据不受影响
        assert len(
            client.get(f"/research/projects/{project_id}/evidence", headers=alice_headers).json()["evidence"]
        ) == 1

    def test_abstract_only_paper_labels_evidence_level(self, client, temp_db, users, monkeypatch):
        monkeypatch.setattr(
            evidence_service,
            "default_providers",
            lambda kb_names=None: [FakePdfProvider(error="PDF 不可用")],
        )
        extractor = FakeExtractor(
            {
                "evidence": [
                    {
                        "evidence_type": "objective",
                        "summary": "研究目标",
                        "quote": "We evaluate retrieval-augmented generation on open QA.",
                    }
                ]
            }
        )
        monkeypatch.setattr(
            evidence_service, "build_default_extractor", lambda model_name=None: extractor
        )
        headers = _headers(temp_db, "alice")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])
        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=headers,
            json={"sub_question_id": "sq1", "sub_question": "目标"},
        )
        assert resp.status_code == 200 and resp.json()["created"] == 1
        card = client.get(
            f"/research/projects/{project_id}/evidence", headers=headers
        ).json()["evidence"][0]
        assert card["level"] == "abstract"
        assert card["locator"]["page"] is None  # 摘要级证据没有页码

    def test_paper_without_any_text_produces_nothing(self, client, temp_db, users, monkeypatch):
        monkeypatch.setattr(
            evidence_service,
            "default_providers",
            lambda kb_names=None: [FakePdfProvider(docs=[])],
        )
        monkeypatch.setattr(
            fulltext_mod,
            "AbstractUnitProvider",
            lambda: type(
                "NoAbstract",
                (FullTextProvider,),
                {"name": "abstract", "fetch": lambda self, paper: []},
            )(),
        )
        headers = _headers(temp_db, "alice")
        project_id, paper_id, _ = _mk_project_and_paper(temp_db, users["alice"]["id"])
        resp = client.post(
            f"/research/projects/{project_id}/papers/{paper_id}/evidence",
            headers=headers,
            json={"sub_question_id": "sq1", "sub_question": "目标"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] == 0 and resp.json()["total"] == 0
        assert client.get(f"/research/projects/{project_id}/evidence", headers=headers).json()["total"] == 0
