"""LaTeX 稿件静态分析接口离线测试（《学生研究与导师审核功能重新设计方案》里程碑5）。

覆盖 §16.6 与本次路由层的放行口径：

1. 未登录 401；非成员与不存在统一 404；
2. 危险压缩包（路径穿越、嵌套归档）被拒 → 409 + 全部命中项，并留下拒绝审计；
3. 稿件里的危险命令（``\\write18``、绝对路径 ``\\input``）原样呈现，
   响应里 ``compiled`` 恒 False；
4. 超预算的文件以 ``partially_read`` / ``not_read`` 透出，不静默截断；
5. 首次上传只出基线（无条目、不推断"完成了什么"）；
6. 草稿不覆盖历史：重新生成是新行，旧草稿仍在；
7. 只有创建草稿的学生本人能确认成正式进展；导师确认 → 403；重复确认 → 409；
8. 模型缺席不影响草稿生成（``model_summary_status="unavailable"``），
   注入模型时按已登记的 ``progress_draft`` 消费者取放容量；
9. 解压用的临时目录解析完立刻删除。

夹具全部用 ``zipfile`` 在 ``tmp_path`` 里现场生成，不读真实数据、不联网、
不调用任何真实模型，也不碰真实 DATA_PATH。
"""
import asyncio
import io
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.api_server.supervision_latex_routes import (
    get_latex_summary_capacity,
    get_latex_summary_llm,
    supervision_latex_router,
)
from chatchat.server.api_server.supervision_routes import supervision_router
from chatchat.server.api_server.supervision_work_routes import supervision_work_router
from chatchat.server.auth import jwt as auth_jwt
from chatchat.server.auth import service
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import apply_migrations, current_version
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.supervision_latex_model import (  # noqa: F401
    SupervisionLatexDraftConfirmationModel,
    SupervisionLatexProgressDraftModel,
    SupervisionLatexSnapshotModel,
)
from chatchat.server.db.models.supervision_model import (  # noqa: F401
    SupervisionAuditEventModel,
    SupervisionNotificationModel,
    SupervisionProjectMemberModel,
    SupervisionProjectModel,
)
from chatchat.server.db.models.supervision_work_model import (  # noqa: F401
    SupervisionAttachmentModel,
    SupervisionProgressUpdateModel,
)
from chatchat.server.db.models.user_model import UserModel  # noqa: F401
from chatchat.server.db.session import get_db as db_get_db
from chatchat.server.model_capacity import CONSUMER_PROGRESS_DRAFT, get_gate


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(
        auth_jwt, "get_secret", lambda: "supervision-latex-unit-test-secret-0000"
    )
    return "supervision-latex-unit-test-secret-0000"


@pytest.fixture
def temp_db(secret, tmp_path):
    path = tmp_path / "supervision_latex.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        # head 版本号由集成方推进（里程碑5 是 v22），这里只确认已经跑到
        # 师生协作那批迁移，不钉死具体版本。
        assert current_version(conn) >= 20
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


@pytest.fixture(autouse=True)
def extract_dirs(monkeypatch):
    """把解压用的临时目录重定向到 tmp_path 下，并记录每一个以便断言已清理。"""
    root = Path(tempfile.mkdtemp(prefix="latex-extract-root-"))
    created: list = []
    real_mkdtemp = tempfile.mkdtemp

    def fake_mkdtemp(*args, **kwargs):
        kwargs["dir"] = str(root)
        made = real_mkdtemp(*args, **kwargs)
        created.append(made)
        return made

    monkeypatch.setattr(tempfile, "mkdtemp", fake_mkdtemp)
    yield created


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
        "alice": _mk_user(temp_db, "alice"),          # 学生
        "mentor": _mk_user(temp_db, "mentor"),        # 导师
        "bob": _mk_user(temp_db, "bob"),              # 非成员
    }


