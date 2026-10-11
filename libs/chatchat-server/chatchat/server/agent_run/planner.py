"""规划器（任务 011）。

``Planner`` 是运行器依赖的最小协议：

- ``plan(ctx) -> Plan``：根据目标/上下文/可用工具生成结构化计划；
- ``decide(ctx, results) -> Decision``：根据已完成步骤的观察结果判断
  answer / replan / fail。

模型输出无法解析或校验失败时必须抛 ``PlanError``——运行器以此让运行
可控失败（failed），绝不无限循环。

``LLMPlanner`` 是生产实现：把规划/判断收敛为"只输出一个 JSON 对象"的
提示词，并用现有 ``get_ChatOpenAI`` 构建的模型实例调用（不新增依赖、不
复用聊天流）。测试中用确定性假规划器替代。

规划/判断的输出是 JSON，用户要看的正文在 JSON 里面。为了让 Agent 任务
"边生成边出字"，``LLMPlanner`` 支持 ``on_chunk`` 回调（逐分片把模型原始
输出交出去），``StreamingPlanner`` 再把它转成正文预览推给运行事件流；
不传 ``on_chunk`` 时行为与原来完全一致（非流式运行、测试都不受影响）。

小模型（如 7B）会偶发输出不合规计划（仅 summary 的空计划、非 JSON、
引用未知工具）。``LLMPlanner.plan`` 对无效输出追加明确纠正提示**重试
一次**；仍无效才抛 ``PlanError``——保持有限次、可控失败，绝不无限循环。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from chatchat.server.agent_run.answer_stream import AnswerStreamExtractor
from chatchat.server.agent_run.schema import (
    Decision,
    Plan,
    PlanContext,
    PlanError,
    StepResult,
    _extract_json_object,
    format_history,
    format_observations,
    format_tools_description,
    parse_decision,
    parse_plan,
)

PLAN_SYSTEM_PROMPT = """你是一个任务规划器，负责把用户目标拆成可执行的结构化计划。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。
只使用下面列出的工具；目标无需工具时，把完整最终答案写入 direct_answer 并令 steps 为空。
steps 与 direct_answer 不可同时为空：要么至少给出一个步骤，要么给出面向用户的完整答案。"""

PLAN_USER_TEMPLATE = """可用工具：
{tools}

最近对话（仅供理解上下文，不要重复回答）：
{history}

{memory}用户目标：
{goal}

输出 JSON 格式（严格遵守）：
{{"summary": "一句话说明完成计划", "steps": [{{"goal": "该步骤做什么（用户可理解）", "tool": "工具名", "args": {{}}}}], "direct_answer": ""}}

约束：
- 每个 step 的 tool 必须是可用工具名之一，args 是该工具参数的 JSON 对象；
- steps 不超过 {max_steps} 个；
- 只要任一可用工具能推进用户目标（如需要联网查询、检索知识库、执行操作），steps 必须包含至少一个步骤，不得只用 summary 或 direct_answer 代替工具执行；
- 仅当目标确实无需任何工具即可完成（常识问答、简单计算、解释概念）时，才把完整最终答案写入 direct_answer 并令 steps 为空；
- steps 与 direct_answer 不可同时为空：即使无法完成任务，也必须在 direct_answer 中写一段面向用户的完整说明。
"""

DECIDE_SYSTEM_PROMPT = """你是一个任务执行判断器，负责根据已完成步骤的观察结果决定任务如何收尾。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。"""

DECIDE_USER_TEMPLATE = """用户目标：
{goal}

可用工具：
{tools}

已完成步骤与观察结果：
{results}

按以下三选一输出 JSON：
1. 信息已足够形成最终答案：
   {{"action": "answer", "answer": "面向用户的最终完整回答（直接针对用户目标，可综合观察结果）"}}
2. 还需要更多工具步骤：
   {{"action": "replan", "steps": [{{"goal": "...", "tool": "...", "args": {{}}}}]}}
3. 无法继续完成任务：
   {{"action": "fail", "reason": "明确说明失败原因"}}

