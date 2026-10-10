"""前端重构 B4/B5：/tools 与 MCP 路由要求登录（离线）。

未登录访问必须 401；不连接真实 MCP 服务或模型。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from chatchat.server.api_server.mcp_routes import mcp_router
    from chatchat.server.api_server.tool_routes import tool_router

    app = FastAPI()
    app.include_router(tool_router)
    app.include_router(mcp_router)
    with TestClient(app) as test_client:
        yield test_client


def test_tools_list_requires_auth(client):
    assert client.get("/tools").status_code == 401


def test_tools_call_requires_auth(client):
    assert client.post("/tools/call", json={"name": "calculate", "tool_input": {}}).status_code == 401


def test_mcp_connections_list_requires_auth(client):
    assert client.get("/api/v1/mcp_connections/").status_code == 401


def test_mcp_profile_requires_auth(client):
    assert client.get("/api/v1/mcp_connections/profile").status_code == 401


def test_mcp_create_requires_auth(client):
    assert client.post(
        "/api/v1/mcp_connections/",
        json={"server_name": "x", "args": []},
    ).status_code == 401
