"""论文检索与文献结论离线测试（《学生研究与导师审核功能重新设计方案》里程碑3）。

§15.3 与 §16.4 没有像 AI 预审方案那样给出编号好的 P 清单（那份方案里的
P-01…P-30 属于里程碑4），因此这里按「里程碑3 的 P 类用例」自编号
``P3-01…P3-30``，逐条对应 §15.3 的十项内容、§16.4 的七条放行标准与 §17
场景 G 的步骤；每个测试函数名里带对应编号，docstring 里写明依据。

覆盖对照：

- P3-01 只有已确认并共享的 ``ResearchBrief`` 能创建正式检索任务；未确认 →
  409 并说明原因（§16.4 第1条、§4.3.1、场景 G 第2步）
- P3-02 不存在、别人的未共享草稿、非成员一律 404（§10）
- P3-03 检索方案未经学生确认不能开始搜索（§6.3.1 第1步）
- P3-04 外部来源未接入 → 503，不伪造候选与命中
- P3-05 检索按「来源 × 关键词批次」拆子任务，显示来源、命中与去重数量
  （§6.3.A、§15.3 第1、3条）
- P3-06 单个来源失败只让该子任务失败，其它子任务与整个任务继续（§15.3 第6条）
- P3-07 三种幂等键去重：重复入库只产生一条记录并合并来源（§6.3 第4条）
- P3-08 可解释关联度：等级 + 中文理由 + 命中要素 + 缺失要素 + 规则版本，
  没有伪精确分数（§6.3.B、§16.4 第2条）
- P3-09 学生决定纳入/排除/稍后阅读必须写理由；导师不是决策方（403）；
  决定与理由留历史（§4.4、§16.4 第4条、场景 G 第7步）
- P3-10 检索不会自动深度提取任何候选（§16.4 第3条）
- P3-11 深度提取受任务上限约束，超过 → 409（§6.3.1 最后一段）
- P3-12 深度提取包含原文定位、方法、实验、结果、局限与项目差异；只有逐字
  核对到的引文算已核对证据（§16.4 第5条、§6.3.C）
- P3-13 单篇深度提取失败不回滚其它论文（§15.3 第6条）
- P3-14 模型未接入 → 检索方案/深度提取/摘要分析/比较一律 503，不假装跑过；
  模型有了但容量适配器没接入时，检索方案同样 503，深度提取走「记录失败并
  返回」路径留下 ``failed`` 与原因，两种情况都不会静默当成成功
- P3-15 每次实际模型调用都有容量取用/释放记录，消费者类型是已登记的
  ``research_plan`` / ``paper_extract`` / ``paper_compare``（§8.2、§15.3 最后一段）
- P3-16 摘要级关联分析有上限，结果写进 ``semantic_analysis`` 且不覆盖规则
  排序字段（§6.3.1 第3步、§6.3.B 最后一段）
- P3-17 多篇比较一次最多 6 篇（400），结果绑定子任务
- P3-18 失败子任务可单独重试：``attempt`` 递增、旧失败留历史、其它子任务不动
- P3-19 服务重启后遗留的 ``running``/``queued`` 子任务收敛为失败并可重试（§8.2）
- P3-20 证据卡片必须带可核对原文；同一证据重复提交不产生重复卡片（§15.3 第8条）
- P3-21 文献结论区分论文事实/学生判断/待验证假设，事实必须指向已纳入论文；
  没有纳入集合时 409（§4.5、场景 G 第8步）
- P3-22 结论绑定文献快照；纳入集合变化后旧结论标记「依据已变化」而不被改写
  （§4.5、§13）
- P3-23 结论修订生成新版本，旧版本标 ``superseded`` 保留（§4.0 版本规则）
- P3-24 导师确认/质疑/要求补充证据；学生调用 403；未确认的草稿 409（§4.5）
- P3-25 私人论文研究成果只能按具体成果授权引用快照，快照固定当时字段与
  内容哈希；别人的私人成果 404（§9.1、§16.4 第6条）
- P3-26 撤销引用只关闭后续授权，历史快照与审计继续保留（§9.1、§13）
- P3-27 未登录、令牌过期、被禁用用户与非成员：401/404，不返回任何内容
  （§10、场景 C）
- P3-28 论文处理失败不阻塞人工任务与进展记录（§16.4 第7条）
- P3-29 正式动作都进审计日志（§13）
- P3-30 字段非法 400、pydantic 形状不符 422（与既有 supervision 模块一致）
- P3-31 来源 provider 适配层桥接论文研究模块的 async 来源；未知来源只让该来源
  失败并列出当前可用来源，不退化成「搜到了 0 篇」
- P3-32 默认注入真实来源适配层；``CHATCHAT_LITERATURE_ENABLE_SOURCES=0`` 退回
  「未接入」→ 检索 503
- P3-33 走真实来源接线的检索：候选入库、来源与 arXiv 编号可追溯，没接入的
  来源逐条标失败

夹具与 ``test_supervision_research.py`` / ``test_supervision_ai_review.py``
一致：临时 SQLite + ``apply_migrations`` + ``Base.metadata.create_all`` +
httpx ASGITransport + 真实 JWT。模型一律用注入的假 llm，来源一律用注入的
假 provider（P3-31/33 用假 async 适配器验证桥接，P3-32 只检查接线与自检），
**不联网、不调真实模型、不起任何服务**；容量闸门用替身记录
取用/释放（真正的单模型并发证据由 ``test_model_capacity_gate.py`` 覆盖）。
"""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server.supervision_literature_routes import (
    ResearchSourcesLiteratureProvider,
    get_literature_capacity,
    get_literature_llm,
    get_literature_provider,
    supervision_literature_router,
)
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
from chatchat.server.db.models.research_model import (  # noqa: F401
    EvidenceCardModel,
    PaperRecordModel,
    ProjectPaperModel,
)
from chatchat.server.db.models.supervision_ai_review_model import (  # noqa: F401
    SupervisionAIReviewRunModel,
    SupervisionAIReviewSettingModel,
)
from chatchat.server.db.models.supervision_literature_model import (  # noqa: F401
    SupervisionLiteratureCitationModel,
    SupervisionLiteratureConclusionModel,
    SupervisionLiteratureEvidenceCardModel,
    SupervisionLiteratureExtractionModel,
    SupervisionLiteratureResultModel,
    SupervisionLiteratureSearchTaskModel,
    SupervisionLiteratureSubtaskModel,
)
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
from chatchat.server.db.models.supervision_work_model import (  # noqa: F401
    SupervisionAttachmentModel,
    SupervisionMilestoneModel,
    SupervisionProgressUpdateModel,
    SupervisionReviewAcModel,
    SupervisionReviewModel,
    SupervisionSubmissionModel,
    SupervisionTaskAcModel,
    SupervisionTaskModel,
)
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.repository import supervision_literature_repository as lit
from chatchat.server.db.session import get_db as db_get_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "literature-unit-test-secret-000000"
    )
    return "literature-unit-test-secret-000000"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "literature.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        # 里程碑0A/1/2/4 的迁移必须已经跑过；本模块的七张新表由下面的
        # ``create_all`` 建出来（正式环境的 v21 DDL 由集成方补写）。
        assert current_version(conn) >= 20
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    yield {"Session": Session, "engine": engine}
    engine.dispose()


@pytest.fixture(autouse=True)
def literature_limits(monkeypatch):
    """每个用例都从默认上限出发，需要收窄的用例自己 monkeypatch.setenv。"""
    for name in (
        "CHATCHAT_LITERATURE_DEFAULT_DEEP_EXTRACT",
        "CHATCHAT_LITERATURE_MAX_ABSTRACT_ANALYSIS",
    ):
        monkeypatch.delenv(name, raising=False)


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
        "alice": _mk_user(temp_db, "alice"),            # 项目里的学生
        "mentor": _mk_user(temp_db, "mentor"),          # 项目里的主导师
        "bob": _mk_user(temp_db, "bob"),                # 别的项目的学生
        "other_mentor": _mk_user(temp_db, "other_mentor"),
        "root": _mk_user(temp_db, "root", role="admin"),
    }


users_id = {}


@pytest.fixture(autouse=True)
def _track_users(users):
    users_id.clear()
    users_id.update({k: v["id"] for k, v in users.items()})


@pytest.fixture
def app(temp_db):
    a = FastAPI()
    a.include_router(auth_router)
    a.include_router(supervision_router)
    a.include_router(supervision_work_router)
    a.include_router(supervision_research_router)
    a.include_router(supervision_literature_router)

    async def override_get_db():
        s = temp_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_get_db] = override_get_db
    # 默认：模型与外部来源都尚未接入（执行类接口 503，绝不伪造结果）。
    a.dependency_overrides[get_literature_llm] = lambda: None
    a.dependency_overrides[get_literature_capacity] = lambda: None
    a.dependency_overrides[get_literature_provider] = (
        lambda: lit.UnconfiguredLiteratureProvider()
    )
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
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 假模型、容量替身与假来源
# ---------------------------------------------------------------------------

BRIEF_CONTENT = {
    "background": "已有工作集中在单一反射面场景",
    "question": "如何实现 sub-meter indoor localization under multipath",
    "objectives": "给出可复现的多反射面定位方法",
    "subjects": "indoor Grid2N simulation",
    "scope": "static scenario 2.4 GHz",
    "prior_attempts": "已复现单反射面基线",
    "hypotheses": "反射阶数越高定位误差越大",
    "constraints": "只有 4 根接收天线",
    "methods": ["ray tracing", "mirror model"],
    "metrics": ["localization error"],
    "expected_outcomes": "中位定位误差小于 1 米",
    "to_verify": "多径阶数与误差的关系是否成立",
    "keywords": ["indoor localization", "multipath"],
    "sub_questions": ["多径阶数如何量化", "baseline selection"],
}

CAND_A = {
    "title": "Ray tracing for indoor localization under multipath",
    "source": "arxiv", "authors": ["A. Zhang", "B. Li"], "year": 2023,
    "abstract": (
        "We study indoor localization with ray tracing and a mirror model in the "
        "Grid2N simulation; localization error drops below one meter."
    ),
    "arxiv_id": "2301.00001v2",
    "url": "https://arxiv.org/abs/2301.00001", "fulltext_status": "pdf",
}
# 同一篇论文的另一个来源：标题与年份相同 → 必须去重成一条记录并合并来源。
CAND_A_DUP = {
    "title": "Ray tracing for indoor localization under multipath",
    "source": "crossref", "authors": ["A. Zhang"], "year": 2023,
    "abstract": "同一篇论文经 Crossref 的元数据。",
    "doi": "https://doi.org/10.1000/xyz",
    "url": "https://doi.org/10.1000/xyz", "fulltext_status": "url",
}
CAND_B = {
    "title": "Baseline selection for mirror model indoor localization",
    "source": "arxiv", "authors": ["C. Wang"], "year": 2022,
    "abstract": (
        "Compares baseline selection strategies for indoor localization and reports "
        "localization error metrics for each configuration."
    ),
    "arxiv_id": "2202.00002", "url": "https://arxiv.org/abs/2202.00002",
    "fulltext_status": "abstract",
}
CAND_C = {
    "title": "Deep learning for protein folding",
    "source": "arxiv", "authors": ["D. Wu"], "year": 2021,
    "abstract": "Unrelated work on protein structure prediction.",
    "arxiv_id": "2103.00003", "url": "https://arxiv.org/abs/2103.00003",
    "fulltext_status": "unknown",
}

# 另一篇不相干的候选：第二个检索任务用它产生新的 P 编号。
CAND_D = {
    "title": "Antenna calibration error budget for indoor localization",
    "source": "arxiv", "authors": ["E. Zhao"], "year": 2024,
    "abstract": "Quantifies antenna calibration error for indoor localization arrays.",
    "arxiv_id": "2405.00004", "url": "https://arxiv.org/abs/2405.00004",
    "fulltext_status": "abstract",
}

PLAN_PAYLOAD = {
    "queries": [
        {"query": "indoor localization multipath", "source": "arxiv",
         "rationale": "核心研究问题"},
    ],
    "exclude_terms": ["protein"], "year_from": 2018, "year_to": 2026,
    "note": "先做一轮宽检索",
}

