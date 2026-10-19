"""师生协作纯人工闭环离线测试（《学生研究与导师审核功能重新设计方案》里程碑1）。

按 §16.2 的十条放行标准组织：

1. 学生与主导师能查看所属项目，非成员读不到项目和子资源；
2. 主导师能发布带结构化验收标准、截止时间和材料要求的任务；
3. 学生能记录进展，进展不会被当成正式提交；
4. 学生能创建不可变正式提交，修改必须生成新版本；
5. 导师能对必选验收标准逐条作出行动型结论；
6. 要求修改必须填写必须修改项，学生能重提新版本；
7. 导师审核始终绑定准确的提交与验收标准快照；
8. AI 完全不参与时，任务、提交、审核、通知、审计照常完整运行；
9. 管理员、学生、导师的越权请求都被服务端拒绝并按规则审计。

外加撤回规则（§5.2）、派生标签（§5.6）与项目附件的版本/哈希/下载鉴权
（§16.1 第3条）。全部使用临时 SQLite 与临时附件目录，不调用任何模型。
"""
import asyncio
import hashlib
import os
from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server.supervision_routes import supervision_router
from chatchat.server.api_server.supervision_work_routes import supervision_work_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.models.supervision_model import (  # noqa: F401
    SupervisionAuditEventModel,
    SupervisionNotificationModel,
    SupervisionProjectMemberModel,
    SupervisionProjectModel,
)
from chatchat.server.db.models.supervision_work_model import (  # noqa: F401
    SupervisionAttachmentModel,
    SupervisionMilestoneModel,
    SupervisionProgressUpdateModel,
    SupervisionReviewModel,
    SupervisionSubmissionModel,
    SupervisionTaskAcModel,
    SupervisionTaskModel,
)
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import supervision_work_repository as work
from chatchat.server.db.session import get_db as db_get_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "supervision-work-unit-test-secret-000000"
    )
    return "supervision-work-unit-test-secret-000000"


@pytest.fixture
def temp_db(secret, tmp_path, monkeypatch):
    path = tmp_path / "supervision_work.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 22
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@pytest.fixture(autouse=True)
def attachment_dir(tmp_path, monkeypatch):
    """附件写到临时目录，绝不碰真实 DATA_PATH。"""
    root = tmp_path / "attachments"
    monkeypatch.setenv("CHATCHAT_SUPERVISION_ATTACHMENT_ROOT", str(root))
    return root


