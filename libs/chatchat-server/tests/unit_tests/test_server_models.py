"""认证版 GET /server/models 离线测试（前端重构任务 B6）。

验证：未登录 401、强制改密 403、正常 200 且响应不含任何密钥字段。
不连接模型或网络。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chatchat.server.auth import service
from chatchat.server.db import session as db_session
from chatchat.server.db.migrate.base import apply_migrations
from chatchat.server.db.migrate.migrations import build_registry


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("CHATCHAT_AUTH_SECRET", "unit-test-secret-key" + "0" * 20)


@pytest.fixture
def app(tmp_path, secret):
    path = tmp_path / "server_models.db"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    from chatchat.server.api_server.server_routes import server_router
    from chatchat.server.auth.routes_auth import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(server_router)

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[db_session.get_db] = override_get_db
    with TestClient(app) as client:
        yield {"client": client, "Session": Session}
    engine.dispose()


def _create_user(Session, username, must_change_password=False):
    s = Session()
    try:
        u = service.create_user(
            s,
            username=username,
            display_name=username,
            password="password1234",
            role="user",
        )
        u.must_change_password = must_change_password
        s.commit()
    finally:
        s.close()


def _login(client, username, password="password1234"):
    res = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res.json()["token"]


def test_requires_auth(app):
    res = app["client"].get("/server/models")
    assert res.status_code == 401


def test_must_change_password_blocked(app):
    _create_user(app["Session"], "newbie", must_change_password=True)
    token = _login(app["client"], "newbie")
    res = app["client"].get("/server/models", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403
    assert "请先修改密码" in res.json()["detail"]


def test_returns_models_without_secrets(app):
    _create_user(app["Session"], "viewer")
    token = _login(app["client"], "viewer")
    res = app["client"].get(
        "/server/models", headers={"Authorization": f"Bearer {token}"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    for key in (
        "platforms",
        "llm_models",
        "embedding_models",
        "image2text_models",
        "default_llm",
        "default_embedding",
        "llm_model_config",
        "search_engines",
        "vs_types",
        "kb_defaults",
    ):
        assert key in body, f"缺少字段 {key}"
    assert body["default_llm"]
    assert isinstance(body["llm_models"], list)
    assert set(body["llm_model_config"]) == {
        "preprocess_model",
        "llm_model",
        "action_model",
        "postprocess_model",
    }
    # 绝不泄露密钥/代理/平台连接配置
    raw = json.dumps(body, ensure_ascii=False)
    for forbidden in ("api_key", "api_base_url", "api_proxy", "bing_key", "sk-"):
        assert forbidden not in raw, f"响应包含敏感字段 {forbidden}"
