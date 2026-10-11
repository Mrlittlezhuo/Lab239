"""把库里的 MCP 连接 + MCP 通用配置转成 ``MultiServerMCPClient`` 需要的连接字典。

手动模式（``chat()``）和 Agent Run 以前各自维护一份同样的映射，已经出现细微漂移；
MCP 通用配置（profile：timeout / working_dir / env_vars）要生效也只能改一处，
所以收敛在这个模块里，两条路径都从这里取。

连接字典的构造必须留在调用方所在的任务里：MCP 客户端进入的是 anyio 的 cancel
scope，只能在进入它的那个任务里退出（见 chat.py / agent_run/service.py 的用法）。

浏览器类 server（``--browserUrl``）依赖一个已经在跑的浏览器。浏览器没开时 MCP 只会
回一句 "Failed to fetch from …"，用户看不出该干什么，所以连接之前先探端口，不通就把
连接记录里配的启动命令跑起来（``config.pre_start``）——见本文件末尾。
"""
import asyncio
import contextlib
import subprocess
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

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


# --- 外部依赖就绪检查（浏览器类 server）-----------------------------------

# 浏览器类 server 用 --browserUrl 接一个已经在跑的浏览器：浏览器不在，连接必失败，
# 而报错（"Failed to fetch from http://127.0.0.1:9333/json/version"）完全没告诉用户
# 该做什么。连接记录里配了 config.pre_start 就先探端口，不通就把外部程序拉起来。
BROWSER_URL_FLAGS = ("--browserUrl", "--browser-url")
DEFAULT_START_TIMEOUT = 20.0
PROBE_TIMEOUT = 1.0
PROBE_INTERVAL = 0.5

# 同一时刻进来多条消息时，只让第一个去启动浏览器
_start_locks: Dict[str, asyncio.Lock] = {}


def get_pre_start_specs() -> Dict[str, Dict[str, Any]]:
    """库里启用的连接上配的外部依赖启动命令（``config.pre_start``）。

    形状 ``{"command": ["/path/launcher.sh"], "url": "http://127.0.0.1:9333",
    "timeout": 20}``：``url`` 省略时从连接参数里的 ``--browserUrl`` 取，
    ``timeout`` 是拉起之后等端口最多等多久。没配 ``command`` 的连接不参与。
    """
    specs: Dict[str, Dict[str, Any]] = {}
    for conn in get_enabled_mcp_connections() or []:
        spec = (conn.get("config") or {}).get("pre_start")
        if isinstance(spec, dict) and spec.get("command"):
            specs[conn["server_name"]] = spec
    return specs


def browser_url_from_args(cfg: Dict[str, Any]) -> Optional[str]:
    """从 stdio 参数里取 ``--browserUrl`` 的值，空格和等号两种写法都认。"""
    args = [str(arg) for arg in (cfg.get("args") or [])]
    for index, arg in enumerate(args):
        for flag in BROWSER_URL_FLAGS:
            if arg == flag and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(f"{flag}="):
                return arg.split("=", 1)[1]
    return None


def _host_port(url: str) -> Optional[Tuple[str, int]]:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:  # 端口写成了非数字
        return None
    return parsed.hostname, port


async def _endpoint_open(host: str, port: int) -> bool:
    """TCP 连得上就算浏览器在。不发 HTTP：少一个依赖，也快得多。"""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), PROBE_TIMEOUT)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def _wait_for_endpoint(host: str, port: int, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await _endpoint_open(host, port):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(PROBE_INTERVAL)


async def _start_external(name: str, spec: Dict[str, Any]) -> None:
    command = [str(part) for part in spec["command"]]
    logger.info(f"启动 {name} 依赖的外部程序：{' '.join(command)}")
    # start_new_session：后端重启不该顺手把用户的浏览器带走
    await asyncio.create_subprocess_exec(
        *command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def ensure_external_prerequisites(
        connections: Dict[str, Dict[str, Any]],
        specs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """连接之前把浏览器类 server 依赖的外部程序准备好。

    端口已经通就什么都不做；不通就跑启动命令，再等端口起来。启动命令跑不起来或
    端口始终不通时抛 ``RuntimeError``，比 MCP 那句 "Failed to fetch" 好定位。
    """
    if specs is None:
        specs = get_pre_start_specs()

    for name, cfg in connections.items():
        spec = specs.get(name)
        if not spec:
            continue
        url = spec.get("url") or browser_url_from_args(cfg)
        if not url:
            logger.warning(
                f"{name} 配了 pre_start，但既没写 url，连接参数里也没有 --browserUrl")
            continue
        target = _host_port(str(url))
        if target is None:
            logger.warning(f"{name} 的浏览器地址无法解析：{url}")
            continue

        host, port = target
        if await _endpoint_open(host, port):
            continue

        lock = _start_locks.setdefault(name, asyncio.Lock())
        async with lock:
            # 并发的另一条消息可能已经把它拉起来了
            if await _endpoint_open(host, port):
                continue
            try:
                await _start_external(name, spec)
            except OSError as exc:
                raise RuntimeError(
                    f"{name} 的依赖启动命令跑不起来：{exc}") from exc
            timeout = float(spec.get("timeout") or DEFAULT_START_TIMEOUT)
            if not await _wait_for_endpoint(host, port, timeout):
                raise RuntimeError(
                    f"{name} 依赖的浏览器没能启动：{host}:{port} 没有监听"
                    f"（启动命令：{' '.join(str(part) for part in spec['command'])}）")


async def prepare_mcp_connections() -> Dict[str, Dict[str, Any]]:
    """``build_mcp_connections`` 加外部依赖就绪检查，两条聊天路径都用这个。"""
    connections = build_mcp_connections()
    if connections:
        await ensure_external_prerequisites(connections)
    return connections
