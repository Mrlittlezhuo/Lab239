"""规划器（任务 011）。

``Planner`` 是运行器依赖的最小协议：

- ``plan(ctx) -> Plan``：根据目标/上下文/可用工具生成结构化计划；
- ``decide(ctx, results) -> Decision``：根据已完成步骤的观察结果判断
  answer / replan / fail。

模型输出无法解析或校验失败时必须抛 ``PlanError``——运行器以此让运行
可控失败（failed），绝不无限循环。

``LLMPlanner`` 是生产实现：把规划/判断收敛为"只输出一个 JSON 对象"的
提示词，并用现有 ``get_ChatOpenAI`` 构建的非流式模型实例调用
（不新增依赖、不复用聊天流）。测试中用确定性假规划器替代。

小模型（如 7B）会偶发输出不合规计划（仅 summary 的空计划、非 JSON、
引用未知工具）。``LLMPlanner.plan`` 对无效输出追加明确纠正提示**重试
一次**；仍无效才抛 ``PlanError``——保持有限次、可控失败，绝不无限循环。
"""
from __future__ import annotations

from typing import Dict, List

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
    的模型实例（如 ``get_ChatOpenAI(streaming=False)`` 的结果）。
    """

    def __init__(self, llm):
        self.llm = llm

    def _invoke(self, system: str, user: str) -> str:
        try:
            resp = self.llm.invoke(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}]
            )
        except PlanError:
            raise
        except Exception as exc:  # 模型调用失败同样要可控失败
            raise PlanError(f"模型调用失败：{exc}") from exc
        content = getattr(resp, "content", None)
        if content is None:
            content = resp
        return str(content or "")

    def plan(self, ctx: PlanContext) -> Plan:
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
            raw = _extract_json_object(self._invoke(PLAN_SYSTEM_PROMPT, user))
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
            raw = _extract_json_object(self._invoke(PLAN_SYSTEM_PROMPT, retry_user))
            return parse_plan(raw, set(ctx.tools.keys()), ctx.max_steps)

    def decide(self, ctx: PlanContext, results: List[StepResult]) -> Decision:
        remaining = max(1, ctx.max_steps - len(results))
        user = DECIDE_USER_TEMPLATE.format(
            goal=ctx.goal,
            tools=format_tools_description(ctx.tools),
            results=format_observations(results),
            remaining=remaining,
        )
        raw = _extract_json_object(self._invoke(DECIDE_SYSTEM_PROMPT, user))
        return parse_decision(raw, set(ctx.tools.keys()), remaining)


__all__ = ["LLMPlanner"]