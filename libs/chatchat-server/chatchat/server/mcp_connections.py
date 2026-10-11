"""把库里的 MCP 连接 + MCP 通用配置转成 ``MultiServerMCPClient`` 需要的连接字典。

手动模式（``chat()``）和 Agent Run 以前各自维护一份同样的映射，已经出现细微漂移；
MCP 通用配置（profile：timeout / working_dir / env_vars）要生效也只能改一处，
所以收敛在这个模块里，两条路径都从这里取。

连接字典的构造必须留在调用方所在的任务里：MCP 客户端进入的是 anyio 的 cancel
scope，只能在进入它的那个任务里退出（见 chat.py / agent_run/service.py 的用法）。
"""
from typing import Any, Dict

from chatchat.server.db.repository.mcp_connection_repository import (
    get_enabled_mcp_connections,
    get_mcp_profile,
)
from chatchat.utils import build_logger

logger = build_logger()

# profile 里这三个值是上游 Docker 镜像时代的种子值，在这台机器上是错的：PATH 里
# 没有 conda/node（npx、node 直接找不到），HOME=/tmp 会把浏览器类 server 的
# profile 目录挪走。库里存着和种子值一模一样的条目时按"没设置"处理，免得把本来
# 能用的连接配死；用户显式写成别的值则照用。
LEGACY_SEED_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "PYTHONPATH": "/app",
    "HOME": "/tmp",
}

DEFAULT_TIMEOUT = 30.0


def profile_settings() -> Dict[str, Any]:
    """MCP 通用配置，作为所有 stdio server 的底座。

    返回 ``{"timeout": float, "cwd": str | None, "env": dict}``；读不到 profile
    时返回空底座（不改变原有行为）。
    """
    try:
        profile = get_mcp_profile()
    except Exception:
        logger.warning("读取 MCP 通用配置失败（按未配置处理）", exc_info=True)
        profile = None
    if not profile:
        return {"timeout": DEFAULT_TIMEOUT, "cwd": None, "env": {}}

    env = {
        str(k): str(v)
        for k, v in (profile.get("env_vars") or {}).items()
        if v not in (None, "") and LEGACY_SEED_ENV.get(str(k)) != str(v)
    }
    cwd = (profile.get("working_dir") or "").strip() or None
    try:
        timeout = float(profile.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return {"timeout": timeout, "cwd": cwd, "env": env}


def build_mcp_connections() -> Dict[str, Dict[str, Any]]:
    """库里启用的连接 + 通用配置 → 连接字典。

    通用配置是底座，连接自己的配置优先：
    - ``env_vars`` 垫在 ``env`` 下面（连接上配的变量覆盖同名项）；
    - ``working_dir`` 作为 stdio 子进程的工作目录，除非该连接自己填了 ``cwd``；
    - ``timeout`` 作为 sse 的默认超时，除非该连接自己填了。
    """
    settings = profile_settings()
    connections = get_enabled_mcp_connections() or []

    mcp_connections: Dict[str, Dict[str, Any]] = {}
    for conn in connections:
        if conn["transport"] == "stdio":
            # 前端 MCP 页创建的记录 config 为空，command 只能从 args[0] 取
            mcp_connections[conn["server_name"]] = {
                "transport": "stdio",
                "command": conn["config"].get("command", conn["args"][0] if conn["args"] else ""),
                "args": conn["args"][1:] if len(conn["args"]) > 1 else [],
                "env": {**settings["env"], **(conn["env"] or {})},
                "cwd": conn.get("cwd") or settings["cwd"],
                "encoding": "utf-8",
                "encoding_error_handler": "strict",
            }
        elif conn["transport"] == "sse":
            mcp_connections[conn["server_name"]] = {
                "transport": "sse",
                "url": conn["config"].get("url", ""),
                "headers": conn["config"].get("headers", {}),
                "timeout": conn.get("timeout") or settings["timeout"],
                "sse_read_timeout": conn.get("sse_read_timeout", 60.0),
            }
    return mcp_connections
