"""项目内研究讨论与确认共享离线测试（《学生研究与导师审核功能重新设计方案》里程碑2）。

按 §16.3 的六条放行标准组织：

1. 创建或进入项目后能建立并列出**自己**的研究讨论会话；
2. 学生和导师各自只能读取自己的原始聊天、摘要和私人草稿（跨角色 404）；
3. 任一角色的模型上下文不包含另一角色的私人内容；
4. 用户能从自己的聊天提取、编辑和确认 ``ResearchBrief``；
5. 确认共享后另一方只能读结构化版本，不能反查来源消息；
6. 项目内容不会被自动写成用户的全局个人偏好。

外加：项目内 AI 讨论回复（§15.2 第1、6条：发言先落库、模型缺席不陪葬、
上下文只含本人历史与已确认共享内容）、未登录 401、非成员 404、项目角色
越权 403、版本比较与"重新提取不覆盖旧版本"（§4.3.1）。全部使用临时
SQLite，模型一律用假对象喂固定内容，不联网、不调真实模型（autouse 夹具
默认把所有消费者置为"未接入"）。
"""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server import supervision_research_routes as research_routes
from chatchat.server.api_server.supervision_research_routes import (
    supervision_research_router,
)
from chatchat.server.api_server.supervision_routes import supervision_router
from chatchat.server.api_server.supervision_work_routes import supervision_work_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
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
from chatchat.server.db.models.supervision_research_model import (  # noqa: F401
    SupervisionResearchBriefModel,
    SupervisionResearchBriefVersionModel,
)
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import supervision_research_repository as research
from chatchat.server.db.session import get_db as db_get_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "supervision-research-unit-test-secret-0"
    )
    return "supervision-research-unit-test-secret-0"


@pytest.fixture
def temp_db(secret, tmp_path, monkeypatch):
    path = tmp_path / "supervision_research.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        # 里程碑2 不新增迁移：新表由 Base.metadata.create_all 建出来，
        # 正式库的 v19 DDL 由集成方统一补。
        assert current_version(conn) == 20
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


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
        "alice": _mk_user(temp_db, "alice"),        # 学生
        "mentor": _mk_user(temp_db, "mentor"),      # 主导师
        "bob": _mk_user(temp_db, "bob"),            # 项目外用户
        "root": _mk_user(temp_db, "root", role="admin"),
    }