EXTRACTION_PAYLOAD = {
    "summary": "用射线追踪与镜像模型做多反射面室内定位。",
    "research_problem": "多径条件下的亚米级定位",
    "method": "ray tracing + mirror model",
    "algorithm": "镜像阶数截断到 3 阶",
    "experiments": "Grid2N 仿真，4 根接收天线",
    "results_metrics": "中位 localization error 0.83 m",
    "limitations": "本次输入只有摘要级材料，未读取全文",
    "differences": {
        "same": "同样是室内多径场景", "different": "他们只有单频点",
        "borrowable": "镜像阶数截断做法", "usable_methods": "镜像模型",
        "baseline_methods": "单反射面基线", "conflicting_assumptions": "静态场景假设",
        "gaps": "未考虑天线标定误差", "suggested_tasks": "复现其基线",
    },
    "evidence": [
        {"evidence_type": "method",
         "quote": "We study indoor localization with ray tracing and a mirror model",
         "locator": {"section": "abstract"}},
        {"evidence_type": "result",
         "quote": "localization error drops below one meter",
         "locator": {"section": "abstract", "char_start": 100, "char_end": 141}},
        {"evidence_type": "method",
         "quote": "模型自己改写的一句话，原文里并没有这样写",
         "locator": {}},
    ],
}

ANALYSIS_PAYLOAD = {
    "papers": [
        {"code": "P-001", "level": "relevant", "solves": "多径定位",
         "same": "场景相同", "different": "频点不同", "borrowable": "镜像阶数",
         "impact": "可作为基线", "reason": "摘要级证据",
         "evidence_quotes": [{"quote": "localization error drops below one meter",
                              "locator": "abstract"}]},
        {"code": "P-002", "level": "reference", "solves": "基线选择",
         "same": "都用镜像模型", "different": "没有多径扫描", "borrowable": "基线配置",
         "impact": "用于对照", "reason": "摘要级证据",
         "evidence_quotes": [{"quote": "reports localization error metrics",
                              "locator": "abstract"}]},
    ],
    "note": "仅摘要级判断",
}

COMPARE_PAYLOAD = {
    "dimensions": [{"name": "场景", "values": {"P-001": "多径", "P-002": "基线选择"}}],
    "agreements": ["都用镜像模型"], "conflicts": ["阶数截断不同"],
    "gaps": ["都没做标定"], "note": "比较结论",
}


class FakeLLM:
    """假模型：``invoke(messages)`` 返回带 ``.content`` 的对象。

    ``payload`` 可以是 dict、JSON 字符串或 ``fn(call_index, messages)``；
    ``fail_at`` 在第 N 次调用时抛异常（模拟模型服务失败）。
    """

    def __init__(self, payload=None, *, fail_at=None, model_name="fake-lit-model"):
        self.payload = payload
        self.fail_at = fail_at
        self.model_name = model_name
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages)
        index = len(self.prompts)
        if self.fail_at is not None and index >= self.fail_at:
            raise RuntimeError("模型服务超时")
        payload = (
            self.payload(index, messages) if callable(self.payload) else self.payload
        )
        if payload is None:
            return SimpleNamespace(content="这不是 JSON，无法解析")
        content = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False
        )
        return SimpleNamespace(content=content)


class RecordingCapacity:
    """闸门替身：只记录取用/释放，不实现任何并发控制（P3-15 的业务侧证据）。"""

    def __init__(self):
        self.events = []

    def acquire(self, *, consumer, business_ref, user_id=""):
        self.events.append(("acquire", consumer, business_ref, user_id))
        return len(self.events)

    def release(self, token, reason=""):
        self.events.append(("release", token, reason))


class FakeProvider:
    """假来源 provider：按来源返回固定候选，可指定某些来源本次失败。"""

    def __init__(self, by_source=None, fail_sources=(), available=True):
        self.by_source = by_source or {}
        self.fail_sources = set(fail_sources)
        self.available_flag = available
        self.calls = []

    def available(self):
        return (self.available_flag, "" if self.available_flag else "外部论文来源尚未接入")

    def search(self, *, query, source, limit):
        self.calls.append({"source": source, "query": query, "limit": limit})
        if source in self.fail_sources:
            raise RuntimeError(f"{source} 暂时不可用")
        return self.by_source.get(source, [])


def use_llm(app, llm, capacity=None):
    app.dependency_overrides[get_literature_llm] = lambda: llm
    app.dependency_overrides[get_literature_capacity] = lambda: capacity


def use_provider(app, provider):
    app.dependency_overrides[get_literature_provider] = lambda: provider


def default_provider():
    return FakeProvider(by_source={"arxiv": [CAND_A, CAND_B, CAND_C],
                                   "crossref": [CAND_A_DUP]})


# ---------------------------------------------------------------------------
# 业务夹具
# ---------------------------------------------------------------------------


def _active_project(client, db, student="alice", mentor="mentor") -> dict:
    """建项目 → 绑主导师 → 激活。"""
    created = client.post(
        "/supervision/projects", headers=_headers(db, student),
        json={"name": "多反射面室内定位研究", "description": "里程碑3 验证"},
    )
    assert created.status_code == 200, created.text
    pid = created.json()["project_id"]
    added = client.post(
        f"/supervision/projects/{pid}/members", headers=_headers(db, student),
        json={"user_id": users_id[mentor], "role": "mentor"},
    )
    assert added.status_code == 200, added.text
    activated = client.post(
        f"/supervision/projects/{pid}/activate", headers=_headers(db, student)
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def _confirmed_brief(client, db, pid, who="alice", **overrides) -> dict:
    """手工创建研究定义草稿并确认共享，返回已确认的 ``RB-001``。"""
    content = dict(BRIEF_CONTENT)
    content.update(overrides)
    created = client.post(
        f"/supervision/projects/{pid}/research/briefs", headers=_headers(db, who),
        json={"title": "多反射面定位研究定义", "content": content},
    )
    assert created.status_code == 200, created.text
    confirmed = client.post(
        f"/supervision/projects/{pid}/research/briefs/{created.json()['code']}/confirm",
        headers=_headers(db, who), json={"note": "按这一版开始检索"},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _draft_brief(client, db, pid, who="alice") -> dict:
    content = dict(BRIEF_CONTENT)
    created = client.post(
        f"/supervision/projects/{pid}/research/briefs", headers=_headers(db, who),
        json={"title": "还没确认的研究定义", "content": content},
    )
    assert created.status_code == 200, created.text
    return created.json()


def _create_task(client, db, pid, brief_code, *, deep_extract_limit=2, who="alice",
                 **extra) -> dict:
    body = {
        "brief_ref": brief_code,
        "goal": "验证多径阶数与定位误差的关系",
        "purpose": "verify",
        "keywords": ["indoor localization", "multipath"],
        "synonyms": ["ray tracing"],
        "exclude_terms": ["protein"],
        "sources": ["arxiv", "crossref"],
        "result_limit": 20,
        "deep_extract_limit": deep_extract_limit,
    }
    body.update(extra)
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(db, who),
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _plan_and_confirm(client, app, db, pid, task_code, *, llm=None, capacity=None) -> dict:
    """生成并确认检索方案（一次短调用 + 学生确认）。"""
    use_llm(app, llm or FakeLLM(PLAN_PAYLOAD), capacity or RecordingCapacity())
    planned = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task_code}/plan",
        headers=_headers(db, "alice"), json={"note": "按研究问题生成"},
    )
    assert planned.status_code == 200, planned.text
    assert planned.json()["plan_status"] == "completed", planned.text
    confirmed = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task_code}/plan/confirm",
        headers=_headers(db, "alice"), json={"note": "确认这一版检索式"},
    )
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def _searched_task(client, app, db, pid, *, deep_extract_limit=2, provider=None,
                   brief_overrides=None) -> dict:
    """走完「确认研究定义 → 建任务 → 生成并确认方案 → 检索」，返回任务详情。"""
    brief = _confirmed_brief(client, db, pid, **(brief_overrides or {}))
    task = _create_task(client, db, pid, brief["code"],
                        deep_extract_limit=deep_extract_limit)
    _plan_and_confirm(client, app, db, pid, task["code"])
    use_provider(app, provider or default_provider())
    searched = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(db, "alice"), json={},
    )
    assert searched.status_code == 200, searched.text
    return searched.json()


def _results(client, db, pid, **params) -> list:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/supervision/projects/{pid}/literature/results" + (f"?{query}" if query else "")
    r = client.get(url, headers=_headers(db, "alice"))
    assert r.status_code == 200, r.text
    return r.json()


def _included(client, db, pid, codes=("P-001", "P-002")) -> list:
    """把指定候选论文标成纳入，返回结果列表。"""
    out = []
    for code in codes:
        r = client.post(
            f"/supervision/projects/{pid}/literature/results/{code}/decision",
            headers=_headers(db, "alice"),
            json={"decision": "included", "reason": "命中核心问题，方法与场景一致"},
        )
        assert r.status_code == 200, r.text
        out.append(r.json())
    return out


def _conclusion_body(**overrides) -> dict:
    body = {
        "statement": "多径阶数是亚米级定位的主要误差来源，镜像模型可作为本项目基线",
        "supported_facts": [
            {"statement": "该文在 Grid2N 上把中位误差降到 1 米以内",
             "result_code": "P-001"},
        ],
        "student_judgements": [
            {"statement": "镜像模型在 2.4 GHz 单频点下可直接作为基线",
             "basis_result_codes": ["P-001", "P-002"]},
        ],
        "unverified_hypotheses": [
            {"statement": "阶数超过 3 后误差增长变缓",
             "how_to_verify": "在自有数据集上按阶数扫描"},
        ],
        "research_gaps": ["两篇都没有考虑天线标定误差"],
        "confirm": True,
    }
    body.update(overrides)
    return body


def _audit_actions(db, pid) -> list:
    with _session(db) as s:
        return [
            row.action
            for row in s.execute(
                select(SupervisionAuditEventModel).where(
                    SupervisionAuditEventModel.project_id == pid
                )
            ).scalars().all()
        ]


# --- TESTS ---


# P3-01 ---------------------------------------------------------------------
def test_task_requires_confirmed_and_shared_brief(client, app, temp_db):
    """未确认共享的研究定义不能创建正式检索任务；409 并说明原因（§16.4 第1条）。"""
    project = _active_project(client, temp_db)
    pid = project["project_id"]
    draft = _draft_brief(client, temp_db, pid)

    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(temp_db, "alice"),
        json={"brief_ref": draft["code"], "goal": "先随便搜一轮"},
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "已确认并共享" in detail and draft["code"] in detail
    assert "确认本版研究定义" in detail

    # 草稿状态的任务也一样拦：依据门槛不看任务自己的状态。
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(temp_db, "alice"),
        json={"brief_ref": draft["code"], "goal": "先记一笔", "as_draft": True},
    )
    assert r.status_code == 409, r.text

    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    assert task["status"] == "pending"
    assert task["brief_code"] == brief["code"] and task["brief_version"] == 1
    assert task["brief_kind"] == brief["kind"]
    # 依据固定成副本：任务里存的是当时那一版的结构化内容。
    assert "multipath" in task["brief_snapshot"]["question"]
    assert task["brief_superseded"] is False and task["brief_shared_version"] == 1