users_id: Dict[str, str] = {}


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
    a.include_router(supervision_latex_router)

    async def override_get_db():
        s = temp_db["Session"]()
        try:
            yield s
        finally:
            s.close()

    a.dependency_overrides[db_get_db] = override_get_db
    # 默认按"模型能力未接入"接线：单元测试绝不构造真实模型、绝不联网。
    # 需要假模型的用例再覆盖成自己的实现。
    a.dependency_overrides[get_latex_summary_llm] = lambda: None
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

    return Client()


@contextmanager
def _session(db):
    s = db["Session"]()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 稿件夹具
# ---------------------------------------------------------------------------

V1_ENTRIES: Dict[str, bytes] = {
    "main.tex": (
        "\\documentclass{article}\n\\usepackage{graphicx}\n"
        "\\title{SA2N Positioning}\n\\author{Zhang San}\n"
        "\\begin{document}\n\\maketitle\n"
        "\\section{Introduction}\n本文研究毫米波定位方法。\n"
        "\\input{methods}\n\\end{document}\n"
    ).encode("utf-8"),
    "methods.tex": "\\section{Methods}\n基线方法描述。\n".encode("utf-8"),
}


def v2_entries() -> Dict[str, bytes]:
    """第二版：新增实验章节、图、引用与 .bib，字数明显增加。"""
    entries = dict(V1_ENTRIES)
    main = entries["main.tex"].decode("utf-8")
    main = main.replace("\\input{methods}", "\\input{methods}\n\\input{experiments}\n")
    main = main.replace("\\end{document}", "\\bibliography{refs}\n\\end{document}")
    entries["main.tex"] = main.encode("utf-8")
    entries["experiments.tex"] = (
        "\\section{Experiments}\n"
        "在 Grid2N 数据集上重复十次实验，中位定位误差为 0.83 米，"
        "并与基线方法在相同随机种子下逐项对比。\n"
        "\\begin{figure}\n\\includegraphics{figs/results.png}\n"
        "\\caption{误差分布}\n\\label{fig:results}\n\\end{figure}\n"
        "\\cite{smith2024}\n"
    ).encode("utf-8")
    entries["figs/results.png"] = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    entries["refs.bib"] = (
        "@article{smith2024,\n title={mmWave positioning},\n year={2024}\n}\n"
    ).encode("utf-8")
    return entries


def write_zip(tmp_path: Path, name: str, entries: Dict[str, bytes]) -> Path:
    """在 tmp_path 里现场生成 zip；不提交任何二进制夹具。"""
    path = tmp_path / "archives" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, data in entries.items():
            archive.writestr(entry, data)
    return path


