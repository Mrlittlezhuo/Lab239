"""浏览器类 MCP 的外部依赖就绪检查，全部离线（不真的启动浏览器）。

覆盖：``config.pre_start`` 的读取与忽略条件、``--browserUrl`` 两种写法、地址解析、
端口已通时不动作、不通时启动一次并等端口、始终不通时报出可读的错误、缺 url 时只告警，
以及并发消息只启动一次浏览器。连接字典本身的构造见 ``test_mcp_connections.py``。
"""
import asyncio

import pytest

import chatchat.server.mcp_connections as mc

CONNECTIONS = {
    "chrome-devtools": {
        "transport": "stdio",
        "command": "node",
        "args": ["/opt/cdp/cli.js", "--browserUrl", "http://127.0.0.1:9333"],
        "env": {},
        "cwd": None,
        "encoding": "utf-8",
        "encoding_error_handler": "strict",
    }
}
SPEC = {"command": ["/opt/agent-chrome.sh"], "url": "http://127.0.0.1:9333", "timeout": 1}


@pytest.fixture(autouse=True)
def _fresh_locks():
    mc._start_locks.clear()
    yield
    mc._start_locks.clear()


def _row(name="chrome-devtools", config=None):
    return {"server_name": name, "transport": "stdio", "config": config or {},
            "args": ["node", "/opt/cdp/cli.js"], "env": {}, "cwd": None,
            "timeout": None, "enabled": True}


class TestSpecsAndUrls:
    def test_reads_pre_start_only_when_it_has_a_command(self, monkeypatch):
        monkeypatch.setattr(mc, "get_enabled_mcp_connections", lambda: [
            _row(config={"pre_start": {"command": ["/opt/x.sh"]}}),
            _row(name="no_command", config={"pre_start": {"url": "http://127.0.0.1:1"}}),
            _row(name="no_pre_start", config={}),
            _row(name="broken", config={"pre_start": "不是字典"}),
        ])

        specs = mc.get_pre_start_specs()

        assert list(specs) == ["chrome-devtools"]
        assert specs["chrome-devtools"] == {"command": ["/opt/x.sh"]}

    @pytest.mark.parametrize("args,expected", [
        (["cli.js", "--browserUrl", "http://127.0.0.1:9333"], "http://127.0.0.1:9333"),
        (["cli.js", "--browser-url=http://localhost:9333"], "http://localhost:9333"),
        (["cli.js", "--autoConnect"], None),
        (["cli.js", "--browserUrl"], None),
    ])
    def test_browser_url_from_args(self, args, expected):
        assert mc.browser_url_from_args({"args": args}) == expected

    @pytest.mark.parametrize("url,expected", [
        ("http://127.0.0.1:9333/json/version", ("127.0.0.1", 9333)),
        ("http://localhost:9333", ("localhost", 9333)),
        ("http://127.0.0.1", ("127.0.0.1", 80)),
        ("not a url", None),
        ("http://127.0.0.1:abc", None),
    ])
    def test_host_port(self, url, expected):
        assert mc._host_port(url) == expected