约束：replan 的 steps 不超过 {remaining} 个，tool 必须是可用工具名之一；answer 必须完整回答用户目标。
"""


class LLMPlanner:
    """基于现有 LLM 的生产规划器。

    ``llm`` 为任意支持 ``invoke(list[dict]) -> 含 .content 的对象/str``
    的模型实例（如 ``get_ChatOpenAI`` 的结果）；需要边生成边出字时还应有
    ``stream(list[dict])``，且该实例必须以 ``streaming=True`` 构建，否则
    langchain 会把 ``stream()`` 退化成一次性返回整段。
    """

    #: 生产实现支持 ``on_chunk`` 流式旁路；假规划器没有该属性，
    #: ``StreamingPlanner`` 据此退回普通调用（不改变其行为）
    supports_stream = True

    def __init__(self, llm):
        self.llm = llm

    def _messages(self, system: str, user: str) -> List[Dict[str, str]]:
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _invoke(self, system: str, user: str,
                on_chunk: Optional[Callable[[str], None]] = None) -> str:
        messages = self._messages(system, user)
        if on_chunk is not None:
            streamed = getattr(self.llm, "stream", None)
            if streamed is not None:
                return self._invoke_streaming(messages, streamed, on_chunk)
        try:
            resp = self.llm.invoke(messages)
        except PlanError:
            raise
        except Exception as exc:  # 模型调用失败同样要可控失败
            raise PlanError(f"模型调用失败：{exc}") from exc
        content = getattr(resp, "content", None)
        if content is None:
            content = resp
        return str(content or "")

    def _invoke_streaming(
        self,
        messages: List[Dict[str, str]],
        streamed: Callable[..., Any],
        on_chunk: Callable[[str], None],
    ) -> str:
        """流式调用：每个分片立刻交给 ``on_chunk``，同时聚合成完整文本。

        解析与校验仍在收完之后做，流式只改变"什么时候能看到字"。
        """
        parts: List[str] = []
        try:
            for piece in streamed(messages):
                content = getattr(piece, "content", None)
                if content is None:
                    content = piece
                text = str(content or "")
                if not text:
                    continue
                parts.append(text)
                on_chunk(text)
        except PlanError:
            raise
        except Exception as exc:
            raise PlanError(f"模型调用失败：{exc}") from exc
        return "".join(parts)

    def plan(self, ctx: PlanContext,
             on_chunk: Optional[Callable[[str], None]] = None) -> Plan:
        memory_text = ""
        if ctx.memory_messages:
            memory_text = "长期记忆/摘要（背景信息）：\n" + "\n".join(
                f"- {m.get('content', '')}" for m in ctx.memory_messages[:5]
            ) + "\n\n"
        user = PLAN_USER_TEMPLATE.format(
            tools=format_tools_description(ctx.tools),
            history=format_history(ctx.history),
            memory=memory_text,
            goal=ctx.goal,
            max_steps=ctx.max_steps,
        )
        try:
            raw = _extract_json_object(
                self._invoke(PLAN_SYSTEM_PROMPT, user, on_chunk)
            )
            return parse_plan(raw, set(ctx.tools.keys()), ctx.max_steps)
        except PlanError as exc:
            # 小模型偶发输出不合规（仅 summary 的空计划、非 JSON、引用未知
            # 工具）：追加明确纠正提示重试一次；仍无效则保持可控失败。
            retry_user = user + (
                "\n\n[纠正] 你上一次的输出无效："
                f"{exc}。请重新输出一个 JSON 对象："
                "只要任一可用工具能推进用户目标，steps 必须包含至少一个步骤"
                "（选择最匹配的工具）；仅当任何工具都无法推进目标时，才把"
                "完整说明写入 direct_answer。steps 与 direct_answer 不可"
                "同时为空。"
            )
            # 重试沿用同一个 on_chunk：已经预览过的字段不再重复推送
            # （每个字段只出一次），没出过字的字段仍可正常预览。
            raw = _extract_json_object(
                self._invoke(PLAN_SYSTEM_PROMPT, retry_user, on_chunk)
            )
            return parse_plan(raw, set(ctx.tools.keys()), ctx.max_steps)

    def decide(self, ctx: PlanContext, results: List[StepResult],
               on_chunk: Optional[Callable[[str], None]] = None) -> Decision:
        remaining = max(1, ctx.max_steps - len(results))
        user = DECIDE_USER_TEMPLATE.format(
            goal=ctx.goal,
            tools=format_tools_description(ctx.tools),
            results=format_observations(results),
            remaining=remaining,
        )
        raw = _extract_json_object(
            self._invoke(DECIDE_SYSTEM_PROMPT, user, on_chunk)
        )
        return parse_decision(raw, set(ctx.tools.keys()), remaining)


#: 规划输出里要提前出字的顶层字段 -> 预览阶段标签
PLAN_PREVIEW_KEYS = {"summary": "plan", "direct_answer": "answer"}
#: 判断输出里要提前出字的顶层字段 -> 预览阶段标签
DECIDE_PREVIEW_KEYS = {"answer": "answer"}


class StreamingPlanner:
    """给规划器套一层"边生成边出正文"的旁路（只在流式运行时套）。

    ``push(阶段, 文本增量)`` 由运行时提供，把增量投进运行事件流。内层
    规划器的返回值完全不变：预览只是把模型分片里的正文字段提前推出去，
    最终答案仍以解析并校验后的结构化结果为准。

    内层没有 ``supports_stream``（测试里的假规划器）时原样透传，行为与
    不套这层一模一样。
    """

    def __init__(self, inner: Any, push: Callable[[str, str], None]):
        self._inner = inner
        self._push = push

    def __getattr__(self, name: str) -> Any:
        # 私有属性不参与透传，避免 __init__ 之前的探测递归
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def _call(self, method: str, keys: Mapping[str, str], args: tuple):
        if not getattr(self._inner, "supports_stream", False):
            return getattr(self._inner, method)(*args)
        extractor = AnswerStreamExtractor(keys, self._push)
        try:
            return getattr(self._inner, method)(*args, on_chunk=extractor.feed)
        finally:
            extractor.close()

    def plan(self, ctx: PlanContext) -> Plan:
        return self._call("plan", PLAN_PREVIEW_KEYS, (ctx,))

    def decide(self, ctx: PlanContext, results: List[StepResult]) -> Decision:
        return self._call("decide", DECIDE_PREVIEW_KEYS, (ctx, results))


__all__ = ["LLMPlanner", "StreamingPlanner", "PLAN_PREVIEW_KEYS", "DECIDE_PREVIEW_KEYS"]