# P3-02 ---------------------------------------------------------------------
def test_task_and_results_visibility_is_404_for_outsiders(client, app, temp_db):
    """不存在、别人的未共享草稿、非成员一律 404（§10）。"""
    pid = _active_project(client, temp_db)["project_id"]
    other_pid = _active_project(client, temp_db, student="bob", mentor="other_mentor")[
        "project_id"
    ]
    draft = _draft_brief(client, temp_db, pid)

    # 别人（这里是同项目的导师）还没共享出去的草稿：按不存在回答，不泄露其存在。
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(temp_db, "mentor"),
        json={"brief_ref": draft["code"], "goal": "看看别人的草稿"},
    )
    assert r.status_code == 404, r.text

    # 研究定义不存在。
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(temp_db, "alice"),
        json={"brief_ref": "RB-404", "goal": "不存在的研究定义"},
    )
    assert r.status_code == 404, r.text

    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])

    # 非成员：alice 项目里的任务、候选论文、结论、引用快照、证据卡片都读不到。
    for url in (
        f"/supervision/projects/{pid}/literature/tasks",
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        f"/supervision/projects/{pid}/literature/results",
        f"/supervision/projects/{pid}/literature/conclusions",
        f"/supervision/projects/{pid}/literature/citations",
        f"/supervision/projects/{pid}/literature/evidence-cards",
    ):
        assert client.get(url, headers=_headers(temp_db, "bob")).status_code == 404, url
    # 非成员也不能在 alice 项目里创建任何东西。
    for url, body in (
        (f"/supervision/projects/{pid}/literature/tasks",
         {"brief_ref": brief["code"], "goal": "替别人建检索任务"}),
        (f"/supervision/projects/{pid}/literature/conclusions", _conclusion_body()),
        (f"/supervision/projects/{pid}/literature/citations",
         {"source_kind": "project_result", "source_ref": "P-001", "purpose": "progress"}),
    ):
        assert client.post(
            url, headers=_headers(temp_db, "bob"), json=body
        ).status_code == 404, url

    # bob 自己项目里引用 alice 项目的任务编号 → 同样 404（跨项目不泄露）。
    r = client.get(
        f"/supervision/projects/{other_pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "bob"),
    )
    assert r.status_code == 404, r.text
    # alice 项目里引用不存在的任务编号。
    r = client.get(
        f"/supervision/projects/{pid}/literature/tasks/LS-404",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 404, r.text


# P3-03 ---------------------------------------------------------------------
def test_search_requires_student_confirmed_plan(client, app, temp_db):
    """检索方案没生成或没经学生确认就不能开始搜索（§6.3.1 第1步）。"""
    pid = _active_project(client, temp_db)["project_id"]
    use_provider(app, default_provider())
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])

    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert r.status_code == 409, r.text
    assert "检索方案尚未生成" in r.json()["detail"]

    use_llm(app, FakeLLM(PLAN_PAYLOAD), RecordingCapacity())
    planned = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert planned.status_code == 200, planned.text
    assert planned.json()["search_plan_confirmed_at"] is None

    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert r.status_code == 409, r.text
    assert "学生确认" in r.json()["detail"]

    # 导师不是检索方案的确认方？方案由学生确认；导师确认也允许（项目成员），
    # 但这里按 §6.3.1 只测学生路径。
    confirmed = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan/confirm",
        headers=_headers(temp_db, "alice"), json={"note": "就按这几条检索式"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["search_plan_confirmed_by"] == users_id["alice"]

    searched = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["status"] == "triage"


# P3-04 ---------------------------------------------------------------------
def test_search_without_source_provider_is_503_and_fakes_nothing(client, app, temp_db):
    """外部来源未接入 → 503 + 中文说明，绝不造一批假候选（§15.3 第2条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    _plan_and_confirm(client, app, temp_db, pid, task["code"])
    # 默认 provider 就是 UnconfiguredLiteratureProvider（app 夹具里已覆盖）。

    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert r.status_code == 503, r.text
    assert "外部论文来源尚未接入" in r.json()["detail"]

    # 没有伪造：任务状态没推进，也没有任何候选论文入库。
    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "alice"),
    )
    assert after.json()["status"] == "pending"
    assert after.json()["hits_total"] == 0 and after.json()["stored_total"] == 0
    assert _results(client, temp_db, pid) == []


# P3-05 ---------------------------------------------------------------------
def test_search_splits_into_independent_subtasks_with_counts(client, app, temp_db):
    """检索按「来源 × 关键词批次」拆子任务，各自显示来源、命中与去重数量。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    assert task["status"] == "triage"
    assert task["hits_total"] == 4          # 3 + 1 条命中
    assert task["stored_total"] == 3        # 去重后入库 3 条
    assert task["deduped_total"] == 1       # 合并掉 1 条
    assert task["source_failures"] == []

    subtasks = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    )
    assert subtasks.status_code == 200, subtasks.text
    by_code = {row["code"]: row for row in subtasks.json()}
    search_rows = [row for row in subtasks.json() if row["kind"] == "search"]
    assert len(search_rows) == 2  # 2 个来源 × 1 条已确认检索式
    assert {row["source"] for row in search_rows} == {"arxiv", "crossref"}
    assert all(row["query"] == "indoor localization multipath" for row in search_rows)
    arxiv = next(row for row in search_rows if row["source"] == "arxiv")
    crossref = next(row for row in search_rows if row["source"] == "crossref")
    assert (arxiv["result_total"], arxiv["new_total"], arxiv["duplicate_total"]) == (3, 3, 0)
    assert (crossref["result_total"], crossref["new_total"], crossref["duplicate_total"]) == (
        1, 0, 1,
    )
    assert all(row["status"] == "completed" for row in search_rows)
    # 排序规则版本单独记账，页面据此显示"排序规则版本"。
    rank_rows = [row for row in subtasks.json() if row["kind"] == "rank"]
    assert len(rank_rows) == 1
    assert rank_rows[0]["payload"]["rules_version"] == lit.RULES_VERSION
    assert set(by_code) == {
        f"{task['code']}-PL01", f"{task['code']}-SE01", f"{task['code']}-SE02",
        f"{task['code']}-RK01",
    }
    assert task["subtask_total"] == 4 and task["subtask_failed_total"] == 0

    rows = _results(client, temp_db, pid)
    assert len(rows) == 3
    first = next(row for row in rows if row["code"] == "P-001")
    # 去重不丢可追溯性：来源被并进 sources。
    assert {entry["source"] for entry in first["sources"]} == {"arxiv", "crossref"}
    assert first["arxiv_id"] == "2301.00001"  # 版本后缀已规范化


# P3-06 ---------------------------------------------------------------------
def test_single_source_failure_does_not_affect_other_subtasks(client, app, temp_db):
    """单个来源失败只让该子任务失败，其它子任务与整个任务继续（§15.3 第6条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    provider = FakeProvider(
        by_source={"arxiv": [CAND_A, CAND_B, CAND_C], "crossref": [CAND_A_DUP]},
        fail_sources={"crossref"},
    )
    task = _searched_task(client, app, temp_db, pid, provider=provider)

    assert task["status"] == "triage"          # 任务照常推进
    assert task["stored_total"] == 3           # arxiv 那批全部入库
    assert task["subtask_failed_total"] == 1   # 只有 crossref 失败
    assert len(task["source_failures"]) == 1
    failure = task["source_failures"][0]
    assert failure["source"] == "crossref" and "暂时不可用" in failure["reason"]

    subtasks = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()
    failed = [row for row in subtasks if row["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["source"] == "crossref"
    assert failed[0]["error_code"] == "source_failed"
    assert failed[0]["result_total"] == 0
    completed = [row for row in subtasks if row["status"] == "completed"]
    assert {row["source"] for row in completed if row["kind"] == "search"} == {"arxiv"}
    assert len(_results(client, temp_db, pid)) == 3


# P3-07 ---------------------------------------------------------------------
def test_three_idempotency_keys_dedup_into_one_record(client, app, temp_db):
    """DOI / arXiv ID / 规范化标题+年份三种键都去重，只产生一条记录（§6.3 第4条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    provider = FakeProvider(by_source={
        "arxiv": [dict(CAND_A, doi="https://doi.org/10.1000/xyz"), CAND_B, CAND_C],
        "crossref": [
            # 同一 DOI，标题完全不同 → 按 DOI 去重
            {"title": "Completely different title", "year": 2024, "source": "crossref",
             "doi": "10.1000/XYZ", "url": "https://doi.org/10.1000/xyz"},
            # 同一 arXiv ID（带版本后缀），标题不同 → 按 arXiv ID 去重
            {"title": "Yet another title", "year": 2020, "source": "crossref",
             "arxiv_id": "2202.00002v3", "url": "https://arxiv.org/abs/2202.00002v3"},
            # 同一标题+年份，没有任何稳定标识 → 按标题键去重
            {"title": "Ray tracing for indoor localization under multipath",
             "year": 2023, "source": "crossref", "url": "https://example.org/a"},
        ],
    })
    task = _searched_task(client, app, temp_db, pid, provider=provider)

    assert task["hits_total"] == 6 and task["stored_total"] == 3 and task["deduped_total"] == 3
    rows = _results(client, temp_db, pid)
    assert len(rows) == 3
    assert [row["code"] for row in rows] == ["P-001", "P-002", "P-003"]
    by_code = {row["code"]: row for row in rows}
    assert by_code["P-001"]["doi"] == "10.1000/xyz"
    assert {entry["source"] for entry in by_code["P-001"]["sources"]} == {"arxiv", "crossref"}
    assert len(by_code["P-001"]["sources"]) == 3
    assert by_code["P-002"]["arxiv_id"] == "2202.00002"
    assert len(by_code["P-002"]["sources"]) == 2
    assert by_code["P-003"]["dedup_key"].startswith("arxiv_id:")

    # 库层三条部分唯一索引兜底：服务层漏判也插不进第二条同 DOI 记录。
    from sqlalchemy.exc import IntegrityError

    with _session(temp_db) as s:
        existing = s.execute(
            select(SupervisionLiteratureResultModel).where(
                SupervisionLiteratureResultModel.code == "P-001"
            )
        ).scalar_one()
        with pytest.raises(IntegrityError):
            with s.begin_nested():
                s.add(SupervisionLiteratureResultModel(
                    id=lit.new_id(), code="P-900", project_id=pid,
                    search_task_id=existing.search_task_id,
                    dedup_key="doi:10.9999/other", doi="10.1000/xyz",
                    title="漏判的重复记录", decision="pending", extract_state="none",
                    relevance_level="low", relevance_score_source="rule_keyword",
                    relevance_rules_version=lit.RULES_VERSION, fulltext_status="unknown",
                ))
                s.flush()


