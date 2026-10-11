"""Agent Run 路径的 MCP 连接生命周期，全部离线。

`_mcp_session` 取代了以前"连接建在路由协程、关在响应体任务"的写法：那样收尾必炸
anyio 的 ``Attempted to exit cancel scope in a different task than it was entered
in``，而且非流式分支成功路径上根本没有关闭调用，每条消息漏一个 server 子进程。
"""
import pytest

from chatchat.server.agent_run import service as svc


class _FakeClient:
    def __init__(self):
        self.closed = 0

    async def __aexit__(self, exc_type, exc, tb):
        self.closed += 1


async def test_no_mcp_never_connects(monkeypatch):
    async def _must_not_connect():
        raise AssertionError("use_mcp=False 时不该建连接")

    monkeypatch.setattr(svc, "_load_mcp_tools", _must_not_connect)

    async with svc._mcp_session(False) as (tools, client):
        assert tools == {}
        assert client is None


async def test_client_closed_on_normal_exit(monkeypatch):
    fake = _FakeClient()

    async def _load():
        return {"navigate": object()}, fake

    monkeypatch.setattr(svc, "_load_mcp_tools", _load)

    async with svc._mcp_session(True) as (tools, client):
        assert list(tools) == ["navigate"]
        assert client is fake

    assert fake.closed == 1


async def test_client_closed_when_body_raises(monkeypatch):
    """运行中途抛错也必须关闭，否则浏览器类 server 的进程整批留在机器上。"""
    fake = _FakeClient()

    async def _load():
        return {}, fake

    monkeypatch.setattr(svc, "_load_mcp_tools", _load)

    with pytest.raises(RuntimeError, match="运行失败"):
        async with svc._mcp_session(True):
            raise RuntimeError("运行失败")

    assert fake.closed == 1


async def test_close_failure_does_not_mask_the_run_error(monkeypatch):
    fake = _FakeClient()

    async def _boom(exc_type=None, exc=None, tb=None):
        fake.closed += 1
        raise RuntimeError("关闭失败")

    fake.__aexit__ = _boom  # type: ignore[assignment]

    async def _load():
        return {}, fake

    monkeypatch.setattr(svc, "_load_mcp_tools", _load)

    with pytest.raises(RuntimeError, match="真正的失败"):
        async with svc._mcp_session(True):
            raise RuntimeError("真正的失败")

    assert fake.closed == 1
