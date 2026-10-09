"""Phoenix 本地可观测性初始化与 Trace 助手（任务 016）。

设计约束：
- 默认关闭：``TRACE_ENABLED=False`` 时完全 no-op，不导入 Phoenix、不产生
  任何网络请求；OpenTelemetry API 在未配置 TracerProvider 时返回
  NonRecordingSpan，手工 span 自动退化为空操作。
- 幂等：同一进程只注册一次；重复初始化 / 重复创建应用不会反复包装
  instrumentor 或重复上报注册。
- 安全降级：Phoenix 未启动、endpoint 不可达或初始化异常时只记录简洁
  告警，不抛出、不中断应用启动与对话；导出失败由 OTel BatchSpanProcessor
  自行丢弃，不重试、不影响业务。
- 内容捕获默认关闭：未显式开启 ``TRACE_CAPTURE_CONTENT`` 时设置
  OpenInference 的隐藏环境变量，prompt / response / 工具输入输出不进入
  span 属性；任何情况下都不记录令牌、密钥、连接串或隐藏思维链。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from chatchat.settings import Settings
from chatchat.utils import build_logger

logger = build_logger()

TRACER_NAME = "chatchat"

_lock = threading.RLock()
_initialized = False
_instrumentor = None


def is_observability_enabled() -> bool:
    """当前进程是否已完成可观测性注册。"""
    return _initialized


def _apply_content_capture_policy(cfg) -> None:
    """内容捕获默认关闭：在 instrument 之前设置 OpenInference 隐藏开关。

    使用 setdefault 语义中更强的强制覆盖：本任务要求默认不记录用户内容，
    因此按配置显式写入环境变量（instrumentor 在 instrument 时读取）。
    """
    if getattr(cfg, "TRACE_CAPTURE_CONTENT", False):
        os.environ["OPENINFERENCE_HIDE_INPUTS"] = "false"
        os.environ["OPENINFERENCE_HIDE_OUTPUTS"] = "false"
        os.environ["OPENINFERENCE_HIDE_RETRIEVAL_DOCUMENTS"] = "false"
    else:
        os.environ["OPENINFERENCE_HIDE_INPUTS"] = "true"
        os.environ["OPENINFERENCE_HIDE_OUTPUTS"] = "true"
        os.environ["OPENINFERENCE_HIDE_RETRIEVAL_DOCUMENTS"] = "true"


def init_observability() -> bool:
    """按配置初始化 Phoenix / OpenTelemetry / OpenInference（幂等）。

    返回是否处于启用状态。任何异常都被吞掉并记录简洁告警，绝不影响
    应用启动与对话功能。
    """
    global _initialized, _instrumentor
    cfg = Settings.basic_settings
    if not getattr(cfg, "TRACE_ENABLED", False):
        return False
    with _lock:
        if _initialized:
            return True
        try:
            from phoenix.otel import register

            # phoenix-otel 0.17.x 不会自动补 OTLP HTTP 路径：配置只写
            # host:port（或根路径）时自动补 /v1/traces，已带路径则原样使用。
            from urllib.parse import urlparse

            endpoint = (cfg.TRACE_PHOENIX_ENDPOINT or "").rstrip("/")
            if endpoint and urlparse(endpoint).path in ("", "/"):
                endpoint += "/v1/traces"

            kwargs: Dict[str, Any] = dict(
                endpoint=endpoint,
                project_name=cfg.TRACE_PROJECT_NAME,
                batch=True,
                set_global_tracer_provider=True,
                # 自动埋点由本模块显式控制，保证幂等与可测试
                auto_instrument=False,
            )
            ratio = getattr(cfg, "TRACE_SAMPLE_RATIO", 1.0)
            if ratio is not None and ratio < 1.0:
                # register 的 **kwargs 会透传给 TracerProvider 构造函数
                from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

                kwargs["sampler"] = TraceIdRatioBased(float(ratio))

            _apply_content_capture_policy(cfg)
            register(**kwargs)

            from openinference.instrumentation.langchain import (
                LangChainInstrumentor,
            )

            if _instrumentor is None:
                _instrumentor = LangChainInstrumentor()
            if not getattr(_instrumentor, "_is_instrumented_by_opentelemetry", False):
                _instrumentor.instrument()

            _initialized = True
            logger.info(
                f"可观测性已启用：Phoenix endpoint={cfg.TRACE_PHOENIX_ENDPOINT} "
                f"project={cfg.TRACE_PROJECT_NAME} 内容捕获="
                f"{bool(getattr(cfg, 'TRACE_CAPTURE_CONTENT', False))}"
            )
            return True
        except Exception as exc:
            # Phoenix 不可用 / 依赖缺失 / endpoint 非法：简洁告警，不中断
            logger.warning(
                f"可观测性初始化失败（已忽略，对话功能不受影响）：{exc}"
            )
            return False


def shutdown_observability() -> None:
    """反注册 instrumentor 并关闭全局 TracerProvider（幂等，忽略异常）。"""
    global _initialized, _instrumentor
    with _lock:
        if not _initialized:
            return
        try:
            if _instrumentor is not None and getattr(
                _instrumentor, "_is_instrumented_by_opentelemetry", False
            ):
                _instrumentor.uninstrument()
        except Exception:
            logger.warning("可观测性 instrumentor 反注册失败（已忽略）")
        try:
            provider = trace.get_tracer_provider()
            if hasattr(provider, "shutdown"):
                provider.shutdown()
        except Exception:
            logger.warning("可观测性 TracerProvider 关闭失败（已忽略）")
        _initialized = False


def get_tracer():
    """返回 OTel tracer；未启用可观测性时产生 NonRecordingSpan（no-op）。"""
    return trace.get_tracer(TRACER_NAME)


def start_business_span(
    name: str, attributes: Optional[Dict[str, Any]] = None
) -> "trace.Span":
    """创建业务手工 span 并激活其上下文，调用方负责 ``span.end()``。

    只记录标识与状态类属性（ID、意图、分支、模型名、计数等），不记录
    用户内容、令牌、密钥或连接串。
    """
    span = get_tracer().start_span(name)
    set_span_attributes(span, attributes)
    return span


def set_span_attributes(
    span: "trace.Span", attributes: Optional[Dict[str, Any]]
) -> None:
    if span is None or not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, value)
        except Exception:  # 属性类型异常不影响业务
            pass


def finish_span(
    span: "trace.Span",
    *,
    error: Optional[BaseException] = None,
    status: Optional[StatusCode] = None,
    description: Optional[str] = None,
) -> None:
    """结束 span：异常记 ERROR，正常可显式给状态；永不抛出。"""
    if span is None:
        return
    try:
        if error is not None:
            span.record_exception(error)
            span.set_status(StatusCode.ERROR, str(error) or type(error).__name__)
        elif status is not None:
            span.set_status(status, description or "")
    except Exception:
        pass
    try:
        span.end()
    except Exception:
        pass
