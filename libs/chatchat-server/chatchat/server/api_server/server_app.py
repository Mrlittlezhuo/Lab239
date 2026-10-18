import argparse
import os
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse

from chatchat import __version__
from chatchat.settings import Settings
from chatchat.server.agent_run.runtime.checkpoint import (
    initialize_agent_checkpoint_store,
    shutdown_agent_checkpoint_store,
)
from chatchat.server.model_capacity import (
    audit_generation_consumers,
    initialize_model_capacity,
    shutdown_model_capacity,
)
from chatchat.server.observability import (
    init_observability,
    shutdown_observability,
)
from chatchat.server.api_server.agent_run_routes import agent_run_router
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.api_server.model_capacity_routes import model_capacity_router
from chatchat.server.api_server.conversation_routes import conversation_router
from chatchat.server.api_server.kb_routes import kb_router
from chatchat.server.api_server.memory_routes import memory_router
from chatchat.server.api_server.session_context_routes import (
    session_context_router,
)
from chatchat.server.api_server.mcp_routes import mcp_router
from chatchat.server.api_server.openai_routes import openai_router
from chatchat.server.api_server.research_routes import research_router
from chatchat.server.api_server.server_routes import server_router
from chatchat.server.api_server.spa_static import install_spa, resolve_web_dist
from chatchat.server.startup_sweep import sweep_interrupted_runs
from chatchat.server.api_server.supervision_ai_review_routes import (
    supervision_ai_review_router,
)
from chatchat.server.api_server.supervision_research_routes import (
    supervision_research_router,
)
from chatchat.server.api_server.supervision_routes import supervision_router
from chatchat.server.api_server.supervision_work_routes import (
    supervision_work_router,
)
from chatchat.server.api_server.tool_routes import tool_router
from chatchat.server.auth.routes_auth import auth_router
from chatchat.server.auth.routes_admin import admin_router
from chatchat.server.chat.completion import completion
from chatchat.server.utils import MakeFastAPIOffline
from chatchat.utils import build_logger


logger = build_logger()


def create_app(run_mode: str = None):
    # 任务 014：Agent 运行 checkpoint 存储（AsyncSqliteSaver）随应用生命周期
    # 打开/关闭；初始化失败不阻塞应用启动（LangGraph 运行时会按需惰性
    # 自愈，仍使用本地持久化文件）。
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 任务 016：可观测性初始化（默认关闭为 no-op；失败只告警不阻塞启动）。
        # 同一进程只注册一次，重复创建应用不会反复埋点。
        init_observability()
        # 里程碑0B：容量闸门随进程启动，先清理本进程残留租约，再做一次生成
        # 模型消费者检查——出现绕过闸门的构造点只告警，不阻塞启动（§15.0B
        # 第 4/6 条、§16.1B 第 5 条）。持久化任务（预审、论文子任务、Agent
        # Run）重启后的状态收敛在下面的 startup_sweep 里处理。
        try:
            initialize_model_capacity()
            audit_generation_consumers()
        except Exception:
            logger.exception("初始化模型容量闸门失败（忽略，首次调用时惰性建立）")
        try:
            await initialize_agent_checkpoint_store()
        except Exception:
            logger.exception("初始化 Agent checkpoint 存储失败（忽略，运行时将惰性重试）")
        try:
            sweep_interrupted_runs()
        except Exception:
            logger.exception("启动收敛中断运行记录失败（忽略，不影响服务启动）")
        try:
            yield
        finally:
            await shutdown_agent_checkpoint_store()
            shutdown_model_capacity()
            shutdown_observability()

    app = FastAPI(
        title="Lab239 工作台 API Server",
        version=__version__,
        lifespan=lifespan,
    )
    MakeFastAPIOffline(app)
    # Add CORS middleware to allow all origins
    # 在config.py中设置OPEN_DOMAIN=True，允许跨域
    # set OPEN_DOMAIN=True in config.py to allow cross-domain
    if Settings.basic_settings.OPEN_CROSS_DOMAIN:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 前端重构 B2：有构建产物时 `/` 交给 SPA（在文件末尾挂载），
    # 没有产物时保持旧的 `/` → `/docs` 行为。
    spa_dist = resolve_web_dist()
    if spa_dist is None:

        @app.get("/", summary="swagger 文档", include_in_schema=False)
        async def document():
            return RedirectResponse(url="/docs")

    app.include_router(chat_router)
    app.include_router(conversation_router)
    app.include_router(session_context_router)
    app.include_router(kb_router)
    app.include_router(tool_router)
    app.include_router(openai_router)
    app.include_router(server_router)
    app.include_router(model_capacity_router)
    app.include_router(mcp_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(memory_router)
    app.include_router(agent_run_router)
    app.include_router(research_router)
    app.include_router(supervision_router)
    app.include_router(supervision_work_router)
    app.include_router(supervision_research_router)
    app.include_router(supervision_ai_review_router)

    # 其它接口
    app.post(
        "/other/completion",
        tags=["Other"],
        summary="要求llm模型补全(通过LLMChain)",
    )(completion)

    # 媒体文件
    app.mount("/media", StaticFiles(directory=Settings.basic_settings.MEDIA_PATH), name="media")

    # 项目相关图片
    img_dir = str(Settings.basic_settings.IMG_DIR)
    app.mount("/img", StaticFiles(directory=img_dir), name="img")

    # 前端 SPA 必须最后挂载：`/` 挂载会匹配任意前缀，先注册会吞掉 API 路由。
    if spa_dist is not None:
        install_spa(app)

    return app


def run_api(host, port, **kwargs):
    if kwargs.get("ssl_keyfile") and kwargs.get("ssl_certfile"):
        uvicorn.run(
            app,
            host=host,
            port=port,
            ssl_keyfile=kwargs.get("ssl_keyfile"),
            ssl_certfile=kwargs.get("ssl_certfile"),
        )
    else:
        uvicorn.run(app, host=host, port=port)


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="langchain-ChatGLM",
        description="About langchain-ChatGLM, local knowledge based ChatGLM with langchain"
        " ｜ 基于本地知识库的 ChatGLM 问答",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7861)
    parser.add_argument("--ssl_keyfile", type=str)
    parser.add_argument("--ssl_certfile", type=str)
    # 初始化消息
    args = parser.parse_args()
    args_dict = vars(args)

    run_api(
        host=args.host,
        port=args.port,
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
    )
