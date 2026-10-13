"""知识库入库统计的离线单元测试（知识库列表页的数据来源）。

覆盖：

- ``get_kb_statistics`` 按知识库聚合文件数 / chunks / 总大小 / 最近更新时间；
- 没有入库文件的知识库不出现在结果里（前端必须按缺失处理）；
- 历史行里的 NULL 计数与大小按 0 处理，mtime 全 0 时返回 None；
- ``GET /knowledge_base/list_statistics`` 已注册且继承 kb_router 的登录鉴权。

只使用临时 SQLite，直接写入 knowledge_file 行；不连接向量库、content 目录、
网络或真实运行数据库。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server.kb_routes import kb_router
from chatchat.server.db import session as db_session
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.knowledge_file_model import KnowledgeFileModel
from chatchat.server.db.repository.knowledge_file_repository import get_kb_statistics


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    path = tmp_path / "kb_stats.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    # 先跑迁移建版本表，再补建迁移未覆盖的业务表（knowledge_file 由 create_all 建）
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # with_session 取的是 chatchat.server.db.session.SessionLocal，
    # 必须就地替换，仓库函数才会落到临时库而不是真实运行数据库。
    monkeypatch.setattr(db_session, "SessionLocal", Session)
    yield Session
    engine.dispose()


def _add_files(Session, kb_name, files):
    with Session() as session:
        for f in files:
            session.add(
                KnowledgeFileModel(
                    file_name=f["file_name"],
                    file_ext=f["file_ext"],
                    kb_name=kb_name,
                    file_size=f.get("file_size"),
                    docs_count=f.get("docs_count"),
                    file_mtime=f.get("file_mtime"),
                )
            )
        session.commit()


def _by_name(rows):
    return {r["kb_name"]: r for r in rows}


class TestGetKbStatistics:
    def test_groups_per_knowledge_base(self, temp_db):
        _add_files(
            temp_db,
            "无线通信",
            [
                {"file_name": "a.pdf", "file_ext": ".pdf", "file_size": 2048,
                 "docs_count": 12, "file_mtime": 100.0},
                {"file_name": "b.pdf", "file_ext": ".pdf", "file_size": 1024,
                 "docs_count": 8, "file_mtime": 300.0},
                {"file_name": "c.txt", "file_ext": ".txt", "file_size": 512,
                 "docs_count": 3, "file_mtime": 200.0},
            ],
        )
        _add_files(
            temp_db,
            "samples",
            [{"file_name": "d.txt", "file_ext": ".txt", "file_size": 100,
              "docs_count": 1, "file_mtime": 50.0}],
        )

        stats = _by_name(get_kb_statistics())

        assert set(stats) == {"无线通信", "samples"}
        kb = stats["无线通信"]
        assert kb["file_count"] == 3
        assert kb["docs_count"] == 23
        assert kb["total_size"] == 3584
        assert kb["latest_mtime"] == 300.0
        assert "file_exts" not in kb

    def test_kb_without_ingested_files_is_absent(self, temp_db):
        """空知识库没有 knowledge_file 行，前端按“无统计”渲染，不能编造 0 行。"""
        _add_files(temp_db, "samples", [{"file_name": "a.txt", "file_ext": ".txt"}])

        stats = _by_name(get_kb_statistics())

        assert "empty_kb" not in stats
        assert stats["samples"]["file_count"] == 1

    def test_null_counts_and_sizes_become_zero(self, temp_db):
        _add_files(temp_db, "legacy", [{"file_name": "old.txt", "file_ext": ".txt"}])

        kb = _by_name(get_kb_statistics())["legacy"]

        assert kb["docs_count"] == 0
        assert kb["total_size"] == 0
        assert kb["latest_mtime"] is None

    def test_empty_table_returns_empty_list(self, temp_db):
        assert get_kb_statistics() == []


class TestListStatisticsRoute:
    def test_route_registered_and_requires_login(self):
        """路由存在，且继承 kb_router 的 router 级鉴权（未登录 401）。"""
        paths = {
            (r.path, tuple(sorted(r.methods)))
            for r in kb_router.routes
            if hasattr(r, "methods")
        }
        assert ("/knowledge_base/list_statistics", ("GET",)) in paths

        app = FastAPI()
        app.include_router(kb_router)
        with TestClient(app) as client:
            res = client.get("/knowledge_base/list_statistics")
        assert res.status_code == 401
