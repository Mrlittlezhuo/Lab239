"""意图路由器与自动模式入口（任务 012）。

``route_auto_agent`` 是 chat API 自动模式（``extra.auto_agent``）的唯一
路由入口：

1. 确定性快速路径（问候/纯应答等明显意图）；
2. 可插拔 provider（当前为现有 LLM；后续 FastJev 只需实现
   ``IntentProvider`` 并在 ``build_router`` 处替换）；
3. 结构化结果校验与低置信度检查；
4. 任何异常、解析失败、超时或低置信度 → 统一回退「直接回答」，
   绝不阻断用户对话，也不向用户抛出内部异常。

超时控制不使用 ``asyncio.to_thread``（本运行环境该路径会永久等待），
改用 ``concurrent.futures`` 线程池 + ``future.result(timeout=...)``。
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Dict, List, Optional

from chatchat.server.utils import build_logger

from .providers import (
    FallbackIntentProvider,
    FastJevIntentProvider,
    IntentProvider,
    LlmIntentProvider,
    RouteContext,
    rule_fast_path,
)
from .schemas import (
    COMPLEXITY_DIRECT,
    INTENT_GENERAL,
    PATH_DIRECT,
    RouteError,
    RouteResult,
    validate_route_result,
)

logger = build_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20
_ROUTER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="intent-router"
)


def _fallback_result(provider_name: str, reason: str) -> RouteResult:
    """统一回退结果：直接回答（普通聊天流程）。"""
    return RouteResult(
        path=PATH_DIRECT,
        intent=INTENT_GENERAL,
        complexity=COMPLEXITY_DIRECT,
        confidence=None,
        summary="自动判断未通过，已按普通对话回答",
        provider=provider_name,
        source="fallback",
        fallback_reason=reason,
    )


class IntentRouter:
    """可测试、可替换的意图路由组件。"""

    def __init__(
        self,
        provider: Optional[IntentProvider] = None,
        *,
        timeout_seconds: Optional[float] = None,
        min_confidence: Optional[float] = None,
    ):
        self.provider = provider
        self.timeout_seconds = (
            DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else max(1.0, float(timeout_seconds))
        )
        self.min_confidence = min_confidence

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "") if self.provider else ""

    def route(self, ctx: RouteContext) -> RouteResult:
        # 1) 确定性快速路径：只覆盖明显意图
        fast = rule_fast_path(ctx.query)
        if fast is not None:
            return fast

        if self.provider is None:
            result = _fallback_result("", "路由 provider 未配置")
            self._log(result, ctx)
            return result

        # 2) provider 通用判断（超时控制在线程池，不用 asyncio.to_thread）
        try:
            future = _ROUTER_EXECUTOR.submit(self.provider.route, ctx)
            result = future.result(timeout=self.timeout_seconds)
            if not isinstance(result, RouteResult):
                raise RouteError(
                    f"provider 返回了非 RouteResult 类型：{type(result).__name__}"
                )
            # 在 provider 边界统一校验，而不是只依赖当前 LLM provider
            # 自行校验。这样后续 FastJev 等 provider 也不能返回不存在的
            # 工具名或无候选工具的 Agent Run。
            result = validate_route_result(result, ctx.available_tools)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            result = _fallback_result(
                self.provider_name,
                f"路由超时（超过 {int(self.timeout_seconds)} 秒）",
            )
        except RouteError as exc:
            result = _fallback_result(self.provider_name, f"路由结果非法：{exc}")
        except Exception as exc:
            result = _fallback_result(self.provider_name, f"路由异常：{exc}")

        # 3) 低置信度的 Agent Run 判定：降级为直接回答（可控回退）
        if (
            result.source != "fallback"
            and result.is_agent_run
            and result.confidence is not None
            and self.min_confidence is not None
            and result.confidence < self.min_confidence
        ):
            logger.info(
                "意图路由置信度 {:.2f} 低于阈值 {:.2f}，回退直接回答",
                result.confidence,
                self.min_confidence,
            )
            result = RouteResult(
                path=PATH_DIRECT,
                intent=result.intent,
                complexity=COMPLEXITY_DIRECT,
                confidence=result.confidence,
                summary="判断置信度不足，已按普通对话回答",
                provider=result.provider or self.provider_name,
                source="fallback",
                fallback_reason=(
                    f"置信度 {result.confidence:.2f} 低于阈值 "
                    f"{self.min_confidence:.2f}"
                ),
            )
        else:
            if not result.provider:
                result.provider = self.provider_name
            if not result.source:
                result.source = self.provider_name

        self._log(result, ctx)
        return result

    def _log(self, result: RouteResult, ctx: RouteContext) -> None:
        # 只记录 provider / 结果 / 回退原因，不记录密钥或完整用户内容
        try:
            if result.source == "fallback":
                logger.warning(
                    "意图路由回退：provider={}, path={}, reason={}, query_len={}",
                    result.provider,
                    result.path,
                    result.fallback_reason,
                    len(ctx.query or ""),
                )
            else:
                logger.info(
                    "意图路由：provider={}, path={}, intent={}, tools={}, "
                    "confidence={}, query_len={}",
                    result.provider,
                    result.path,
                    result.intent,
                    result.candidate_tools,
                    result.confidence,
                    len(ctx.query or ""),
                )
        except Exception:
            pass


def build_router(
    provider: Optional[IntentProvider] = None,
) -> IntentRouter:
    """按当前配置构建路由器。

    ``provider`` 缺省时按 ``AUTO_ROUTE_PROVIDER`` 构建。FastJev 模式
    优先访问本机服务，调用失败时自动回退现有 LLM provider。
    """
    model_settings = None
    try:
        from chatchat.settings import Settings

        model_settings = Settings.model_settings
    except Exception:
        model_settings = None

    timeout = getattr(model_settings, "AUTO_ROUTE_TIMEOUT_SECONDS", None)
    min_confidence = getattr(model_settings, "AUTO_ROUTE_MIN_CONFIDENCE", None)

    if provider is None:
        from chatchat.server.utils import get_ChatOpenAI

        llm_provider = None
        try:
            llm_provider = LlmIntentProvider(get_ChatOpenAI())
        except Exception as exc:
            logger.warning("构建 LLM 路由 provider 失败，将统一回退直接回答：{}", exc)

        provider_name = str(getattr(model_settings, "AUTO_ROUTE_PROVIDER", "llm")).lower()
        if provider_name == "fastjev":
            primary = FastJevIntentProvider(
                getattr(model_settings, "FASTJEV_BASE_URL", "http://127.0.0.1:8811"),
                getattr(model_settings, "FASTJEV_MODEL", "fastjev-qwen3.5-4b-q4-k-m"),
                timeout_seconds=getattr(model_settings, "FASTJEV_TIMEOUT_SECONDS", 12.0),
                tool_threshold=getattr(model_settings, "FASTJEV_TOOL_THRESHOLD", 0.0),
                promotion_threshold=getattr(
                    model_settings, "FASTJEV_PROMOTION_THRESHOLD", 0.9
                ),
            )
            provider = FallbackIntentProvider(primary, llm_provider)
        else:
            provider = llm_provider

    return IntentRouter(provider, timeout_seconds=timeout, min_confidence=min_confidence)


def route_auto_agent(
    query: str,
    *,
    available_tools: Dict[str, Any],
    history: Optional[List[Dict[str, str]]] = None,
    use_mcp: bool = False,
    router: Optional[IntentRouter] = None,
) -> RouteResult:
    """自动模式路由入口：任何失败都回退「直接回答」。

    ``available_tools`` 必须来自当前实际可用的工具注册表（含按配置
    允许进入自动候选的 MCP 工具），避免维护与实际工具脱节的副本。
    """
    ctx = RouteContext(
        query=query or "",
        available_tools=dict(available_tools or {}),
        history=list(history or []),
        use_mcp=bool(use_mcp),
    )
    if router is None:
        try:
            router = build_router()
        except Exception as exc:
            logger.warning("构建意图路由器失败，回退直接回答：{}", exc)
            return _fallback_result("", f"路由器构建失败：{exc}")
    try:
        return router.route(ctx)
    except Exception as exc:
        logger.warning("意图路由意外异常，回退直接回答：{}", exc)
        return _fallback_result(router.provider_name, f"路由意外异常：{exc}")


__all__ = ["IntentRouter", "build_router", "route_auto_agent"]
