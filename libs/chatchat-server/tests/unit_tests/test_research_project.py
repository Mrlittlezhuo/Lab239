"""论文研究领域数据基础离线测试（任务 017 阶段 A）。

覆盖：
- v8 迁移：空库建表、部分唯一索引、v7 库升级保留旧数据、重复升级幂等、
  失败回滚且不记录版本；
- 研究项目 CRUD API（字段校验、删除）；
- 用户 A 不能读写用户 B 的项目、运行、论文与筛选状态（统一 404）；
- 删除项目只清私有产物，不触碰公开 ``paper_record``；
- 未登录 401。

全部使用临时 SQLite（迁移到 v8）+ 临时 JWT 密钥，不连接真实模型、
外部服务或用户数据。
"""
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import (
    MigrationRegistry,
    apply_migrations,
    current_version,
)
from chatchat.server.db.migrate import migrations as migrations_mod
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401
from chatchat.server.db.models.research_model import (  # noqa: F401
    EvidenceCardModel,
    PaperRecordModel,
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


# ---------------------------------------------------------------------------
# fixtures（与 test_intent_routing.py 一致：临时库 + httpx ASGI client）
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "research-project-unit-test-secret-000000",
    )
    return "research-project-unit-test-secret-000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：迁移到 v8、建全表。"""
    path = tmp_path / "research_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 20
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
            s.close()
            _mk_user(db, username)
            s = S()
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


def _registry_upto(last_version):
    """构造只包含内置 1..last_version 的注册表（模拟 v7 存量库）。"""
    registry = MigrationRegistry()
    for version, name, upgrade in migrations_mod._MIGRATIONS[:last_version]:
        registry.register(version, name, upgrade)
    return registry


# ---------------------------------------------------------------------------
# 1. v8 迁移
# ---------------------------------------------------------------------------


