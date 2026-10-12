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

import httpx

from chatchat.server.agent_run.schema import PlanError, _extract_json_object

from .schemas import (
    COMPLEXITY_DIRECT,
    INTENT_GENERAL,
    MCP_CANDIDATE,
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
        return validate_route_result(
            obj, ctx.available_tools, mcp_enabled=ctx.use_mcp
        )

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
            lines.append("- （当前没有可用工具；除下面 MCP 场景外所有请求都应 path=direct）")
        # MCP 工具的真实名称必须连上 MCP 服务才知道（连接表不缓存工具清单），
        # 路由阶段无法枚举候选，只能让路由器输出保留候选名 mcp，由智能对话
        # 总图的执行分支建立连接后换成真实工具。以前这里只有一句「还会额外
        # 包含 MCP 工具（无需为它们选择候选）」，路由器既不能选也不该选，
        # 结果需要 MCP 的请求被当成无候选工具回退直接回答。
        mcp_rule = ""
        mcp_example = ""
        if ctx.use_mcp:
            lines.append(
                "- 额外可用：MCP 工具（操作浏览器、控制本机外部程序或第三方服务）。"
                f"它们的名称此刻未知，需要时 candidate_tools 写 \"{MCP_CANDIDATE}\"，"
                "禁止编造具体 MCP 工具名。"
            )
            mcp_rule = (
                "- 需要操作浏览器 / 读取或填写网页 / 控制本机外部程序 / 调用第三方服务，"
                f"而上面清单里没有对应工具时：path=agent_run, intent=complex, "
                f'complexity=multi, candidate_tools 只写 "{MCP_CANDIDATE}"。\n'
            )
            mcp_example = (
                "- 「打开浏览器看看某官网今天的公告并总结」→ "
                '{"path": "agent_run", "intent": "complex", "complexity": "multi", '
                f'"candidate_tools": ["{MCP_CANDIDATE}"], "confidence": 0.85, '
                '"summary": "需要操作浏览器读取网页"}\n'
            )
        return (
            "你是本地助手平台的意图路由器。判断用户当前请求应「直接回答」还是"
            "交给 Agent 执行闭环（规划 → 逐步调用工具 → 观察 → 最终回答）。\n\n"
            + "\n".join(lines)
            + "\n\n判断规则：\n"
            "- 问候、闲聊、改写润色、翻译、常识解释等明显不需要工具的请求：path=direct, intent=general。\n"
            "- 明确查询本地知识库 / 论文语料 / 自有资料的内容：path=agent_run, intent=kb_search, candidate_tools 包含 search_local_knowledgebase（若可用）。\n"
            "- 用户明确要求联网搜索 / 上网查 / 搜索互联网 / 查最新信息（如出现「联网搜索」「搜索一下」「搜一下」「查一下最新/最近」「最新进展/新闻/发布」等措辞）：path=agent_run, intent=single_tool（若还需多步则 complex），candidate_tools 包含 search_internet（若可用）。\n"
            "- 含「最新 / 最近发布 / 近期进展」等时效性措辞的问题，应优先按联网搜索处理（search_internet），不要归为 unknown。\n"
            "- 提到「搜索/查一下」但未指明来源时：涉及本地知识库 / 论文语料 / 自有资料 → search_local_knowledgebase；涉及网上 / 互联网 / 最新 → search_internet。\n"
            "- 单一明确能力且与某个工具匹配（天气、计算等）：path=agent_run, intent=single_tool, candidate_tools 只放相关工具。\n"
            "- 需要检索、比较、汇总等连续动作的多步骤请求：path=agent_run, intent=complex, candidate_tools 列出合理的一组相关工具。\n"
            f"{mcp_rule}"
            "- 只有当请求信息完全不足、没有任何工具线索时才：path=direct, intent=unknown（不要猜测）。\n\n"
            "示例：\n"
            "- 「帮我联网搜索一下DeepSeek最近发布的推理优化技术」→ {\"path\": \"agent_run\", \"intent\": \"single_tool\", \"complexity\": \"single\", \"candidate_tools\": [\"search_internet\"], \"confidence\": 0.95, \"summary\": \"用户明确要求联网搜索最新技术\"}\n"
            "- 「搜索互联网上关于投机解码的最新论文」→ {\"path\": \"agent_run\", \"intent\": \"single_tool\", \"complexity\": \"single\", \"candidate_tools\": [\"search_internet\"], \"confidence\": 0.9, \"summary\": \"明确要求联网搜索论文\"}\n"
            "- 「在知识库里查一下MTP相关的内容」→ {\"path\": \"agent_run\", \"intent\": \"kb_search\", \"complexity\": \"single\", \"candidate_tools\": [\"search_local_knowledgebase\"], \"confidence\": 0.9, \"summary\": \"明确指定检索本地知识库\"}\n"
            "- 「什么是量子计算」→ {\"path\": \"direct\", \"intent\": \"general\", \"complexity\": \"direct\", \"candidate_tools\": [], \"confidence\": 0.9, \"summary\": \"常识问题，直接回答\"}\n\n"
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


class FastJevIntentProvider(IntentProvider):
    """Use the local FastJev System One service for intent/tool classification."""

    name = "fastjev"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 12.0,
        tool_threshold: float = 0.0,
        promotion_threshold: float = 0.9,
        http_client: Any = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.tool_threshold = min(1.0, max(0.0, float(tool_threshold)))
        self.promotion_threshold = min(1.0, max(0.0, float(promotion_threshold)))
        self.http_client = http_client or httpx.Client(trust_env=False)

    def route(self, ctx: RouteContext) -> RouteResult:
        payload, offered_tools = self._payload(ctx)
        try:
            response = self.http_client.post(
                f"{self.base_url}/v1/systemone",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RouteError(f"FastJev 服务调用失败：{exc}") from exc

        try:
            answers = data["answers"]
            intent_answer = answers["intent"]
            intent = str(intent_answer["choice"])
            probabilities = intent_answer["probabilities"]
            confidence = float(probabilities[intent])
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteError(f"FastJev 返回结构非法：{exc}") from exc

        if intent in ("general", "unknown"):
            # 工具头提升：意图头会把"明确要求调用工具"的请求（如"联网搜索
            # X"）系统性误判为 general，而同一请求的 primary_tool 头往往给
            # 对应工具打出高分（如 search_internet 0.96）。工具头得分达到
            # 阈值时信任工具头、提升为 Agent 调用，避免真实工具请求被漏掉；
            # 未达阈值维持直接回答，避免闲聊被提升进 Agent。
            primary = self._tool_answer(answers, "primary_tool")
            if (
                primary is not None
                and primary[0] in offered_tools
                and primary[1] >= self.promotion_threshold
            ):
                tool_name, score = primary
                result = RouteResult(
                    path="agent_run",
                    intent="single_tool",
                    complexity="single",
                    candidate_tools=[tool_name],
                    confidence=score,
                    summary=(
                        f"意图头判为{intent}，但工具头给 {tool_name} "
                        f"高分 {score:.2f}，提升为 Agent 工具调用"
                    ),
                    provider=self.name,
                    source=self.name,
                )
                return validate_route_result(
                    result, ctx.available_tools, mcp_enabled=ctx.use_mcp
                )
            return RouteResult(
                path=PATH_DIRECT,
                intent=intent,
                complexity=COMPLEXITY_DIRECT,
                confidence=confidence,
                summary="FastJev 判断为普通对话" if intent == "general" else "FastJev 无法可靠判断意图",
                provider=self.name,
                source=self.name,
            )

        selected: List[str] = []
        tool_scores: Dict[str, float] = {}
        for question_id in ("primary_tool", "secondary_tool"):
            picked = self._tool_answer(answers, question_id)
            if picked is None:
                continue
            tool_name, score = picked
            if (
                tool_name in offered_tools
                and tool_name not in selected
                and score >= self.tool_threshold
            ):
                selected.append(tool_name)
                tool_scores[tool_name] = score
        if intent == "kb_search" and "search_local_knowledgebase" in ctx.available_tools:
            if "search_local_knowledgebase" not in selected:
                selected.insert(0, "search_local_knowledgebase")

        if intent == "kb_search":
            normalized_intent = "kb_search"
        elif intent == "single_tool":
            normalized_intent = "single_tool"
            if len(selected) > 1:
                selected = [max(selected, key=lambda name: tool_scores.get(name, 0.0))]
        else:
            normalized_intent = "complex"

        # 工具头置信度托底：意图头对"明确要求调用工具"的请求系统性低估
        # （如"搜索互联网上关于X的最新论文"意图头只给 complex 0.46，而工具头
        # 给 search_internet 0.96），低分会被路由器低置信度护栏降级为直接
        # 回答。工具头对某具体工具给出达到提升阈值的得分时，用工具头得分
        # 托底整体置信度；工具头未达阈值时保持意图头原分，交由护栏处置。
        backed_confidence = max(
            confidence,
            max(
                (
                    score
                    for score in tool_scores.values()
                    if score >= self.promotion_threshold
                ),
                default=0.0,
            ),
        )

        result = RouteResult(
            path="agent_run",
            intent=normalized_intent,
            complexity="multi" if normalized_intent == "complex" and len(selected) > 1 else "single",
            candidate_tools=selected,
            confidence=backed_confidence,
            summary="FastJev 判断需要 Agent 调用工具",
            provider=self.name,
            source=self.name,
        )
        return validate_route_result(
            result, ctx.available_tools, mcp_enabled=ctx.use_mcp
        )

    @staticmethod
    def _tool_answer(answers: Dict[str, Any], question_id: str) -> Optional[tuple[str, float]]:
        """读取某工具题（primary_tool / secondary_tool）的选中工具及其概率。

        缺失、格式非法或选择 none 时返回 None。
        """
        try:
            answer = answers[question_id]
            tool_name = str(answer["choice"])
            score = float(answer["probabilities"][tool_name])
        except (KeyError, TypeError, ValueError):
            return None
        if tool_name == "none":
            return None
        return tool_name, score

    def _payload(self, ctx: RouteContext) -> tuple[Dict[str, Any], set[str]]:
        questions: Dict[str, Any] = {
            "intent": {
                "type": "choice",
                "instructions": "判断用户当前请求最适合采用哪种处理方式。",
                "criteria": {
                    "general": "普通对话、改写、翻译或无需工具即可回答的问题",
                    "kb_search": "需要检索本地知识库、论文语料或用户已有资料",
                    "single_tool": "需要调用一个明确工具完成的任务",
                    "complex": "需要多个工具或检索、比较、汇总等连续步骤的复杂任务",
                    "unknown": "信息不足，无法可靠判断处理方式",
                },
            }
        }
        tool_criteria: Dict[str, str] = {"none": "不需要使用任何工具"}
        for name, tool in list((ctx.available_tools or {}).items())[:15]:
            description = str(getattr(tool, "description", "") or "").splitlines()[0][:160]
            tool_criteria[name] = description or name
        if ctx.use_mcp:
            # 与 LLM provider 同一约定：MCP 工具名要连上服务才知道，路由阶段
            # 无法枚举，所以把保留候选名 mcp 作为一个候选项交给分类器。工具题的
            # criteria 本来就按当前配置动态生成，多一项不改变请求协议；该候选
            # 不参与注册表校验（见 schemas.validate_route_result 的 mcp_enabled）。
            tool_criteria[MCP_CANDIDATE] = (
                "需要操作浏览器、读取或填写网页、控制本机外部程序或第三方服务"
            )
        if len(tool_criteria) > 1:
            questions["primary_tool"] = {
                "type": "choice",
                "instructions": "选择完成用户请求最需要的主工具；若无需工具则选择 none。",
                "criteria": tool_criteria,
            }
            questions["secondary_tool"] = {
                "type": "choice",
                "instructions": "选择复杂任务还需要的第二个工具；若无需第二个工具则选择 none。",
                "criteria": tool_criteria,
            }
        return {
            "model": self.model,
            "state": {
                "query": ctx.query,
                "recent_history": list(ctx.history[-6:]),
                "mcp_enabled": bool(ctx.use_mcp),
            },
            "questions": questions,
        }, set(tool_criteria) - {"none"}


class FallbackIntentProvider(IntentProvider):
    """Try the local classifier first, then preserve the existing LLM path."""

    def __init__(self, primary: IntentProvider, fallback: Optional[IntentProvider]):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name

    def route(self, ctx: RouteContext) -> RouteResult:
        try:
            result = self.primary.route(ctx)
            result.provider = self.primary.name
            result.source = self.primary.name
            return result
        except Exception:
            if self.fallback is None:
                raise
            result = self.fallback.route(ctx)
            result.provider = self.fallback.name
            result.source = self.fallback.name
            return result


__all__ = [
    "RouteContext",
    "IntentProvider",
    "LlmIntentProvider",
    "FastJevIntentProvider",
    "FallbackIntentProvider",
    "rule_fast_path",
]