class TestEnsureExternalPrerequisites:
    async def test_nothing_happens_when_the_port_is_already_open(self, monkeypatch):
        started = []

        async def open_(host, port):
            return True

        async def start(name, spec):
            started.append(name)

        monkeypatch.setattr(mc, "_endpoint_open", open_)
        monkeypatch.setattr(mc, "_start_external", start)

        await mc.ensure_external_prerequisites(CONNECTIONS, {"chrome-devtools": SPEC})

        assert started == []

    async def test_starts_the_browser_when_the_port_is_closed(self, monkeypatch):
        started = []
        state = {"open": False}

        async def open_(host, port):
            return state["open"]

        async def start(name, spec):
            started.append((name, spec["command"]))
            state["open"] = True

        monkeypatch.setattr(mc, "_endpoint_open", open_)
        monkeypatch.setattr(mc, "_start_external", start)

        await mc.ensure_external_prerequisites(CONNECTIONS, {"chrome-devtools": SPEC})

        assert started == [("chrome-devtools", ["/opt/agent-chrome.sh"])]

    async def test_url_falls_back_to_the_browserUrl_in_the_args(self, monkeypatch):
        seen = {}

        async def open_(host, port):
            seen["target"] = (host, port)
            return True

        monkeypatch.setattr(mc, "_endpoint_open", open_)

        await mc.ensure_external_prerequisites(
            CONNECTIONS, {"chrome-devtools": {"command": ["/opt/x.sh"]}})

        assert seen["target"] == ("127.0.0.1", 9333)

    async def test_reports_a_readable_error_when_the_browser_never_comes_up(
            self, monkeypatch):
        async def closed(host, port):
            return False

        async def start(name, spec):
            return None

        monkeypatch.setattr(mc, "_endpoint_open", closed)
        monkeypatch.setattr(mc, "_start_external", start)

        with pytest.raises(RuntimeError) as exc:
            await mc.ensure_external_prerequisites(
                CONNECTIONS, {"chrome-devtools": {**SPEC, "timeout": 0.2}})

        assert "127.0.0.1:9333" in str(exc.value)
        assert "/opt/agent-chrome.sh" in str(exc.value)

    async def test_reports_when_the_launcher_itself_cannot_run(self, monkeypatch):
        async def closed(host, port):
            return False

        async def start(name, spec):
            raise OSError("No such file or directory: /opt/agent-chrome.sh")

        monkeypatch.setattr(mc, "_endpoint_open", closed)
        monkeypatch.setattr(mc, "_start_external", start)

        with pytest.raises(RuntimeError) as exc:
            await mc.ensure_external_prerequisites(
                CONNECTIONS, {"chrome-devtools": {**SPEC, "timeout": 0.2}})

        assert "跑不起来" in str(exc.value)

    async def test_connections_without_a_spec_are_left_alone(self, monkeypatch):
        started = []

        async def closed(host, port):
            return False

        async def start(name, spec):
            started.append(name)

        monkeypatch.setattr(mc, "_endpoint_open", closed)
        monkeypatch.setattr(mc, "_start_external", start)

        await mc.ensure_external_prerequisites(CONNECTIONS, {})

        assert started == []

    async def test_spec_without_any_url_only_warns(self, monkeypatch):
        started = []
        warnings = []

        async def start(name, spec):
            started.append(name)

        monkeypatch.setattr(mc, "_start_external", start)
        monkeypatch.setattr(mc.logger, "warning", lambda *a: warnings.append(a))

        await mc.ensure_external_prerequisites(
            {"plain": {"transport": "stdio", "args": ["node", "cli.js"]}},
            {"plain": {"command": ["/opt/x.sh"]}})

        assert started == []
        assert warnings

    async def test_concurrent_messages_start_the_browser_once(self, monkeypatch):
        started = []
        state = {"open": False}

        async def open_(host, port):
            return state["open"]

        async def start(name, spec):
            started.append(name)
            # 模拟浏览器要几秒才监听：期间另外两条消息也在等
            await asyncio.sleep(0.05)
            state["open"] = True

        monkeypatch.setattr(mc, "_endpoint_open", open_)
        monkeypatch.setattr(mc, "_start_external", start)

        await asyncio.gather(*(
            mc.ensure_external_prerequisites(CONNECTIONS, {"chrome-devtools": SPEC})
            for _ in range(3)))

        assert started == ["chrome-devtools"]


class TestPrepareMcpConnections:
    async def test_builds_connections_then_checks_prerequisites(self, monkeypatch):
        calls = []

        monkeypatch.setattr(mc, "build_mcp_connections", lambda: dict(CONNECTIONS))

        async def ensure(connections, specs=None):
            calls.append((sorted(connections), specs))

        monkeypatch.setattr(mc, "ensure_external_prerequisites", ensure)

        conns = await mc.prepare_mcp_connections()

        assert sorted(conns) == ["chrome-devtools"]
        assert calls == [(["chrome-devtools"], None)]

    async def test_no_connections_means_no_prerequisite_check(self, monkeypatch):
        monkeypatch.setattr(mc, "build_mcp_connections", lambda: {})

        async def ensure(connections, specs=None):
            raise AssertionError("没有连接就不该去探端口")

        monkeypatch.setattr(mc, "ensure_external_prerequisites", ensure)

        assert await mc.prepare_mcp_connections() == {}
