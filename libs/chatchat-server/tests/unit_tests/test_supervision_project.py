"""师生协作数据基础离线测试（《学生研究与导师审核功能重新设计方案》里程碑0A）。

覆盖：
- v17 迁移：空库建四张协作表与部分唯一索引、v16 存量库升级保留旧数据并
  补可空外键列、重复升级幂等、库层钉死"一名在册学生 + 一名在册主导师"；
- 协作项目 CRUD 与状态机（筹备中 → 进行中 → 归档申请 → 同意/拒绝）；
- 权限：未登录 401、非成员与不存在统一 404、角色不足 403、
  非法状态迁移 409、字段非法 400；
- 成员退出后失去后续访问，但历史审计与通知保留（§13）；
- 空白筹备项目可软删除，已有内容只能归档（§15.1）；
- 通知按接收者独立保存已读状态，同一事件编号不重复发送（§12）；
- 管理员可查看非自己项目的记录，且这次查看本身形成审计（§13）。

全部使用临时 SQLite + 临时 JWT 密钥，不连接真实模型、外部服务或用户数据。
"""
import asyncio
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server.supervision_routes import supervision_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
from chatchat.server.db.migrate import migrations as migrations_mod
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel  # noqa: F401
from chatchat.server.db.models.message_model import MessageModel  # noqa: F401
from chatchat.server.db.models.supervision_model import (  # noqa: F401
    SupervisionAuditEventModel,
    SupervisionNotificationModel,
    SupervisionProjectMemberModel,
    SupervisionProjectModel,
)
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import supervision_repository as repo
from chatchat.server.db.session import get_db as db_get_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret",
        lambda: "supervision-project-unit-test-secret-00000",
    )
    return "supervision-project-unit-test-secret-00000"


@pytest.fixture
def temp_db(secret, tmp_path):
    """临时 SQLite 数据库：迁移到 v17、建全表。"""
    path = tmp_path / "supervision_test.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 17
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


def _mk_user(db, username, role="user"):
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
            u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def users(temp_db):
    return {
        "alice": _mk_user(temp_db, "alice"),          # 学生
        "carol": _mk_user(temp_db, "carol"),          # 第二名学生（用于唯一约束）
        "mentor": _mk_user(temp_db, "mentor"),        # 主导师
        "bob": _mk_user(temp_db, "bob"),              # 项目外用户
        "root": _mk_user(temp_db, "root", role="admin"),
    }


