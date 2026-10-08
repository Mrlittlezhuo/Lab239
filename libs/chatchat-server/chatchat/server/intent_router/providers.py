"""意图路由 provider 边界（任务 012）。

决策来源可插拔：

- ``rule_fast_path``：确定性快速路径，只覆盖明显意图（问候/纯应答），
  不参与工具判断；未命中的请求交给通用判断（LLM provider），
  保证不依赖中文关键词硬编码即可覆盖通用请求；
- ``LlmIntentProvider``：当前版本的默认 provider，使用项目现有
  ``get_ChatOpenAI`` 模型路径（与任务 011 规划器同一模型来源）做
  通用判断；
- 后续 FastJev 接入：实现 ``IntentProvider`` 同步接口
  ``route(ctx) -> RouteResult`` 并在 ``router.build_router`` 处注册即可，
  chat API、Agent Run 与 WebUI 无需改动。本任务不引入 FastJev 模型
  或任何新外部依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from chatchat.server.agent_run.schema import PlanError, _extract_json_object

from .schemas import (
    COMPLEXITY_DIRECT,
    INTENT_GENERAL,
    PATH_DIRECT,
    RouteError,
    RouteResult,
    validate_route_result,
)


@dataclass
class RouteContext:
    """一次路由决策的输入上下文。"""

    query: str
    available_tools: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, str]] = field(default_factory=list)
    use_mcp: bool = False


class IntentProvider:
    """路由决策来源接口：同步调用，超时控制由路由器负责。"""

    name = "base"

    def route(self, ctx: RouteContext) -> RouteResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 确定性快速路径：仅明显意图（问候 / 纯符号 / 纯应答），不做工具判断
# ---------------------------------------------------------------------------

_GREETING_WORDS = {
    "你好", "您好", "嗨", "哈喽", "喂", "早上好", "下午好", "晚上好", "早安",
    "晚安", "再见", "拜拜", "谢谢", "感谢您", "多谢", "好的", "好", "嗯",
    "ok", "okay", "sure", "yes", "no", "haha", "哈哈",
    "hello", "hi", "hey", "thanks", "thank you", "bye",
}
_GREETING_STRIP_CHARS = " \t!！?？。.,，~～…:：;；()（）\"'`*☆★"


def rule_fast_path(query: str) -> Optional[RouteResult]:
    """明显意图快速路径：问候/纯符号/纯应答 → 直接回答。

    未命中返回 ``None``（交给 LLM 通用判断）。
    """
    q = (query or "").strip()
    if not q:
        return None
    compact = q.strip(_GREETING_STRIP_CHARS).casefold()
    if not compact:
        return RouteResult(
            path=PATH_DIRECT,
            intent=INTENT_GENERAL,
            complexity=COMPLEXITY_DIRECT,
            confidence=1.0,
            summary="纯符号输入，直接回答",
            provider="rules",
            source="rules",
        )
    if len(compact) <= 24:
        parts = compact.split()
        if all(p in _GREETING_WORDS for p in parts):
            return RouteResult(
                path=PATH_DIRECT,
                intent=INTENT_GENERAL,
                complexity=COMPLEXITY_DIRECT,
                confidence=1.0,
                summary="问候或简单应答，直接回答",
                provider="rules",
                source="rules",
            )
    return None


# ---------------------------------------------------------------------------
# 现有 LLM 通用判断（默认 provider）
# ---------------------------------------------------------------------------


class LlmIntentProvider(IntentProvider):
    """使用现有本地 LLM 做通用判断（与任务 011 规划器同一模型路径）。"""

    name = "llm"

    def __init__(self, llm: Any):
        self.llm = llm

    def route(self, ctx: RouteContext) -> RouteResult:
        raw = self._invoke(ctx)
        try:
            obj = _extract_json_object(raw)
        except PlanError as exc:
            raise RouteError(f"路由输出解析失败：{exc}") from exc
        return validate_route_result(obj, ctx.available_tools)

    def _invoke(self, ctx: RouteContext) -> str:
        try:
            resp = self.llm.invoke(
                [
                    {"role": "system", "content": self._system_prompt(ctx)},
                    {"role": "user", "content": self._user_prompt(ctx)},
                ]
            )
        except Exception as exc:
            raise RouteError(f"路由模型调用失败：{exc}") from exc
        content = getattr(resp, "content", None)
        if content is None:
            content = resp
        return content if isinstance(content, str) else str(content)

    def _system_prompt(self, ctx: RouteContext) -> str:
        lines = ["可用工具（candidate_tools 只能从中选取，禁止编造其他名称）："]
        for name, tool in (ctx.available_tools or {}).items():
            desc = str(getattr(tool, "description", "") or "").strip()
            desc = desc.splitlines()[0] if desc else ""
            lines.append(f"- {name}: {desc[:80]}")
        if len(lines) == 1:
            lines.append("- （当前没有可用工具；所有请求都应 path=direct）")
        if ctx.use_mcp:
            lines.append("- 本次运行还会额外包含 MCP 工具（无需为它们选择候选）")
        return (
            "你是本地助手平台的意图路由器。判断用户当前请求应「直接回答」还是"
            "交给 Agent 执行闭环（规划 → 逐步调用工具 → 观察 → 最终回答）。\n\n"
            + "\n".join(lines)
            + "\n\n判断规则：\n"
            "- 问候、闲聊、改写润色、翻译、常识解释等明显不需要工具的请求：path=direct, intent=general。\n"
            "- 明确查询本地知识库 / 论文语料 / 自有资料的内容：path=agent_run, intent=kb_search, candidate_tools 包含 search_local_knowledgebase（若可用）。\n"
            "- 单一明确能力且与某个工具匹配（天气、计算、网络检索等）：path=agent_run, intent=single_tool, candidate_tools 只放相关工具。\n"
            "- 需要检索、比较、汇总等连续动作的多步骤请求：path=agent_run, intent=complex, candidate_tools 列出合理的一组相关工具。\n"
            "- 无法可靠判断时：path=direct, intent=unknown（不要猜测）。\n\n"
            "只输出一个 JSON 对象，不要输出其他文字：\n"
            '{"path": "direct|agent_run", "intent": "general|kb_search|single_tool|complex|unknown", '
            '"complexity": "direct|single|multi", "candidate_tools": ["工具名"], '
            '"confidence": 0.0-1.0, "summary": "不超过40字的判断说明（面向用户展示，不要包含推理过程）"}'
        )

    def _user_prompt(self, ctx: RouteContext) -> str:
        parts: List[str] = []
        if ctx.history:
            parts.append("最近的对话：")
            for m in ctx.history[-6:]:
                parts.append(f"{m.get('role')}: {str(m.get('content', ''))[:200]}")
            parts.append("")
        parts.append(f"用户当前请求：\n{ctx.query}")
        return "\n".join(parts)


__all__ = [
    "RouteContext",
    "IntentProvider",
    "LlmIntentProvider",
    "rule_fast_path",
]