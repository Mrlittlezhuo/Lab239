"""MCP 连接字典的构造（两条聊天路径共用），全部离线。

覆盖：stdio/sse 形状、``config`` 为空时 command 回落到 ``args[0]``、
MCP 通用配置（env_vars / working_dir / timeout）作为底座下发、连接自身配置覆盖
底座，以及旧库里那组镜像种子环境变量不能把机器配坏。
"""
import chatchat.server.mcp_connections as mc


def _stdio(name="cdp", env=None, cwd=None, args=None, config=None):
    return {
        "server_name": name,
        "transport": "stdio",
        "config": config if config is not None else {},
        "args": args if args is not None else ["node", "/opt/cdp/cli.js", "--slim"],
        "env": env if env is not None else {},
        "cwd": cwd,
        "timeout": None,
        "enabled": True,
    }


def _sse(name="remote", timeout=None, config=None):
    return {
        "server_name": name,
        "transport": "sse",
        "config": config if config is not None else {
            "url": "http://127.0.0.1:9000/sse",
            "headers": {"Authorization": "Bearer x"},
        },
        "args": [],
        "env": {},
        "cwd": None,
        "timeout": timeout,
        "enabled": True,
    }


def _patch(monkeypatch, connections, profile):
    monkeypatch.setattr(mc, "get_enabled_mcp_connections", lambda: connections)
    monkeypatch.setattr(mc, "get_mcp_profile", lambda: profile)


class TestBuildMcpConnections:
    def test_maps_stdio_and_sse_rows(self, monkeypatch):
        _patch(monkeypatch, [
            _stdio(env={"ONLY_USER_KEY": "keep"},
                   args=["node", "/opt/cdp/cli.js", "--slim", "--headless"]),
            _sse(timeout=45),
        ], None)

        conns = mc.build_mcp_connections()

        assert set(conns) == {"cdp", "remote"}
        assert conns["cdp"] == {
            "transport": "stdio",
            # 前端 MCP 页创建的记录 config 为空，command 只能从 args[0] 取
            "command": "node",
            "args": ["/opt/cdp/cli.js", "--slim", "--headless"],
            "env": {"ONLY_USER_KEY": "keep"},
            "cwd": None,
            "encoding": "utf-8",
            "encoding_error_handler": "strict",
        }
        assert conns["remote"]["url"] == "http://127.0.0.1:9000/sse"
        assert conns["remote"]["headers"] == {"Authorization": "Bearer x"}
        assert conns["remote"]["timeout"] == 45

    def test_tolerates_no_connections(self, monkeypatch):
        _patch(monkeypatch, None, None)
        assert mc.build_mcp_connections() == {}

    def test_command_falls_back_to_args_when_config_given(self, monkeypatch):
        _patch(monkeypatch, [_stdio(config={"command": "/usr/bin/node"})], None)
        assert mc.build_mcp_connections()["cdp"]["command"] == "/usr/bin/node"


class TestProfileAsBase:
    """MCP 通用配置（profile）必须真的下发到 stdio 子进程。"""

    def test_profile_env_is_the_base_and_connection_wins(self, monkeypatch):
        _patch(monkeypatch, [_stdio(env={"API_KEY": "conn", "EXTRA": "1"})], {
            "timeout": 60,
            "working_dir": "/srv/mcp",
            "env_vars": {"API_KEY": "profile", "TZ": "UTC"},
        })

        conn = mc.build_mcp_connections()["cdp"]

        assert conn["env"] == {"API_KEY": "conn", "EXTRA": "1", "TZ": "UTC"}

    def test_working_dir_becomes_cwd_and_connection_can_override(self, monkeypatch):
        _patch(monkeypatch, [_stdio(cwd="/own/dir"), _stdio(name="other")],
               {"timeout": 30, "working_dir": "/srv/mcp", "env_vars": {}})

        conns = mc.build_mcp_connections()
        assert conns["other"]["cwd"] == "/srv/mcp"
        assert conns["cdp"]["cwd"] == "/own/dir"

    def test_profile_timeout_is_the_sse_default(self, monkeypatch):
        _patch(monkeypatch, [_sse(), _sse(name="own", timeout=90)],
               {"timeout": 120, "working_dir": "", "env_vars": {}})

        conns = mc.build_mcp_connections()
        assert conns["remote"]["timeout"] == 120
        assert conns["own"]["timeout"] == 90

    def test_legacy_image_seed_env_is_ignored(self, monkeypatch):
        """库里存着上游镜像那组 PATH/HOME/PYTHONPATH 时按未配置处理。

        照原样下发会把 node/conda 从 PATH 里抹掉、把 HOME 挪到 /tmp，
        等于把本来能用的连接配死。
        """
        _patch(monkeypatch, [_stdio()], {
            "timeout": 30,
            "working_dir": "/tmp",
            "env_vars": dict(mc.LEGACY_SEED_ENV),
        })

        conn = mc.build_mcp_connections()["cdp"]
        assert conn["env"] == {}
        # working_dir=/tmp 是编辑器里的正常取值，不受上面那组种子值影响
        assert conn["cwd"] == "/tmp"

    def test_explicit_path_value_is_respected(self, monkeypatch):
        """用户显式写成别的 PATH 时照用，只有和种子值一模一样的才忽略。"""
        _patch(monkeypatch, [_stdio()], {
            "timeout": 30, "working_dir": "", "env_vars": {"PATH": "/opt/node/bin"},
        })
        assert mc.build_mcp_connections()["cdp"]["env"] == {"PATH": "/opt/node/bin"}

    def test_broken_profile_degrades_to_no_base(self, monkeypatch):
        monkeypatch.setattr(mc, "get_enabled_mcp_connections", lambda: [_stdio()])

        def _boom():
            raise RuntimeError("库读不动")

        monkeypatch.setattr(mc, "get_mcp_profile", _boom)
        conn = mc.build_mcp_connections()["cdp"]
        assert conn["env"] == {}
        assert conn["cwd"] is None