@pytest.fixture
def app(temp_db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(supervision_router)

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
    """构造只包含内置 1..last_version 的注册表（模拟 v16 存量库）。"""
    from chatchat.server.db.migrate.base import MigrationRegistry

    registry = MigrationRegistry()
    for version, name, upgrade in migrations_mod._MIGRATIONS[:last_version]:
        registry.register(version, name, upgrade)
    return registry


def _create_project(client, db, name="遥感变化检测"):
    r = client.post(
        "/supervision/projects",
        headers=_headers(db, "alice"),
        json={"name": name, "description": "面向论文写作的协作项目"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _activate(client, db, project_id):
    client.post(
        f"/supervision/projects/{project_id}/members",
        headers=_headers(db, "alice"),
        json={"user_id": users_id["mentor"], "role": "mentor"},
    )
    r = client.post(
        f"/supervision/projects/{project_id}/activate",
        headers=_headers(db, "alice"),
    )
    assert r.status_code == 200, r.text
    return r.json()


users_id = {}


@pytest.fixture(autouse=True)
def _track_users(users):
    users_id.clear()
    users_id.update({k: v["id"] for k, v in users.items()})


# ---------------------------------------------------------------------------
# 1. v17 迁移
# ---------------------------------------------------------------------------


class TestV17Migration:
    def test_empty_db_creates_supervision_schema(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'v17_empty.db'}")
        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert result.final_version == 17
            assert 17 in [m.version for m in result.applied]
            tables = set(conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars())
            assert {
                "supervision_project",
                "supervision_project_member",
                "supervision_audit_event",
                "supervision_notification",
            } <= tables

            indexes = {
                i["name"] for i in inspect(conn).get_indexes("supervision_project_member")
            }
            assert {
                "uq_supervision_member_active_student",
                "uq_supervision_member_active_mentor",
            } <= indexes
        engine.dispose()

    def test_upgrade_from_v16_preserves_old_data(self, tmp_path):
        """v16 存量库升级：旧业务数据一字不动，只加新表与可空新列。"""
        engine = create_engine(f"sqlite:///{tmp_path / 'v16_legacy.db'}")
        with engine.connect() as conn:
            apply_migrations(_registry_upto(16), conn)
            assert current_version(conn) == 16
            # conversation 由应用启动的 create_all 创建，存量库里手工补出旧形状
            conn.execute(text(
                "CREATE TABLE conversation (id VARCHAR(32) PRIMARY KEY,"
                " name VARCHAR(50), chat_type VARCHAR(50), owner_id VARCHAR(32),"
                " create_time TIMESTAMP, update_time TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO user_account (id, username, display_name, password_hash,"
                " create_time, update_time) VALUES ('u-old','olduser','旧用户','x',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.execute(text(
                "INSERT INTO conversation (id, name, chat_type, owner_id, create_time,"
                " update_time) VALUES ('c-old','旧会话','openai','u-old',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.commit()

        with engine.connect() as conn:
            result = apply_migrations(build_registry(), conn)
            assert [m.version for m in result.applied] == [17]
            assert result.final_version == 17
            assert conn.execute(
                text("SELECT COUNT(*) FROM conversation WHERE id = 'c-old'")
            ).scalar() == 1
            assert conn.execute(
                text("SELECT COUNT(*) FROM user_account WHERE id = 'u-old'")
            ).scalar() == 1
            # 旧会话的协作外键保持 NULL：升级不改变既有会话归属
            assert conn.execute(
                text("SELECT supervision_project_id FROM conversation WHERE id = 'c-old'")
            ).scalar() is None
            conn.commit()
        engine.dispose()

    def test_repeated_upgrade_is_idempotent(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'v17_idem.db'}")
        with engine.connect() as conn:
            first = apply_migrations(build_registry(), conn)
            assert first.final_version == 17
        with engine.connect() as conn:
            second = apply_migrations(build_registry(), conn)
            assert second.applied == []
            assert second.final_version == 17
        # 直接重跑 v17 的 DDL 也不报错、不丢数据（IF NOT EXISTS）
        with engine.connect() as conn:
            conn.execute(text(
                "INSERT INTO supervision_project (id, code, name, status, create_time,"
                " update_time) VALUES ('p1','RP-001','旧项目','draft',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
            conn.commit()
            migrations_mod._v17_supervision_collaboration(conn)
            conn.commit()
            assert conn.execute(
                text("SELECT COUNT(*) FROM supervision_project")
            ).scalar() == 1
        engine.dispose()

    def test_db_rejects_second_active_student(self, temp_db):
        """首期每项目一名在册学生由库层部分唯一索引兜住（§3.2）。"""
        with _session(temp_db) as s:
            s.add(SupervisionProjectModel(
                id="p1", code="RP-001", name="项目", status="active",
            ))
            s.add_all([
                SupervisionProjectMemberModel(
                    id="m1", project_id="p1", user_id="u-a", role="student",
                    status="active",
                ),
                SupervisionProjectMemberModel(
                    id="m2", project_id="p1", user_id="u-b", role="student",
                    status="active",
                ),
            ])
            with pytest.raises(IntegrityError):
                s.commit()
            s.rollback()

    def test_removed_member_frees_the_role_slot(self, temp_db):
        """移出成员后同一项目可以再接纳一名学生，历史记录仍在。"""
        with _session(temp_db) as s:
            s.add(SupervisionProjectModel(
                id="p1", code="RP-001", name="项目", status="active",
            ))
            s.add(SupervisionProjectMemberModel(
                id="m1", project_id="p1", user_id="u-a", role="student",
                status="active",
            ))
            s.commit()
            member = s.get(SupervisionProjectMemberModel, "m1")
            member.status = "removed"
            s.add(SupervisionProjectMemberModel(
                id="m2", project_id="p1", user_id="u-b", role="student",
                status="active",
            ))
            s.commit()
            assert s.get(SupervisionProjectMemberModel, "m1").status == "removed"
            assert s.get(SupervisionProjectMemberModel, "m2").status == "active"


# ---------------------------------------------------------------------------
# 2. 认证与可见性
# ---------------------------------------------------------------------------


class TestAccessControl:
    def test_unauthenticated_requests_are_rejected(self, client, temp_db):
        for method, url in [
            ("get", "/supervision/projects"),
            ("post", "/supervision/projects"),
            ("get", "/supervision/notifications"),
        ]:
            r = getattr(client, method)(url, json={"name": "x"} if method == "post" else None)
            assert r.status_code == 401, (method, url, r.text)

    def test_expired_or_revoked_token_is_rejected(self, client, temp_db, users):
        token = auth_jwt.create_token(users["alice"]["id"], 999999)
        r = client.get(
            "/supervision/projects", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401

    def test_non_member_gets_the_same_404_as_missing_project(self, client, temp_db):
        project = _create_project(client, temp_db, "同学的项目")
        outsider = _headers(temp_db, "bob")
        for url in [
            f"/supervision/projects/{project['project_id']}",
            f"/supervision/projects/{project['code']}",
            f"/supervision/projects/no-such-project",
            f"/supervision/projects/{project['project_id']}/audit",
        ]:
            r = client.get(url, headers=outsider)
            assert r.status_code == 404, (url, r.text)
        # 越权写操作同样收敛成 404，不泄露项目是否存在
        r = client.post(
            f"/supervision/projects/{project['project_id']}/activate", headers=outsider
        )
        assert r.status_code == 404

    def test_project_list_only_shows_own_projects(self, client, temp_db):
        _create_project(client, temp_db, "爱丽丝的课题")
        r = client.post(
            "/supervision/projects",
            headers=_headers(temp_db, "bob"),
            json={"name": "鲍勃的课题"},
        )
        assert r.status_code == 200
        mine = client.get(
            "/supervision/projects", headers=_headers(temp_db, "bob")
        ).json()
        assert [p["name"] for p in mine] == ["鲍勃的课题"]
        assert mine[0]["my_role"] == "student"

    def test_admin_reads_foreign_project_and_the_read_is_audited(self, client, temp_db):
        project = _create_project(client, temp_db, "受监管项目")
        root = _headers(temp_db, "root")
        r = client.get(
            f"/supervision/projects/{project['project_id']}", headers=root
        )
        assert r.status_code == 200
        assert r.json()["my_role"] is None
        events = client.get(
            f"/supervision/projects/{project['project_id']}/audit", headers=root
        ).json()
        assert "管理员查看项目审计" in [e["action"] for e in events]
        # 管理员没有项目角色，不能替学生或导师做动作
        r = client.post(
            f"/supervision/projects/{project['project_id']}/activate", headers=root
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. 项目状态机与成员
# ---------------------------------------------------------------------------


class TestProjectLifecycle:
    def test_created_project_is_draft_with_creator_as_student(self, client, temp_db):
        project = _create_project(client, temp_db)
        assert project["code"] == "RP-001"
        assert project["status"] == "draft"
        assert [(m["user_id"], m["role"]) for m in project["members"]] == [
            (users_id["alice"], "student")
        ]

    def test_codes_are_unique_and_readable(self, client, temp_db):
        first = _create_project(client, temp_db, "第一个")
        second = client.post(
            "/supervision/projects", headers=_headers(temp_db, "bob"), json={"name": "第二个"}
        ).json()
        assert (first["code"], second["code"]) == ("RP-001", "RP-002")

    def test_activate_requires_both_roles(self, client, temp_db):
        project = _create_project(client, temp_db)
        r = client.post(
            f"/supervision/projects/{project['project_id']}/activate",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 409
        assert "主导师" in r.text
        r = client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "draft"
        r = client.post(
            f"/supervision/projects/{project['project_id']}/activate",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"

    def test_second_mentor_is_rejected(self, client, temp_db):
        project = _create_project(client, temp_db)
        client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        r = client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["bob"], "role": "mentor"},
        )
        assert r.status_code == 409

    def test_invalid_role_is_rejected(self, client, temp_db):
        project = _create_project(client, temp_db)
        r = client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "observer"},
        )
        assert r.status_code == 422

    def test_member_leaving_loses_access_but_history_stays(self, client, temp_db):
        project = _activate(client, temp_db, _create_project(client, temp_db)["project_id"])
        mentor_headers = _headers(temp_db, "mentor")
        r = client.get(f"/supervision/projects/{project['project_id']}", headers=mentor_headers)
        assert r.status_code == 200
        # 本人退出
        r = client.delete(
            f"/supervision/projects/{project['project_id']}/members/{users_id['mentor']}",
            headers=mentor_headers,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "active"
        assert [m["role"] for m in r.json()["members"] if m["status"] == "active"] == ["student"]
        # 退出后读不到项目，但审计与通知都还在库里
        assert client.get(
            f"/supervision/projects/{project['project_id']}", headers=mentor_headers
        ).status_code == 404
        with _session(temp_db) as s:
            assert s.query(SupervisionAuditEventModel).filter_by(
                project_id=project["project_id"]
            ).count() > 0
            assert s.query(SupervisionProjectMemberModel).filter_by(
                project_id=project["project_id"], status="removed"
            ).count() == 1

    def test_student_cannot_remove_the_mentor(self, client, temp_db):
        project = _create_project(client, temp_db)
        client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        r = client.delete(
            f"/supervision/projects/{project['project_id']}/members/{users_id['mentor']}",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 403

    def test_archive_needs_student_request_then_mentor_decision(self, client, temp_db):
        project = _activate(client, temp_db, _create_project(client, temp_db)["project_id"])
        pid = project["project_id"]
        # 导师不能替学生申请归档，学生也不能自己归档
        r = client.post(
            f"/supervision/projects/{pid}/archive/decide",
            headers=_headers(temp_db, "mentor"),
            json={"approve": True},
        )
        assert r.status_code == 409  # 没有待处理申请
        r = client.post(
            f"/supervision/projects/{pid}/archive/request",
            headers=_headers(temp_db, "alice"),
            json={"reason": "论文已投稿"},
        )
        assert r.status_code == 200
        assert r.json()["archive_request_reason"] == "论文已投稿"
        assert r.json()["status"] == "active"  # 申请不关闭访问
        assert client.get(f"/supervision/projects/{pid}", headers=_headers(temp_db, "alice")).status_code == 200
        # 学生没有归档决定权
        r = client.post(
            f"/supervision/projects/{pid}/archive/decide",
            headers=_headers(temp_db, "alice"),
            json={"approve": True},
        )
        assert r.status_code == 403
        # 拒绝必须写原因
        r = client.post(
            f"/supervision/projects/{pid}/archive/decide",
            headers=_headers(temp_db, "mentor"),
            json={"approve": False},
        )
        assert r.status_code == 400
        r = client.post(
            f"/supervision/projects/{pid}/archive/decide",
            headers=_headers(temp_db, "mentor"),
            json={"approve": False, "reason": "还有一轮修改没交"},
        )
        assert r.status_code == 200
        assert r.json()["archive_reject_reason"] == "还有一轮修改没交"
        assert r.json()["status"] == "active"
        # 同意归档
        client.post(
            f"/supervision/projects/{pid}/archive/request",
            headers=_headers(temp_db, "alice"),
            json={"reason": "结题"},
        )
        r = client.post(
            f"/supervision/projects/{pid}/archive/decide",
            headers=_headers(temp_db, "mentor"),
            json={"approve": True},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "archived"
        assert r.json()["archived_at"]

    def test_only_blank_draft_projects_can_be_deleted(self, client, temp_db):
        project = _create_project(client, temp_db, "误建的项目")
        r = client.delete(
            f"/supervision/projects/{project['project_id']}",
            headers=_headers(temp_db, "alice"),
            json={"reason": "重复创建"},
        )
        assert r.status_code == 200
        assert client.get(
            f"/supervision/projects/{project['project_id']}",
            headers=_headers(temp_db, "alice"),
        ).status_code == 404
        assert client.get("/supervision/projects", headers=_headers(temp_db, "alice")).json() == []
        with _session(temp_db) as s:
            # 软删除：行仍在，访问已关闭
            assert s.get(SupervisionProjectModel, project["project_id"]) is not None

        active = _activate(client, temp_db, _create_project(client, temp_db, "有内容的项目")["project_id"])
        r = client.delete(
            f"/supervision/projects/{active['project_id']}",
            headers=_headers(temp_db, "alice"),
            json={"reason": "不想做了"},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# 4. 审计与通知
# ---------------------------------------------------------------------------


class TestAuditAndNotifications:
    def test_one_event_code_shared_by_audit_and_notifications(self, client, temp_db):
        """同一业务事件只产生一个事件编号，发给各接收者的通知引用它（§12）。"""
        project = _create_project(client, temp_db, "共享事件")
        client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        with _session(temp_db) as s:
            added = s.query(SupervisionAuditEventModel).filter_by(action="加入成员").one()
            notes = s.query(SupervisionNotificationModel).filter_by(
                event_code=added.event_code
            ).all()
            assert {n.recipient_id for n in notes} == {users_id["mentor"]}
            assert all(n.event_code.startswith("EV-") for n in notes)

    def test_retry_does_not_duplicate_a_notification(self, temp_db):
        with _session(temp_db) as s:
            first = repo.notify(
                s, event_code="EV-900", recipients=["u-x"], project_id="p1",
                title="提交待审核", event_type="submission.created",
            )
            s.commit()
            again = repo.notify(
                s, event_code="EV-900", recipients=["u-x"], project_id="p1",
                title="提交待审核", event_type="submission.created",
            )
            s.commit()
            assert first == 1
            assert again == 0
            assert s.query(SupervisionNotificationModel).filter_by(
                event_code="EV-900"
            ).count() == 1

    def test_read_state_is_per_recipient(self, client, temp_db):
        project = _create_project(client, temp_db, "已读状态")
        pid = project["project_id"]
        client.post(
            f"/supervision/projects/{pid}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        client.post(
            f"/supervision/projects/{pid}/archive/request",
            headers=_headers(temp_db, "alice"),
            json={"reason": "阶段性完成"},
        )
        alice_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "alice")
        ).json()
        mentor_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert any(n["event_type"] == "archive.requested" for n in mentor_notes)
        target = next(n for n in alice_notes if n["event_type"] == "project.created")
        r = client.post(
            "/supervision/notifications/read",
            headers=_headers(temp_db, "alice"),
            json={"notification_id": target["notification_id"]},
        )
        assert r.status_code == 200 and r.json()["updated"] == 1
        # 学生标记已读不影响导师自己的通知状态
        mentor_after = client.get("/supervision/notifications",
                                  headers=_headers(temp_db, "mentor")).json()
        assert mentor_after and all(not n["read"] for n in mentor_after)
        alice_after = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "alice")
        ).json()
        assert next(n for n in alice_after
                    if n["notification_id"] == target["notification_id"])["read"] is True
        # unread_only 只看未读
        unread = client.get(
            "/supervision/notifications?unread_only=true",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert target["notification_id"] not in [n["notification_id"] for n in unread]
        r = client.post(
            "/supervision/notifications/read",
            headers=_headers(temp_db, "alice"),
            json={"mark_all": True},
        )
        assert r.status_code == 200
        assert client.get("/supervision/notifications?unread_only=true",
                          headers=_headers(temp_db, "alice")).json() == []

    def test_cannot_mark_another_users_notification_read(self, client, temp_db):
        project = _create_project(client, temp_db, "别人的通知")
        mentor_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert mentor_notes == []  # 导师还不是成员，收不到通知
        client.post(
            f"/supervision/projects/{project['project_id']}/members",
            headers=_headers(temp_db, "alice"),
            json={"user_id": users_id["mentor"], "role": "mentor"},
        )
        mentor_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert len(mentor_notes) == 1
        r = client.post(
            "/supervision/notifications/read",
            headers=_headers(temp_db, "alice"),
            json={"notification_id": mentor_notes[0]["notification_id"]},
        )
        assert r.status_code == 200 and r.json()["updated"] == 0
        assert client.get("/supervision/notifications",
                          headers=_headers(temp_db, "mentor")).json()[0]["read"] is False

    def test_audit_records_who_did_what_with_readable_codes(self, client, temp_db):
        project = _create_project(client, temp_db, "审计留痕")
        events = client.get(
            f"/supervision/projects/{project['project_id']}/audit",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert [e["action"] for e in events] == ["创建协作项目"]
        assert events[0]["object_code"] == "RP-001"
        assert events[0]["actor_id"] == users_id["alice"]
        assert events[0]["event_code"].startswith("EV-")
        filtered = client.get(
            f"/supervision/projects/{project['project_id']}/audit?action=加入成员",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert filtered == []


def test_router_is_mounted_on_the_real_server_app():
    """钉住挂载点：真实服务应用必须暴露协作接口（否则前端只会打到 SPA 兜底）。"""
    from chatchat.server.api_server.server_app import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert "/supervision/projects" in paths
    assert "/supervision/projects/{project_ref}/audit" in paths
    assert "/supervision/notifications" in paths
