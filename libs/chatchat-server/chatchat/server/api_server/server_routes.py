from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.pydantic_v2 import BaseModel, Field
from chatchat.server.types.server.response.base import BaseResponse
from chatchat.settings import Settings
from chatchat.server.utils import get_prompt_template, get_server_configs

server_router = APIRouter(prefix="/server", tags=["Server State"])

# pydantic v2.11+ 禁止在实例上访问 model_fields，改经类属性访问
available_template_types = list(Settings.prompt_settings.__class__.model_fields.keys())

# 服务器相关接口
server_router.post(
    "/configs",
    summary="获取服务器原始配置信息",
)(get_server_configs)


def _collect_models(field: str) -> list:
    """跨平台汇总模型名，去重且忽略 auto 占位。"""
    collected: list = []
    for platform in Settings.model_settings.MODEL_PLATFORMS:
        value = getattr(platform, field, None)
        if not isinstance(value, list):
            continue
        for name in value:
            if isinstance(name, str) and name and name not in collected:
                collected.append(name)
    return collected


class PlatformModelsOut(BaseModel):
    """单个模型平台的模型清单（不含任何连接凭据）。"""

    name: str
    llm_models: List[str] = Field(default_factory=list)
    embed_models: List[str] = Field(default_factory=list)
    image2text_models: List[str] = Field(default_factory=list)


class KBDefaultsOut(BaseModel):
    """知识库默认参数。类型用宽口径：这些值由用户 yaml 配置，可被改成任意名称/取值。"""

    chunk_size: int
    chunk_overlap: int
    vector_search_top_k: int
    score_threshold: float
    default_knowledge_base: str
    default_vs_type: str
    default_search_engine: Optional[str] = None


class ServerModelsOut(BaseModel):
    """`GET /server/models` 的响应契约。

    字段必须与 `get_server_models()` 的返回一一对应：FastAPI 的 `response_model`
    会按本模型**过滤**输出，漏声明字段等于直接从响应里删掉它。改这个模型时，
    前端 `src/api/server.ts` 会经 `schema.d.ts` 在编译期同步，不需要手抄。
    """

    platforms: List[PlatformModelsOut] = Field(default_factory=list)
    llm_models: List[str] = Field(default_factory=list)
    embedding_models: List[str] = Field(default_factory=list)
    image2text_models: List[str] = Field(default_factory=list)
    default_llm: str
    default_embedding: str
    history_len: int
    temperature: float
    max_tokens: Optional[int] = None
    support_agent_models: List[str] = Field(default_factory=list)
    # LLM_MODEL_CONFIG 是 Dict[str, Dict]：键固定为四个阶段，内层取值自由，
    # 这里刻意不收紧，避免把用户配置里的字段过滤掉。
    llm_model_config: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    search_engines: List[str] = Field(default_factory=list)
    vs_types: List[str] = Field(default_factory=list)
    kb_defaults: KBDefaultsOut


@server_router.get(
    "/models",
    summary="获取模型清单与默认参数（需登录，不含任何密钥）",
    response_model=ServerModelsOut,
    dependencies=[Depends(require_password_changed)],
)
def get_server_models() -> dict:
    """前端模型选择器使用的认证版配置接口。

    与 /server/configs 的区别：只暴露模型名与默认参数，
    绝不返回 api_key、api_base_url、代理等敏感字段。
    """
    model_settings = Settings.model_settings
    kb_settings = Settings.kb_settings
    platforms = [
        {
            "name": platform.platform_name,
            "llm_models": [
                m for m in (platform.llm_models or []) if isinstance(m, str)
            ],
            "embed_models": [
                m for m in (platform.embed_models or []) if isinstance(m, str)
            ],
            "image2text_models": [
                m for m in (platform.image2text_models or []) if isinstance(m, str)
            ],
        }
        for platform in model_settings.MODEL_PLATFORMS
    ]
    search_engine_config = (Settings.tool_settings.search_internet or {}).get(
        "search_engine_config", {}
    )
    llm_model_config = {
        key: dict(model_settings.LLM_MODEL_CONFIG.get(key, {}))
        for key in ("preprocess_model", "llm_model", "action_model", "postprocess_model")
    }
    return {
        "platforms": platforms,
        "llm_models": _collect_models("llm_models"),
        "embedding_models": _collect_models("embed_models"),
        "image2text_models": _collect_models("image2text_models"),
        "default_llm": model_settings.DEFAULT_LLM_MODEL,
        "default_embedding": model_settings.DEFAULT_EMBEDDING_MODEL,
        "history_len": model_settings.HISTORY_LEN,
        "temperature": model_settings.TEMPERATURE,
        "max_tokens": model_settings.MAX_TOKENS,
        "support_agent_models": list(model_settings.SUPPORT_AGENT_MODELS),
        "llm_model_config": llm_model_config,
        "search_engines": list(search_engine_config.keys()),
        "vs_types": list(kb_settings.kbs_config.keys()),
        "kb_defaults": {
            "chunk_size": kb_settings.CHUNK_SIZE,
            "chunk_overlap": kb_settings.OVERLAP_SIZE,
            "vector_search_top_k": kb_settings.VECTOR_SEARCH_TOP_K,
            "score_threshold": kb_settings.SCORE_THRESHOLD,
            "default_knowledge_base": kb_settings.DEFAULT_KNOWLEDGE_BASE,
            "default_vs_type": kb_settings.DEFAULT_VS_TYPE,
            "default_search_engine": kb_settings.DEFAULT_SEARCH_ENGINE,
        },
    }


@server_router.post("/get_prompt_template", summary="获取服务区配置的 prompt 模板", response_model=BaseResponse)
def get_server_prompt_template(
        type: str = Body(
            "llm_model", description="模板类型，可选值：{available_template_types}"
        ),
        name: str = Body("default", description="模板名称"),
):
    prompt_template = get_prompt_template(type=type, name=name)
    if prompt_template is None:
        return BaseResponse.error("Prompt template not found")
    return BaseResponse.success(prompt_template)