class TestV8Migration:
    def test_empty_db_upgrades_to_v8_with_research_schema(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'v8_empty.db'}")
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert result.final_version == 20
            assert 8 in [m.version for m in result.applied]
            tables = set(conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars())
            assert {
                "research_project", "research_run", "paper_record",
                "project_paper", "evidence_card", "research_report",
            } <= tables
            idx = {
                r[1] for r in conn.exec_driver_sql(
                    "PRAGMA index_list('paper_record')"
                ).fetchall()
            }
            assert {"ux_paper_record_doi", "ux_paper_record_arxiv_id"} <= idx
        engine.dispose()

    def test_v8_idempotent_reapply(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'v8_idem.db'}")
        with engine.connect() as conn:
            apply_migrations(build_registry(), conn)
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
            assert second.final_version == 20
        engine.dispose()

    def test_v8_partial_unique_index_on_doi(self, temp_db):
        """同一 DOI 只能有一条规范记录；DOI 为空不受限。"""
        with temp_db["engine"].connect() as conn:
            conn.execute(
                text("INSERT INTO paper_record (id, title, doi) VALUES ('p1', 'A', '10.1/x')")
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO paper_record (id, title, doi) VALUES ('p2', 'B', '10.1/x')")
                )
            conn.rollback()
            conn.execute(text("INSERT INTO paper_record (id, title) VALUES ('p3', 'C')"))
            conn.execute(text("INSERT INTO paper_record (id, title) VALUES ('p4', 'D')"))
            conn.commit()

    def test_v7_db_upgrades_to_v8_preserving_data(self, tmp_path):
        """已有 v7 库升级到 v8：只新增研究表，既有数据完整保留。"""
        path = tmp_path / "v7_to_v8.db"
        engine = create_engine(f"sqlite:///{path}")
        with engine.connect() as conn:
            result = apply_migrations(_registry_upto(7), conn)
            assert result.final_version == 7
        # v7 时代的库：只有旧业务表（研究表尚不存在，由 v8 迁移创建）
        research_names = {
            "research_project", "research_run", "paper_record",
            "project_paper", "evidence_card", "research_report",
        }
        Base.metadata.create_all(
            engine,
            tables=[
                table for name, table in Base.metadata.tables.items()
                if name not in research_names
            ],
        )
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO user_account (id, username, display_name, password_hash, "
                "role, status, must_change_password, auth_version, create_time, update_time) "
                "VALUES ('u1', 'legacy', 'L', 'h', 'user', 'active', 0, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO conversation (id, name, chat_type, owner_id, create_time, update_time) "
                "VALUES ('c1', 'old', 'llm_chat', 'u1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO agent_run (id, owner_id, goal, status, current_step, max_steps, create_time) "
                "VALUES ('r1', 'u1', 'old goal', 'completed', 1, 8, CURRENT_TIMESTAMP)"
            ))
            conn.commit()
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
            assert result.final_version == 20
            assert conn.execute(text("SELECT COUNT(*) FROM user_account")).scalar() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM conversation")).scalar() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM agent_run")).scalar() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM research_project")).scalar() == 0
        engine.dispose()

    def test_v8_failure_rolls_back_and_version_not_recorded(self, tmp_path):
        """v8 中途失败：研究表不残留，版本仍为 v7。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'v8_fail.db'}")

        def bad_v8(conn):
            conn.execute(text("CREATE TABLE research_project (id VARCHAR(32) PRIMARY KEY)"))
            raise RuntimeError("模拟 v8 中途失败")

        registry = _registry_upto(7)
        registry.register(8, "research_broken", bad_v8)
        with engine.connect() as conn:
            with pytest.raises(RuntimeError):
                apply_migrations(registry, conn)
            assert current_version(conn) == 7
            assert not conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='research_project'"
            ).fetchall()
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. 项目 CRUD API
# ---------------------------------------------------------------------------


class TestProjectCrudApi:
    def test_unauthenticated_requests_rejected(self, client):
        assert client.get("/research/projects").status_code == 401
        assert client.post("/research/projects", json={
            "name": "x", "research_question": "q"}).status_code == 401

    def test_create_and_roundtrip(self, client, temp_db):
        h = _headers(temp_db, "alice")
        r = client.post("/research/projects", headers=h, json={
            "name": "无线定位研究", "research_question": "WiFi 指纹定位精度", "scope": "室内"})
        assert r.status_code == 200, r.text
        pid = r.json()["project_id"]

        lst = client.get("/research/projects", headers=h).json()["projects"]
        assert [p["project_id"] for p in lst] == [pid]

        got = client.get(f"/research/projects/{pid}", headers=h)
        assert got.status_code == 200
        assert got.json()["research_question"] == "WiFi 指纹定位精度"

        upd = client.patch(f"/research/projects/{pid}", headers=h,
                           json={"name": "改名", "status": "archived"})
        assert upd.status_code == 200
        assert upd.json()["name"] == "改名" and upd.json()["status"] == "archived"

    def test_create_requires_fields(self, client, temp_db):
        h = _headers(temp_db, "alice")
        assert client.post("/research/projects", headers=h,
                           json={"name": "", "research_question": "q"}).status_code == 422
        assert client.post("/research/projects", headers=h,
                           json={"name": "n"}).status_code == 422

    def test_body_owner_id_ignored(self, client, temp_db, users):
        """请求体里的 owner_id 绝不参与授权：伪造字段被忽略。"""
        h = _headers(temp_db, "alice")
        r = client.post("/research/projects", headers=h, json={
            "name": "n", "research_question": "q", "owner_id": users["bob"]["id"]})
        assert r.status_code == 200
        with _session(temp_db) as s:
            project = repo.get_owned_project(
                s, r.json()["project_id"], users["alice"]["id"])
            assert project is not None  # 归属仍是认证用户

    def test_cross_user_project_invisible(self, client, temp_db):
        ha = _headers(temp_db, "alice")
        hb = _headers(temp_db, "bob")
        pid = client.post("/research/projects", headers=ha,
                          json={"name": "a", "research_question": "q"}).json()["project_id"]
        assert client.get(f"/research/projects/{pid}", headers=hb).status_code == 404
        assert client.patch(f"/research/projects/{pid}", headers=hb,
                            json={"name": "hack"}).status_code == 404
        assert client.delete(f"/research/projects/{pid}", headers=hb).status_code == 404
        assert client.post(f"/research/projects/{pid}/runs", headers=hb).status_code == 404
        assert client.get(f"/research/projects/{pid}/papers", headers=hb).status_code == 404

    def test_delete_project(self, client, temp_db):
        h = _headers(temp_db, "alice")
        pid = client.post("/research/projects", headers=h,
                          json={"name": "a", "research_question": "q"}).json()["project_id"]
        assert client.delete(f"/research/projects/{pid}", headers=h).status_code == 200
        assert client.get(f"/research/projects/{pid}", headers=h).status_code == 404


# ---------------------------------------------------------------------------
# 3. 删除级联：私有产物清除，公开论文元数据保留
# ---------------------------------------------------------------------------


class TestDeleteCascade:
    def test_delete_preserves_public_paper_records(self, temp_db):
        with _session(temp_db) as s:
            project = repo.create_project(s, "owner-a", "p", "q")
            run = repo.create_run(s, "owner-a", project.id)
            record, created = repo.upsert_paper(s, {
                "title": "Shared Paper", "doi": "10.1/shared", "source": "arxiv",
                "title_key": "shared paper:2024",
            })
            s.commit()
            repo.link_project_paper(s, project.id, "owner-a", record.id, run_id=run.id)
            s.commit()
            s.add(EvidenceCardModel(id="e1", project_id=project.id, paper_id=record.id,
                                    owner_id="owner-a"))
            s.add(ResearchReportModel(id="rep1", project_id=project.id, owner_id="owner-a"))
            s.commit()
            project_id, paper_id = project.id, record.id

        with _session(temp_db) as s:
            repo.delete_owned_project(s, project_id, "owner-a")

        with _session(temp_db) as s:
            assert s.query(ResearchRunModel).count() == 0
            assert s.query(ProjectPaperModel).count() == 0
            assert s.query(EvidenceCardModel).count() == 0
            assert s.query(ResearchReportModel).count() == 0
            # 公开论文元数据不受项目删除影响
            assert s.get(PaperRecordModel, paper_id) is not None


# ---------------------------------------------------------------------------
# 4. 运行/论文/筛选的所有者隔离
# ---------------------------------------------------------------------------


class TestOwnershipIsolation:
    def _make_project_run_paper(self, db, owner_id):
        with _session(db) as s:
            project = repo.create_project(s, owner_id, "p", "q")
            run = repo.create_run(s, owner_id, project.id)
            record, _ = repo.upsert_paper(s, {
                "title": "T", "arxiv_id": "2301.00001", "source": "arxiv",
                "title_key": "t:2023",
            })
            s.commit()
            pp = repo.link_project_paper(s, project.id, owner_id, record.id)
            s.commit()
            return project.id, run.id, pp.id

    def test_cross_user_run_invisible_via_api(self, temp_db, client, monkeypatch):
        # cancel 走服务层自有会话：必须指向临时库，绝不触碰真实数据库
        from chatchat.server.research import service as research_service

        monkeypatch.setattr(
            research_service, "_session_factory", temp_db["Session"])
        _, run_id, _ = self._make_project_run_paper(temp_db, "owner-a")
        hb = _headers(temp_db, "bob")
        assert client.get(f"/research/runs/{run_id}", headers=hb).status_code == 404
        assert client.post(f"/research/runs/{run_id}/cancel", headers=hb).status_code == 404
        # 合法计划体：所有权校验必须给出 404
        assert client.put(f"/research/runs/{run_id}/plan", headers=hb, json={
            "plan": {"summary": "s",
                     "sub_questions": [{"question": "q", "queries": ["x"]}],
                     "sources": ["arxiv"]}}).status_code == 404

    def test_cross_user_screening_invisible(self, temp_db, client):
        _, _, pp_id = self._make_project_run_paper(temp_db, "owner-a")
        hb = _headers(temp_db, "bob")
        r = client.patch(f"/research/papers/{pp_id}/screening", headers=hb,
                         json={"screening_status": "included", "reason": "hack"})
        assert r.status_code == 404
        with _session(temp_db) as s:
            pp = repo.get_owned_project_paper(s, pp_id, "owner-a")
            assert pp.screening_status == "pending_review"

    def test_owner_can_screen(self, temp_db, client, users):
        h = _headers(temp_db, "alice")
        alice_id = users["alice"]["id"]
        pid = client.post("/research/projects", headers=h,
                          json={"name": "p2", "research_question": "q2"}).json()["project_id"]
        with _session(temp_db) as s:
            record, _ = repo.upsert_paper(s, {"title": "T2", "doi": "10.2/t2", "source": "arxiv"})
            s.commit()
            # 关系 owner 必须与项目 owner（认证用户 id）一致
            pp = repo.link_project_paper(s, pid, alice_id, record.id)
            s.commit()
            pp_id = pp.id
        ok = client.patch(f"/research/papers/{pp_id}/screening", headers=h,
                          json={"screening_status": "excluded", "reason": "不相关"})
        assert ok.status_code == 200
        assert ok.json()["screening_status"] == "excluded"
        assert ok.json()["screening_reason"] == "不相关"

    def test_invalid_screening_status_rejected(self, temp_db, client, users):
        h = _headers(temp_db, "alice")
        pid = client.post("/research/projects", headers=h,
                          json={"name": "p", "research_question": "q"}).json()["project_id"]
        with _session(temp_db) as s:
            record, _ = repo.upsert_paper(s, {"title": "T3", "doi": "10.3/t3", "source": "arxiv"})
            s.commit()
            pp = repo.link_project_paper(s, pid, users["alice"]["id"], record.id)
            s.commit()
            pp_id = pp.id
        r = client.patch(f"/research/papers/{pp_id}/screening", headers=h,
                         json={"screening_status": "bogus"})
        assert r.status_code == 400

    def test_paper_records_shared_across_projects_not_screenings(self, temp_db):
        """同一规范论文可被两个项目复用，但筛选状态互不可见。"""
        with _session(temp_db) as s:
            pa = repo.create_project(s, "alice", "A", "q")
            pb = repo.create_project(s, "bob", "B", "q")
            record, created = repo.upsert_paper(
                s, {"title": "Shared", "doi": "10.9/s", "source": "arxiv"})
            s.commit()
            assert created
            pp_a = repo.link_project_paper(s, pa.id, "alice", record.id)
            pp_b = repo.link_project_paper(s, pb.id, "bob", record.id)
            s.commit()
            assert pp_a.id != pp_b.id
            # 论文记录只有一条
            assert s.query(PaperRecordModel).count() == 1
            repo.set_screening(s, pp_a.id, "alice", "included", "ok")
            assert repo.get_owned_project_paper(
                s, pp_b.id, "bob").screening_status == "pending_review"
            # 跨用户不可见
            assert repo.get_owned_project_paper(s, pp_a.id, "bob") is None


# ---------------------------------------------------------------------------
# 5. 仓储层状态机基础（确认/取消/重试的非法迁移）
# ---------------------------------------------------------------------------


class TestRunStateMachine:
    def test_confirm_requires_awaiting_confirmation(self, temp_db):
        with _session(temp_db) as s:
            project = repo.create_project(s, "o", "p", "q")
            run = repo.create_run(s, "o", project.id)
            with pytest.raises(repo.ResearchConflict):
                repo.confirm_run(s, run.id, "o")  # pending 不可确认
            repo.set_plan(s, run.id, "o", "{}")
            confirmed = repo.confirm_run(s, run.id, "o")
            assert confirmed.status == "running"
            with pytest.raises(repo.ResearchConflict):
                repo.confirm_run(s, run.id, "o")  # 不可重复确认

    def test_update_plan_only_before_confirmation(self, temp_db):
        with _session(temp_db) as s:
            project = repo.create_project(s, "o", "p", "q")
            run = repo.create_run(s, "o", project.id)
            with pytest.raises(repo.ResearchConflict):
                repo.update_plan(s, run.id, "o", '{"x": 1}')  # pending
            repo.set_plan(s, run.id, "o", "{}")
            updated = repo.update_plan(s, run.id, "o", '{"y": 2}')
            assert updated.plan_json == '{"y": 2}'
            repo.confirm_run(s, run.id, "o")
            with pytest.raises(repo.ResearchConflict):
                repo.update_plan(s, run.id, "o", '{"z": 3}')  # running 后不可改

    def test_cancel_and_retry_transitions(self, temp_db):
        with _session(temp_db) as s:
            project = repo.create_project(s, "o", "p", "q")
            run = repo.create_run(s, "o", project.id)
            repo.set_plan(s, run.id, "o", "{}")
            cancelled = repo.cancel_run(s, run.id, "o")
            assert cancelled.status == "cancelled"
            # cancelled 不自动重试
            assert repo.retry_run(s, run.id, "o") is None
            # failed 可重试
            run2 = repo.create_run(s, "o", project.id)
            repo.set_plan(s, run2.id, "o", "{}")
            repo.confirm_run(s, run2.id, "o")
            repo.finalize_run(s, run2.id, "o", "failed", error="来源全部失败")
            retried = repo.retry_run(s, run2.id, "o")
            assert retried is not None and retried.status == "running"
            # 重试后 running 可再次收敛；终态之后不可被覆盖
            done = repo.finalize_run(s, run2.id, "o", "completed")
            assert done is not None and done.status == "completed"
            assert repo.finalize_run(s, run2.id, "o", "failed") is None
