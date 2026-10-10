"""版本化报告与引用可追溯性离线测试（任务 018 阶段 C）。

覆盖：

- v11 迁移：为 ``research_report`` 追加版本链与引用映射列，v10 存量库
  升级保留旧报告行并回填 ``report_key``，重复升级幂等；
- 引用校验：正文标记内嵌真实证据 id；指向不存在/他人证据的标记被剥离
  并记录原因；引用映射可反查论文与原文定位；
- 无证据结论：被归入"证据不足与推测"并显式标注，不进入综合结论；
- 渲染与导出：Markdown 脚注含定位与原文；BibTeX 只由真实论文元数据生成，
  key 冲突自动加后缀；
- 版本语义：生成与用户编辑都插入新版本，旧版本不被覆盖；
- 所有者隔离：跨用户无法读取/编辑/删除/导出报告（API 404）。

全部使用临时 SQLite + 假生成器，不连接真实模型、外部网络或用户数据。
"""
import asyncio
import json
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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
from chatchat.server.research import report_service
from chatchat.server.research.report import (
    ReportDraft,
    ReportEvidence,
    ReportGenerator,
    bibtex_entries,
    markers_in,
    parse_report_payload,
    render_markdown,
    validate_report_body,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "research-report-unit-test-secret-000000"
    )
    return "research-report-unit-test-secret-000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "report_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 12
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


def _seed_project(db, owner_id):
    """项目 + 运行 + 两篇纳入论文 + 各一张证据，返回 ids。"""
    with _session(db) as s:
        project = repo.create_project(
            s, owner_id, name="RAG 综述", research_question="RAG 的指标与局限？", scope="2020 年以后"
        )
        run = repo.create_run(s, owner_id, project.id)
        repo.set_source_stats(s, run.id, owner_id, [{"source": "arxiv", "hits": 12, "failures": 0}])
        ids = {}
        for num, (title, quote) in enumerate(
            (
                ("Retrieval-Augmented Generation for QA", "Exact match measures the answer string."),
                ("Efficient Retrieval at Scale", "F1 is the primary metric in our evaluation."),
            )
        ):
            paper, _ = repo.upsert_paper(
                s,
                {
                    "title": title,
                    "authors": "Alice Author; Bob Builder",
                    "year": 2023,
                    "abstract": "abs",
                    "arxiv_id": f"2301.000{num}",
                    "url": f"https://arxiv.org/pdf/2301.000{num}.pdf",
                    "source": "arxiv",
                },
            )
            pp = repo.link_project_paper(s, project.id, owner_id, paper.id, source="arxiv")
            pp.screening_status = "included"
            card, _ = repo.upsert_evidence(
                s,
                project_id=project.id,
                owner_id=owner_id,
                paper_id=paper.id,
                sub_question_id="sq1",
                evidence_type="metric",
                quote=quote,
                summary=f"{title} 的指标证据",
                level="fulltext",
                source_kind="pdf",
            )
            ids[f"paper{num}"] = paper.id
            ids[f"card{num}"] = card.id
        s.commit()
        return project.id, run.id, ids


# ---------------------------------------------------------------------------
# 1. v11 迁移
# ---------------------------------------------------------------------------