# P3-08 ---------------------------------------------------------------------
def test_relevance_is_explainable_without_fake_scores(client, app, temp_db):
    """关联度是等级 + 中文理由 + 命中/缺失要素 + 规则版本，没有伪精确分数。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    rows = _results(client, temp_db, pid)
    by_code = {row["code"]: row for row in rows}

    high = by_code["P-001"]
    assert high["relevance_level"] == "high"
    assert high["relevance_score"] is None          # 绝不伪造 95/91/94
    assert high["relevance_score_source"] == "rule_keyword"
    assert high["relevance_rules_version"] == lit.RULES_VERSION
    assert high["recommended_action"] == "priority_read"
    assert "高相关" in high["relevance_reason"]
    assert "命中" in high["relevance_reason"]
    fields = {item["field"] for item in high["relevance_matched"]}
    assert {"question", "methods", "metrics"} <= fields
    assert all(item["term"] and item["label"] for item in high["relevance_matched"])

    low = by_code["P-003"]
    assert low["relevance_level"] == "low"
    assert low["recommended_action"] == "low_relevant"
    assert low["relevance_matched"] == []
    assert low["relevance_reason"].startswith("低相关")
    assert {item["field"] for item in low["relevance_missing"]}

    # 列表默认按关联等级从高到低排。
    order = ["high", "relevant", "reference", "low"]
    levels = [row["relevance_level"] for row in _results(client, temp_db, pid)]
    assert levels == sorted(levels, key=order.index)
    assert levels[0] == "high" and levels[-1] == "low"
    assert [row["code"] for row in _results(client, temp_db, pid, relevance="low")] == ["P-003"]
    # 非法筛选值 → 400（不是 422：枚举校验在仓储层）。
    r = client.get(
        f"/supervision/projects/{pid}/literature/results?relevance=very_high",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 400, r.text


# P3-09 ---------------------------------------------------------------------
def test_student_decisions_need_reason_and_are_not_mentor_job(client, app, temp_db):
    """纳入/排除/稍后阅读由学生决定且必须写理由；导师看得到但不是决策方。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    url = f"/supervision/projects/{pid}/literature/results/P-001/decision"

    # 导师不是决策方（§16.4 第4条）。
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"decision": "included", "reason": "导师替学生定了"})
    assert r.status_code == 403, r.text
    assert "学生" in r.json()["detail"]

    # 缺理由 → 422（pydantic 形状）；理由为空、决定取值非法 → 400。
    r = client.post(url, headers=_headers(temp_db, "alice"), json={"decision": "included"})
    assert r.status_code == 422, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"decision": "included", "reason": "   "})
    assert r.status_code == 400, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"decision": "maybe", "reason": "取值不在枚举里"})
    assert r.status_code == 400, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"decision": "pending", "reason": "pending 不是决定"})
    assert r.status_code == 400, r.text

    included = client.post(url, headers=_headers(temp_db, "alice"),
                           json={"decision": "included", "reason": "命中核心问题与方法"})
    assert included.status_code == 200, included.text
    body = included.json()
    assert body["decision"] == "included" and body["decision_reason"] == "命中核心问题与方法"
    assert body["decided_by"] == users_id["alice"] and body["decided_role"] == "student"
    assert body["decision_history"] == []

    # 改主意不覆盖旧理由：旧决定进历史。
    changed = client.post(url, headers=_headers(temp_db, "alice"),
                          json={"decision": "read_later", "reason": "先补全文再定"})
    assert changed.status_code == 200, changed.text
    history = changed.json()["decision_history"]
    assert len(history) == 1
    assert history[0]["decision"] == "included"
    assert history[0]["reason"] == "命中核心问题与方法"
    assert changed.json()["decision"] == "read_later"

    _included(client, temp_db, pid, codes=("P-002",))
    task = client.get(
        f"/supervision/projects/{pid}/literature/tasks/LS-001",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert task["included_total"] == 1 and task["read_later_total"] == 1
    assert task["undecided_total"] == 1 and task["result_total"] == 3

    # 导师读得到决定与理由（可见，不是决策方）。
    mentor_view = client.get(
        f"/supervision/projects/{pid}/literature/results/P-001",
        headers=_headers(temp_db, "mentor"),
    )
    assert mentor_view.status_code == 200, mentor_view.text
    assert mentor_view.json()["decision_reason"] == "先补全文再定"
    assert mentor_view.json()["decided_by"] == users_id["alice"]


# P3-10 ---------------------------------------------------------------------
def test_search_never_auto_deep_extracts(client, app, temp_db):
    """检索不会自动深度提取任何候选（§16.4 第3条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid, deep_extract_limit=20)
    assert task["deep_extract_used"] == 0
    assert "deep_extract" not in {row["kind"] for row in task["recent_subtasks"]}
    for row in _results(client, temp_db, pid):
        assert row["extract_state"] == "none"
        assert row["latest_extraction_version"] == 0
        assert client.get(
            f"/supervision/projects/{pid}/literature/results/{row['code']}/extraction",
            headers=_headers(temp_db, "alice"),
        ).json() is None
    with _session(temp_db) as s:
        assert s.execute(
            select(func.count(SupervisionLiteratureExtractionModel.id))
        ).scalar() == 0


# P3-11 ---------------------------------------------------------------------
def test_deep_extract_is_bounded_by_task_limit(client, app, temp_db):
    """深度提取受任务上限约束：超过上限 → 409，并说明不会自动全量提取。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid, deep_extract_limit=1)
    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), RecordingCapacity())

    first = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert first.status_code == 200, first.text
    assert first.json()["extract_state"] == "completed"

    second = client.post(
        f"/supervision/projects/{pid}/literature/results/P-002/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert "深度提取上限是 1 篇" in detail and "不会自动深度提取全部候选论文" in detail

    # 同一篇重跑不额外占用额度。
    again = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert again.status_code == 200, again.text
    assert again.json()["latest_extraction_version"] == 2

    # 上限 0 的任务：第一篇就不允许。
    brief = _confirmed_brief(client, temp_db, pid, question="如何降低天线标定误差")
    zero = _create_task(client, temp_db, pid, brief["code"], deep_extract_limit=0)
    _plan_and_confirm(client, app, temp_db, pid, zero["code"])
    # 去重是项目级的：换一个不相干候选才能产生新的 P 编号。
    use_provider(app, FakeProvider(by_source={"arxiv": [CAND_D], "crossref": []}))
    searched = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{zero['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert searched.status_code == 200, searched.text
    r = client.post(
        f"/supervision/projects/{pid}/literature/results/P-004/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 409, r.text
    assert "深度提取上限是 0 篇" in r.json()["detail"]


# P3-12 ---------------------------------------------------------------------
def test_deep_extract_structure_and_verified_quotes(client, app, temp_db):
    """深度提取给出方法/实验/结果/局限与项目差异；只有逐字核对到的引文算已核对证据。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), RecordingCapacity())

    r = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["extract_state"] == "completed"
    assert r.json()["latest_extraction_version"] == 1

    got = client.get(
        f"/supervision/projects/{pid}/literature/results/P-001/extraction",
        headers=_headers(temp_db, "alice"),
    )
    assert got.status_code == 200, got.text
    row = got.json()
    assert row["version"] == 1 and row["result_code"] == "P-001"
    # 首版只有摘要级材料：绝不声称有全文页码定位。
    assert row["level"] == "abstract"
    for field in ("summary", "research_problem", "method", "algorithm", "experiments",
                  "results_metrics", "limitations"):
        assert row[field], field
    diff = row["differences"]
    assert set(diff) == {"same", "different", "borrowable", "usable_methods",
                         "baseline_methods", "conflicting_assumptions", "gaps",
                         "suggested_tasks"}
    assert diff["borrowable"] == "镜像阶数截断做法"
    assert row["model_name"] == "fake-lit-model"
    assert row["prompt_version"] == lit.PROMPT_VERSION

    evidence = row["evidence"]
    assert len(evidence) == 3
    assert evidence[0]["quote_verified"] is True
    assert evidence[1]["quote_verified"] is True
    assert evidence[1]["locator"] == {"section": "abstract", "char_start": 100,
                                      "char_end": 141}
    # 模型自己改写的一句话不能冒充已核对证据。
    assert evidence[2]["quote_verified"] is False
    assert all(item["level"] == "abstract" for item in evidence)

    # 重跑不覆盖旧版本：旧版只标 superseded_at。
    again = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert again.status_code == 200, again.text
    versions = client.get(
        f"/supervision/projects/{pid}/literature/results/P-001/extractions",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["superseded_at"] is None and versions[1]["superseded_at"]


# P3-13 ---------------------------------------------------------------------
def test_one_paper_extraction_failure_does_not_roll_back_others(client, app, temp_db):
    """单篇深度提取失败不回滚其它论文，也不牵连任务（§15.3 第6条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid, deep_extract_limit=2)

    use_llm(app, FakeLLM(None, fail_at=1), RecordingCapacity())
    failed = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["extract_state"] == "failed"
    assert failed.json()["latest_extraction_version"] == 0

    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), RecordingCapacity())
    ok = client.post(
        f"/supervision/projects/{pid}/literature/results/P-002/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["extract_state"] == "completed"

    subtasks = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks"
        "?kind=deep_extract",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert len(subtasks) == 2
    by_result = {row["payload"]["result_code"]: row for row in subtasks}
    assert by_result["P-001"]["status"] == "failed"
    assert by_result["P-001"]["error_code"] == "model_call_failed"
    assert by_result["P-002"]["status"] == "completed"
    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert after["status"] == "triage" and after["deep_extract_used"] == 1
    assert after["subtask_failed_total"] == 1


# P3-14 ---------------------------------------------------------------------
def test_missing_model_capability_is_503_never_faked(client, app, temp_db):
    """模型未接入：检索方案/深度提取/摘要分析/比较一律 503，不假装跑过。"""
    pid = _active_project(client, temp_db)["project_id"]
    use_provider(app, default_provider())
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    # app 夹具默认 get_literature_llm → None
    for url, body in (
        (f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan", {}),
        (f"/supervision/projects/{pid}/literature/tasks/{task['code']}/abstract-analysis", {}),
        (f"/supervision/projects/{pid}/literature/tasks/{task['code']}/compare",
         {"result_refs": ["P-001", "P-002"]}),
        (f"/supervision/projects/{pid}/literature/results/P-001/deep-extract", None),
    ):
        r = (client.post(url, headers=_headers(temp_db, "alice"))
             if body is None else
             client.post(url, headers=_headers(temp_db, "alice"), json=body))
        assert r.status_code == 503, f"{url} -> {r.status_code} {r.text}"
        assert "模型能力尚未接入" in r.json()["detail"], url

    # 没有伪造：没有生成方案、没有任何子任务被写成完成、没有任何 AI 内容。
    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert after["plan_status"] == "" and after["search_plan"] == {}
    assert after["subtask_total"] == 0
    with _session(temp_db) as s:
        assert s.execute(
            select(func.count(SupervisionLiteratureSubtaskModel.id))
        ).scalar() == 0

    # 模型有了但容量闸门没接入：检索方案明确 503，绝不静默当成成功。
    use_llm(app, FakeLLM(PLAN_PAYLOAD), None)
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert r.status_code == 503, r.text
    assert "容量适配器尚未接入" in r.json()["detail"]

    # 深度提取走的是「记录失败并返回」路径：状态明确 failed + 原因可查。
    _plan_and_confirm(client, app, temp_db, pid, task["code"])
    use_provider(app, default_provider())
    searched = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert searched.status_code == 200, searched.text
    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), None)
    r = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["extract_state"] == "failed"
    sub = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks"
        "?kind=deep_extract", headers=_headers(temp_db, "alice"),
    ).json()
    assert sub[0]["status"] == "failed"
    assert "容量闸门" in sub[0]["error_message"]


# P3-15 ---------------------------------------------------------------------
def test_every_model_call_has_capacity_acquire_and_release(client, app, temp_db):
    """每次实际模型调用都有一条取用/释放记录，消费者是已登记类型（§8.2）。"""
    from chatchat.server.model_capacity.consumers import (
        CONSUMER_PAPER_COMPARE,
        CONSUMER_PAPER_EXTRACT,
        CONSUMER_RESEARCH_PLAN,
        is_registered_consumer,
    )

    assert all(is_registered_consumer(item) for item in (
        CONSUMER_RESEARCH_PLAN, CONSUMER_PAPER_EXTRACT, CONSUMER_PAPER_COMPARE,
    ))

    pid = _active_project(client, temp_db)["project_id"]
    gate = RecordingCapacity()
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    use_llm(app, FakeLLM(PLAN_PAYLOAD), gate)
    planned = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert planned.status_code == 200, planned.text
    assert gate.events == [
        ("acquire", "research_plan", task["code"], users_id["alice"]),
        ("release", 1, "completed"),
    ]

    client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan/confirm",
        headers=_headers(temp_db, "alice"), json={},
    )
    use_provider(app, default_provider())
    client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    # 搜索与规则排序不调模型，也就没有容量取用。
    assert len([e for e in gate.events if e[0] == "acquire"]) == 1

    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), gate)
    client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert [e for e in gate.events if e[0] == "acquire"][-1] == (
        "acquire", "paper_extract", f"{task['code']}/P-001", users_id["alice"],
    )
    assert gate.events[-1][0] == "release"

    use_llm(app, FakeLLM(ANALYSIS_PAYLOAD), gate)
    client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/abstract-analysis",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert [e for e in gate.events if e[0] == "acquire"][-1] == (
        "acquire", "paper_compare", task["code"], users_id["alice"],
    )
    use_llm(app, FakeLLM(COMPARE_PAYLOAD), gate)
    client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/compare",
        headers=_headers(temp_db, "alice"), json={"result_refs": ["P-001", "P-002"]},
    )
    assert [e for e in gate.events if e[0] == "acquire"][-1] == (
        "acquire", "paper_compare", task["code"], users_id["alice"],
    )
    acquires = [e for e in gate.events if e[0] == "acquire"]
    releases = [e for e in gate.events if e[0] == "release"]
    assert len(acquires) == len(releases) == 4

    # 子任务上留下可追踪的调用记录：消费者、业务对象、发起人、取放时间与结果。
    subtasks = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks?limit=50",
        headers=_headers(temp_db, "alice"),
    ).json()
    by_kind = {}
    for row in subtasks:
        if not row["model_calls"]:
            continue
        by_kind[row["kind"]] = row
        for record in row["model_calls"]:
            assert record["consumer"] == row["consumer"]
            assert record["user_id"] == users_id["alice"]
            assert record["business_ref"]
            assert record["acquired_at"] and record["released_at"]
            assert record["outcome"] == "ok"
            assert record["release_reason"] == "completed"
        assert row["prompt_version"] == lit.PROMPT_VERSION
    assert by_kind["plan"]["consumer"] == "research_plan"
    assert by_kind["deep_extract"]["consumer"] == "paper_extract"
    assert by_kind["abstract_analysis"]["consumer"] == "paper_compare"
    assert by_kind["compare"]["consumer"] == "paper_compare"


