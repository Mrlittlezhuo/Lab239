"""候选论文标准化/去重与幂等入库离线测试（任务 017 阶段 B）。

覆盖：
- DOI / ArXiv ID / URL / 标题规范化；
- 批内去重优先级（doi → arxiv → url → title+year）与来源合并；
- ``upsert_paper`` 跨运行幂等（稳定标识命中合并来源、补齐缺失字段、
  不覆盖已有值）；
- ``link_project_paper`` 按 ``(project_id, paper_id)`` 幂等，
  重试不重置筛选状态。

不连接外部网络；使用临时 SQLite（v8）。
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.research_model import (  # noqa: F401
    PaperRecordModel,
    ProjectPaperModel,
)
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.papers import (
    PaperCandidate,
    dedup_candidates,
    dedup_priority_keys,
    normalize_arxiv_id,
    normalize_doi,
    normalize_title,
    normalize_url,
    paper_record_fields,
    standardize_candidate,
    title_key,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'papers_test.db'}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 12
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 1. 规范化
# ---------------------------------------------------------------------------


class TestNormalization:
    def test_normalize_doi(self):
        assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
        assert normalize_doi("  DOI:10.1000/Abc. ") in ("10.1000/abc",)
        assert normalize_doi("10.1000/abc") == "10.1000/abc"
        assert normalize_doi("") is None
        assert normalize_doi(None) is None

    def test_normalize_arxiv_id(self):
        assert normalize_arxiv_id("http://arxiv.org/abs/2301.00001v2") == "2301.00001"
        assert normalize_arxiv_id("arXiv:2301.00001v1") == "2301.00001"
        assert normalize_arxiv_id("2301.00001") == "2301.00001"
        assert normalize_arxiv_id("https://arxiv.org/pdf/2301.00001.pdf") == "2301.00001"
        assert normalize_arxiv_id(None) is None

    def test_normalize_url(self):
        assert normalize_url("HTTPS://Example.COM/Path/#frag") == "https://example.com/Path"
        assert normalize_url("https://example.com/a/") == "https://example.com/a"
        assert normalize_url("") is None

    def test_normalize_title_and_key(self):
        assert normalize_title("  Graph-RAG:  A Survey!! ") == "graph rag a survey"
        assert title_key("Graph-RAG: A Survey", 2024) == "graph rag a survey:2024"
        assert title_key("", 2024) is None
        assert title_key("Solo", None) == "solo"

    def test_dedup_priority_keys_order(self):
        c = PaperCandidate(
            title="T", source="s", doi="10.1/x", arxiv_id="2301.00001",
            url="https://a/b", year=2023,
        )
        keys = dedup_priority_keys(c)
        assert keys[0] == ("doi", "10.1/x")
        assert ("arxiv_id", "2301.00001") in keys
        assert keys[-1] == ("title_key", "t:2023")


# ---------------------------------------------------------------------------
# 2. 批内去重
# ---------------------------------------------------------------------------


def _c(**kw):
    kw.setdefault("source", "arxiv")
    return PaperCandidate(**kw)


class TestDedupCandidates:
    def test_doi_match_merges_sources(self):
        # 实际管线中候选先经 standardize_candidate 再进入去重
        a = standardize_candidate(_c(title="Paper A", doi="10.1000/x", year=2023))
        b = standardize_candidate(_c(title="Paper A (kb copy)", doi="DOI:10.1000/X",
                                     source="local_kb", url="https://kb/a", abstract="abs"))
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 1 and merged == 1
        assert kept[0].title == "Paper A"
        # 来源可追溯：两条来源信息都保留
        assert kept[0].merged_sources == [
            {"source": "arxiv", "url": ""},
            {"source": "local_kb", "url": "https://kb/a"},
        ]
        # 缺失字段补齐
        assert kept[0].url == "https://kb/a"
        assert kept[0].abstract == "abs"

    def test_arxiv_id_match_across_url_forms(self):
        a = _c(title="X", arxiv_id="2301.00001")
        b = _c(title="X preprint", source="local_kb",
               url="https://arxiv.org/abs/2301.00001v3")
        # 无 arxiv 字段但 URL 相同不命中；标准化后应命中 arxiv 键
        b.arxiv_id = normalize_arxiv_id(b.url)
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 1 and merged == 1

    def test_url_match(self):
        a = standardize_candidate(_c(title="Same Page", url="https://example.com/paper#s1"))
        b = standardize_candidate(_c(title="Same Page (dup)", url="https://example.com/paper/"))
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 1 and merged == 1

    def test_title_year_fallback(self):
        a = _c(title="Wireless Fingerprinting Localization", year=2022)
        b = _c(title="Wireless Fingerprinting Localization.", year=2022,
               source="local_kb")
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 1 and merged == 1
        # 年份不同不合并
        c = _c(title="Wireless Fingerprinting Localization", year=2021)
        kept2, merged2 = dedup_candidates([a, c])
        assert len(kept2) == 2 and merged2 == 0

    def test_stable_id_wins_over_title_mismatch(self):
        """DOI 相同但标题不同（出版方改名）仍合并。"""
        a = _c(title="Old Title", doi="10.5/y")
        b = _c(title="Completely Different Title", doi="10.5/y")
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 1 and merged == 1

    def test_no_false_positive(self):
        a = _c(title="Paper One", doi="10.1/one")
        b = _c(title="Paper Two", doi="10.1/two")
        kept, merged = dedup_candidates([a, b])
        assert len(kept) == 2 and merged == 0


# ---------------------------------------------------------------------------
# 3. 标准化与入库字段
# ---------------------------------------------------------------------------


class TestStandardize:
    def test_standardize_candidate(self):
        raw = PaperCandidate(
            title="  A Survey of GraphRAG  ", source="arxiv",
            authors=[" Alice ", "", "Bob"], year=2024,
            doi="https://doi.org/10.1005/G", arxiv_id="arXiv:2401.00002v1",
            url="https://arxiv.org/abs/2401.00002v1#x",
            abstract="x" * 5000,
        )
        c = standardize_candidate(raw)
        assert c.title == "A Survey of GraphRAG"
        assert c.authors == ["Alice", "Bob"]
        assert c.doi == "10.1005/g"
        assert c.arxiv_id == "2401.00002"
        assert c.url == "https://arxiv.org/abs/2401.00002v1"
        assert len(c.abstract) == 4000

    def test_paper_record_fields_carries_merged_sources(self):
        c = _c(title="M", doi="10.9/m", url="https://a")
        c.merged_sources = [{"source": "local_kb", "url": "https://b"}]
        fields = paper_record_fields(c)
        assert fields["sources"] == [
            {"source": "arxiv", "url": "https://a"},
            {"source": "local_kb", "url": "https://b"},
        ]
        assert fields["title_key"] == "m"


# ---------------------------------------------------------------------------
# 4. 仓储层幂等入库
# ---------------------------------------------------------------------------


class TestUpsertPaper:
    def test_creates_then_merges_by_doi(self, db):
        with _session(db) as s:
            r1, created1 = repo.upsert_paper(s, {
                "title": "Paper A", "doi": "10.1/a", "source": "arxiv",
                "url": "https://arxiv.org/a", "year": 2023,
            })
            s.commit()
            assert created1
            # 仓储层接收的是已规范化字段（规范化在 papers 层完成）
            r2, created2 = repo.upsert_paper(s, {
                "title": "Paper A copy", "doi": "10.1/a", "source": "local_kb",
                "url": "https://kb/a",
            })
            s.commit()
            assert not created2
            assert r1.id == r2.id
            import json
            sources = json.loads(s.get(PaperRecordModel, r1.id).sources_json)
            assert {x["source"] for x in sources} == {"arxiv", "local_kb"}
            # 不覆盖已有字段（标题保持首条）
            assert s.get(PaperRecordModel, r1.id).title == "Paper A"
            assert s.query(PaperRecordModel).count() == 1

    def test_backfills_missing_fields(self, db):
        with _session(db) as s:
            r1, _ = repo.upsert_paper(s, {"title": "B", "arxiv_id": "2301.00009"})
            s.commit()
            r2, created = repo.upsert_paper(s, {
                "title": "B", "arxiv_id": "2301.00009",
                "doi": "10.2/b", "year": 2023, "abstract": "abs",
                "authors": "Alice; Bob",
            })
            s.commit()
            assert not created and r1.id == r2.id
            rec = s.get(PaperRecordModel, r1.id)
            assert rec.doi == "10.2/b" and rec.year == 2023
            assert rec.abstract == "abs" and rec.authors == "Alice; Bob"

    def test_falls_back_to_title_key(self, db):
        with _session(db) as s:
            r1, _ = repo.upsert_paper(s, {
                "title": "No Identifier Paper", "year": 2020,
                "title_key": "no identifier paper:2020", "source": "arxiv",
            })
            s.commit()
            r2, created = repo.upsert_paper(s, {
                "title": "No Identifier Paper", "year": 2020,
                "title_key": "no identifier paper:2020", "source": "local_kb",
            })
            s.commit()
            assert not created and r1.id == r2.id

    def test_repeated_upsert_is_idempotent(self, db):
        payload = {"title": "C", "doi": "10.3/c", "source": "arxiv", "url": "https://c"}
        with _session(db) as s:
            for _ in range(3):
                repo.upsert_paper(s, payload)
            s.commit()
            assert s.query(PaperRecordModel).count() == 1
            import json
            rec = s.query(PaperRecordModel).one()
            assert len(json.loads(rec.sources_json)) == 1


class TestLinkProjectPaper:
    def test_link_idempotent_and_preserves_screening(self, db):
        with _session(db) as s:
            project = repo.create_project(s, "o", "p", "q")
            run1 = repo.create_run(s, "o", project.id)
            record, _ = repo.upsert_paper(s, {"title": "L", "doi": "10.4/l"})
            s.commit()
            pp1 = repo.link_project_paper(s, project.id, "o", record.id,
                                          run_id=run1.id, relevance=0.2)
            s.commit()
            # 用户已筛选
            repo.set_screening(s, pp1.id, "o", "excluded", "不相关")
            # 重试再次挂接：不新增关系、不重置筛选
            run2 = repo.create_run(s, "o", project.id)
            pp2 = repo.link_project_paper(s, project.id, "o", record.id,
                                          run_id=run2.id, relevance=0.9)
            s.commit()
            assert pp1.id == pp2.id
            assert s.query(ProjectPaperModel).count() == 1
            fresh = repo.get_owned_project_paper(s, pp1.id, "o")
            assert fresh.screening_status == "excluded"
            assert fresh.screening_reason == "不相关"
            assert fresh.relevance == 0.9  # 只提升相关度
            assert fresh.first_run_id == run1.id  # 首次来源保留
