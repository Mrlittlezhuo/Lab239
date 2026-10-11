"""`/tools` 与 `/tools/call` 的角色与配置过滤（离线，模型与注册表都被替换）。

`shell` 是 `subprocess.run(shell=True)` 直通，以前任何登录用户都能通过
`POST /tools/call` 直接点名执行；未配置的工具（假 key、占位连接串）也会
出现在 `GET /tools` 里，被前端勾上、被意图路由选中，然后必然失败。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatchat.server.agent import tools_access as access
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.api_server.tool_routes import tool_router


class FakeUser:
    def __init__(self, role):
        self.role = role


class FakeTool:
    def __init__(self, name):
        self.name = name
        self.title = name
        self.description = f"{name} 的描述"
        self.args = {}

    async def ainvoke(self, tool_input):
        if self.name == "boom":
            raise RuntimeError("缺少 api_key")
        return {"ok": True, "input": tool_input}


@pytest.fixture
def config(monkeypatch):
    table: dict = {"shell_tool": {"use": True}}
    monkeypatch.setattr(access, "_section", lambda name: table.get(name, {}))
    return table


@pytest.fixture
def registry(monkeypatch):
    tools = {n: FakeTool(n) for n in ("calculate", "shell", "boom")}
    monkeypatch.setattr("chatchat.server.utils.get_tool", lambda *a, **k: tools)
    monkeypatch.setattr("chatchat.server.api_server.tool_routes.get_tool", lambda name=None: (
        tools if name is None else tools.get(name)))
    monkeypatch.setattr(
        "chatchat.server.api_server.tool_routes.get_tool_config", lambda name=None: {})
    return tools


def _client(role, config, registry):
    app = FastAPI()
    app.include_router(tool_router)
    app.dependency_overrides[require_password_changed] = lambda: FakeUser(role)
    return TestClient(app)


class TestListTools:
    def test_shell_is_not_listed_for_regular_users(self, config, registry):
        client = _client("user", config, registry)

        data = client.get("/tools").json()["data"]

        assert "calculate" in data
        assert "shell" not in data

    def test_shell_is_listed_for_admins_when_switched_on(self, config, registry):
        client = _client("admin", config, registry)

        data = client.get("/tools").json()["data"]

        assert "shell" in data

    def test_shell_is_hidden_from_admins_when_switched_off(self, config, registry):
        config["shell_tool"] = {"use": False}
        client = _client("admin", config, registry)

        assert "shell" not in client.get("/tools").json()["data"]


class TestCallTool:
    def test_regular_user_cannot_call_shell(self, config, registry):
        client = _client("user", config, registry)

        res = client.post("/tools/call", json={"name": "shell", "tool_input": {"query": "id"}})

        assert res.status_code == 403
        assert "管理员" in res.json()["detail"]

    def test_admin_can_call_shell_when_switched_on(self, config, registry):
        client = _client("admin", config, registry)

        res = client.post("/tools/call", json={"name": "shell", "tool_input": {"query": "echo"}})

        assert res.status_code == 200
        assert res.json()["data"]["ok"] is True

    def test_switched_off_blocks_even_admins(self, config, registry):
        config["shell_tool"] = {"use": False}
        client = _client("admin", config, registry)

        res = client.post("/tools/call", json={"name": "shell", "tool_input": {}})

        assert res.status_code == 403
        assert "默认关闭" in res.json()["detail"]

    def test_failure_carries_the_real_reason(self, config, registry):
        client = _client("admin", config, registry)

        body = client.post("/tools/call", json={"name": "boom", "tool_input": {}}).json()

        assert body["code"] == 500
        assert "缺少 api_key" in body["msg"]

    def test_unknown_tool_says_so(self, config, registry):
        client = _client("admin", config, registry)

        body = client.post("/tools/call", json={"name": "nope", "tool_input": {}}).json()

        assert "没有名为" in body["msg"]