def upload_attachment(client, db, pid, path: Path, *, name: str = "") -> dict:
    """走正式附件接口上传稿件归档（附件本身有 sha256 与版本）。"""
    response = client.post(
        f"/supervision/projects/{pid}/attachments",
        headers=_headers(db, "alice"),
        files={"file": (name or path.name, path.read_bytes(), "application/zip")},
        data={"kind": "manuscript"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def active_project(client, db) -> str:
    created = client.post(
        "/supervision/projects", headers=_headers(db, "alice"),
        json={"name": "反射面定位研究", "description": "里程碑5 验证"},
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
    return pid


def analyze(client, db, pid, attachment_code, **kwargs):
    return client.post(
        f"/supervision/projects/{pid}/latex/analyze",
        headers=_headers(db, kwargs.pop("as_user", "alice")),
        json={"attachment_ref": attachment_code, **kwargs},
    )


def make_draft(client, db, pid, attachment_code, base_code="", **kwargs):
    return client.post(
        f"/supervision/projects/{pid}/latex/progress-draft",
        headers=_headers(db, kwargs.pop("as_user", "alice")),
        json={
            "attachment_ref": attachment_code,
            "base_attachment_ref": base_code,
            **kwargs,
        },
    )


def audit_actions(db, object_code: str) -> list:
    with _session(db) as s:
        return [
            (event.action, event.detail)
            for event in s.query(SupervisionAuditEventModel)
            .filter(SupervisionAuditEventModel.object_code == object_code)
            .order_by(SupervisionAuditEventModel.created_at)
            .all()
        ]


# ---------------------------------------------------------------------------
# 1. 认证与成员边界
# ---------------------------------------------------------------------------


class TestAccessBoundaries:
    def test_unauthenticated_requests_are_rejected(self, client, temp_db):
        pid = active_project(client, temp_db)
        r = client.get(f"/supervision/projects/{pid}/latex/progress-drafts")
        assert r.status_code == 401
        r = client.post(
            f"/supervision/projects/{pid}/latex/analyze",
            json={"attachment_ref": "AT-001"},
        )
        assert r.status_code == 401

    def test_non_member_gets_the_same_404(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        archive = write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        attachment = upload_attachment(client, temp_db, pid, archive)
        r = analyze(client, temp_db, pid, attachment["code"], as_user="bob")
        assert r.status_code == 404
        r = client.get(
            f"/supervision/projects/{pid}/latex/progress-drafts",
            headers=_headers(temp_db, "bob"),
        )
        assert r.status_code == 404
        r = client.get(
            f"/supervision/projects/{pid}/latex/snapshots/LX-001",
            headers=_headers(temp_db, "bob"),
        )
        assert r.status_code == 404

    def test_unknown_refs_are_404_not_500(self, client, temp_db):
        pid = active_project(client, temp_db)
        r = analyze(client, temp_db, pid, "AT-999")
        assert r.status_code == 404
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/PD-999/confirm",
            headers=_headers(temp_db, "alice"), json={},
        )
        assert r.status_code == 404

    def test_non_zip_attachment_is_rejected_with_400(self, client, temp_db):
        pid = active_project(client, temp_db)
        uploaded = client.post(
            f"/supervision/projects/{pid}/attachments",
            headers=_headers(temp_db, "alice"),
            files={"file": ("run.log", b"seed=42 rmse=0.83\n", "text/plain")},
            data={"kind": "log"},
        )
        assert uploaded.status_code == 200, uploaded.text
        r = analyze(client, temp_db, pid, uploaded.json()["code"])
        assert r.status_code == 400
        assert ".zip" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 分析快照
# ---------------------------------------------------------------------------


class TestAnalyzeSnapshot:
    def test_snapshot_reports_structure_versions_and_never_compiles(
        self, client, temp_db, tmp_path
    ):
        pid = active_project(client, temp_db)
        archive = write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        attachment = upload_attachment(client, temp_db, pid, archive)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["code"] == "LX-001"
        assert body["attachment_code"] == attachment["code"]
        assert body["attachment_sha256"] == attachment["sha256"]
        assert body["main_status"] == "resolved"
        assert body["main_path"] == "main.tex"
        assert body["document_present"] is True
        # 系统从不编译 LaTeX：响应里必须钉死这一点
        assert body["compiled"] is False
        assert body["check"]["compiled"] is False
        assert body["parser_version"] == "latex-static/1.0"
        assert body["checker_version"] == "latex-check/1.0"
        assert body["zip_safety_version"] == "zip-safety/1.0"
        assert body["counts"]["sections"] == 2
        assert len(body["outline"]) == 2
        assert body["file_count"] == 2
        assert {item["entry"] for item in body["extracted_files"]} == {
            "main.tex", "methods.tex",
        }
        assert all("sha256" in item for item in body["extracted_files"])
        # 快照里绝不出现解压目录或附件磁盘路径
        assert "available_files" in body and "main.tex" in body["available_files"]
        with _session(temp_db) as s:
            stored = s.query(SupervisionAttachmentModel).filter_by(
                code=attachment["code"]
            ).one().stored_path
        text = str(body)
        assert "latex-extract-root" not in text
        assert stored not in text, "响应里不得泄露附件磁盘路径或解压目录"

    def test_dangerous_commands_are_surfaced_not_swallowed(
        self, client, temp_db, tmp_path
    ):
        pid = active_project(client, temp_db)
        entries = {
            "main.tex": (
                "\\documentclass{article}\n\\begin{document}\n"
                "\\section{Danger}\n"
                "\\immediate\\write18{rm -rf /tmp/whatever}\n"
                "\\input{/etc/passwd}\n"
                "\\end{document}\n"
            ).encode("utf-8"),
        }
        archive = write_zip(tmp_path, "danger.zip", entries)
        attachment = upload_attachment(client, temp_db, pid, archive)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        codes = {item["target"] for item in body["dangerous_findings"]}
        assert {"write18", "input_absolute"} <= codes
        assert body["check"]["ok"] is False
        assert body["finding_counts"].get("error", 0) >= 2
        # 危险命令必须原样可定位：归档内相对路径 + 行号
        first = body["dangerous_findings"][0]
        assert first["path"] == "main.tex" and first["line"] >= 4
        actions = [action for action, _ in audit_actions(temp_db, body["code"])]
        assert "稿件危险命令提示" in actions

    def test_oversized_inputs_are_disclosed(self, client, temp_db, tmp_path, monkeypatch):
        """单文件超解析预算 / 文件数超上限：必须列出来，不能静默截断。"""
        monkeypatch.setenv("CHATCHAT_LATEX_MAX_ENTRIES", "320")
        monkeypatch.setenv("CHATCHAT_LATEX_MAX_COMPRESSION_RATIO", "1000")
        pid = active_project(client, temp_db)
        entries = {
            "main.tex": (
                "\\documentclass{article}\n\\begin{document}\n"
                "\\input{big}\n\\end{document}\n"
            ).encode("utf-8"),
            "big.tex": ("\\section{Big}\n" + "这是一段很长的正文内容用于触发截断。\n" * 90000)
            .encode("utf-8"),
        }
        for index in range(305):
            entries[f"parts/p{index}.tex"] = f"\\section{{P{index}}}\n正文 {index}\n".encode(
                "utf-8"
            )
        archive = write_zip(tmp_path, "big.zip", entries)
        attachment = upload_attachment(client, temp_db, pid, archive)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["limits"]["max_entries"] == 320
        assert body["parse_budget"]["max_source_files"] == 300
        assert [item["path"] for item in body["partially_read"]] == ["big.tex"]
        assert body["partially_read"][0]["truncated_at_line"] > 1
        assert "超过单文件解析预算" in body["partially_read"][0]["reason"]
        assert body["not_read"], "超过文件数上限的文件必须列出来"
        assert "文件数超过解析上限 300" in body["not_read"][0]["reason"]

    def test_reanalysis_appends_new_snapshot(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        archive = write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        attachment = upload_attachment(client, temp_db, pid, archive)
        first = analyze(client, temp_db, pid, attachment["code"])
        second = analyze(client, temp_db, pid, attachment["code"])
        assert first.json()["code"] == "LX-001"
        assert second.json()["code"] == "LX-002"
        listed = client.get(
            f"/supervision/projects/{pid}/latex/snapshots",
            headers=_headers(temp_db, "alice"),
        )
        assert listed.status_code == 200
        assert [item["code"] for item in listed.json()] == ["LX-002", "LX-001"]
        got = client.get(
            f"/supervision/projects/{pid}/latex/snapshots/LX-001",
            headers=_headers(temp_db, "alice"),
        )
        assert got.status_code == 200 and got.json()["code"] == "LX-001"

    def test_temp_extract_directories_are_removed(self, client, temp_db, tmp_path, extract_dirs):
        pid = active_project(client, temp_db)
        archive = write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        attachment = upload_attachment(client, temp_db, pid, archive)
        assert analyze(client, temp_db, pid, attachment["code"]).status_code == 200
        assert extract_dirs, "应该确实解压过"
        assert all(not Path(directory).exists() for directory in extract_dirs)


# ---------------------------------------------------------------------------
# 3. 危险归档：拒绝 + 审计
# ---------------------------------------------------------------------------


class TestUnsafeArchives:
    def test_path_traversal_is_rejected_and_audited(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        path = tmp_path / "archives" / "traversal.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("main.tex", V1_ENTRIES["main.tex"])
            archive.writestr("../evil.tex", b"\\section{evil}")
        attachment = upload_attachment(client, temp_db, pid, path)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "安全检查" in detail["message"]
        codes = {problem["code"] for problem in detail["problems"]}
        assert "path_traversal" in codes
        # 命中项原样呈现：条目名与中文说明都在
        problem = next(p for p in detail["problems"] if p["code"] == "path_traversal")
        assert problem["entry"] == "../evil.tex" and problem["message"]
        # 没有落任何快照
        with _session(temp_db) as s:
            assert s.query(SupervisionLatexSnapshotModel).count() == 0
        # 但审计必须留下拒绝痕迹
        actions = [action for action, _ in audit_actions(temp_db, attachment["code"])]
        assert "拒绝稿件归档" in actions

    def test_nested_archive_is_rejected(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        path = tmp_path / "archives" / "nested.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("main.tex", V1_ENTRIES["main.tex"])
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as nested:
                nested.writestr("hidden.tex", b"\\section{hidden}")
            archive.writestr("inner.zip", inner.getvalue())
        attachment = upload_attachment(client, temp_db, pid, path)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 409
        assert "nested_archive" in {p["code"] for p in r.json()["detail"]["problems"]}

    def test_compression_bomb_is_rejected(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        path = tmp_path / "archives" / "bomb.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("main.tex", V1_ENTRIES["main.tex"])
            archive.writestr("bomb.tex", "a" * (3 * 1024 * 1024))
        attachment = upload_attachment(client, temp_db, pid, path)
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 409
        assert "compression_ratio" in {p["code"] for p in r.json()["detail"]["problems"]}

    def test_draft_on_unsafe_archive_is_rejected_too(
        self, client, temp_db, tmp_path, extract_dirs
    ):
        pid = active_project(client, temp_db)
        good = upload_attachment(client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES))
        path = tmp_path / "archives" / "bad.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("main.tex", V1_ENTRIES["main.tex"])
            archive.writestr("inner/deep/../evil.tex", b"x")
        bad = upload_attachment(client, temp_db, pid, path)
        r = make_draft(client, temp_db, pid, bad["code"], good["code"])
        assert r.status_code == 409
        assert "path_traversal" in {p["code"] for p in r.json()["detail"]["problems"]}
        # 被拒的那一份不落快照，也不生成草稿；好的一份也没被解析（草稿生成失败）
        with _session(temp_db) as s:
            assert s.query(SupervisionLatexProgressDraftModel).count() == 0
            assert s.query(SupervisionLatexSnapshotModel).count() == 0
        assert all(not Path(directory).exists() for directory in extract_dirs)


# ---------------------------------------------------------------------------
# 4. 主文件判定
# ---------------------------------------------------------------------------


class TestMainFileResolution:
    def test_ambiguous_main_file_asks_the_user(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        entries = {
            "a.tex": (
                "\\documentclass{article}\n\\begin{document}\n\\section{A}\n甲\n\\end{document}\n"
            ).encode("utf-8"),
            "b.tex": (
                "\\documentclass{article}\n\\begin{document}\n\\section{B}\n乙\n\\end{document}\n"
            ).encode("utf-8"),
        }
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "ambiguous.zip", entries)
        )
        r = analyze(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["main_status"] == "ambiguous"
        assert body["document_present"] is False
        assert {c["path"] for c in body["candidates"]} == {"a.tex", "b.tex"}
        # 草稿不能替用户猜主文件
        r = make_draft(client, temp_db, pid, attachment["code"])
        assert r.status_code == 409
        assert {c["path"] for c in r.json()["detail"]["candidates"]} == {"a.tex", "b.tex"}
        # 用户指定后就能解析
        r = analyze(client, temp_db, pid, attachment["code"], main_file="b.tex")
        assert r.status_code == 200 and r.json()["main_status"] == "user_specified"
        assert r.json()["counts"]["sections"] == 1

    def test_wrong_main_file_is_400(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        r = analyze(client, temp_db, pid, attachment["code"], main_file="nope.tex")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# 5. 进度草稿
# ---------------------------------------------------------------------------


class TestProgressDrafts:
    def test_first_upload_only_builds_a_baseline(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        r = make_draft(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["code"] == "PD-001"
        assert body["baseline_only"] is True
        assert body["status"] == "baseline"
        assert body["items"] == []
        assert body["item_count"] == 0
        assert "第一个版本" in body["summary"]
        assert body["word_count_delta"] == 0
        assert body["base_attachment_code"] == ""
        assert body["base_attachment_version"] == 0
        # 草稿不等于进展：未发布、必须学生确认
        assert body["published"] is False
        assert body["requires_student_confirmation"] is True
        assert "未编译" in body["caveat"]

    def test_diff_items_and_word_count_change(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft"
        assert body["baseline_only"] is False
        assert body["base_attachment_code"] == base["code"]
        # 界面上要能写出"对比 ATT-001 v1"，所以版本号必须随响应给出
        assert body["base_attachment_version"] == base["version"]
        assert body["attachment_sha256"] == current["sha256"]
        assert body["base_attachment_sha256"] == base["sha256"]
        assert body["word_count_delta"] > 0
        assert body["counts"]["added_sections"] == 1
        statements = " ".join(item["statement"] for item in body["items"])
        assert "新增章节《Experiments》" in statements
        assert "新增图片文件 figs/results.png" in statements
        assert "新增参考文献条目 smith2024" in statements
        assert "正文总字数" in statements
        # 每条草稿条目都要能定位到归档内相对路径 + 行号
        located = [
            item for item in body["items"]
            if item["item_type"] != "word_count" and item["locations"]
        ]
        assert located
        for item in located:
            assert item["locations"][0]["path"].endswith((".tex", ".bib", ".png"))
            assert item["locations"][0]["line"] >= 1
        assert body["snapshot_code"] == "LX-002"
        assert body["base_snapshot_code"] == "LX-001"

    def test_draft_history_is_never_overwritten(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        first = make_draft(client, temp_db, pid, current["code"], base["code"]).json()
        second = make_draft(client, temp_db, pid, current["code"], base["code"]).json()
        assert first["code"] == "PD-001" and second["code"] == "PD-002"
        listed = client.get(
            f"/supervision/projects/{pid}/latex/progress-drafts",
            headers=_headers(temp_db, "alice"),
        )
        assert [item["code"] for item in listed.json()] == ["PD-002", "PD-001"]
        got = client.get(
            f"/supervision/projects/{pid}/latex/progress-drafts/PD-001",
            headers=_headers(temp_db, "alice"),
        )
        assert got.status_code == 200
        assert got.json()["status"] == "draft"
        assert got.json()["items"] == first["items"]
        with _session(temp_db) as s:
            assert s.query(SupervisionLatexProgressDraftModel).count() == 2
        # 按状态过滤
        listed = client.get(
            f"/supervision/projects/{pid}/latex/progress-drafts?status=draft",
            headers=_headers(temp_db, "alice"),
        )
        assert len(listed.json()) == 2

    def test_truncated_draft_says_so(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        many = {
            "main.tex": (
                "\\documentclass{article}\n\\begin{document}\n"
                + "".join(f"\\section{{S{index}}}\n正文 {index}\n" for index in range(260))
                + "\\end{document}\n"
            ).encode("utf-8")
        }
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "many.zip", many)
        )
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["item_count"] == 200
        assert body["truncated"] is True
        assert body["dropped_items"] > 0
        assert f"已省略 {body['dropped_items']} 条" in body["summary"]

    def test_same_attachment_as_base_is_400(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        r = make_draft(client, temp_db, pid, attachment["code"], attachment["code"])
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# 6. 学生确认 → 正式进展
# ---------------------------------------------------------------------------


class TestConfirmAndDiscard:
    def _draft(self, client, db, pid, tmp_path) -> dict:
        base = upload_attachment(
            client, db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        made = make_draft(client, db, pid, current["code"], base["code"])
        assert made.status_code == 200, made.text
        return made.json()

    def test_student_confirm_creates_a_progress_record(
        self, client, temp_db, tmp_path
    ):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"),
            json={"done": "本周完成实验章节初稿并补了误差图",
                  "results": "中位定位误差 0.83 米",
                  "next_steps": "补消融实验"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["draft"]["status"] == "confirmed"
        assert body["draft"]["confirmed_by"] == users_id["alice"]
        assert body["draft"]["confirmed_role"] == "student"
        assert body["draft"]["confirmed_at"]
        assert body["draft"]["confirmed_progress_code"] == "PR-001"
        progress = body["progress"]
        assert progress["code"] == "PR-001"
        assert "本周完成实验章节初稿" in progress["done"]
        assert "【LaTeX 稿件静态差异条目】" in progress["done"]
        assert "新增章节《Experiments》" in progress["done"]
        # 进展必须自带口径：静态差异不等于实验完成
        assert "未编译" in progress["done"]
        assert progress["results"] == "中位定位误差 0.83 米"
        assert progress["next_steps"] == "补消融实验"
        assert progress["links"][0]["draft_code"] == draft["code"]
        with _session(temp_db) as s:
            row = s.query(SupervisionProgressUpdateModel).one()
            assert row.author_id == users_id["alice"]
            confirmation = s.query(SupervisionLatexDraftConfirmationModel).one()
            assert confirmation.progress_code == "PR-001"
            assert confirmation.draft_code == draft["code"]
            assert confirmation.actor_id == users_id["alice"]
        actions = [a for a, _ in audit_actions(temp_db, draft["code"])]
        assert "确认 LaTeX 进度草稿" in actions

    def test_confirm_without_any_text_still_uses_draft_summary(
        self, client, temp_db, tmp_path
    ):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={},
        )
        assert r.status_code == 200, r.text
        assert "与上一版本相比" in r.json()["progress"]["done"]

    def test_mentor_cannot_confirm_a_draft(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "mentor"), json={"done": "导师替学生确认"},
        )
        assert r.status_code == 403
        assert "学生" in r.json()["detail"]
        with _session(temp_db) as s:
            assert s.query(SupervisionProgressUpdateModel).count() == 0
            assert s.query(SupervisionLatexDraftConfirmationModel).count() == 0

    def test_double_confirm_is_a_conflict(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        url = f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm"
        first = client.post(url, headers=_headers(temp_db, "alice"), json={"done": "第一版说明"})
        assert first.status_code == 200, first.text
        second = client.post(url, headers=_headers(temp_db, "alice"), json={"done": "再确认一次"})
        assert second.status_code == 409
        assert "已确认" in second.json()["detail"]
        with _session(temp_db) as s:
            assert s.query(SupervisionProgressUpdateModel).count() == 1
            assert s.query(SupervisionLatexDraftConfirmationModel).count() == 1

    def test_baseline_draft_can_be_confirmed_too(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        draft = make_draft(client, temp_db, pid, attachment["code"]).json()
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"done": "建立稿件基线"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["draft"]["status"] == "confirmed"
        assert "建立稿件基线" in r.json()["progress"]["done"]

    def test_discard_only_marks_status(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/discard",
            headers=_headers(temp_db, "alice"), json={"reason": "这一版比较的是错的附件"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "discarded"
        assert body["discarded_by"] == users_id["alice"]
        assert body["discard_reason"] == "这一版比较的是错的附件"
        assert body["items"], "丢弃不能删数据"
        with _session(temp_db) as s:
            assert s.query(SupervisionLatexProgressDraftModel).count() == 1
            assert s.query(SupervisionLatexSnapshotModel).count() == 2
        # 已丢弃的草稿不能再确认
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"done": "还是想确认"},
        )
        assert r.status_code == 409
        # 再丢弃一次也只是冲突，不报错
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/discard",
            headers=_headers(temp_db, "alice"), json={"reason": "重复"},
        )
        assert r.status_code == 409

    def test_confirmed_draft_cannot_be_discarded(self, client, temp_db, tmp_path):
        pid = active_project(client, temp_db)
        draft = self._draft(client, temp_db, pid, tmp_path)
        confirm = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/confirm",
            headers=_headers(temp_db, "alice"), json={"done": "确认"},
        )
        assert confirm.status_code == 200, confirm.text
        r = client.post(
            f"/supervision/projects/{pid}/latex/progress-drafts/{draft['code']}/discard",
            headers=_headers(temp_db, "alice"), json={"reason": "反悔"},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# 7. 模型摘要（可选，绝不阻断草稿）
# ---------------------------------------------------------------------------


class FakeModel:
    model_name = "fake-progress-model"

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return type("Result", (), {"content": "已新增实验章节与误差图，稿件内容较上一版扩充。"})()


class FakeCapacity:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, *, consumer="", business_ref="", user_id=""):
        self.acquired.append((consumer, business_ref, user_id))
        return f"lease-{len(self.acquired)}"

    def release(self, token, reason="completed"):
        self.released.append((token, reason))


class BoomModel:
    def invoke(self, messages):
        raise RuntimeError("模型超时")


class TestModelSummaryIsOptional:
    def test_missing_model_still_produces_the_draft(self, client, temp_db, tmp_path):
        """默认没有模型：草稿照常生成，摘要字段留空并说明未接入。"""
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model_summary"] == ""
        assert body["model_summary_status"] == "unavailable"
        assert "模型能力未接入" in body["model_summary_note"]
        assert body["items"], "模型缺席不影响草稿条目"

    def test_injected_model_uses_registered_consumer_and_capacity(
        self, client, app, temp_db, tmp_path
    ):
        model = FakeModel()
        capacity = FakeCapacity()
        app.dependency_overrides[get_latex_summary_llm] = lambda: model
        app.dependency_overrides[get_latex_summary_capacity] = lambda: capacity
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model_summary_status"] == "generated"
        assert "误差图" in body["model_summary"]
        assert body["model_name"] == "fake-progress-model"
        assert len(model.calls) == 1
        # 消费者必须是已登记的 progress_draft，业务对象是草稿编号
        assert capacity.acquired == [
            (CONSUMER_PROGRESS_DRAFT, body["code"], users_id["alice"])
        ]
        assert capacity.released and capacity.released[0][0] == "lease-1"

    def test_real_capacity_adapter_leaves_no_lease_held(
        self, client, app, temp_db, tmp_path
    ):
        """用真的 CapacityLeaseAdapter：调用结束后闸门里没有残留租约。"""
        app.dependency_overrides[get_latex_summary_llm] = lambda: FakeModel()
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        before = get_gate().status()["active_count"]
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        assert r.json()["model_summary_status"] == "generated"
        after = get_gate().status()
        assert after["active_count"] == before
        assert after["holders"] == []

    def test_model_failure_does_not_break_the_draft(self, client, app, temp_db, tmp_path):
        app.dependency_overrides[get_latex_summary_llm] = lambda: BoomModel()
        pid = active_project(client, temp_db)
        base = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        current = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v2.zip", v2_entries())
        )
        r = make_draft(client, temp_db, pid, current["code"], base["code"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model_summary_status"] == "failed"
        # 只暴露异常类型，不把内部错误细节写进响应（审计口径同样保守）
        assert "RuntimeError" in body["model_summary_note"]
        assert "模型超时" not in body["model_summary_note"]
        assert body["items"]

    def test_baseline_draft_has_nothing_to_summarize(
        self, client, app, temp_db, tmp_path
    ):
        app.dependency_overrides[get_latex_summary_llm] = lambda: FakeModel()
        pid = active_project(client, temp_db)
        attachment = upload_attachment(
            client, temp_db, pid, write_zip(tmp_path, "v1.zip", V1_ENTRIES)
        )
        r = make_draft(client, temp_db, pid, attachment["code"])
        assert r.status_code == 200, r.text
        assert r.json()["model_summary_status"] == "unavailable"
        assert r.json()["items"] == []