# P3-16 ---------------------------------------------------------------------
def test_abstract_analysis_is_bounded_and_does_not_override_rules(
    client, app, temp_db, monkeypatch
):
    """摘要级关联分析有上限，结果写进 semantic_analysis 且不覆盖规则排序字段。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    before = {row["code"]: row for row in _results(client, temp_db, pid)}
    assert all(row["semantic_analysis"] == {} for row in before.values())

    use_llm(app, FakeLLM(ANALYSIS_PAYLOAD), RecordingCapacity())
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/abstract-analysis",
        headers=_headers(temp_db, "alice"), json={"limit": 2},
    )
    assert r.status_code == 200, r.text
    subtask = r.json()
    assert subtask["kind"] == "abstract_analysis" and subtask["status"] == "completed"
    assert subtask["payload"]["result_codes"] == ["P-001", "P-002"]
    assert subtask["payload"]["requested_limit"] == 2
    assert subtask["new_total"] == 2

    after = {row["code"]: row for row in _results(client, temp_db, pid)}
    for code in ("P-001", "P-002"):
        semantic = after[code]["semantic_analysis"]
        assert semantic["label"] == "AI语义分析（仅摘要级材料）"
        assert semantic["model"] == "fake-lit-model"
        assert semantic["prompt_version"] == lit.PROMPT_VERSION
        assert semantic["evidence_quotes"]
        # 规则字段一个都不许被语义分析悄悄改掉（§6.3.B）。
        for field in ("relevance_level", "relevance_reason", "relevance_matched",
                      "relevance_missing", "relevance_score", "relevance_score_source",
                      "relevance_rules_version", "recommended_action"):
            assert after[code][field] == before[code][field], field
    assert after["P-003"]["semantic_analysis"] == {}

    # 全局上限来自环境变量：请求更多也只用允许的数量（不堆无上限后台任务）。
    monkeypatch.setenv("CHATCHAT_LITERATURE_MAX_ABSTRACT_ANALYSIS", "1")
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/abstract-analysis",
        headers=_headers(temp_db, "alice"), json={"limit": 5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["payload"]["result_codes"] == ["P-001"]
    assert r.json()["payload"]["requested_limit"] == 1

    # 没有候选论文时不能凭空分析（409，而不是伪造一份结果）。
    empty_pid = _active_project(client, temp_db, student="bob", mentor="other_mentor")[
        "project_id"
    ]
    empty_brief = _confirmed_brief(client, temp_db, empty_pid, who="bob")
    empty_task = _create_task(client, temp_db, empty_pid, empty_brief["code"], who="bob")
    r = client.post(
        f"/supervision/projects/{empty_pid}/literature/tasks/{empty_task['code']}"
        "/abstract-analysis",
        headers=_headers(temp_db, "bob"), json={},
    )
    assert r.status_code == 409, r.text
    assert "没有可分析的候选论文" in r.json()["detail"]


# P3-17 ---------------------------------------------------------------------
def test_compare_is_bounded_and_bound_to_a_subtask(client, app, temp_db):
    """多篇比较一次最多 6 篇；结果绑定子任务与论文编号。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    use_llm(app, FakeLLM(COMPARE_PAYLOAD), RecordingCapacity())
    url = f"/supervision/projects/{pid}/literature/tasks/{task['code']}/compare"

    # 一次 7 篇 → 400（不堆无上限后台任务）。
    r = client.post(url, headers=_headers(temp_db, "alice"), json={
        "result_refs": [f"P-00{i}" for i in range(1, 8)]})
    assert r.status_code == 400, r.text
    assert "最多比较 6 篇" in r.json()["detail"]

    # 少于两篇 → 422（pydantic 形状）。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_refs": ["P-001"]})
    assert r.status_code == 422, r.text

    # 编号不在本项目 → 404（不泄露它项目的论文）。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_refs": ["P-001", "P-900"]})
    assert r.status_code == 404, r.text
    assert "P-900" in r.json()["detail"]

    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_refs": ["P-001", "P-002"]})
    assert r.status_code == 200, r.text
    subtask = r.json()
    assert subtask["kind"] == "compare" and subtask["status"] == "completed"
    assert subtask["result_total"] == 2
    assert subtask["payload"]["result_codes"] == ["P-001", "P-002"]
    comparison = subtask["payload"]["comparison"]
    assert comparison["label"] == "AI多篇比较"
    assert comparison["model"] == "fake-lit-model"
    assert comparison["prompt_version"] == lit.PROMPT_VERSION
    assert comparison["dimensions"][0]["name"] == "场景"
    assert comparison["dimensions"][0]["values"]["P-001"] == "多径"
    assert comparison["agreements"] and comparison["conflicts"] and comparison["gaps"]
    # 比较结果挂在子任务上，页面能反查是哪几篇、哪一次调用。
    assert subtask["code"] == f"{task['code']}-CP01"
    assert subtask["model_calls"][0]["business_ref"] == task["code"]


# P3-18 ---------------------------------------------------------------------
def test_failed_subtask_can_be_retried_alone(client, app, temp_db):
    """失败子任务可单独重试：attempt 递增、旧失败留历史、其它子任务不动。"""
    pid = _active_project(client, temp_db)["project_id"]
    provider = FakeProvider(
        by_source={"arxiv": [CAND_A, CAND_B, CAND_C], "crossref": []},
        fail_sources={"crossref"},
    )
    task = _searched_task(client, app, temp_db, pid, provider=provider)
    failed_code = f"{task['code']}-SE02"
    before = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()
    arxiv_before = next(row for row in before if row["source"] == "arxiv")

    use_provider(app, FakeProvider(by_source={"arxiv": [], "crossref": [CAND_D]}))
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}"
        f"/subtasks/{failed_code}/retry",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 200, r.text
    retried = r.json()
    assert retried["code"] == failed_code
    assert retried["attempt"] == 2 and retried["status"] == "completed"
    assert (retried["result_total"], retried["new_total"], retried["duplicate_total"]) == (
        1, 1, 0,
    )
    history = retried["failure_history"]
    assert len(history) == 1
    assert history[0]["attempt"] == 1 and history[0]["error_code"] == "source_failed"
    assert "暂时不可用" in history[0]["error_message"]

    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()
    arxiv_after = next(row for row in after if row["source"] == "arxiv")
    assert arxiv_after == arxiv_before          # 其它子任务一点没动
    assert len(_results(client, temp_db, pid)) == 4

    # 已完成的子任务不能重试（避免重复入库与重复计数）。
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}"
        f"/subtasks/{task['code']}-SE01/retry",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 409, r.text
    assert "只有失败或已取消的子任务能重试" in r.json()["detail"]

    # 未结束的子任务可以取消；取消后也能重试。
    with _session(temp_db) as s:
        rank = s.execute(
            select(SupervisionLiteratureSubtaskModel).where(
                SupervisionLiteratureSubtaskModel.code == f"{task['code']}-RK01"
            )
        ).scalar_one()
        rank.status = "queued"
        rank.finished_at = None
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}"
        f"/subtasks/{task['code']}-RK01/cancel",
        headers=_headers(temp_db, "alice"), json={"note": "先停掉重排"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled" and r.json()["cancel_reason"] == "先停掉重排"
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}"
        f"/subtasks/{task['code']}-RK01/retry",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed" and r.json()["attempt"] == 2
    # 规则排序重跑不改结论字段，也不产生模型调用。
    assert r.json()["model_calls"] == []
    assert all(row["relevance_rules_version"] == lit.RULES_VERSION
               for row in _results(client, temp_db, pid))


