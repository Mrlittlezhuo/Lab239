"""意图路由数据结构与校验（任务 012）。

路由结果是结构化、可校验的数据：处理路径（直接回答 / Agent Run）、
意图类型、复杂度、候选工具名称列表，以及可选的置信度与简短、
面向用户的判断摘要（不包含模型隐藏推理过程）。

校验规则：
- 候选工具必须全部来自当前实际可用的工具集合（由调用方传入，
  来源于现有工具注册表）；未知工具名被剔除；
- 本次请求开启 MCP 时额外允许候选名 ``mcp``（「需要 MCP 工具」的占位，
  不参与注册表校验，由执行分支在建立 MCP 连接后换成真实工具）；
- 路径为 Agent Run 但剔除后没有有效候选工具 → 视为非法结果
  （``RouteError``），由路由器进入回退流程；
- 未知意图降级为 ``unknown``，并按统一策略落到直接回答路径；
- ``confidence`` 截断到 [0, 1]，缺失表示不参与置信度检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PATH_DIRECT = "direct"
PATH_AGENT_RUN = "agent_run"

INTENT_GENERAL = "general"  # 普通对话
INTENT_KB_SEARCH = "kb_search"  # 知识库检索
INTENT_SINGLE_TOOL = "single_tool"  # 单工具任务
INTENT_COMPLEX = "complex"  # 复杂多步骤任务
INTENT_UNKNOWN = "unknown"  # 未知 / 无法可靠判断

VALID_INTENTS = (
    INTENT_GENERAL,
    INTENT_KB_SEARCH,
    INTENT_SINGLE_TOOL,
    INTENT_COMPLEX,
    INTENT_UNKNOWN,
)

# 前端展示用：意图 -> 简短中文名称（不展示隐藏推理过程）
INTENT_LABELS = {
    INTENT_GENERAL: "普通对话",
    INTENT_KB_SEARCH: "知识库检索",
    INTENT_SINGLE_TOOL: "单工具任务",
    INTENT_COMPLEX: "复杂任务",
    INTENT_UNKNOWN: "意图不明",
}

PATH_LABELS = {PATH_DIRECT: "直接回答", PATH_AGENT_RUN: "Agent 执行"}

COMPLEXITY_DIRECT = "direct"
COMPLEXITY_SINGLE = "single"
COMPLEXITY_MULTI = "multi"
VALID_COMPLEXITY = (COMPLEXITY_DIRECT, COMPLEXITY_SINGLE, COMPLEXITY_MULTI)

# 保留候选名：代表「这一步需要 MCP 工具」，不是某个具体工具名。
# MCP 工具的真实名字必须连上 MCP 服务才知道（连接表里不缓存工具清单），
# 路由阶段无法枚举，只能让路由器声明「要用 MCP」，由执行分支在建立连接后
# 把真实工具交给 Agent。仅当本次请求开启 MCP 时才允许出现在候选清单里。
MCP_CANDIDATE = "mcp"

SUMMARY_MAX_CHARS = 120


class RouteError(ValueError):
    """路由结果非法（结构非法、无有效候选工具等）。"""


@dataclass
class RouteResult:
    """结构化的意图路由结果。"""

    path: str = PATH_DIRECT
    intent: str = INTENT_GENERAL
    complexity: str = COMPLEXITY_DIRECT
    candidate_tools: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    summary: str = ""
    # 决策来源（provider）：rules / llm / 后续 fastjev 等
    provider: str = ""
    # 判定途径：rules（快速路径）/ llm（模型判断）/ fallback（回退）
    source: str = ""
    fallback_reason: str = ""

    @property
    def is_agent_run(self) -> bool:
        return self.path == PATH_AGENT_RUN

    def to_dict(self) -> Dict[str, Any]:
        """供响应/SSE 扩展字段透传与日志定位使用。"""
        return {
            "path": self.path,
            "path_label": PATH_LABELS.get(self.path, self.path),
            "intent": self.intent,
            "intent_label": INTENT_LABELS.get(self.intent, self.intent),
            "complexity": self.complexity,
            "candidate_tools": list(self.candidate_tools),
            "confidence": self.confidence,
            "summary": self.summary,
            "provider": self.provider,
            "source": self.source,
            "fallback_reason": self.fallback_reason,
        }


def _clamp_confidence(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    if conf != conf:  # NaN
        return None
    return min(1.0, max(0.0, conf))


def validate_route_result(
    raw: Any, available_tools: Any, *, mcp_enabled: bool = False
) -> RouteResult:
    """校验并归一化原始路由输出为 ``RouteResult``。

    ``available_tools`` 是当前实际可用的工具集合：工具注册表 dict
    （取键为工具名）或工具名可迭代对象。未知工具名被剔除；结构非法、
    或 Agent Run 路径没有有效候选工具时抛出 ``RouteError``。

    ``mcp_enabled`` 为真时保留候选名 ``mcp``（见 ``MCP_CANDIDATE``），
    它代表「需要 MCP 工具」而不是某个具体工具，因此不参与注册表校验。
    """
    if isinstance(available_tools, dict):
        available = {str(n) for n in available_tools}
    else:
        available = {str(n) for n in (available_tools or ())}
    if mcp_enabled:
        available.add(MCP_CANDIDATE)

    if isinstance(raw, RouteResult):
        path = str(raw.path or "").strip().lower()
        intent = str(raw.intent or "").strip().lower()
        complexity = str(raw.complexity or "").strip().lower()
        raw_tools = list(raw.candidate_tools or [])
        confidence = _clamp_confidence(raw.confidence)
        summary = str(raw.summary or "")
        provider = str(raw.provider or "")
        source = str(raw.source or "")
        fallback_reason = str(raw.fallback_reason or "")
    elif isinstance(raw, dict):
        path = str(raw.get("path") or "").strip().lower()
        intent = str(raw.get("intent") or "").strip().lower()
        complexity = str(raw.get("complexity") or "").strip().lower()
        raw_tools = [str(t) for t in (raw.get("candidate_tools") or [])]
        confidence = _clamp_confidence(raw.get("confidence"))
        summary = str(raw.get("summary") or "")
        provider = str(raw.get("provider") or "")
        source = str(raw.get("source") or "")
        fallback_reason = str(raw.get("fallback_reason") or "")
    else:
        raise RouteError(
            f"路由结果必须是 dict 或 RouteResult，实际为 {type(raw).__name__}"
        )

    if path not in (PATH_DIRECT, PATH_AGENT_RUN):
        raise RouteError(f"非法处理路径：{path!r}")

    # 候选工具：仅保留实际存在的名称（保序去重）
    seen = set()
    tools: List[str] = []
    for name in raw_tools:
        name = str(name).strip()
        if name and name in available and name not in seen:
            seen.add(name)
            tools.append(name)

    # 未知意图降级为 unknown，并按统一策略落到直接回答
    if intent not in VALID_INTENTS:
        intent = INTENT_UNKNOWN
    if intent == INTENT_UNKNOWN:
        path = PATH_DIRECT
        tools = []

    if path == PATH_DIRECT:
        tools = []
        complexity = COMPLEXITY_DIRECT
    else:
        if not tools:
            raise RouteError("Agent Run 路由结果没有有效候选工具")
        if complexity not in (COMPLEXITY_SINGLE, COMPLEXITY_MULTI):
            complexity = COMPLEXITY_MULTI if len(tools) > 1 else COMPLEXITY_SINGLE

    return RouteResult(
        path=path,
        intent=intent,
        complexity=complexity,
        candidate_tools=tools,
        confidence=confidence,
        summary=summary[:SUMMARY_MAX_CHARS],
        provider=provider,
        source=source,
        fallback_reason=fallback_reason,
    )


__all__ = [
    "RouteError",
    "RouteResult",
    "validate_route_result",
    "PATH_DIRECT",
    "PATH_AGENT_RUN",
    "INTENT_GENERAL",
    "INTENT_KB_SEARCH",
    "INTENT_SINGLE_TOOL",
    "INTENT_COMPLEX",
    "INTENT_UNKNOWN",
    "INTENT_LABELS",
    "PATH_LABELS",
    "COMPLEXITY_DIRECT",
    "COMPLEXITY_SINGLE",
    "COMPLEXITY_MULTI",
    "MCP_CANDIDATE",
]
