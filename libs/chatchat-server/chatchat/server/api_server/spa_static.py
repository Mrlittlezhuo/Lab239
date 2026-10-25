"""前端 SPA 静态托管（前端重构 B1/B2）。

React SPA 构建产物（默认仓库根 `frontend/dist`，可用 `CHATCHAT_WEB_DIST`
覆盖）由 FastAPI 同源挂载，避免额外端口与 CORS。需要同时满足三件事：

1. **真实文件**（js/css/图片）按 ``StaticFiles`` 原语义返回；
2. **history 路由硬刷新**（``/kb/x``、``/research/<id>/runs/<rid>``）磁盘上
   没有对应文件，必须回落到 ``index.html``；
3. **打错的 API 地址仍返回 404 JSON**，不能被一份 HTML 掩盖。

难点在于 SPA 路由前缀与 API 前缀有重叠，甚至完全同名
（``GET /admin/users``、``GET /agent-runs/{run_id}`` 既是接口也是页面）。
因此分两层，各自职责单一：

- :class:`SPAStaticFiles`：兜住 *没有任何 API 路由命中* 的
  GET/HEAD 无扩展名路径（``/kb/x`` 这类），纯 API 前缀下不兜底；
- :class:`SPANavigationMiddleware`：处理 *与 API 路由同名* 的页面导航。
  浏览器地址栏导航的 ``Accept`` 一定包含 ``text/html``，而 fetch/XHR 默认是
  ``*/*`` 或 ``application/json``，据此把“人在浏览页面”与“程序在调接口”
  区分开：前者直接返回 ``index.html``，后者照常走 API。

挂载顺序：``/`` 挂载会匹配任意前缀，必须放在所有 router/mount 之后。
"""

import os
from pathlib import Path
from typing import Iterable, Optional

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from chatchat.utils import build_logger

logger = build_logger()

# 只有后端接口/内建页面使用的一级路径。SPA fallback 不兜这些前缀下的
# 未知路径，保证 API 拼错地址仍是 404 JSON。
API_PREFIXES = frozenset(
    {
        "auth",
        "chat",
        "conversations",
        "knowledge_base",
        "kb_summary_api",
        "tools",
        "v1",
        "server",
        "api",
        "memories",
        "research",
        "supervision",
        "agent-runs",
        "admin",
        "other",
        "media",
        "img",
        "docs",
        "redoc",
        "openapi.json",
        "static-offline-docs",
    }
)

# 前端 history 路由的一级路径（见 frontend/src/routes/router.tsx）。
# 其中 research / agent-runs / admin 与 API 前缀重叠甚至同名，
# 需要中间件按 Accept 头优先服务浏览器导航。
SPA_ROUTE_PREFIXES = frozenset(
    {
        "login",
        "change-password",
        "chat",
        "rag",
        "kb",
        "mcp",
        "memory",
        "admin",
        "research",
        "agent-runs",
    }
)

WEB_DIST_ENV = "CHATCHAT_WEB_DIST"


def resolve_web_dist() -> Optional[Path]:
    """返回存在 `index.html` 的前端构建目录，否则返回 ``None``。"""
    raw = os.environ.get(WEB_DIST_ENV, "").strip()
    if raw:
        candidates = [Path(raw).expanduser()]
    else:
        # 本文件位于 <repo>/libs/chatchat-server/chatchat/server/api_server/
        repo_root = Path(__file__).resolve().parents[5]
        candidates = [repo_root / "frontend" / "dist"]
    for dist in candidates:
        if (dist / "index.html").is_file():
            return dist.resolve()
    return None


def _first_segment(path: str) -> str:
    return path.lstrip("/").split("/", 1)[0]


def _has_extension(path: str) -> bool:
    return "." in path.rsplit("/", 1)[-1]


def _accepts_html(scope: Scope) -> bool:
    """浏览器地址栏导航（含刷新/直接输入 URL）的 Accept 必含 text/html。"""
    for name, value in scope.get("headers", []):
        if name == b"accept":
            return "text/html" in value.decode("latin-1").lower()
    return False


class SPAStaticFiles(StaticFiles):
    """``StaticFiles`` + history-API 回落（没有任何 API 路由命中的路径）。"""

    def __init__(
        self, *, api_prefixes: Iterable[str] = API_PREFIXES, **kwargs
    ) -> None:
        kwargs.setdefault("html", True)
        super().__init__(**kwargs)
        self.api_prefixes = frozenset(api_prefixes)

    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code not in (404, 405):
                raise

        if scope.get("method") not in ("GET", "HEAD"):
            raise HTTPException(status_code=404)
        # starlette 的 StaticFiles.get_path 走 os.path.normpath，Windows 上会把
        # "/" 换成 "\\"，于是 _first_segment 取不到一级路径，API 前缀闸门失效：
        # 打错的 /auth/xxx 会被 index.html 以 200 吞掉。这里只归一化用来判断的副本，
        # 真正的文件查找仍用原 path（POSIX 下反斜杠是合法文件名字符，不能改）。
        lookup = path.replace("\\", "/")
        if _first_segment(lookup) in self.api_prefixes or _has_extension(lookup):
            raise HTTPException(status_code=404)

        try:
            response = await super().get_response("index.html", scope)
        except HTTPException:
            raise HTTPException(status_code=404)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


class SPANavigationMiddleware:
    """与 API 路由同名的 SPA 页面：浏览器导航返回 index.html。

    只对 GET/HEAD、``Accept`` 含 ``text/html``、且一级路径属于前端路由的
    请求生效；API 调用（``Accept: */*`` / ``application/json``）不受影响。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        index_file: Path,
        spa_prefixes: Iterable[str] = SPA_ROUTE_PREFIXES,
    ) -> None:
        self.app = app
        self.index_file = index_file
        self.spa_prefixes = frozenset(spa_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in ("GET", "HEAD")
            and _accepts_html(scope)
            and _first_segment(scope.get("path", "")) in self.spa_prefixes
        ):
            response = FileResponse(self.index_file, media_type="text/html")
            response.headers.setdefault("Cache-Control", "no-cache")
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_spa(app: FastAPI) -> bool:
    """把前端构建产物挂到 ``/``；没有产物时什么都不做并返回 ``False``。"""
    dist = resolve_web_dist()
    if dist is None:
        env = os.environ.get(WEB_DIST_ENV, "").strip()
        if env:
            logger.warning(
                f"{WEB_DIST_ENV}={env} 下未找到 index.html，跳过 SPA 挂载"
                "（/ 仍跳转 /docs）"
            )
        else:
            logger.info(
                "未找到前端构建产物 frontend/dist，跳过 SPA 挂载（/ 仍跳转 /docs）。"
                "在 frontend/ 执行 `npm run build` 后重启即可启用 Web UI。"
            )
        return False

    # 中间件包裹整个应用（含路由匹配之前），用于同名页面/接口分流；
    # `/` 静态挂载必须最后注册，否则会吞掉所有 API 路由。
    app.add_middleware(
        SPANavigationMiddleware, index_file=dist / "index.html"
    )
    app.mount("/", SPAStaticFiles(directory=str(dist)), name="spa")
    logger.info(f"已挂载前端 SPA：{dist}")
    return True