# P3-19 ---------------------------------------------------------------------
def test_interrupted_subtasks_converge_after_restart(client, app, temp_db):
    """服务重启后遗留的 running/queued 子任务收敛为失败并可重试（§8.2）。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    codes = [row["code"] for row in client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()]
    with _session(temp_db) as s:
        rows = s.execute(
            select(SupervisionLiteratureSubtaskModel).where(
                SupervisionLiteratureSubtaskModel.code.in_(codes[:2])
            )
        ).scalars().all()
        rows[0].status = "running"
        rows[1].status = "queued"
        s.get(SupervisionLiteratureSearchTaskModel, task["task_id"]).status = "searching"

    with _session(temp_db) as s:
        assert lit.converge_interrupted_subtasks(s) == 2
        assert lit.converge_interrupted_subtasks(s) == 0   # 幂等：没有遗留就不动

    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()
    converged = [row for row in after if row["status"] == "failed"]
    assert len(converged) == 2
    assert all(row["error_code"] == "interrupted_by_restart" for row in converged)
    assert "服务重启导致中断" in converged[0]["error_message"]
    # 任务不能永远停在「检索中」：收敛后回到待筛选，学生可以重试。
    detail = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert detail["status"] == "triage"
    assert detail["subtask_failed_total"] == 2
    assert "收敛中断的检索子任务" in _audit_actions(temp_db, pid)

    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}"
        f"/subtasks/{converged[0]['code']}/retry",
        headers=_headers(temp_db, "alice"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["attempt"] == 2 and r.json()["status"] == "completed"


# P3-20 ---------------------------------------------------------------------
def test_evidence_cards_require_verifiable_quote_and_are_idempotent(
    client, app, temp_db
):
    """证据卡片必须带可核对原文；同一证据重复提交不产生重复卡片（§15.3 第8条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), RecordingCapacity())
    extracted = client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    )
    assert extracted.status_code == 200, extracted.text
    extraction_id = client.get(
        f"/supervision/projects/{pid}/literature/results/P-001/extraction",
        headers=_headers(temp_db, "alice"),
    ).json()["extraction_id"]

    url = f"/supervision/projects/{pid}/literature/evidence-cards"
    quote = "We study indoor localization with ray tracing and a mirror model"

    # 没有原文就不能成卡（模型改写的内容不能当已核对证据）。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-001"})
    assert r.status_code == 422, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-001", "quote": "   "})
    assert r.status_code == 400, r.text
    assert "可核对原文" in r.json()["detail"]
    # 非法枚举 → 400。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-001", "quote": quote,
                          "evidence_type": "vibes"})
    assert r.status_code == 400, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-001", "quote": quote,
                          "support_relation": "basically_agrees"})
    assert r.status_code == 400, r.text
    # 别人的论文 → 404；挂错深度提取 → 404。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-900", "quote": quote})
    assert r.status_code == 404, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"result_ref": "P-001", "quote": quote,
                          "extraction_id": "not-a-real-extraction"})
    assert r.status_code == 404, r.text
    # 导师不是证据的添加方。
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"result_ref": "P-001", "quote": quote})
    assert r.status_code == 403, r.text

    first = client.post(url, headers=_headers(temp_db, "alice"), json={
        "result_ref": "P-001", "quote": quote, "evidence_type": "method",
        "summary": "用射线追踪与镜像模型", "locator": {"section": "abstract"},
        "support_relation": "supports", "brief_field": "methods",
        "sub_question": "多径阶数如何量化", "extraction_id": extraction_id,
    })
    assert first.status_code == 200, first.text
    card = first.json()
    assert card["code"] == "EC-001" and card["result_code"] == "P-001"
    assert card["quote_verified"] is True
    assert card["level"] == "abstract"          # 只有摘要级材料，不冒充全文定位
    assert card["locator"] == {"section": "abstract"}
    assert card["extraction_version"] == 1 and card["extraction_id"] == extraction_id
    assert card["support_relation"] == "supports" and card["sub_question"]

    # 重复点击同一证据不堆重复卡片。
    again = client.post(url, headers=_headers(temp_db, "alice"), json={
        "result_ref": "P-001", "quote": quote, "evidence_type": "method",
        "summary": "用射线追踪与镜像模型", "locator": {"section": "abstract"},
        "support_relation": "supports", "brief_field": "methods",
        "sub_question": "多径阶数如何量化", "extraction_id": extraction_id,
    })
    assert again.status_code == 200, again.text
    assert again.json()["card_id"] == card["card_id"] and again.json()["code"] == "EC-001"
    assert len(client.get(url, headers=_headers(temp_db, "alice")).json()) == 1

    # 模型改写、原文里找不到的一句话：可以存下来，但明确标未核对。
    invented = client.post(url, headers=_headers(temp_db, "alice"), json={
        "result_ref": "P-001", "quote": "模型自己改写的一句话，原文里并没有这样写",
        "evidence_type": "result",
    })
    assert invented.status_code == 200, invented.text
    assert invented.json()["code"] == "EC-002"
    assert invented.json()["quote_verified"] is False
    assert invented.json()["extraction_version"] == 0

    rows = client.get(
        url + "?result_ref=P-002", headers=_headers(temp_db, "alice")
    ).json()
    assert rows == []
    assert len(client.get(url + "?result_ref=P-001",
                          headers=_headers(temp_db, "alice")).json()) == 2
    # 证据卡片也出现在论文详情里。
    detail = client.get(
        f"/supervision/projects/{pid}/literature/results/P-001",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert [c["code"] for c in detail["evidence_cards"]] == ["EC-001", "EC-002"]


# P3-21 ---------------------------------------------------------------------
def test_conclusion_separates_facts_judgements_and_hypotheses(client, app, temp_db):
    """文献结论区分论文事实/学生判断/待验证假设；事实必须指向已纳入论文。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    url = f"/supervision/projects/{pid}/literature/conclusions"

    # 还没有纳入任何论文 → 409，并说明要先做决定。
    r = client.post(url, headers=_headers(temp_db, "alice"), json=_conclusion_body())
    assert r.status_code == 409, r.text
    assert "没有已纳入的论文" in r.json()["detail"]

    _included(client, temp_db, pid, codes=("P-001", "P-002"))

    # 论文事实引用未纳入的论文 → 400（无依据的断言）。
    bad = _conclusion_body(supported_facts=[{"statement": "某篇没纳入的论文",
                                             "result_code": "P-003"}])
    r = client.post(url, headers=_headers(temp_db, "alice"), json=bad)
    assert r.status_code == 400, r.text
    assert "P-003" in r.json()["detail"] and "已纳入" in r.json()["detail"]
    # 学生判断依据未纳入论文 → 400。
    bad = _conclusion_body(student_judgements=[{"statement": "越界判断",
                                                "basis_result_codes": ["P-003"]}])
    r = client.post(url, headers=_headers(temp_db, "alice"), json=bad)
    assert r.status_code == 400, r.text
    # 只罗列论文事实不算结论 → 400。
    bad = _conclusion_body(student_judgements=[])
    r = client.post(url, headers=_headers(temp_db, "alice"), json=bad)
    assert r.status_code == 400, r.text
    assert "综合判断" in r.json()["detail"]
    # 证据卡片编号不存在 → 404。
    bad = _conclusion_body(evidence_card_codes=["EC-900"])
    r = client.post(url, headers=_headers(temp_db, "alice"), json=bad)
    assert r.status_code == 404, r.text
    # 导师不是结论的形成方。
    r = client.post(url, headers=_headers(temp_db, "mentor"), json=_conclusion_body())
    assert r.status_code == 403, r.text
    # 缺表述 → 422。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json=_conclusion_body(statement=""))
    assert r.status_code == 422, r.text

    created = client.post(url, headers=_headers(temp_db, "alice"),
                          json=_conclusion_body(task_ref=task["code"]))
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["code"] == "LC-001" and row["version"] == 1
    assert row["status"] == "confirmed" and row["confirmed_by"] == users_id["alice"]
    assert row["search_task_code"] == task["code"]
    # 三类内容各自独立保存，不互相混写。
    assert row["supported_facts"][0]["result_code"] == "P-001"
    assert row["student_judgements"][0]["basis_result_codes"] == ["P-001", "P-002"]
    assert row["unverified_hypotheses"][0]["how_to_verify"]
    assert row["research_gaps"] == ["两篇都没有考虑天线标定误差"]
    assert row["literature_count"] == 2 and len(row["literature_snapshot"]) == 2
    assert len(row["literature_snapshot_hash"]) == 64
    assert row["mentor_state"] == ""
    # 任务进入「已形成文献结论」。
    after = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert after["status"] == "concluded" and after["conclusion_total"] == 1
    # 已结论的任务不能再检索（要换依据请新建任务）。
    r = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert r.status_code == 409, r.text
    assert "已形成文献结论" in r.json()["detail"]


# P3-22 ---------------------------------------------------------------------
def test_conclusion_keeps_its_literature_snapshot_as_history(client, app, temp_db):
    """结论绑定文献快照；纳入集合变化后只标「依据已变化」，不改写旧结论。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    _included(client, temp_db, pid, codes=("P-001", "P-002"))
    created = client.post(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"), json=_conclusion_body(task_ref=task["code"]),
    )
    assert created.status_code == 200, created.text
    first = created.json()
    assert first["literature_changed"] is False
    assert first["current_included_count"] == 2
    old_hash = first["literature_snapshot_hash"]

    # 学生把 P-002 改成排除：旧结论内容一字不改，只现算出「依据已变化」。
    changed = client.post(
        f"/supervision/projects/{pid}/literature/results/P-002/decision",
        headers=_headers(temp_db, "alice"),
        json={"decision": "excluded", "reason": "只做了基线选择，没做多径扫描"},
    )
    assert changed.status_code == 200, changed.text
    after = client.get(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert after["literature_changed"] is True
    assert after["current_included_count"] == 1
    assert after["literature_snapshot_hash"] == old_hash
    assert after["literature_count"] == 2 and len(after["literature_snapshot"]) == 2
    assert {item["result_code"] for item in after["literature_snapshot"]} == {
        "P-001", "P-002",
    }
    assert after["statement"] == first["statement"]
    assert after["supported_facts"] == first["supported_facts"]
    assert after["student_judgements"] == first["student_judgements"]

    # 重新形成结论是同一 code 的新版本，绑的是当时的新快照。
    revised = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/revise",
        headers=_headers(temp_db, "alice"),
        json=_conclusion_body(
            statement="只有 P-001 支持多径误差结论，基线选择论文只能作背景",
            student_judgements=[{"statement": "基线选择论文只能作背景材料",
                                 "basis_result_codes": ["P-001"]}],
        ),
    )
    assert revised.status_code == 200, revised.text
    second = revised.json()
    assert second["version"] == 2 and second["literature_count"] == 1
    assert second["literature_snapshot_hash"] != old_hash
    assert second["literature_changed"] is False


# P3-23 ---------------------------------------------------------------------
def test_conclusion_revision_versions_old_one_as_superseded(client, app, temp_db):
    """修订文献结论生成新版本，旧版本标 superseded 保留（§4.0 版本规则）。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    _included(client, temp_db, pid)
    created = client.post(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"), json=_conclusion_body(task_ref=task["code"]),
    )
    assert created.status_code == 200, created.text
    # 导师先对 v1 表态，修订后这份表态必须还在。
    reviewed = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/mentor-review",
        headers=_headers(temp_db, "mentor"),
        json={"state": "questioned", "note": "两篇都只有摘要级证据"},
    )
    assert reviewed.status_code == 200, reviewed.text

    revised = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/revise",
        headers=_headers(temp_db, "alice"),
        json=_conclusion_body(statement="多径阶数结论只在 P-001 证据范围内成立",
                              confirm=True),
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["version"] == 2

    current = client.get(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert [row["version"] for row in current] == [2]
    assert current[0]["status"] == "confirmed"

    history = client.get(
        f"/supervision/projects/{pid}/literature/conclusions?include_superseded=true",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert [(row["code"], row["version"]) for row in history] == [
        ("LC-001", 2), ("LC-001", 1),
    ]
    old = history[1]
    assert old["status"] == "superseded"
    assert old["superseded_by_code"] == "LC-001" and old["superseded_by_version"] == 2
    assert old["superseded_at"]
    # 历史版本的内容与导师意见都不被改写。
    assert old["statement"] == "多径阶数是亚米级定位的主要误差来源，镜像模型可作为本项目基线"
    assert old["mentor_state"] == "questioned"
    assert old["mentor_note"] == "两篇都只有摘要级证据"
    assert old["literature_count"] == 2

    # 按 code 取到的是最新版本；历史版本用 id 取。
    by_code = client.get(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert by_code["version"] == 2
    by_id = client.get(
        f"/supervision/projects/{pid}/literature/conclusions/{old['conclusion_id']}",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert by_id["version"] == 1 and by_id["status"] == "superseded"

    # 已被替代的版本不能再作为修订基础。
    r = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/{old['conclusion_id']}/revise",
        headers=_headers(temp_db, "alice"), json=_conclusion_body(),
    )
    assert r.status_code == 409, r.text
    assert "已被新版本替代" in r.json()["detail"]

    # 纳入集合被清空：旧结论留作历史，新版本不允许凭空产生。
    for code in ("P-001", "P-002"):
        r = client.post(
            f"/supervision/projects/{pid}/literature/results/{code}/decision",
            headers=_headers(temp_db, "alice"),
            json={"decision": "excluded", "reason": "证据只有摘要级"},
        )
        assert r.status_code == 200, r.text
    r = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/revise",
        headers=_headers(temp_db, "alice"), json=_conclusion_body(),
    )
    assert r.status_code == 409, r.text
    assert "纳入集合已清空" in r.json()["detail"]
    still = client.get(
        f"/supervision/projects/{pid}/literature/conclusions?include_superseded=true",
        headers=_headers(temp_db, "alice"),
    ).json()
    assert len(still) == 2


# P3-24 ---------------------------------------------------------------------
def test_mentor_review_of_conclusion_is_attitude_not_decision(client, app, temp_db):
    """导师可确认/质疑/要求补充证据；学生不能复核，草稿与历史版本不能被复核。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    _included(client, temp_db, pid)
    draft = client.post(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"),
        json=_conclusion_body(task_ref=task["code"], confirm=False),
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["status"] == "draft"
    url = f"/supervision/projects/{pid}/literature/conclusions/LC-001/mentor-review"

    # 学生不是复核方。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"state": "confirmed", "note": "自己确认自己"})
    assert r.status_code == 403, r.text
    assert "主导师" in r.json()["detail"]

    # 草稿状态还不是项目正式依据。
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"state": "confirmed", "note": "先看着"})
    assert r.status_code == 409, r.text
    assert "只有学生已确认的文献结论" in r.json()["detail"]

    confirmed = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/revise",
        headers=_headers(temp_db, "alice"),
        json=_conclusion_body(statement="多径阶数是主要误差来源", confirm=True),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    # 取值非法 → 400；质疑与要求补充证据必须写理由 → 400。
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"state": "approved", "note": "取值不在枚举里"})
    assert r.status_code == 400, r.text
    # 缺 note 的字段形状是合法的（默认空串），但业务上必须写理由 → 400。
    r = client.post(url, headers=_headers(temp_db, "mentor"), json={"state": "questioned"})
    assert r.status_code == 400, r.text
    assert "必须写明理由" in r.json()["detail"]
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"state": "questioned", "note": "   "})
    assert r.status_code == 400, r.text
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"state": "evidence_required", "note": ""})
    assert r.status_code == 400, r.text

    ok = client.post(url, headers=_headers(temp_db, "mentor"),
                     json={"state": "evidence_required",
                           "note": "请补全文级证据与消融对照"})
    assert ok.status_code == 200, ok.text
    row = ok.json()
    assert row["mentor_state"] == "evidence_required"
    assert row["mentor_note"] == "请补全文级证据与消融对照"
    assert row["mentor_by"] == users_id["mentor"] and row["mentor_role"] == "mentor"
    assert row["mentor_at"]
    # 导师的态度不改写结论本身，也不碰里程碑1 的逐条验收结论。
    assert row["statement"] == "多径阶数是主要误差来源"
    assert row["supported_facts"] == confirmed.json()["supported_facts"]

    # 被新版本替代的历史结论不能再复核。
    older = client.get(
        f"/supervision/projects/{pid}/literature/conclusions?include_superseded=true",
        headers=_headers(temp_db, "alice"),
    ).json()[1]
    r = client.post(
        f"/supervision/projects/{pid}/literature/conclusions/{older['conclusion_id']}"
        "/mentor-review",
        headers=_headers(temp_db, "mentor"), json={"state": "confirmed", "note": ""},
    )
    assert r.status_code == 409, r.text
    assert "已被新版本替代" in r.json()["detail"]
    assert "导师要求补充证据文献结论" in _audit_actions(temp_db, pid)


# P3-25 ---------------------------------------------------------------------
def _private_paper(db, owner_id, title="私人论文：多径信道下的镜像模型标定"):
    """在既有私人论文研究库里造一条属于 ``owner_id`` 的论文成果（只用于断言）。"""
    with _session(db) as s:
        record = PaperRecordModel(
            id=lit.new_id(), title=title, authors="陈同学", year=2025,
            abstract="私人摘要：镜像模型标定流程与误差预算。",
            source="manual", fulltext_status="abstract",
            title_key=f"私人论文 多径信道下的镜像模型标定:{2025}",
        )
        s.add(record)
        s.flush()
        link = ProjectPaperModel(
            id=lit.new_id(), project_id="private-research-project-0001",
            paper_id=record.id, owner_id=owner_id, source="manual",
            screening_status="included", screening_reason="私人项目里已纳入",
        )
        s.add(link)
        s.flush()
        return {"link_id": link.id, "record_id": record.id, "title": title}