class TestV11Migration:
    def test_report_columns_present(self, temp_db):
        with temp_db["engine"].connect() as conn:
            cols = {c[1] for c in conn.exec_driver_sql("PRAGMA table_info(research_report)")}
            assert {
                "report_key",
                "citations_json",
                "evidence_ids_json",
                "status",
                "unsupported",
                "generated_by",
            } <= cols

    def test_v10_db_upgrades_and_backfills_report_key(self, tmp_path):
        """v10 存量库升级：旧报告行保留，report_key 回填为自身 id。"""
        path = tmp_path / "v10_to_v11.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(10), conn)
            conn.execute(
                text(
                    "INSERT INTO research_report (id, project_id, owner_id, title, content, "
                    "version, create_time, update_time) VALUES "
                    "('r1', 'p1', 'u1', '旧报告', '旧内容', 4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            conn.commit()
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [11, 12]
            row = conn.execute(
                text("SELECT title, content, version, report_key, status FROM research_report WHERE id='r1'")
            ).fetchone()
            assert row == ("旧报告", "旧内容", 4, "r1", "generated")
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. 引用校验与渲染
# ---------------------------------------------------------------------------


def _evidence():
    return [
        ReportEvidence("e1" * 6, "p1", "Paper A", "metric", "fulltext", "A 指标",
                       "Exact match measures the answer string.",
                       {"page": 3, "section": "Evaluation", "block_index": 2}),
        ReportEvidence("e2" * 6, "p2", "Paper B", "metric", "abstract", "B 指标",
                       "F1 is the primary metric.", {"page": None, "section": "abstract", "block_index": 0},
                       verified=False),
    ]


class TestCitations:
    def test_markers_are_id_based_and_ordered(self):
        body = f"结论一 [[e:{'e1' * 6}]]；结论二 [[e:{'e2' * 6}]]；重复 [[e:{'e1' * 6}]]"
        assert markers_in(body) == ["e1" * 6, "e2" * 6]

    def test_unknown_markers_are_stripped_with_warning(self):
        body = f"可信 [[e:{'e1' * 6}]] 与 不可信 [[e:{'zz' * 6}]]"
        cleaned, citations, warnings = validate_report_body(body, _evidence())
        assert "zz" * 6 not in cleaned and "可信" in cleaned
        assert [c["evidence_id"] for c in citations] == ["e1" * 6]
        assert citations[0]["paper_title"] == "Paper A"
        assert citations[0]["locator"]["page"] == 3
        assert len(warnings) == 1

    def test_render_markdown_uses_footnotes_with_locators(self):
        body = f"结论 [[e:{'e1' * 6}]]；另一结论 [[e:{'e2' * 6}]]"
        _cleaned, citations, _warnings = validate_report_body(body, _evidence())
        rendered = render_markdown(title="RAG 报告", body=body, citations=citations)
        assert "[[e:" not in rendered
        assert "[^1]" in rendered and "[^2]" in rendered
        assert "[^1]: Paper A — 第 3 页，Evaluation，文本块 2" in rendered
        assert "> Exact match measures the answer string." in rendered
        # 摘要级 + 未核对的证据必须如实标注
        assert "[^2]: Paper B — 摘要级证据（原文未核对）" in rendered

    def test_conclusion_without_evidence_is_labelled_speculation(self):
        raw = {
            "title": "报告",
            "overview": "概览",
            "conclusions": [
                {"text": "有证据的结论", "evidence_ids": ["e1" * 6]},
                {"text": "模型自己的判断", "evidence_ids": []},
                {"text": "引用不存在的证据", "evidence_ids": ["zz" * 6]},
            ],
            "gaps": ["缺少消融实验"],
            "next_steps": ["补充检索"],
        }
        draft = parse_report_payload(raw, _evidence())
        assert len(draft.conclusions) == 1
        assert draft.conclusions[0][1] == ["e1" * 6]
        assert any("推测：无证据支持" in u for u in draft.unsupported)
        assert any("引用了不存在" in r for r in draft.rejected)
        assert "缺少消融实验" in draft.unsupported

    def test_bibtex_only_from_real_metadata(self):
        papers = [
            type(
                "P",
                (),
                {
                    "title": "Paper A",
                    "authors": "Alice Author; Bob Builder",
                    "year": 2023,
                    "doi": None,
                    "arxiv_id": "2301.00001",
                    "url": "https://arxiv.org/pdf/2301.00001.pdf",
                },
            ),
            type(
                "P",
                (),
                {
                    "title": "Paper B",
                    "authors": "Alice Author",
                    "year": 2023,
                    "doi": "10.1/x",
                    "arxiv_id": None,
                    "url": None,
                },
            ),
        ]
        bib = bibtex_entries(papers)
        assert bib.count("@misc{") == 2
        assert "author = {Alice Author and Bob Builder}" in bib
        assert "eprint = {2301.00001}" in bib
        assert "doi = {10.1/x}" in bib
        # 同一作者同一年 → key 唯一
        keys = [line.split("{")[1].rstrip(",") for line in bib.splitlines() if line.startswith("@misc{")]
        assert len(set(keys)) == 2

    def test_generator_rejects_unparseable_output(self):
        class BadLLM:
            def invoke(self, messages):
                return type("R", (), {"content": "不是一段 JSON"})()

        generator = ReportGenerator(BadLLM(), model_name="fake")
        from chatchat.server.research.report import ReportError

        with pytest.raises(ReportError):
            generator.generate(
                question="q", scope="", coverage={}, comparisons=[], conflicts=[], evidence=_evidence()
            )


# ---------------------------------------------------------------------------
# 3. 版本链
# ---------------------------------------------------------------------------


class TestReportVersions:
    def test_versions_increment_and_old_stays(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, run_id, _ids = _seed_project(temp_db, owner)
            first = repo.create_report_version(
                s, project_id=project_id, owner_id=owner, title="报告", content="v1 正文",
                citations=[], evidence_ids=[], run_id=run_id,
            )
            second = repo.create_report_version(
                s, project_id=project_id, owner_id=owner, title="报告", content="v2 正文",
                citations=[], evidence_ids=[], report_key=first.report_key, run_id=run_id,
            )
            assert (first.version, second.version) == (1, 2)
            assert first.id != second.id
            kept = repo.get_owned_report(s, first.id, owner)
            assert kept.content == "v1 正文"
            assert [r.version for r in repo.latest_project_reports(s, project_id, owner)] == [2]
            assert len(repo.list_project_reports(s, project_id, owner)) == 2

    def test_same_key_and_version_is_rejected(self, temp_db):
        with _session(temp_db) as s:
            owner = _mk_user(temp_db, "alice")["id"]
            project_id, _run, _ids = _seed_project(temp_db, owner)
            report = repo.create_report_version(
                s, project_id=project_id, owner_id=owner, title="t", content="c",
                citations=[], evidence_ids=[],
            )
            s.add(
                ResearchReportModel(
                    id="dup-id", project_id=project_id, owner_id=owner, title="t",
                    content="c", version=report.version, report_key=report.report_key,
                )
            )
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()


# ---------------------------------------------------------------------------
# 4. 服务层 + API
# ---------------------------------------------------------------------------


class FakeReportGenerator:
    model_name = "fake-report-model"

    def __init__(self, payload):
        self.payload = payload

    def generate(self, *, question, scope, coverage, comparisons, conflicts, evidence):
        return parse_report_payload(self.payload, evidence)


def _payload(card_ids):
    return {
        "title": "RAG 指标综述报告",
        "overview": "两篇论文对指标口径不同。",
        "conclusions": [
            {"text": "A 用 exact match", "evidence_ids": [card_ids["card0"]]},
            {"text": "B 用 F1", "evidence_ids": [card_ids["card1"]]},
            {"text": "整体趋势在改进", "evidence_ids": []},
        ],
        "gaps": ["缺少中文数据集上的评测"],
        "next_steps": ["补充检索中文评测"],
    }


class TestReportApi:
    def test_generate_list_detail_and_citations_resolve(self, client, temp_db, users, monkeypatch):
        project_id, run_id, ids = _seed_project(temp_db, users["alice"]["id"])
        monkeypatch.setattr(
            report_service, "build_default_generator", lambda model_name=None: FakeReportGenerator(_payload(ids))
        )
        headers = _headers(temp_db, "alice")
        created = client.post(
            f"/research/projects/{project_id}/reports", headers=headers, json={"run_id": run_id}
        )
        assert created.status_code == 200, created.text
        report = created.json()
        assert report["version"] == 1 and report["status"] == "generated"
        # 必需小节齐全
        for section in ("## 研究问题与范围", "## 检索与筛选说明", "## 代表论文", "## 维度比较",
                        "## 综合结论", "## 冲突", "## 证据不足与推测", "## 后续建议", "## 参考文献"):
            assert section in report["content"]
        # 事实结论带可反查的引用标记
        assert f"[[e:{ids['card0']}]]" in report["content"]
        assert {c["evidence_id"] for c in report["citations"]} == {ids["card0"], ids["card1"]}
        assert all(c["paper_title"] and c["quote"] for c in report["citations"])
        # 引用可反查到真实原文（正文只存标记，原文随引用映射落库）
        quotes = {c["quote"] for c in report["citations"]}
        assert "Exact match measures the answer string." in quotes
        assert all(c["marker"] == f"[[e:{c['evidence_id']}]]" for c in report["citations"])
        # 无证据结论被标注为推测
        assert "整体趋势在改进" in report["unsupported"] and "推测" in report["unsupported"]

        listed = client.get(f"/research/projects/{project_id}/reports", headers=headers).json()
        assert listed["total"] == 1
        detail = client.get(f"/research/reports/{report['report_id']}", headers=headers).json()
        assert detail["report_id"] == report["report_id"]

    def test_edit_creates_new_version_without_overwriting(self, client, temp_db, users, monkeypatch):
        project_id, run_id, ids = _seed_project(temp_db, users["alice"]["id"])
        monkeypatch.setattr(
            report_service, "build_default_generator", lambda model_name=None: FakeReportGenerator(_payload(ids))
        )
        headers = _headers(temp_db, "alice")
        first = client.post(
            f"/research/projects/{project_id}/reports", headers=headers, json={"run_id": run_id}
        ).json()

        edited_body = first["content"] + f"\n\n用户补充：另有 [[e:{ids['card1']}]] 支持。\n"
        second = client.patch(
            f"/research/reports/{first['report_id']}",
            headers=headers,
            json={"content": edited_body, "title": "RAG 指标综述报告（修订）"},
        )
        assert second.status_code == 200, second.text
        new = second.json()
        assert new["version"] == 2 and new["status"] == "edited"
        assert new["report_key"] == first["report_key"]
        assert new["report_id"] != first["report_id"]
        # 旧版本原样保留
        old = client.get(f"/research/reports/{first['report_id']}", headers=headers).json()
        assert old["version"] == 1 and old["content"] == first["content"]

        # 编辑里伪造引用 → 被剥离并回报，不留下无法反查的引用
        forged = client.patch(
            f"/research/reports/{new['report_id']}",
            headers=headers,
            json={"content": "只有 [[e:ffffffffffff]] 这一句。"},
        )
        third = forged.json()
        assert third["citations"] == []
        assert any("已移除" in w for w in third["warnings"])
        assert "ffffffffffff" not in third["content"]

        versions = client.get(
            f"/research/projects/{project_id}/reports",
            headers=headers,
            params={"include_versions": "true"},
        ).json()
        assert [v["version"] for v in versions["reports"]] == [3, 2, 1]

    def test_exports(self, client, temp_db, users, monkeypatch):
        project_id, run_id, ids = _seed_project(temp_db, users["alice"]["id"])
        monkeypatch.setattr(
            report_service, "build_default_generator", lambda model_name=None: FakeReportGenerator(_payload(ids))
        )
        headers = _headers(temp_db, "alice")
        report = client.post(
            f"/research/projects/{project_id}/reports", headers=headers, json={"run_id": run_id}
        ).json()

        md = client.get(f"/research/reports/{report['report_id']}/export.md", headers=headers)
        assert md.status_code == 200
        assert md.headers["content-type"].startswith("text/markdown")
        assert "[^1]" in md.text and "[^2]" in md.text and "[[e:" not in md.text

        bib = client.get(f"/research/reports/{report['report_id']}/export.bib", headers=headers)
        assert bib.status_code == 200
        assert bib.headers["content-type"].startswith("application/x-bibtex")
        assert bib.text.count("@misc{") == 2
        assert "2301.0000" in bib.text

    def test_cross_user_and_anonymous(self, client, temp_db, users, monkeypatch):
        project_id, run_id, ids = _seed_project(temp_db, users["alice"]["id"])
        monkeypatch.setattr(
            report_service, "build_default_generator", lambda model_name=None: FakeReportGenerator(_payload(ids))
        )
        alice = _headers(temp_db, "alice")
        bob = _headers(temp_db, "bob")
        report = client.post(
            f"/research/projects/{project_id}/reports", headers=alice, json={"run_id": run_id}
        ).json()

        assert client.get(f"/research/reports/{report['report_id']}").status_code == 401
        for method, url in (
            ("get", f"/research/reports/{report['report_id']}"),
            ("get", f"/research/reports/{report['report_id']}/export.md"),
            ("get", f"/research/reports/{report['report_id']}/export.bib"),
            ("delete", f"/research/reports/{report['report_id']}"),
        ):
            assert getattr(client, method)(url, headers=bob).status_code == 404
        assert (
            client.patch(
                f"/research/reports/{report['report_id']}", headers=bob, json={"content": "越权"}
            ).status_code
            == 404
        )
        assert (
            client.get(f"/research/projects/{project_id}/reports", headers=bob).status_code == 404
        )
        # Alice 的报告仍在
        assert client.get(f"/research/reports/{report['report_id']}", headers=alice).status_code == 200

    def test_generation_failure_is_reported_not_hidden(self, client, temp_db, users, monkeypatch):
        project_id, run_id, _ids = _seed_project(temp_db, users["alice"]["id"])

        class Broken:
            model_name = "broken"

            def generate(self, **kwargs):
                raise RuntimeError("模型不可用")

        monkeypatch.setattr(report_service, "build_default_generator", lambda model_name=None: Broken())
        headers = _headers(temp_db, "alice")
        resp = client.post(f"/research/projects/{project_id}/reports", headers=headers, json={})
        assert resp.status_code == 400 and "报告生成失败" in resp.json()["detail"]
        with _session(temp_db) as s:
            assert repo.list_project_reports(s, project_id, users["alice"]["id"]) == []