@pytest.fixture
def app(temp_db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(supervision_router)
    a.include_router(supervision_work_router)
    a.include_router(supervision_research_router)

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


users_id = {}


@pytest.fixture(autouse=True)
def _track_users(users):
    users_id.clear()
    users_id.update({k: v["id"] for k, v in users.items()})


#: 真工厂留一份引用：只有一个用例需要走真构造路径核对消费者声明。
_REAL_BUILD_GATED_MODEL = research_routes._build_gated_model


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """整个文件都不许起真模型：默认所有消费者都"未接入"（→ 503）。

    需要模型行为的用例自己把 ``_resolve_llm`` / ``_resolve_discussion_llm``
    换成 ``FakeLLM``。少了这道保险，测试环境里配了默认 LLM 就会真的联网。
    """
    research_routes.reset_research_model_cache()
    monkeypatch.setattr(research_routes, "_build_gated_model", lambda consumer: None)
    yield
    research_routes.reset_research_model_cache()


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 公共夹具与假模型
# ---------------------------------------------------------------------------

# 学生与导师各自会话里的私人内容：文本必须足够独特，才能断言它没有
# 出现在对方的读取结果和模型上下文里。
STUDENT_SECRET = "张同学的私人讨论：我在 Grid2N 数据集上跑到中位误差 0.83 米"
STUDENT_SECRET_2 = "张同学还没公开的想法：改用镜像模型重做基线"
MENTOR_SECRET = "王老师的私人讨论：我建议先补消融实验再谈精度提升"

EXTRACTED = {
    "background": "已有工作集中在单一反射面场景",
    "question": "多反射面条件下如何实现亚米级室内定位",
    "objectives": "给出可复现的多反射面定位方法",
    "subjects": "室内 Grid2N 仿真场景",
    "scope": "只考虑静态场景与 2.4 GHz 单频点",
    "prior_attempts": "已复现单反射面基线",
    "hypotheses": "反射阶数越高定位误差越大",
    "constraints": "只有 4 根接收天线",
    "methods": ["射线追踪", "镜像模型"],
    "metrics": ["中位定位误差", "P90 误差"],
    "expected_outcomes": "中位定位误差小于 1 米",
    "to_verify": "多径阶数与误差的关系是否成立",
    "keywords": ["indoor localization", "multipath"],
    "sub_questions": ["多径阶数如何量化", "基线怎么选"],
}


class FakeLLM:
    """假模型：只记录收到的提示词，并返回固定内容。"""

    def __init__(self, content):
        self.fixed = content
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        return SimpleNamespace(content=self.fixed)


def _active_project(client, db) -> dict:
    """建项目 → 绑主导师 → 激活，返回项目详情。"""
    created = client.post(
        "/supervision/projects", headers=_headers(db, "alice"),
        json={"name": "多反射面室内定位研究", "description": "里程碑2 验证"},
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


def _dialogue(client, db, who, pid, title="研究讨论") -> dict:
    r = client.post(
        f"/supervision/projects/{pid}/research/dialogues",
        headers=_headers(db, who), json={"title": title},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _add_turns(db, conversation_id, turns):
    """往会话里写原始消息（模拟聊天主链路已经落库的历史）。"""
    s = db["Session"]()
    try:
        base = datetime.now() - timedelta(minutes=len(turns) + 1)
        for index, (query, response) in enumerate(turns):
            s.add(MessageModel(
                id=f"m{conversation_id[:16]}{index:04d}",
                conversation_id=conversation_id, chat_type="agent_chat",
                query=query, response=response, meta_data={},
                create_time=base + timedelta(minutes=index),
            ))
        s.commit()
    finally:
        s.close()


def _private_dialogue(client, db, pid) -> dict:
    """学生建自己的会话并写两条私人消息。"""
    dialogue = _dialogue(client, db, "alice", pid, "学生会话·私有")
    _add_turns(db, dialogue["conversation_id"], [
        (STUDENT_SECRET, "已记录，这属于讨论记录，还不是正式研究定义。"),
        (STUDENT_SECRET_2, "要不要把它写成研究假设？"),
    ])
    return dialogue


def _content(**overrides) -> dict:
    payload = dict(EXTRACTED)
    payload.update(overrides)
    return payload


def _create_draft(client, db, pid, who="alice", conversation_id="", **overrides) -> dict:
    r = client.post(
        f"/supervision/projects/{pid}/research/briefs", headers=_headers(db, who),
        json={"title": "多反射面定位研究定义", "conversation_id": conversation_id,
              "content": _content(**overrides)},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. 进入项目后能建立并列出自己的研究讨论会话（§16.3 第1条）
# ---------------------------------------------------------------------------


class TestDialogueSetup:
    def test_member_creates_and_lists_own_dialogue(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        created = _dialogue(client, temp_db, "alice", pid, "学生会话·私有")
        assert created["code"] == "CV-001"
        assert created["name"] == "学生会话·私有"
        assert created["supervision_project_id"] == pid
        # 复用既有 conversation 表，走智能聊天链路，但不占用个人论文研究项目
        assert created["chat_type"] == "agent_chat"

        listed = client.get(
            f"/supervision/projects/{pid}/research/dialogues",
            headers=_headers(temp_db, "alice"),
        )
        assert listed.status_code == 200
        assert [d["conversation_id"] for d in listed.json()] == [created["conversation_id"]]

        with _session(temp_db) as s:
            row = s.get(ConversationModel, created["conversation_id"])
            assert row.owner_id == users_id["alice"]
            assert row.supervision_project_id == pid
            # §4.2：协作项目ID绝不能写进 project_id（那是个人论文研究项目）
            assert row.project_id is None

    def test_second_call_reuses_own_dialogue(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        first = _dialogue(client, temp_db, "alice", pid, "学生会话")
        again = _dialogue(client, temp_db, "alice", pid, "换个标题")
        assert again["conversation_id"] == first["conversation_id"]

    def test_student_and_mentor_have_separate_dialogues(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        student = _dialogue(client, temp_db, "alice", pid, "学生会话·私有")
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        assert student["conversation_id"] != mentor["conversation_id"]
        assert student["code"] == "CV-001" and mentor["code"] == "CV-002"

        # 角色切换不会切到对方聊天：各自只列自己的
        student_list = client.get(
            f"/supervision/projects/{pid}/research/dialogues",
            headers=_headers(temp_db, "alice"),
        ).json()
        mentor_list = client.get(
            f"/supervision/projects/{pid}/research/dialogues",
            headers=_headers(temp_db, "mentor"),
        ).json()
        assert [d["conversation_id"] for d in student_list] == [student["conversation_id"]]
        assert [d["conversation_id"] for d in mentor_list] == [mentor["conversation_id"]]

    def test_dialogue_title_required(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        r = client.post(
            f"/supervision/projects/{pid}/research/dialogues",
            headers=_headers(temp_db, "alice"), json={"title": ""},
        )
        assert r.status_code == 422   # 路由层 min_length 先挡住


# ---------------------------------------------------------------------------
# 2. 原始消息、私人草稿按角色隔离（§16.3 第2条、场景 0-C 第3步）
# ---------------------------------------------------------------------------


class TestPrivateIsolation:
    def test_own_messages_readable_other_roles_are_404(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        _add_turns(db=temp_db, conversation_id=mentor["conversation_id"],
                   turns=[(MENTOR_SECRET, "已记录导师的私人讨论。")])

        mine = client.get(
            f"/supervision/projects/{pid}/research/dialogues/"
            f"{student['conversation_id']}/messages",
            headers=_headers(temp_db, "alice"),
        )
        assert mine.status_code == 200, mine.text
        assert [m["query"] for m in mine.json()["messages"]] == [
            STUDENT_SECRET, STUDENT_SECRET_2,
        ]

        # 王老师猜张同学的会话 ID：拒绝访问
        r = client.get(
            f"/supervision/projects/{pid}/research/dialogues/"
            f"{student['conversation_id']}/messages",
            headers=_headers(temp_db, "mentor"),
        )
        assert r.status_code == 404
        r = client.get(
            f"/supervision/projects/{pid}/research/dialogues/"
            f"{mentor['conversation_id']}/messages",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 404

    def test_non_member_and_unauthenticated_rejected(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        outsider = _headers(temp_db, "bob")
        for url in [
            f"/supervision/projects/{pid}/research/dialogues",
            f"/supervision/projects/{pid}/research/briefs",
            f"/supervision/projects/{pid}/research/shared-briefs",
            f"/supervision/projects/{pid}/research/context",
            f"/supervision/projects/{pid}/research/dialogues/{student['conversation_id']}/messages",
        ]:
            assert client.get(url, headers=outsider).status_code == 404, url
        for url in [
            f"/supervision/projects/{pid}/research/dialogues",
            f"/supervision/projects/{pid}/research/briefs",
            f"/supervision/projects/{pid}/research/context",
        ]:
            assert client.get(url).status_code == 401, url

    def test_private_draft_invisible_to_other_role(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        draft = _create_draft(client, temp_db, pid)
        assert draft["status"] == "draft"
        assert draft["code"] == "RB-001"

        for method, url in [
            ("get", f"/supervision/projects/{pid}/research/briefs/{draft['code']}"),
            ("get", f"/supervision/projects/{pid}/research/briefs/{draft['code']}/versions"),
            ("get", f"/supervision/projects/{pid}/research/briefs/{draft['code']}/source-messages"),
            ("patch", f"/supervision/projects/{pid}/research/briefs/{draft['code']}"),
            ("post", f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm"),
        ]:
            r = getattr(client, method)(
                url, headers=_headers(temp_db, "mentor"), json={}
            )
            assert r.status_code == 404, (method, url, r.text)

        listed = client.get(
            f"/supervision/projects/{pid}/research/briefs",
            headers=_headers(temp_db, "mentor"),
        ).json()
        assert listed == []

    def test_role_mismatch_on_brief_kind_is_403(self, client, temp_db):
        """§4.2：学生只能出研究定义，导师只能出导师建议。"""
        pid = _active_project(client, temp_db)["project_id"]
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs",
            headers=_headers(temp_db, "alice"),
            json={"title": "学生冒充导师", "kind": "mentor_guidance",
                  "content": _content()},
        )
        assert r.status_code == 403 and "角色" in r.text
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs",
            headers=_headers(temp_db, "mentor"),
            json={"title": "导师代学生定研究定义", "kind": "research_brief",
                  "content": _content()},
        )
        assert r.status_code == 403 and "角色" in r.text
        # 不传 kind 时按角色自动取，能建出来
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs",
            headers=_headers(temp_db, "mentor"),
            json={"title": "导师建议", "content": _content()},
        )
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "mentor_guidance"
        assert r.json()["owner_role"] == "mentor"


# ---------------------------------------------------------------------------
# 3. 模型上下文不含另一角色私人内容（§16.3 第3条）
# ---------------------------------------------------------------------------


class TestProjectContext:
    def test_context_excludes_other_roles_private_content(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        _add_turns(temp_db, mentor["conversation_id"], [(MENTOR_SECRET, "收到。")])
        draft = _create_draft(client, temp_db, pid)
        confirmed = client.post(
            f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "作为当前工作定义"},
        )
        assert confirmed.status_code == 200, confirmed.text

        for who in ("alice", "mentor"):
            with _session(temp_db) as s:
                context = research.build_project_context(s, pid, users_id[who])
            assert "多反射面条件下如何实现亚米级室内定位" in context
            assert "项目共享研究内容" in context
            # 另一角色的私人聊天、私人草稿不进上下文
            assert MENTOR_SECRET not in context
            assert STUDENT_SECRET not in context
            assert STUDENT_SECRET_2 not in context

    def test_context_endpoint_matches_repository(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        empty = client.get(
            f"/supervision/projects/{pid}/research/context",
            headers=_headers(temp_db, "alice"),
        )
        assert empty.status_code == 200, empty.text
        assert empty.json()["context"] == ""
        assert empty.json()["has_project_context"] is False

        draft = _create_draft(client, temp_db, pid)
        client.post(
            f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"note": ""},
        )
        got = client.get(
            f"/supervision/projects/{pid}/research/context",
            headers=_headers(temp_db, "mentor"),
        ).json()
        assert got["has_project_context"] is True
        assert "RB-001 v1" in got["context"]
        assert "学生研究定义" in got["context"]
        # 上下文里不能出现来源消息线索
        assert "source" not in got["context"]

    def test_unconfirmed_draft_never_enters_context(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        _create_draft(client, temp_db, pid)
        with _session(temp_db) as s:
            assert research.build_project_context(s, pid, users_id["mentor"]) == ""


# ---------------------------------------------------------------------------
# 3.5 项目内 AI 讨论回复（§15.2 第1、6条；边界同 §16.3 第2、3条）
# ---------------------------------------------------------------------------

AI_ANSWER = "可以先把多反射面拆成可验证的子问题，再决定基线怎么选。"


class TestProjectDiscussionTurn:
    def _post(self, client, db, pid, conversation_id, text="基线到底该选哪一个？"):
        return client.post(
            f"/supervision/projects/{pid}/research/dialogues/{conversation_id}/messages",
            headers=_headers(db, "alice"), json={"text": text},
        )

    def _messages(self, client, db, pid, conversation_id):
        r = client.get(
            f"/supervision/projects/{pid}/research/dialogues/{conversation_id}/messages",
            headers=_headers(db, "alice"),
        )
        assert r.status_code == 200, r.text
        return r.json()["messages"]

    def test_message_survives_when_model_unavailable(self, client, temp_db):
        """模型未接入：发言照样落库（503 只针对回复），讨论记录不陪葬。"""
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        r = self._post(client, temp_db, pid, dialogue["conversation_id"])
        assert r.status_code == 503
        assert "已保存" in r.text
        messages = self._messages(client, temp_db, pid, dialogue["conversation_id"])
        assert messages[-1]["query"] == "基线到底该选哪一个？"
        assert not messages[-1]["response"]
        assert messages[-1]["metadata"]["pending_reply"] is True

    def test_reply_is_written_back_to_the_same_turn(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)

        r = self._post(client, temp_db, pid, dialogue["conversation_id"])
        assert r.status_code == 200, r.text
        assert r.json()["response"] == AI_ANSWER
        assert r.json()["metadata"]["pending_reply"] is False
        assert r.json()["metadata"]["consumer"] == "project_discussion"

        messages = self._messages(client, temp_db, pid, dialogue["conversation_id"])
        assert messages[-1]["query"] == "基线到底该选哪一个？"
        assert messages[-1]["response"] == AI_ANSWER
        assert len(fake.prompts) == 1

    def test_prompt_has_own_history_and_confirmed_brief_only(
        self, client, temp_db, monkeypatch
    ):
        """模型上下文 = 本人会话历史 + 已确认共享内容；另一角色私人内容不进。"""
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        _add_turns(temp_db, mentor["conversation_id"], [(MENTOR_SECRET, "收到。")])
        draft = _create_draft(client, temp_db, pid)
        client.post(
            f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "作为当前工作定义"},
        )

        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        r = self._post(client, temp_db, pid, student["conversation_id"])
        assert r.status_code == 200, r.text

        prompt_text = json.dumps(fake.prompts[0], ensure_ascii=False)
        assert STUDENT_SECRET in prompt_text          # 本人历史带入
        assert MENTOR_SECRET not in prompt_text       # 对方私人消息绝不带入
        assert "项目共享研究内容" in prompt_text       # 已确认结构化内容注入
        assert EXTRACTED["question"] in prompt_text
        assert r.json()["metadata"]["project_context_injected"] is True
        # 项目内容不是个人偏好：提示词里必须这样声明
        assert "不是成员的个人偏好" in prompt_text

    def test_prompt_says_when_nothing_confirmed_yet(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        _create_draft(client, temp_db, pid)  # 只有草稿，未确认
        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        assert self._post(client, temp_db, pid, dialogue["conversation_id"]).status_code == 200
        prompt_text = json.dumps(fake.prompts[0], ensure_ascii=False)
        assert "还没有已确认共享的研究内容" in prompt_text
        assert EXTRACTED["question"] not in prompt_text  # 草稿不进模型

    def test_pending_reply_can_be_generated_later(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        first = self._post(client, temp_db, pid, dialogue["conversation_id"])
        assert first.status_code == 503
        pending = self._messages(client, temp_db, pid, dialogue["conversation_id"])[-1]

        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        r = client.post(
            f"/supervision/projects/{pid}/research/messages/{pending['message_id']}/reply",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 200, r.text
        assert r.json()["response"] == AI_ANSWER

    def test_answered_turn_is_not_regenerated(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        answered = self._post(client, temp_db, pid, dialogue["conversation_id"]).json()

        r = client.post(
            f"/supervision/projects/{pid}/research/messages/{answered['message_id']}/reply",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 409
        assert len(fake.prompts) == 1  # 不重复烧模型

    def test_other_role_cannot_post_into_your_dialogue(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        student = _dialogue(client, temp_db, "alice", pid)
        fake = FakeLLM(AI_ANSWER)
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        r = client.post(
            f"/supervision/projects/{pid}/research/dialogues/{student['conversation_id']}/messages",
            headers=_headers(temp_db, "mentor"), json={"text": "偷看学生的会话"},
        )
        assert r.status_code == 404
        assert fake.prompts == []
        assert self._messages(client, temp_db, pid, student["conversation_id"]) == []

    def test_outsider_and_anonymous_are_rejected(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        url = f"/supervision/projects/{pid}/research/dialogues/{dialogue['conversation_id']}/messages"
        outsider = client.post(url, headers=_headers(temp_db, "bob"), json={"text": "外项目用户"})
        assert outsider.status_code == 404
        anonymous = client.post(url, json={"text": "未登录"})
        assert anonymous.status_code == 401

    def test_blank_and_oversized_text_are_rejected(self, client, temp_db):
        """空正文是字段非法（400），超列宽由 pydantic 形状检查拦下（422）。"""
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        blank = self._post(client, temp_db, pid, dialogue["conversation_id"], "   ")
        assert blank.status_code == 400
        assert "不能为空" in blank.text
        too_long = self._post(
            client, temp_db, pid, dialogue["conversation_id"], "长" * 4097,
        )
        assert too_long.status_code == 422
        assert self._messages(client, temp_db, pid, dialogue["conversation_id"]) == []

    def test_history_is_capped_and_truncation_is_recorded(
        self, client, temp_db, monkeypatch
    ):
        pid = _active_project(client, temp_db)["project_id"]
        dialogue = _dialogue(client, temp_db, "alice", pid)
        _add_turns(
            temp_db, dialogue["conversation_id"],
            [(f"问{i}", f"答{i}") for i in range(research.DISCUSSION_HISTORY_LIMIT + 5)],
        )
        fake = FakeLLM("长" * (research.MAX_RESPONSE_CHARS + 500))
        monkeypatch.setattr(research_routes, "_resolve_discussion_llm", lambda: fake)
        r = self._post(client, temp_db, pid, dialogue["conversation_id"])
        assert r.status_code == 200, r.text
        # 提示词 = system + 最近 N 轮（每轮 user+assistant）+ 本次提问
        assert len(fake.prompts[0]) == 1 + 2 * research.DISCUSSION_HISTORY_LIMIT + 1
        body = r.json()
        assert len(body["response"]) == research.MAX_RESPONSE_CHARS
        assert body["metadata"]["truncated_response"] is True

    def test_discussion_and_extraction_declare_registered_consumers(self, monkeypatch):
        """两个入口各自声明消费者，通道由登记表推导（§8.2、§16.1B 第5条）。"""
        import chatchat.server.utils as server_utils

        captured = {}

        def fake_factory(**kwargs):
            captured[kwargs["consumer"]] = kwargs
            return SimpleNamespace(model_name=kwargs["model_name"])

        monkeypatch.setattr(research_routes, "_build_gated_model", _REAL_BUILD_GATED_MODEL)
        monkeypatch.setattr(server_utils, "get_ChatOpenAI", fake_factory)

        assert research_routes._resolve_discussion_llm() is not None
        assert research_routes._resolve_llm() is not None
        assert captured["project_discussion"]["consumer"] == "project_discussion"
        assert captured["research_definition"]["consumer"] == "research_definition"
        assert captured["project_discussion"]["streaming"] is False
        research_routes.reset_research_model_cache()


# ---------------------------------------------------------------------------
# 4. 从本人聊天提取、编辑、确认（§16.3 第4条）
# ---------------------------------------------------------------------------


class TestExtractionAndConfirmation:
    def test_extract_returns_503_when_model_unavailable(self, client, temp_db):
        """模型未接入 → 503 + 中文说明，绝不假装生成过（autouse 夹具已断模型）。"""
        pid = _active_project(client, temp_db)["project_id"]
        _private_dialogue(client, temp_db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/extract",
            headers=_headers(temp_db, "alice"), json={},
        )
        assert r.status_code == 503
        assert "模型能力尚未接入" in r.text
        with _session(temp_db) as s:
            assert s.query(SupervisionResearchBriefModel).count() == 0

    def test_extract_builds_draft_from_own_messages(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        _add_turns(temp_db, mentor["conversation_id"], [(MENTOR_SECRET, "收到。")])

        fake = FakeLLM(json.dumps(EXTRACTED, ensure_ascii=False))
        monkeypatch.setattr(research_routes, "_resolve_llm", lambda: fake)

        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/extract",
            headers=_headers(temp_db, "alice"),
            json={"conversation_id": student["conversation_id"], "title": "提取结果"},
        )
        assert r.status_code == 200, r.text
        draft = r.json()
        assert draft["status"] == "draft"
        assert draft["code"] == "RB-001"
        assert draft["current_version"] == 1
        assert draft["shared_version"] is None
        assert draft["question"] == EXTRACTED["question"]
        # 列表型文本字段被接成一句话，列表字段保持数组
        assert draft["methods"] == "射线追踪、镜像模型"
        assert draft["keywords"] == ["indoor localization", "multipath"]
        assert draft["source_conversation_id"] == student["conversation_id"]
        assert len(draft["source_message_ids"]) == 2

        # 提示词里只有本人消息，没有导师的私人消息
        assert len(fake.prompts) == 1
        prompt_text = json.dumps(fake.prompts[0], ensure_ascii=False)
        assert STUDENT_SECRET in prompt_text
        assert MENTOR_SECRET not in prompt_text

        # 草稿阶段另一角色完全看不到
        assert client.get(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "mentor"),
        ).status_code == 404

    def test_edit_then_confirm_shares_and_notifies(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        draft = _create_draft(client, temp_db, pid, question="")
        # 缺核心研究问题时不能确认
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": ""},
        )
        assert r.status_code == 400 and "研究问题" in r.text

        edited = client.patch(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "alice"),
            json={"title": "改过的研究定义",
                  "content": _content(question="多反射面下的亚米级定位",
                                      keywords=["indoor localization"])},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["question"] == "多反射面下的亚米级定位"
        assert edited.json()["current_version"] == 1   # 草稿阶段就地改，不产生版本噪音

        confirmed = client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "作为当前工作定义"},
        )
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["status"] == "confirmed"
        assert body["shared_version"] == 1
        assert body["display_code"] == "RB-001-v1"
        assert body["confirmed_at"]

        # 重复确认被状态机挡住
        assert client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "再点一次"},
        ).status_code == 409

        notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "mentor")
        ).json()
        assert any(n["event_type"] == "research_brief.confirmed" for n in notes)
        own_notes = client.get(
            "/supervision/notifications", headers=_headers(temp_db, "alice")
        ).json()
        assert not any(n["event_type"] == "research_brief.confirmed" for n in own_notes)

        with _session(temp_db) as s:
            actions = [
                row.action for row in s.query(SupervisionAuditEventModel)
                .filter_by(project_id=pid).all()
            ]
            assert actions.count("确认共享研究定义") == 1
            assert "创建研究定义草稿" in actions

    def test_reject_and_discard_only_for_own_drafts(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        first = _create_draft(client, temp_db, pid, title="否定的那份")
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/{first['code']}/reject",
            headers=_headers(temp_db, "alice"), json={"reason": ""},
        )
        assert r.status_code == 422
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/{first['code']}/reject",
            headers=_headers(temp_db, "alice"), json={"reason": "方向不对"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"
        # 已否定不能再确认
        assert client.post(
            f"/supervision/projects/{pid}/research/briefs/{first['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"note": ""},
        ).status_code == 409

        second = _create_draft(client, temp_db, pid, title="要丢弃的那份")
        assert client.delete(
            f"/supervision/projects/{pid}/research/briefs/{second['code']}",
            headers=_headers(temp_db, "alice"), json={"reason": "重复提取"},
        ).status_code == 200
        assert client.get(
            f"/supervision/projects/{pid}/research/briefs/{second['code']}",
            headers=_headers(temp_db, "alice"),
        ).status_code == 404


# ---------------------------------------------------------------------------
# 5. 确认共享后只能读结构化版本，不能反查来源消息（§16.3 第5条）
# ---------------------------------------------------------------------------


class TestSharedBriefBoundaries:
    def _confirmed(self, client, db, pid) -> dict:
        draft = _create_draft(client, db, pid)
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm",
            headers=_headers(db, "alice"), json={"note": "作为当前工作定义"},
        )
        assert r.status_code == 200, r.text
        return r.json()

    def test_other_role_reads_structured_brief_only(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        _private_dialogue(client, temp_db, pid)
        confirmed = self._confirmed(client, temp_db, pid)

        shared = client.get(
            f"/supervision/projects/{pid}/research/shared-briefs",
            headers=_headers(temp_db, "mentor"),
        )
        assert shared.status_code == 200, shared.text
        rows = shared.json()
        assert [r["code"] for r in rows] == ["RB-001"]
        row = rows[0]
        assert row["question"] == EXTRACTED["question"]
        assert row["is_owner"] is False
        assert row["can_edit"] is False
        # 结构化内容之外没有任何来源线索
        assert row["source_conversation_id"] is None
        assert row["source_message_ids"] == []

        got = client.get(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "mentor"),
        ).json()
        assert got["shown_version"] == 1
        assert got["source_message_ids"] == []

    def test_source_messages_are_rejected_for_other_role(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        # 手工创建时也带上来源会话，才能测"本人可回看、另一角色反查不到"
        draft = _create_draft(client, temp_db, pid,
                              conversation_id=student["conversation_id"])
        confirmed = client.post(
            f"/supervision/projects/{pid}/research/briefs/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "作为当前工作定义"},
        )
        assert confirmed.status_code == 200, confirmed.text

        r = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/source-messages",
            headers=_headers(temp_db, "mentor"),
        )
        assert r.status_code == 404
        # 本人仍然能打开自己研究定义的来源消息
        mine = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/source-messages",
            headers=_headers(temp_db, "alice"),
        )
        assert mine.status_code == 200, mine.text
        assert [m["query"] for m in mine.json()["messages"]] == [
            STUDENT_SECRET, STUDENT_SECRET_2,
        ]

    def test_other_role_cannot_mutate_shared_brief(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        self._confirmed(client, temp_db, pid)
        # 请求体都填成合法值，确保走到的是鉴权判定而不是字段校验
        cases = [
            ("patch", "/supervision/projects/RP-001/research/briefs/RB-001",
             {"title": "导师改学生的研究定义"}),
            ("post", "/supervision/projects/RP-001/research/briefs/RB-001/confirm",
             {"note": "导师替学生确认"}),
            ("post", "/supervision/projects/RP-001/research/briefs/RB-001/reject",
             {"reason": "导师替学生否定"}),
            ("get", "/supervision/projects/RP-001/research/briefs/RB-001/versions", None),
            ("get", "/supervision/projects/RP-001/research/briefs/RB-001/compare"
                    "?from_version=1&to_version=1", None),
        ]
        for method, url, payload in cases:
            kwargs = {"headers": _headers(temp_db, "mentor")}
            if payload is not None:
                kwargs["json"] = payload
            r = getattr(client, method)(url, **kwargs)
            assert r.status_code == 404, (method, url, r.text)
        r = client.delete(
            "/supervision/projects/RP-001/research/briefs/RB-001",
            headers=_headers(temp_db, "mentor"), json={"reason": "删掉"},
        )
        assert r.status_code == 404

    def test_draft_without_source_conversation_reports_conflict(self, client, temp_db):
        """手工创建、没带来源会话的研究定义：本人也只能看结构化内容。"""
        pid = _active_project(client, temp_db)["project_id"]
        _create_draft(client, temp_db, pid)
        r = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/source-messages",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 409 and "来源会话" in r.text

    def test_shared_brief_cannot_be_discarded(self, client, temp_db):
        """§13：已确认共享的研究定义是项目审计资产，只能出新版本。"""
        pid = _active_project(client, temp_db)["project_id"]
        self._confirmed(client, temp_db, pid)
        r = client.delete(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "alice"), json={"reason": "不想共享了"},
        )
        assert r.status_code == 409 and "审计" in r.text
        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/reject",
            headers=_headers(temp_db, "alice"), json={"reason": "改主意"},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# 6. 项目内容不会写成个人全局偏好（§16.3 第6条）
# ---------------------------------------------------------------------------


def _preference_table_counts(db) -> dict:
    """个人记忆/偏好与会话摘要相关表的行数（§9.3 要求全部保持 0）。"""
    tables = ("memory", "memory_revision", "conversation_memory_ignore", "session_summary")
    counts = {}
    with _session(db) as s:
        for table in tables:
            counts[table] = int(
                s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
            )
    return counts


class TestNoPersonalPreferenceWrites:
    def test_whole_flow_writes_no_memory_or_preference(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        mentor = _dialogue(client, temp_db, "mentor", pid, "导师会话·私有")
        _add_turns(temp_db, mentor["conversation_id"], [(MENTOR_SECRET, "收到。")])

        fake = FakeLLM(json.dumps(EXTRACTED, ensure_ascii=False))
        monkeypatch.setattr(research_routes, "_resolve_llm", lambda: fake)
        extracted = client.post(
            f"/supervision/projects/{pid}/research/briefs/extract",
            headers=_headers(temp_db, "alice"),
            json={"conversation_id": student["conversation_id"]},
        )
        assert extracted.status_code == 200, extracted.text
        client.patch(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "alice"),
            json={"content": _content(question="多反射面下的亚米级定位")},
        )
        client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "作为当前工作定义"},
        )
        for who in ("alice", "mentor"):
            with _session(temp_db) as s:
                assert research.build_project_context(s, pid, users_id[who]) != ""

        assert all(v == 0 for v in _preference_table_counts(temp_db).values()), (
            _preference_table_counts(temp_db)
        )

    def test_context_text_states_it_is_not_a_personal_preference(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        _create_draft(client, temp_db, pid)
        client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": ""},
        )
        with _session(temp_db) as s:
            context = research.build_project_context(s, pid, users_id["mentor"])
        assert "个人偏好" in context and "不得写入个人记忆表" in context


# ---------------------------------------------------------------------------
# 版本历史与比较（§4.3.1、§16.3 第4条）
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_reextract_appends_version_and_diff_is_structured(self, client, temp_db, monkeypatch):
        pid = _active_project(client, temp_db)["project_id"]
        student = _private_dialogue(client, temp_db, pid)
        draft = _create_draft(client, temp_db, pid)
        client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "第一版共享"},
        )

        second = dict(EXTRACTED)
        second["question"] = "多反射面 + 单频点条件下的亚米级定位"
        second["keywords"] = ["indoor localization", "multipath", "metasurface"]
        second["constraints"] = "只有 4 根接收天线，且只能离线处理"
        fake = FakeLLM(json.dumps(second, ensure_ascii=False))
        monkeypatch.setattr(research_routes, "_resolve_llm", lambda: fake)

        r = client.post(
            f"/supervision/projects/{pid}/research/briefs/extract",
            headers=_headers(temp_db, "alice"),
            json={"conversation_id": student["conversation_id"], "brief_ref": "RB-001"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["current_version"] == 2
        assert r.json()["status"] == "draft"
        # 已共享的那一版仍然是 v1，另一角色读到的还是旧依据（§4.4）
        assert r.json()["shared_version"] == 1
        mentor_view = client.get(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "mentor"),
        ).json()
        assert mentor_view["shown_version"] == 1
        assert mentor_view["question"] == EXTRACTED["question"]

        versions = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/versions",
            headers=_headers(temp_db, "alice"),
        )
        assert versions.status_code == 200
        rows = versions.json()
        assert [v["version"] for v in rows] == [1, 2]
        assert rows[0]["status"] == "confirmed"
        assert rows[0]["superseded_at"]
        assert rows[1]["status"] == "draft"

        diff = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/compare"
            "?from_version=1&to_version=2",
            headers=_headers(temp_db, "alice"),
        )
        assert diff.status_code == 200, diff.text
        body = diff.json()
        assert body["from_version"] == 1 and body["to_version"] == 2
        changes = {c["field"]: c for c in body["changes"]}
        assert set(changes) == {"question", "keywords", "constraints"}
        assert changes["question"]["to"].startswith("多反射面 + 单频点")
        assert changes["keywords"]["added"] == ["metasurface"]
        assert changes["keywords"]["removed"] == []
        assert changes["constraints"]["label"] == "约束条件"

    def test_editing_confirmed_brief_creates_new_version(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        _create_draft(client, temp_db, pid)
        client.post(
            f"/supervision/projects/{pid}/research/briefs/RB-001/confirm",
            headers=_headers(temp_db, "alice"), json={"note": "第一版共享"},
        )
        patched = client.patch(
            f"/supervision/projects/{pid}/research/briefs/RB-001",
            headers=_headers(temp_db, "alice"),
            json={"content": _content(expected_outcomes="中位定位误差小于 0.5 米")},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["current_version"] == 2
        assert patched.json()["shared_version"] == 1
        with _session(temp_db) as s:
            old = s.query(SupervisionResearchBriefVersionModel).filter_by(
                version=1).one()
            assert old.expected_outcomes == EXTRACTED["expected_outcomes"]
            new = s.query(SupervisionResearchBriefVersionModel).filter_by(
                version=2).one()
            assert new.expected_outcomes == "中位定位误差小于 0.5 米"
            assert "expected_outcomes" in new.edited_fields

    def test_unknown_version_compare_is_404(self, client, temp_db):
        pid = _active_project(client, temp_db)["project_id"]
        _create_draft(client, temp_db, pid)
        r = client.get(
            "/supervision/projects/RP-001/research/briefs/RB-001/compare"
            "?from_version=1&to_version=7",
            headers=_headers(temp_db, "alice"),
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# 提取提示词与结果解析（纯函数，不依赖模型接入）
# ---------------------------------------------------------------------------


class TestPromptAndParsing:
    def test_prompt_lists_every_field_and_only_given_turns(self):
        prompt = research.build_extraction_prompt(
            "RP-001", "多反射面室内定位研究", "student",
            [{"query": "我想做亚米级定位", "response": "范围边界还需要明确"},
             {"query": MENTOR_SECRET}],
        )
        assert [m["role"] for m in prompt] == ["system", "user"]
        joined = "\n".join(m["content"] for m in prompt)
        for field in ("question", "expected_outcomes", "keywords", "sub_questions"):
            assert field in joined
        assert "只能使用对话中出现过的信息" in joined
        assert "我想做亚米级定位" in joined

    def test_parse_json_payload(self):
        content = research.parse_brief_payload(json.dumps(EXTRACTED, ensure_ascii=False))
        assert content["question"] == EXTRACTED["question"]
        assert content["keywords"] == ["indoor localization", "multipath"]
        assert content["methods"] == "射线追踪、镜像模型"

    def test_parse_fenced_and_wrapped_json(self):
        fenced = "好的，下面是结果：\n```json\n" + json.dumps(
            {"brief": EXTRACTED}, ensure_ascii=False) + "\n```"
        content = research.parse_brief_payload(fenced)
        assert content["question"] == EXTRACTED["question"]
        assert content["sub_questions"] == EXTRACTED["sub_questions"]

    def test_parse_labeled_text_fallback(self):
        raw = (
            "研究背景：已有工作集中在单一反射面场景\n"
            "核心研究问题：多反射面条件下如何实现亚米级室内定位\n"
            "检索关键词：indoor localization，multipath\n"
            "约束条件：只有 4 根接收天线\n"
        )
        content = research.parse_brief_payload(raw)
        assert content["question"] == "多反射面条件下如何实现亚米级室内定位"
        assert content["background"] == "已有工作集中在单一反射面场景"
        assert content["keywords"] == ["indoor localization", "multipath"]
        assert content["constraints"] == "只有 4 根接收天线"

    def test_unparsable_payload_raises_409_not_empty_draft(self):
        with pytest.raises(research.WorkError):
            research.parse_brief_payload("抱歉，我没法整理。")
        with pytest.raises(research.WorkError):
            research.parse_brief_payload("")

    def test_diff_brief_content_is_pure(self):
        before = research._clean_content(_content())
        after = research._clean_content(_content(question="换了个问题",
                                                  keywords=["a", "b"]))
        changes = {c["field"]: c for c in research.diff_brief_content(before, after)}
        assert set(changes) == {"question", "keywords"}
        assert changes["keywords"]["added"] == ["a", "b"]
        assert changes["keywords"]["removed"] == ["indoor localization", "multipath"]

    def test_extract_brief_draft_rejects_none_llm(self, client, temp_db):
        """仓储层自己也要挡住 llm=None，避免绕过路由直接调用时起模型。"""
        pid = _active_project(client, temp_db)["project_id"]
        _private_dialogue(client, temp_db, pid)
        with _session(temp_db) as s:
            with pytest.raises(research.WorkError) as exc:
                research.extract_brief_draft(s, pid, users_id["alice"], llm=None)
            assert "模型能力尚未接入" in str(exc.value)