def _private_card(db, owner_id, paper_id, quote="私人证据：标定误差预算表第三行"):
    with _session(db) as s:
        card = EvidenceCardModel(
            id=lit.new_id(), project_id="private-research-project-0001",
            paper_id=paper_id, owner_id=owner_id, quote=quote,
            summary="私人笔记里的证据", evidence_type="result", level="abstract",
            locator_json='{"section":"附录"}', quote_verified=1, review_status="confirmed",
        )
        s.add(card)
        s.flush()
        return {"card_id": card.id, "quote": quote}


def test_private_material_enters_only_as_frozen_snapshot(client, app, temp_db):
    """私人论文成果只能按具体成果授权引用快照，快照固定当时字段与内容哈希。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    _included(client, temp_db, pid)
    url = f"/supervision/projects/{pid}/literature/citations"

    mine = _private_paper(temp_db, users_id["alice"])
    theirs = _private_paper(temp_db, users_id["bob"], title="别人的私人论文：不要看")

    # 字段非法 → 400；缺编号 → 422。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"source_kind": "whole_private_project",
                          "source_ref": mine["link_id"], "purpose": "research_plan"})
    assert r.status_code == 400, r.text
    assert "来源种类" in r.json()["detail"]
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"source_kind": "private_paper", "source_ref": mine["link_id"],
                          "purpose": "everything"})
    assert r.status_code == 400, r.text
    assert "用途" in r.json()["detail"]
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"source_kind": "private_paper", "source_ref": "",
                          "purpose": "research_plan"})
    assert r.status_code == 422, r.text

    # 别人的私人成果：按不存在回答，不因加入协作项目就放开私人数据（§9.1）。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"source_kind": "private_paper", "source_ref": theirs["link_id"],
                          "purpose": "research_plan"})
    assert r.status_code == 404, r.text
    # 导师不能替学生授权私人材料。
    r = client.post(url, headers=_headers(temp_db, "mentor"),
                    json={"source_kind": "private_paper", "source_ref": mine["link_id"],
                          "purpose": "mentor_review"})
    assert r.status_code == 403, r.text

    granted = client.post(url, headers=_headers(temp_db, "alice"), json={
        "source_kind": "private_paper", "source_ref": mine["link_id"],
        "purpose": "research_plan", "bound_object_type": "research_plan",
        "bound_object_code": "RP-001", "note": "只授权这一篇的元数据",
    })
    assert granted.status_code == 200, granted.text
    citation = granted.json()
    assert citation["code"] == "CS-001"
    assert citation["source_owner_id"] == users_id["alice"]
    assert citation["source_code"] == mine["record_id"]
    assert citation["bound_object_code"] == "RP-001"
    assert len(citation["content_hash"]) == 64
    assert citation["snapshot"]["title"] == mine["title"]
    assert citation["snapshot"]["kind"] == "private_paper"
    assert citation["revoked_at"] is None
    # 快照里没有私人研究项目、私人搜索或笔记的入口。
    assert "project_id" not in citation["snapshot"]
    assert "screening_reason" in citation["snapshot"]

    # 来源后来变化不回改这份历史快照。
    with _session(temp_db) as s:
        s.get(PaperRecordModel, mine["record_id"]).title = "改了标题：快照不该跟着变"
    after = client.get(
        f"{url}/CS-001", headers=_headers(temp_db, "alice")
    ).json()
    assert after["snapshot"]["title"] == mine["title"]
    assert after["content_hash"] == citation["content_hash"]

    # 私人证据卡片同样按具体成果授权。
    card = _private_card(temp_db, users_id["alice"], mine["record_id"])
    r = client.post(url, headers=_headers(temp_db, "alice"), json={
        "source_kind": "private_evidence_card", "source_ref": card["card_id"],
        "purpose": "mentor_review",
    })
    assert r.status_code == 200, r.text
    assert r.json()["code"] == "CS-002"
    assert r.json()["snapshot"]["quote"] == card["quote"]
    assert r.json()["snapshot"]["quote_verified"] is True
    assert r.json()["source_version"].startswith("v")

    # 本项目论文与已确认结论也能作为引用依据（快照同样固定当时字段）。
    r = client.post(url, headers=_headers(temp_db, "alice"), json={
        "source_kind": "project_result", "source_ref": "P-001", "purpose": "progress"})
    assert r.status_code == 200, r.text
    assert r.json()["snapshot"]["decision"] == "included"
    assert r.json()["source_owner_id"] == users_id["alice"]

    conclusion = client.post(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"),
        json=_conclusion_body(confirm=False),
    ).json()
    r = client.post(url, headers=_headers(temp_db, "alice"), json={
        "source_kind": "conclusion", "source_ref": conclusion["code"],
        "purpose": "mentor_review"})
    assert r.status_code == 409, r.text
    assert "只有已确认的文献结论" in r.json()["detail"]

    # 导师读得到被授权的那份快照，读不到别的私人对象。
    mentor_rows = client.get(url, headers=_headers(temp_db, "mentor")).json()
    assert [row["code"] for row in mentor_rows] == ["CS-001", "CS-002", "CS-003"]
    assert all(row["snapshot"] for row in mentor_rows)
    assert theirs["title"] not in json.dumps(mentor_rows, ensure_ascii=False)


# P3-26 ---------------------------------------------------------------------
def test_revoking_citation_only_closes_future_use(client, app, temp_db):
    """撤销引用只关闭后续授权；历史快照与审计继续保留（§9.1、§13）。"""
    pid = _active_project(client, temp_db)["project_id"]
    _searched_task(client, app, temp_db, pid)
    mine = _private_paper(temp_db, users_id["alice"])
    url = f"/supervision/projects/{pid}/literature/citations"
    granted = client.post(url, headers=_headers(temp_db, "alice"), json={
        "source_kind": "private_paper", "source_ref": mine["link_id"],
        "purpose": "mentor_review", "bound_object_type": "submission",
        "bound_object_code": "SB-001",
    })
    assert granted.status_code == 200, granted.text
    citation = granted.json()

    # 撤销必须写原因。
    r = client.post(f"{url}/CS-001/revoke", headers=_headers(temp_db, "alice"), json={})
    assert r.status_code == 422, r.text
    r = client.post(f"{url}/CS-001/revoke", headers=_headers(temp_db, "alice"),
                    json={"reason": "   "})
    assert r.status_code == 400, r.text
    # 只有当初授权的人能撤销。
    r = client.post(f"{url}/CS-001/revoke", headers=_headers(temp_db, "mentor"),
                    json={"reason": "导师代撤销"})
    assert r.status_code == 403, r.text

    revoked = client.post(f"{url}/CS-001/revoke", headers=_headers(temp_db, "alice"),
                          json={"reason": "该论文全文尚未核实，先撤回授权"})
    assert revoked.status_code == 200, revoked.text
    row = revoked.json()
    assert row["revoked_at"] and row["revoked_by"] == users_id["alice"]
    assert row["revoke_reason"] == "该论文全文尚未核实，先撤回授权"
    # 历史快照本身不消失：内容哈希与快照字段仍在，已形成的审核记录仍可核对。
    assert row["snapshot"]["title"] == mine["title"]
    assert row["content_hash"] == citation["content_hash"]

    all_rows = client.get(url, headers=_headers(temp_db, "alice")).json()
    assert [row["code"] for row in all_rows] == ["CS-001"]
    active = client.get(url + "?include_revoked=false",
                        headers=_headers(temp_db, "alice")).json()
    assert active == []
    # 再撤销一次 → 409。
    r = client.post(f"{url}/CS-001/revoke", headers=_headers(temp_db, "alice"),
                    json={"reason": "再撤一次"})
    assert r.status_code == 409, r.text
    actions = _audit_actions(temp_db, pid)
    assert "授权引用快照" in actions and "撤销引用快照授权" in actions


# P3-27 ---------------------------------------------------------------------
def test_unauthenticated_expired_disabled_and_outsider(client, app, temp_db, users):
    """未登录、令牌过期、被禁用用户与非成员：401/403/404，不返回任何内容。"""
    pid = _active_project(client, temp_db)["project_id"]
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    # 能力都接好：撞到的必须是鉴权/授权，而不是 503。
    use_llm(app, FakeLLM(PLAN_PAYLOAD), RecordingCapacity())
    use_provider(app, default_provider())

    urls = [
        f"/supervision/projects/{pid}/literature/tasks",
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}",
        f"/supervision/projects/{pid}/literature/results",
        f"/supervision/projects/{pid}/literature/conclusions",
        f"/supervision/projects/{pid}/literature/citations",
    ]
    # 未登录。
    for url in urls:
        assert client.get(url).status_code == 401, url
    r = client.post(f"/supervision/projects/{pid}/literature/tasks",
                    json={"brief_ref": brief["code"], "goal": "未登录建任务"})
    assert r.status_code == 401, r.text

    # 令牌过期。
    expired = auth_jwt.create_token(
        users_id["alice"], users["alice"]["auth_version"],
        now=datetime.utcnow() - timedelta(days=3),
    )
    for url in urls:
        r = client.get(url, headers={"Authorization": f"Bearer {expired}"})
        assert r.status_code == 401, url

    # 被禁用的用户。
    with _session(temp_db) as s:
        s.get(UserModel, users_id["alice"]).status = "disabled"
    for url in urls:
        assert client.get(url, headers=_headers(temp_db, "alice")).status_code == 401, url
    with _session(temp_db) as s:
        s.get(UserModel, users_id["alice"]).status = "active"

    # 还没改过密码的用户：403，且不带任何业务内容。
    newbie = _mk_user(temp_db, "newbie")
    with _session(temp_db) as s:
        user = s.get(UserModel, newbie["id"])
        user.must_change_password = True
    token = auth_jwt.create_token(newbie["id"], newbie["auth_version"])
    for url in urls:
        r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403, url
        assert r.json()["detail"] == "请先修改密码"

    # 非成员：即使能力都接好了，也只能拿到 404。
    for url, body in (
        (f"/supervision/projects/{pid}/literature/tasks",
         {"brief_ref": brief["code"], "goal": "非成员建任务"}),
        (f"/supervision/projects/{pid}/literature/tasks/{task['code']}/plan", {}),
        (f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search", {}),
        (f"/supervision/projects/{pid}/literature/results/P-001/decision",
         {"decision": "included", "reason": "非成员越权"}),
        (f"/supervision/projects/{pid}/literature/results/P-001/deep-extract", None),
    ):
        r = (client.post(url, headers=_headers(temp_db, "bob"))
             if body is None else
             client.post(url, headers=_headers(temp_db, "bob"), json=body))
        assert r.status_code == 404, f"{url} -> {r.status_code} {r.text}"


# P3-28 ---------------------------------------------------------------------
WORK_ACS = [
    {
        "statement": "在指定 Grid2N 数据集与固定随机种子下，中位定位误差小于 1 m",
        "evidence_requirement": "实验配置、结果文件与图表",
        "required": True,
        "check_type": "experiment",
    },
]


def test_paper_failures_never_block_milestone_one_work(client, app, temp_db):
    """论文侧全失败也不阻塞人工任务与进展记录（§16.4 第7条）。"""
    pid = _active_project(client, temp_db)["project_id"]
    brief = _confirmed_brief(client, temp_db, pid)
    task = _create_task(client, temp_db, pid, brief["code"])
    # 检索方案正常生成，但两个来源都失败：整个检索一步没成，任务不卡死。
    _plan_and_confirm(client, app, temp_db, pid, task["code"])
    use_provider(app, FakeProvider(fail_sources={"arxiv", "crossref"}))
    searched = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/search",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert searched.status_code == 200, searched.text
    assert searched.json()["subtask_failed_total"] == 2
    assert len(searched.json()["source_failures"]) == 2
    assert _results(client, temp_db, pid) == []

    # 里程碑1 的人工链路完全不受影响：下发任务、进展记录、待审核队列。
    created = client.post(
        f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor"),
        json={"title": "基线算法复现", "purpose": "确认现有方法可达精度",
              "description": "先复现基线", "acs": WORK_ACS, "publish": True},
    )
    assert created.status_code == 200, created.text
    work_task = created.json()
    assert work_task["status"] == "active" and work_task["label"] != "任务草稿"

    progress = client.post(
        f"/supervision/projects/{pid}/progress", headers=_headers(temp_db, "alice"),
        json={"task_ref": work_task["code"], "done": "跑通基线训练脚本",
              "results": "RMSE 0.83", "next_steps": "补消融"},
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["code"] == "PR-001"

    queue = client.get(
        f"/supervision/projects/{pid}/review-queue", headers=_headers(temp_db, "mentor")
    )
    assert queue.status_code == 200, queue.text
    tasks = client.get(
        f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "alice")
    )
    assert tasks.status_code == 200, tasks.text
    assert len(tasks.json()) == 1

    # 论文侧的失败也都还留着自己的记录，不会被静默抹掉。
    subtasks = client.get(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/subtasks",
        headers=_headers(temp_db, "alice"),
    ).json()
    failed = [row for row in subtasks if row["status"] == "failed"]
    assert len(failed) == 2 and all(row["error_code"] == "source_failed" for row in failed)
    assert [row for row in subtasks if row["kind"] == "plan"][0]["status"] == "completed"

    # 模型调用失败同样只留自己的失败记录，不波及别人。
    use_llm(app, FakeLLM(None, fail_at=1), RecordingCapacity())
    second = client.post(
        f"/supervision/projects/{pid}/literature/tasks", headers=_headers(temp_db, "alice"),
        json={"brief_ref": brief["code"], "goal": "换一批关键词再试", "sources": ["arxiv"]},
    )
    assert second.status_code == 200, second.text
    planned = client.post(
        f"/supervision/projects/{pid}/literature/tasks/{second.json()['code']}/plan",
        headers=_headers(temp_db, "alice"), json={},
    )
    assert planned.status_code == 200, planned.text
    assert planned.json()["plan_status"] == "failed"
    assert "模型调用失败" in planned.json()["plan_error"]
    again = client.get(
        f"/supervision/projects/{pid}/tasks", headers=_headers(temp_db, "mentor")
    )
    assert again.status_code == 200, again.text


# P3-29 ---------------------------------------------------------------------
def test_formal_actions_are_audited(client, app, temp_db):
    """所有正式动作进审计日志（§13）。"""
    pid = _active_project(client, temp_db)["project_id"]
    task = _searched_task(client, app, temp_db, pid)
    _included(client, temp_db, pid)
    use_llm(app, FakeLLM(EXTRACTION_PAYLOAD), RecordingCapacity())
    assert client.post(
        f"/supervision/projects/{pid}/literature/results/P-001/deep-extract",
        headers=_headers(temp_db, "alice"),
    ).status_code == 200
    assert client.post(
        f"/supervision/projects/{pid}/literature/evidence-cards",
        headers=_headers(temp_db, "alice"),
        json={"result_ref": "P-001",
              "quote": "localization error drops below one meter",
              "evidence_type": "result", "locator": {"section": "abstract"}},
    ).status_code == 200
    assert client.post(
        f"/supervision/projects/{pid}/literature/conclusions",
        headers=_headers(temp_db, "alice"), json=_conclusion_body(task_ref=task["code"]),
    ).status_code == 200
    assert client.post(
        f"/supervision/projects/{pid}/literature/conclusions/LC-001/mentor-review",
        headers=_headers(temp_db, "mentor"), json={"state": "confirmed", "note": "同意"},
    ).status_code == 200
    mine = _private_paper(temp_db, users_id["alice"])
    assert client.post(
        f"/supervision/projects/{pid}/literature/citations",
        headers=_headers(temp_db, "alice"),
        json={"source_kind": "private_paper", "source_ref": mine["link_id"],
              "purpose": "mentor_review"},
    ).status_code == 200
    assert client.post(
        "/supervision/projects/" + pid + "/literature/citations/CS-001/revoke",
        headers=_headers(temp_db, "alice"), json={"reason": "暂不引用"},
    ).status_code == 200
    assert client.post(
        f"/supervision/projects/{pid}/literature/tasks/{task['code']}/close",
        headers=_headers(temp_db, "alice"), json={"reason": "本轮检索结束"},
    ).status_code == 200

    actions = _audit_actions(temp_db, pid)
    for expected in (
        "创建论文检索任务", "生成检索方案", "确认检索方案", "执行论文检索",
        "决定纳入论文", "深度提取论文内容", "添加论文证据卡片",
        "形成文献研究结论", "导师确认文献结论", "授权引用快照",
        "撤销引用快照授权", "关闭论文检索任务",
    ):
        assert expected in actions, expected


# P3-30 ---------------------------------------------------------------------
def test_field_validation_shapes_and_state_rules(client, app, temp_db):
    """字段非法 400、pydantic 形状 422、状态不允许 409（与既有模块一致）。"""
    pid = _active_project(client, temp_db)["project_id"]
    brief = _confirmed_brief(client, temp_db, pid)
    url = f"/supervision/projects/{pid}/literature/tasks"

    # pydantic 形状：缺 goal、上限越界、年份越界、深度上限过大 → 422。
    for body in (
        {"brief_ref": brief["code"]},
        {"brief_ref": brief["code"], "goal": "x", "result_limit": 0},
        {"brief_ref": brief["code"], "goal": "x", "result_limit": 201},
        {"brief_ref": brief["code"], "goal": "x", "year_from": 1800},
        {"brief_ref": brief["code"], "goal": "x", "deep_extract_limit": 21},
        {"brief_ref": "", "goal": "x"},
    ):
        r = client.post(url, headers=_headers(temp_db, "alice"), json=body)
        assert r.status_code == 422, f"{body} -> {r.status_code} {r.text}"

    # 仓储层字段校验 → 400。
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"brief_ref": brief["code"], "goal": "   "})
    assert r.status_code == 400, r.text
    assert "检索目标" in r.json()["detail"]
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"brief_ref": brief["code"], "goal": "x", "purpose": "maybe"})
    assert r.status_code == 400, r.text
    r = client.post(url, headers=_headers(temp_db, "alice"),
                    json={"brief_ref": brief["code"], "goal": "x",
                          "year_from": 2024, "year_to": 2019})
    assert r.status_code == 400, r.text

    # 草稿任务可以改输入；开始检索后不能再改。
    draft = client.post(url, headers=_headers(temp_db, "alice"),
                        json={"brief_ref": brief["code"], "goal": "先记一笔",
                              "as_draft": True, "sources": ["arxiv"]})
    assert draft.status_code == 200, draft.text
    assert draft.json()["status"] == "draft"
    patched = client.patch(
        f"{url}/{draft.json()['code']}", headers=_headers(temp_db, "alice"),
        json={"goal": "改成要验证的假设", "keywords": ["indoor localization"],
              "deep_extract_limit": 1},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["goal"] == "改成要验证的假设"
    assert patched.json()["keywords"] == ["indoor localization"]
    r = client.patch(f"{url}/{draft.json()['code']}", headers=_headers(temp_db, "alice"),
                     json={"result_limit": 0})
    assert r.status_code == 422, r.text
    r = client.patch(f"{url}/{draft.json()['code']}", headers=_headers(temp_db, "alice"),
                     json={"goal": "   "})
    assert r.status_code == 400, r.text

    # 关闭必须写原因；关闭之后不能再改、不能再检索。
    r = client.post(f"{url}/{draft.json()['code']}/close",
                    headers=_headers(temp_db, "alice"), json={})
    assert r.status_code == 422, r.text
    r = client.post(f"{url}/{draft.json()['code']}/close",
                    headers=_headers(temp_db, "alice"), json={"reason": "  "})
    assert r.status_code == 400, r.text
    closed = client.post(f"{url}/{draft.json()['code']}/close",
                         headers=_headers(temp_db, "alice"),
                         json={"reason": "这一轮不做了"})
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    r = client.post(f"{url}/{draft.json()['code']}/close",
                    headers=_headers(temp_db, "alice"), json={"reason": "再关一次"})
    assert r.status_code == 409, r.text
    for path in (f"{url}/{draft.json()['code']}/plan",
                 f"{url}/{draft.json()['code']}/search"):
        use_llm(app, FakeLLM(PLAN_PAYLOAD), RecordingCapacity())
        use_provider(app, default_provider())
        r = client.post(path, headers=_headers(temp_db, "alice"), json={})
        assert r.status_code == 409, f"{path} -> {r.status_code} {r.text}"
        assert "已关闭" in r.json()["detail"]
    r = client.patch(f"{url}/{draft.json()['code']}", headers=_headers(temp_db, "alice"),
                     json={"goal": "改不动了"})
    assert r.status_code == 409, r.text

    # 已检索的任务不能再改输入。
    searched = _searched_task(client, app, temp_db, pid)
    r = client.patch(f"{url}/{searched['code']}", headers=_headers(temp_db, "alice"),
                     json={"keywords": ["换个关键词"]})
    assert r.status_code == 409, r.text
    assert "不能修改检索输入" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 来源接线：默认 provider 复用论文研究模块的 async 来源适配层
# ---------------------------------------------------------------------------


class FakeAsyncSource:
    """``chatchat/server/research/sources.py`` 的适配器形状：async search()。"""

    def __init__(self, name: str, rows=(), *, fail=False):
        self.name = name
        self.rows = list(rows)
        self.fail = fail
        self.calls: list[dict] = []

    async def search(self, query, limit=10):
        self.calls.append({"query": query, "limit": limit})
        if self.fail:
            raise RuntimeError(f"{self.name} 暂时不可用")
        return self.rows


# P3-31 ---------------------------------------------------------------------
def test_p3_31_provider_adapter_bridges_async_sources_and_names_availability():
    """来源适配器是 async 的，检索契约是同步的：桥接必须真的取回结果。

    未知来源只让那一个来源失败并说明当前可用来源，绝不退化成「搜到了 0 篇」
    （§15.3 第6条、§16.4 第2条）。
    """
    arxiv = FakeAsyncSource("arxiv", [CAND_A, CAND_B])
    provider = ResearchSourcesLiteratureProvider({"arxiv": arxiv})

    ok, note = provider.available()
    assert ok is True and "arxiv" in note

    rows = provider.search(query="indoor localization", source="arxiv", limit=5)
    assert [row["title"] for row in rows] == [CAND_A["title"], CAND_B["title"]]
    assert arxiv.calls == [{"query": "indoor localization", "limit": 5}]

    with pytest.raises(lit.LiteratureSourceError) as exc:
        provider.search(query="q", source="crossref", limit=5)
    assert "未接入来源「crossref」" in str(exc.value)
    assert "arxiv" in str(exc.value)

    broken = ResearchSourcesLiteratureProvider({"arxiv": FakeAsyncSource("arxiv", fail=True)})
    with pytest.raises(lit.LiteratureSourceError) as broken_exc:
        broken.search(query="q", source="arxiv", limit=5)
    assert "arxiv 暂时不可用" in str(broken_exc.value)

    assert ResearchSourcesLiteratureProvider({}).available()[0] is False


# P3-32 ---------------------------------------------------------------------
def test_p3_32_default_provider_is_wired_to_real_sources_unless_forced_off(monkeypatch):
    """默认注入真实来源；``CHATCHAT_LITERATURE_ENABLE_SOURCES=0`` 退回「未接入」。

    退回后检索一律 503，界面显示「外部论文来源尚未接入」而不是空结果。
    """
    monkeypatch.setenv("CHATCHAT_LITERATURE_ENABLE_SOURCES", "0")
    provider = get_literature_provider()
    assert isinstance(provider, lit.UnconfiguredLiteratureProvider)
    assert provider.available()[0] is False

    monkeypatch.delenv("CHATCHAT_LITERATURE_ENABLE_SOURCES", raising=False)
    wired = get_literature_provider()
    ok, note = wired.available()
    assert ok is True
    assert "arxiv" in note


# P3-33 ---------------------------------------------------------------------
def test_p3_33_search_through_the_real_wiring_stores_candidates_and_lists_failures(
    client, app, temp_db
):
    """走真实来源接线（async 适配器 + 同步仓储）：候选入库，没接的来源如实标失败。"""
    pid = _active_project(client, temp_db)["project_id"]
    provider = ResearchSourcesLiteratureProvider(
        {"arxiv": FakeAsyncSource("arxiv", [CAND_A, CAND_B])}
    )
    searched = _searched_task(client, app, temp_db, pid, provider=provider)

    assert searched["stored_total"] == 2, searched
    assert searched["subtask_failed_total"] > 0  # crossref 未接入
    failures = {str(item.get("source")) for item in searched["source_failures"]}
    assert "crossref" in failures

    rows = _results(client, temp_db, pid)
    assert {row["source"] for row in rows} == {"arxiv"}
    assert all(row["arxiv_id"] for row in rows)