def _mk_user(db, username, role="user"):
    s = db["Session"]()
    try:
        u = service.create_user(
            s, username=username, display_name=username.title(),
            password="password12345678", role=role,
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
            _mk_user(db, username)
            u = s.query(UserModel).filter(UserModel.username == username).one()
        return {"Authorization": f"Bearer {auth_jwt.create_token(u.id, u.auth_version)}"}
    finally:
        s.close()


@pytest.fixture
def users(temp_db):
    return {
        "alice": _mk_user(temp_db, "alice"),
        "mentor": _mk_user(temp_db, "mentor"),
        "bob": _mk_user(temp_db, "bob"),
        "root": _mk_user(temp_db, "root", role="admin"),
    }


@pytest.fixture
def app(temp_db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(supervision_router)
    a.include_router(supervision_work_router)

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


ACS = [
    {
        "statement": "在指定 Grid2N 数据集与固定随机种子下，中位定位误差小于 1 m",
        "evidence_requirement": "实验配置、结果文件与图表",
        "required": True,
        "check_type": "experiment",
    },
    {
        "statement": "与基线方法使用相同输入与随机种子，并给出配置对照",
        "evidence_requirement": "配置文件与运行日志",
        "required": False,
        "check_type": "code",
    },
]


def _active_project(client, db) -> dict:
    """建项目 → 加主导师 → 激活，返回项目详情。"""
    created = client.post(
        "/supervision/projects", headers=_headers(db, "alice"),
        json={"name": "反射面定位研究", "description": "里程碑1 验证"},
    )
    assert created.status_code == 200, created.text
    pid = created.json()["project_id"]
    added = client.post(
        f"/supervision/projects/{pid}/members", headers=_headers(db, "alice"),
        json={"user_id": users_id["mentor"], "role": "mentor"},
    )
    assert added.status_code == 200, added.text
    activated = client.post(
        f"/supervision/projects/{pid}/activate", headers=_headers(db, "alice")
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def _published_task(client, db, pid) -> dict:
    created = client.post(
        f"/supervision/projects/{pid}/tasks", headers=_headers(db, "mentor"),
        json={
            "title": "基线算法复现", "purpose": "确认现有方法在本数据集上的可达精度",
            "description": "先复现基线，再谈改进", "acs": ACS,
            "required_materials": [
                {"key": "log", "label": "运行日志", "required": True},
                {"key": "result", "label": "结果文件", "required": True},
            ],
            "due_at": "2026-11-01T00:00:00", "priority": "high",
            "requires_runtime": True, "publish": True,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()


def _submitted(client, db, pid, task_code, *, summary="第一版", version_note="") -> dict:
    r = client.post(
        f"/supervision/projects/{pid}/tasks/{task_code}/submissions",
        headers=_headers(db, "alice"),
        json={
            "summary": summary,
            "self_assessment": [
                {"ac_code": "AC-1", "self_assessment": "done",
                 "evidence": "results/median_error.csv"},
                {"ac_code": "AC-2", "self_assessment": "partially_done",
                 "evidence": "configs/baseline.yaml"},
            ],
            "git_repo": "https://example.invalid/loc", "git_branch": "main",
            "git_commit": "a" * 40,
            "experiment_notes": "python run.py --seed 42" + version_note,
            "unfinished_items": "消融实验未完成",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _upload(client, db, pid, *, name="run.log", content=b"seed=42 rmse=0.83\n", kind="log"):
    return client.post(
        f"/supervision/projects/{pid}/attachments",
        headers=_headers(db, "alice"),
        files={"file": (name, content, "text/plain")},
        data={"kind": kind},
    )


users_id = {}


@pytest.fixture(autouse=True)
def _track_users(users):
    users_id.clear()
    users_id.update({k: v["id"] for k, v in users.items()})


# ---------------------------------------------------------------------------
# 1. 任务下发
# ---------------------------------------------------------------------------


class TestTaskPublishing:
    def test_mentor_publishes_structured_task(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        assert task["code"] == "T-001"
        assert task["status"] == "active"
        assert task["label"] == "待开始"          # active 且尚无进展（§5.1）
        assert task["due_at"].startswith("2026-11-01")
        assert [ac["ac_code"] for ac in task["acs"]] == ["AC-1", "AC-2"]
        assert task["acs"][0]["required"] is True
        assert task["acs"][1]["required"] is False
        assert task["required_materials"][0]["label"] == "运行日志"

    def test_student_cannot_publish_tasks(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        r = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "alice"),
            json={"title": "自己给自己派活", "purpose": "越权测试", "acs": ACS},
        )
        assert r.status_code == 403
        # 管理员没有项目角色，同样不能下发（§10）
        r = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "root"),
            json={"title": "管理员代派", "purpose": "越权测试", "acs": ACS},
        )
        assert r.status_code == 404

    def test_publish_requires_structured_acs(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        r = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
            json={"title": "复现基线", "purpose": "确认可达精度", "acs": [], "publish": True},
        )
        assert r.status_code == 400
        r = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
            json={"title": "复现基线", "purpose": "确认可达精度",
                  "acs": [{"statement": "完成算法"}]},
        )
        assert r.status_code == 400
        assert "可核验" in r.text

    def test_draft_task_invisible_label_and_publish_flow(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        draft = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
            json={"title": "还没想好", "purpose": "先记一笔", "acs": ACS},
        )
        assert draft.status_code == 200
        assert draft.json()["status"] == "draft"
        assert draft.json()["label"] == "任务草稿"
        r = client.post(
            f"/supervision/projects/{pid}/tasks/T-001/publish",
            headers=_headers(temp_db, "mentor"),
        )
        assert r.status_code == 200 and r.json()["status"] == "active"

    def test_cancel_requires_reason_and_mentor(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/cancel",
            headers=_headers(temp_db, "mentor"), json={"reason": ""},
        )
        assert r.status_code == 422
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/cancel",
            headers=_headers(temp_db, "alice"), json={"reason": "不想做了"},
        )
        assert r.status_code == 403
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/cancel",
            headers=_headers(temp_db, "mentor"), json={"reason": "方向调整"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled" and r.json()["label"] == "已取消"

    def test_ac_edit_bumps_version_without_touching_old_submissions(
        self, client, temp_db
    ):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        _submitted(client, temp_db, pid, task["code"])
        r = client.patch(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "mentor"),
            json={"acs": [
                {"statement": "在固定随机种子下中位定位误差小于 0.5 m",
                 "evidence_requirement": "结果文件", "required": True,
                 "check_type": "experiment"},
            ]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["version"] == 2
        assert [ac["ac_code"] for ac in r.json()["acs"]] == ["AC-1"]
        with _session(temp_db) as s:
            submission = s.query(SupervisionSubmissionModel).one()
            # 提交仍然绑定当时那两条标准（§13）
            assert {item["ac_code"] for item in submission.ac_snapshot} == {
                "AC-1", "AC-2"
            }
            assert len(s.query(SupervisionTaskAcModel).all()) == 3


# ---------------------------------------------------------------------------
# 2. 进展记录
# ---------------------------------------------------------------------------


class TestProgress:
    def test_progress_is_not_a_submission(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/progress", headers=_headers(temp_db, "alice"),
            json={"task_ref": task["code"], "done": "跑通了基线训练脚本",
                  "results": "RMSE 0.83", "next_steps": "补消融"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "PR-001"
        assert r.json()["notes"] == []
        # 进展不会进待审核队列
        queue = client.get(
            f"/supervision/projects/{pid}/review-queue", headers=_headers(temp_db, "mentor")
        ).json()
        assert queue == []
        # 任务标签变成"进行中"，不是"待导师审核"
        got = client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert got["label"] == "进行中"

    def test_progress_note_is_append_only(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        created = client.post(
            f"/supervision/projects/{pid}/progress", headers=_headers(temp_db, "alice"),
            json={"task_ref": task["code"], "done": "原始记录"},
        ).json()
        r = client.post(
            f"/supervision/projects/{pid}/progress/{created['progress_id']}/notes",
            headers=_headers(temp_db, "mentor"), json={"note": "补充：请补种子号"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["done"] == "原始记录"          # 原文没被改写
        assert [n["note"] for n in r.json()["notes"]] == ["补充：请补种子号"]
        assert r.json()["notes"][0]["author_role"] == "mentor"

    def test_empty_progress_rejected(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        r = client.post(
            f"/supervision/projects/{pid}/progress", headers=_headers(temp_db, "alice"),
            json={},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# 3. 正式提交
# ---------------------------------------------------------------------------


class TestSubmissions:
    def test_submission_creates_immutable_version(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        first = _submitted(client, temp_db, pid, task["code"])
        assert first["code"] == "S-001" and first["version"] == 1
        assert first["display_code"] == "S-001-v1"
        assert first["status"] == "submitted"
        assert first["git_commit"] == "a" * 40
        assert {w["key"] for w in first["completeness_check"]} == {
            "self_assessment", "required_materials", "runtime_material",
            "unfinished_items",
        }
        # 完整性检查只提醒，不阻塞提交（§6.4）
        assert any(not w["passed"] for w in first["completeness_check"])

        second = _submitted(client, temp_db, pid, task["code"], summary="第二版",
                            version_note=" --ablation")
        assert second["version"] == 2
        assert second["display_code"] == "S-001-v2"
        got = client.get(
            f"/supervision/projects/{pid}/submissions/S-001-v1",
            headers=_headers(temp_db, "alice"),
        )
        # 旧版本仍可查，但已被新版本替代
        assert got.status_code == 200
        assert got.json()["status"] == "superseded"
        assert got.json()["summary"] == "第一版"

    def test_only_active_tasks_accept_submissions(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        draft = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
            json={"title": "草稿任务", "purpose": "还没发布", "acs": ACS},
        ).json()
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{draft['code']}/submissions",
            headers=_headers(temp_db, "alice"), json={"summary": "抢先提交"},
        )
        assert r.status_code == 409

    def test_mentor_cannot_submit_for_the_student(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/submissions",
            headers=_headers(temp_db, "mentor"), json={"summary": "导师代交"},
        )
        assert r.status_code == 403

    def test_self_assessment_must_match_task_acs(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/submissions",
            headers=_headers(temp_db, "alice"),
            json={"summary": "自评对不上", "self_assessment": [
                {"ac_code": "AC-9", "self_assessment": "done"}]},
        )
        assert r.status_code == 400

    def test_withdraw_rules(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        sid = submission["submission_id"]

        r = client.post(
            f"/supervision/projects/{pid}/submissions/{sid}/withdraw",
            headers=_headers(temp_db, "alice"), json={"reason": ""},
        )
        assert r.status_code == 422
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{sid}/withdraw",
            headers=_headers(temp_db, "mentor"), json={"reason": "导师替学生撤回"},
        )
        assert r.status_code == 409

        withdrawn = client.post(
            f"/supervision/projects/{pid}/submissions/{sid}/withdraw",
            headers=_headers(temp_db, "alice"), json={"reason": "材料传错"},
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["status"] == "withdrawn"
        assert withdrawn.json()["withdraw_reason"] == "材料传错"
        # 撤回后离开待审核队列，但快照继续只读保留
        assert client.get(
            f"/supervision/projects/{pid}/review-queue",
            headers=_headers(temp_db, "mentor"),
        ).json() == []
        with _session(temp_db) as s:
            row = s.get(SupervisionSubmissionModel, sid)
            assert row.summary == "第一版"
            audit = s.query(SupervisionAuditEventModel).filter_by(action="撤回提交").one()
            assert "未开始" in audit.detail

        # 撤回后重提是新版本，不把旧提交改回来
        again = _submitted(client, temp_db, pid, task["code"], summary="重传")
        assert again["version"] == 2
        with _session(temp_db) as s:
            assert s.get(SupervisionSubmissionModel, sid).status == "withdrawn"

    def test_withdraw_blocked_after_review_published(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish",
            headers=_headers(temp_db, "mentor"),
            json={"decision": "paused", "reason": "等外部数据"},
        )
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/withdraw",
            headers=_headers(temp_db, "alice"), json={"reason": "还是撤了"},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# 4. 导师审核
# ---------------------------------------------------------------------------


class TestMentorReview:
    def _queue(self, client, db, pid):
        return client.get(
            f"/supervision/projects/{pid}/review-queue", headers=_headers(db, "mentor")
        ).json()

    def test_review_queue_and_draft_label(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        queue = self._queue(client, temp_db, pid)
        assert [s["display_code"] for s in queue] == ["S-001-v1"]
        assert client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()["label"] == "待导师审核"

        draft = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/draft",
            headers=_headers(temp_db, "mentor"),
            json={"conclusions": [{"ac_code": "AC-1", "conclusion": "partially_met",
                                   "reason": "缺少多种子结果"}]},
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["status"] == "drafting"
        assert draft.json()["first_touched_at"]
        assert client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()["label"] == "审核草稿中"

    def test_opening_the_review_page_creates_nothing(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        r = client.get(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}/review",
            headers=_headers(temp_db, "mentor"),
        )
        assert r.status_code == 200 and r.json() is None
        with _session(temp_db) as s:
            assert s.query(SupervisionReviewModel).count() == 0

    def test_unpublished_draft_is_invisible_to_the_student(self, client, temp_db):
        """drafting 是导师的私人工作状态：学生读不到半截结论（§5.4、§5.6）。

        学生仍能从任务标签看到"审核草稿中"（那是事实，不是结论内容），
        但审核正文、逐条结论和 must_fix 必须等到发布才出现。
        """
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        url = (
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review"
        )
        draft = client.post(
            f"{url}/draft", headers=_headers(temp_db, "mentor"),
            json={
                "conclusions": [{"ac_code": "AC-1", "conclusion": "not_met",
                                 "reason": "先记着：多种子结果还没跑"}],
                "feedback": "草稿里的半截想法，还没打算现在给学生看",
                "must_fix": ["补多种子结果"],
            },
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["status"] == "drafting"

        as_student = client.get(url, headers=_headers(temp_db, "alice"))
        assert as_student.status_code == 200, as_student.text
        assert as_student.json() is None
        assert "还没打算现在给学生看" not in as_student.text

        # 写草稿的导师本人仍然读得到自己的草稿
        as_mentor = client.get(url, headers=_headers(temp_db, "mentor"))
        assert as_mentor.status_code == 200
        assert as_mentor.json()["status"] == "drafting"
        assert as_mentor.json()["feedback"] == "草稿里的半截想法，还没打算现在给学生看"

        published = client.post(
            f"{url}/publish", headers=_headers(temp_db, "mentor"),
            json={"decision": "revision_required", "reason": "先补实验",
                  "feedback": "多种子结果缺失", "must_fix": ["补多种子结果"]},
        )
        assert published.status_code == 200, published.text
        after = client.get(url, headers=_headers(temp_db, "alice"))
        assert after.json()["status"] == "published"
        assert after.json()["feedback"] == "多种子结果缺失"

    def test_approved_requires_conclusion_on_every_required_ac(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        url = (
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish"
        )
        r = client.post(url, headers=_headers(temp_db, "mentor"),
                        json={"decision": "approved"})
        assert r.status_code == 400 and "AC-1" in r.text

        r = client.post(
            url, headers=_headers(temp_db, "mentor"),
            json={"decision": "approved", "feedback": "结果可用",
                  "conclusions": [
                      {"ac_code": "AC-1", "conclusion": "met", "reason": "达标"},
                      {"ac_code": "AC-2", "conclusion": "not_applicable",
                       "reason": "本任务不涉及基线对照"},
                  ]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "published"
        assert r.json()["overall_decision"] == "approved"
        got = client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert got["status"] == "completed" and got["label"] == "已通过"

    def test_not_applicable_needs_reason(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/draft",
            headers=_headers(temp_db, "mentor"),
            json={"conclusions": [{"ac_code": "AC-1", "conclusion": "not_applicable"}]},
        )
        assert r.status_code == 400 and "理由" in r.text

    def test_revision_requires_must_fix_then_resubmission_waits_rereview(
        self, client, temp_db
    ):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        url = (
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish"
        )
        r = client.post(url, headers=_headers(temp_db, "mentor"),
                        json={"decision": "revision_required"})
        assert r.status_code == 400 and "必须修改" in r.text

        r = client.post(
            url, headers=_headers(temp_db, "mentor"),
            json={"decision": "revision_required",
                  "must_fix": ["补上多种子重复实验"],
                  "conclusions": [{"ac_code": "AC-1", "conclusion": "more_evidence_required",
                                   "reason": "只有一个种子"}]},
        )
        assert r.status_code == 200, r.text
        task_after = client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()
        assert task_after["status"] == "active" and task_after["label"] == "退回修改"

        _submitted(client, temp_db, pid, task["code"], summary="补多种子")
        assert client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()["label"] == "等待重审"
        assert [s["display_code"] for s in self._queue(client, temp_db, pid)] == [
            "S-001-v2"
        ]

    def test_rejected_and_paused_need_reason(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        url = (
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish"
        )
        for decision in ("rejected", "paused"):
            r = client.post(url, headers=_headers(temp_db, "mentor"),
                            json={"decision": decision})
            assert r.status_code == 400, decision
        r = client.post(url, headers=_headers(temp_db, "mentor"),
                        json={"decision": "paused", "reason": "等外部数据"})
        assert r.status_code == 200
        assert client.get(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "alice"),
        ).json()["label"] == "审核暂缓"

    def test_unknown_ac_in_conclusion_rejected(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        client.patch(
            f"/supervision/projects/{pid}/tasks/{task['code']}",
            headers=_headers(temp_db, "mentor"),
            json={"acs": [{"statement": "换成一条全新的验收标准，误差小于 0.1 m",
                           "required": True, "check_type": "experiment"}]},
        )
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/draft",
            headers=_headers(temp_db, "mentor"),
            json={"conclusions": [{"ac_code": "AC-2", "conclusion": "met"}]},
        )
        assert r.status_code == 200   # AC-2 在提交快照里，仍然可以下结论
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/draft",
            headers=_headers(temp_db, "mentor"),
            json={"conclusions": [{"ac_code": "AC-7", "conclusion": "met"}]},
        )
        assert r.status_code == 400

    def test_published_review_cannot_be_overwritten_only_amended(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        published = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish",
            headers=_headers(temp_db, "mentor"),
            json={"decision": "approved",
                  "conclusions": [{"ac_code": "AC-1", "conclusion": "met"},
                                  {"ac_code": "AC-2", "conclusion": "met"}]},
        ).json()
        r = client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish",
            headers=_headers(temp_db, "mentor"),
            json={"decision": "rejected", "reason": "改主意了"},
        )
        assert r.status_code == 409
        r = client.post(
            f"/supervision/projects/{pid}/reviews/{published['code']}/notes",
            headers=_headers(temp_db, "mentor"),
            json={"note": "补充：后续任务里补消融", "kind": "clarification"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["overall_decision"] == "approved"
        assert [n["note"] for n in r.json()["notes"]] == ["补充：后续任务里补消融"]


# ---------------------------------------------------------------------------
# 5. 项目附件
# ---------------------------------------------------------------------------


class TestAttachments:
    def test_upload_download_and_hash(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        content = b"seed=42 rmse=0.83\n"
        r = _upload(client, temp_db, pid, name="run.log", content=content)
        assert r.status_code == 200, r.text
        attachment = r.json()
        assert attachment["code"] == "AT-001"
        assert attachment["sha256"] == hashlib.sha256(content).hexdigest()
        assert attachment["size"] == len(content)

        got = client.get(
            f"/supervision/projects/{pid}/attachments/{attachment['code']}/download",
            headers=_headers(temp_db, "mentor"),
        )
        assert got.status_code == 200
        assert got.content == content

    def test_non_member_cannot_list_or_download(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        attachment = _upload(client, temp_db, pid).json()
        outsider = _headers(temp_db, "bob")
        assert client.get(
            f"/supervision/projects/{pid}/attachments", headers=outsider
        ).status_code == 404
        r = client.get(
            f"/supervision/projects/{pid}/attachments/{attachment['code']}/download",
            headers=outsider,
        )
        assert r.status_code == 404

    def test_dangerous_and_oversized_uploads_rejected(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        r = _upload(client, temp_db, pid, name="evil.exe", content=b"MZ...")
        assert r.status_code == 400 and "不允许" in r.text
        r = _upload(client, temp_db, pid, name="notes.rtf", content=b"{\\rtf1}")
        assert r.status_code == 400
        monkeypatch.setenv("CHATCHAT_SUPERVISION_MAX_UPLOAD_BYTES", "16")
        r = _upload(client, temp_db, pid, name="big.log", content=b"x" * 64)
        assert r.status_code == 400 and "上限" in r.text

    def test_traversal_filename_is_sanitized(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        r = _upload(client, temp_db, pid, name="../../etc/passwd.txt", content=b"hi")
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "passwd.txt"
        with _session(temp_db) as s:
            stored = s.query(SupervisionAttachmentModel).one().stored_path
            assert stored.startswith(f"{pid}/")
            assert ".." not in stored

    def test_attachment_bound_to_submission_cannot_be_removed(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        attachment = _upload(client, temp_db, pid).json()
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/submissions",
            headers=_headers(temp_db, "alice"),
            json={"summary": "带日志的提交", "attachment_codes": [attachment["code"]],
                  "self_assessment": [{"ac_code": "AC-1", "self_assessment": "done"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["materials"][0]["sha256"] == attachment["sha256"]

        r = client.delete(
            f"/supervision/projects/{pid}/attachments/{attachment['code']}",
            headers=_headers(temp_db, "alice"), json={"reason": "删掉"},
        )
        assert r.status_code == 409 and "提交" in r.text
        # 未绑定的附件可以软删除
        spare = _upload(client, temp_db, pid, name="draft.txt", content=b"x").json()
        r = client.delete(
            f"/supervision/projects/{pid}/attachments/{spare['code']}",
            headers=_headers(temp_db, "alice"), json={"reason": "传错了"},
        )
        assert r.status_code == 200 and r.json()["status"] == "removed"
        assert client.get(
            f"/supervision/projects/{pid}/attachments/{spare['code']}",
            headers=_headers(temp_db, "alice"),
        ).status_code == 404

    def test_submission_rejects_unknown_attachment_code(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/tasks/{task['code']}/submissions",
            headers=_headers(temp_db, "alice"),
            json={"summary": "引用不存在的附件", "attachment_codes": ["AT-999"]},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# 6. 隔离、审计与通知
# ---------------------------------------------------------------------------


class TestIsolationAuditNotifications:
    def test_non_member_cannot_read_any_subresource(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        _submitted(client, temp_db, pid, task["code"])
        outsider = _headers(temp_db, "bob")
        for url in [
            f"/supervision/projects/{pid}/tasks",
            f"/supervision/projects/{pid}/tasks/T-001",
            f"/supervision/projects/{pid}/progress",
            f"/supervision/projects/{pid}/review-queue",
            f"/supervision/projects/{pid}/attachments",
            f"/supervision/projects/{pid}/milestones",
        ]:
            assert client.get(url, headers=outsider).status_code == 404, url

    def test_unauthenticated_requests_rejected(self, client, temp_db):
        for url in [
            "/supervision/projects/RP-001/tasks",
            "/supervision/projects/RP-001/review-queue",
            "/supervision/projects/RP-001/attachments",
        ]:
            assert client.get(url).status_code == 401, url

    def test_formal_actions_are_audited_once_each(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        submission = _submitted(client, temp_db, pid, task["code"])
        client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish",
            headers=_headers(temp_db, "mentor"),
            json={"decision": "approved",
                  "conclusions": [{"ac_code": "AC-1", "conclusion": "met"},
                                  {"ac_code": "AC-2", "conclusion": "met"}]},
        )
        with _session(temp_db) as s:
            actions = [
                row.action for row in s.query(SupervisionAuditEventModel)
                .filter_by(project_id=pid).all()
            ]
            for expected in ("发布任务", "正式提交", "审核通过"):
                assert actions.count(expected) == 1, (expected, actions)

    def test_notifications_follow_the_business_events(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        student_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "alice")
        ).json()
        assert any(n["event_type"] == "task.published" for n in student_notes)

        submission = _submitted(client, temp_db, pid, task["code"])
        mentor_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert any(n["event_type"] == "submission.created" for n in mentor_notes)

        client.post(
            f"/supervision/projects/{pid}/submissions/{submission['submission_id']}"
            "/review/publish",
            headers=_headers(temp_db, "mentor"),
            json={"decision": "revision_required", "must_fix": ["补种子"],
                  "conclusions": [{"ac_code": "AC-1",
                                   "conclusion": "more_evidence_required"}]},
        )
        student_after = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "alice")
        ).json()
        assert any(n["event_type"] == "review.revision_required" for n in student_after)
        # 导师不会收到自己动作的通知
        mentor_after = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert not any(n["event_type"] == "review.revision_required"
                       for n in mentor_after)

    def test_whole_loop_runs_without_any_model(self, client, temp_db, monkeypatch):
        """§16.2 第9条：AI 完全不参与时闭环照常工作。"""
        def boom(*args, **kwargs):  # 任何模型调用都会炸
            raise AssertionError("纯人工闭环不应该调用模型")

        monkeypatch.setattr("chatchat.server.utils.get_ChatOpenAI", boom)
        pid = _active_project(client, temp_db)["project_id"]
        task = _published_task(client, temp_db, pid)
        _upload(client, temp_db, pid)
        _submitted(client, temp_db, pid, task["code"])
        queue = client.get(
            f"/supervision/projects/{pid}/review-queue", headers=_headers(temp_db, "mentor")
        ).json()
        assert len(queue) == 1

    def test_milestone_confirmation_flow(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        created = client.post(
            f"/supervision/projects/{pid}/milestones", headers=_headers(temp_db, "alice"),
            json={"title": "完成基线复现", "success_criteria": "误差 < 1m",
                  "planned_end": "2026-12-01T00:00:00"},
        )
        assert created.status_code == 200, created.text
        milestone = created.json()
        assert milestone["code"] == "M-001"
        assert milestone["mentor_confirmation"] == ""

        r = client.post(
            f"/supervision/projects/{pid}/milestones/{milestone['milestone_id']}/confirm",
            headers=_headers(temp_db, "alice"), json={"confirmation": "confirmed"},
        )
        assert r.status_code == 403   # 只有主导师能确认
        r = client.post(
            f"/supervision/projects/{pid}/milestones/{milestone['milestone_id']}/confirm",
            headers=_headers(temp_db, "mentor"),
            json={"confirmation": "partially_confirmed", "note": "消融未做"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["mentor_confirmation"] == "partially_confirmed"
        assert r.json()["mentor_confirmation_note"] == "消融未做"

    def test_task_can_bind_milestone_and_show_it(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        milestone = client.post(
            f"/supervision/projects/{pid}/milestones", headers=_headers(temp_db, "mentor"),
            json={"title": "完成论文第二章初稿", "success_criteria": "导师确认"},
        ).json()
        task = client.post(
            f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
            json={"title": "写方法章节", "purpose": "形成方法描述", "acs": ACS,
                  "milestone_id": milestone["milestone_id"], "publish": True},
        ).json()
        assert task["milestone_id"] == milestone["milestone_id"]
