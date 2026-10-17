"""前端 SPA 静态托管离线测试（前端重构 B1/B2）。

用与 `create_app` 相同的挂载顺序（先 router、最后 `/` 挂载）搭一个最小
应用，钉住四条底线：

1. 浏览器导航（Accept 含 text/html）访问任意前端路由 → index.html，
   包括与 API 同名的 `/admin/users`、`/agent-runs/<id>`；
2. 程序化 API 调用（Accept: */* 或 application/json）不被 SPA 掩盖；
3. 打错的 API 地址仍是 404 JSON；
4. 真实静态文件（js/css）按原语义返回。
"""
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from chatchat.server.api_server.spa_static import (
    WEB_DIST_ENV,
    install_spa,
    resolve_web_dist,
)

# 浏览器地址栏导航的 Accept
BROWSER_ACCEPT = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


@pytest.fixture
def web_dist(tmp_path, monkeypatch):
    """构造一个临时前端构建目录并指向 CHATCHAT_WEB_DIST。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setenv(WEB_DIST_ENV, str(dist))
    return dist


def _make_app() -> FastAPI:
    """镜像 create_app 的路由/挂载顺序，含与 SPA 路由同名的接口。"""
    app = FastAPI()

    kb_router = APIRouter(prefix="/knowledge_base")
    kb_router.get("/list_knowledge_bases", summary="list")(lambda: {"code": 200})
    app.include_router(kb_router)

    run_router = APIRouter(prefix="/agent-runs")
    run_router.get("/{run_id}", summary="run")(lambda run_id: {"run_id": run_id})
    app.include_router(run_router)

    admin_router = APIRouter(prefix="/admin")
    admin_router.get("/users", summary="users")(lambda: {"users": []})
    app.include_router(admin_router)

    assert install_spa(app) is True
    return app


# ---------------------------------------------------------------------------
# resolve_web_dist / install_spa
# ---------------------------------------------------------------------------


def test_resolve_web_dist_env(web_dist):
    assert resolve_web_dist() == web_dist.resolve()


def test_resolve_web_dist_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(WEB_DIST_ENV, str(tmp_path / "nope"))
    assert resolve_web_dist() is None


def test_resolve_web_dist_requires_index_html(tmp_path, monkeypatch):
    empty = tmp_path / "dist"
    empty.mkdir()
    monkeypatch.setenv(WEB_DIST_ENV, str(empty))
    assert resolve_web_dist() is None


def test_install_spa_skips_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(WEB_DIST_ENV, str(tmp_path / "missing"))
    app = FastAPI()
    assert install_spa(app) is False
    assert not any(getattr(r, "path", "") == "/" for r in app.routes)


# ---------------------------------------------------------------------------
# 浏览器导航 → index.html
# ---------------------------------------------------------------------------


def test_root_serves_index(web_dist):
    client = TestClient(_make_app())
    r = client.get("/", headers=BROWSER_ACCEPT)
    assert r.status_code == 200
    assert "SPA" in r.text


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/change-password",
        "/chat",
        "/chat/123456789012345678901234567890ab",
        "/rag/conv",
        "/kb/my_kb",
        "/mcp",
        "/memory",
        "/research",
        "/research/p1/runs/r1",
        "/agent-runs",
        "/agent-runs/123456789012345678901234567890ab",
        "/admin/users",
    ],
)
def test_browser_navigation_serves_shell(web_dist, path):
    """所有前端路由硬刷新都回壳，含与 API 同名的 /admin/users、/agent-runs/<id>。"""
    client = TestClient(_make_app())
    r = client.get(path, headers=BROWSER_ACCEPT)
    assert r.status_code == 200, path
    assert "SPA" in r.text, path


def test_real_asset_served_directly(web_dist):
    client = TestClient(_make_app())
    r = client.get("/assets/app.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_asset_navigation_not_shadowed(web_dist):
    """带扩展名的路径即使 Accept: text/html 也照常返回文件/404。"""
    client = TestClient(_make_app())
    assert client.get("/assets/app.js", headers=BROWSER_ACCEPT).status_code == 200
    assert client.get("/assets/missing.js", headers=BROWSER_ACCEPT).status_code == 404


# ---------------------------------------------------------------------------
# API 调用不被 SPA 掩盖
# ---------------------------------------------------------------------------


def test_api_routes_still_work(web_dist):
    client = TestClient(_make_app())
    assert client.get("/knowledge_base/list_knowledge_bases").json() == {"code": 200}
    assert client.get("/agent-runs/abc123").json() == {"run_id": "abc123"}
    assert client.get("/admin/users").json() == {"users": []}


def test_api_json_accept_header_not_shadowed(web_dist):
    client = TestClient(_make_app())
    r = client.get("/admin/users", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"users": []}


@pytest.mark.parametrize(
    "path",
    [
        "/knowledge_base/no_such_endpoint",
        "/auth/no_such_endpoint",
        "/server/no_such_endpoint",
        "/research/no_such_endpoint",
        "/supervision/no_such_endpoint",
        "/admin/no_such_endpoint",
    ],
)
def test_unknown_api_path_is_404_not_index(web_dist, path):
    client = TestClient(_make_app())
    r = client.get(path)
    assert r.status_code == 404, path
    assert "SPA" not in r.text, path


def test_post_to_unknown_path_is_404(web_dist):
    client = TestClient(_make_app())
    r = client.post("/some/spa/path", json={})
    assert r.status_code == 404
    assert "SPA" not in r.text


def test_docs_route_still_available(web_dist):
    client = TestClient(_make_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
