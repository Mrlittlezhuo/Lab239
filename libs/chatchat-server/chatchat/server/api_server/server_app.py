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
from chatchat.server.api_server.agent_run_routes import agent_run_router
from chatchat.server.api_server.chat_routes import chat_router
from chatchat.server.api_server.conversation_routes import conversation_router
from chatchat.server.api_server.kb_routes import kb_router
from chatchat.server.api_server.memory_routes import memory_router
from chatchat.server.api_server.mcp_routes import mcp_router
from chatchat.server.api_server.openai_routes import openai_router
from chatchat.server.api_server.server_routes import server_router
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
        try:
            await initialize_agent_checkpoint_store()
        except Exception:
            logger.exception("初始化 Agent checkpoint 存储失败（忽略，运行时将惰性重试）")
        try:
            yield
        finally:
            await shutdown_agent_checkpoint_store()

    app = FastAPI(
        title="Langchain-Chatchat API Server",
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

    @app.get("/", summary="swagger 文档", include_in_schema=False)
    async def document():
        return RedirectResponse(url="/docs")

    app.include_router(chat_router)
    app.include_router(conversation_router)
    app.include_router(kb_router)
    app.include_router(tool_router)
    app.include_router(openai_router)
    app.include_router(server_router)
    app.include_router(mcp_router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(memory_router)
    app.include_router(agent_run_router)

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
