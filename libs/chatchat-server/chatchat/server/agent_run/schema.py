"""单 Agent 闭环的数据结构与校验（任务 011）。

- ``PlanStep`` / ``Plan``：规划器输出的结构化计划；
- ``Decision``：执行判断（answer / replan / fail）；
- ``PlanError``：模型输出无法解析或校验失败——调用方必须以此可控失败，
  不得无限重试/循环。

计划/判断的 JSON 契约（规划器必须只输出一个 JSON 对象）::

    {"summary": "...", "steps": [{"goal": "...", "tool": "...", "args": {}}],
     "direct_answer": ""}
    {"action": "answer", "answer": "..."}
    {"action": "replan", "steps": [...]}
    {"action": "fail", "reason": "..."}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 观察结果送入后续决策时的截断长度（完整结果仍会传给工具链，不因此丢失）
OBSERVATION_MAX_CHARS = 1500


class PlanError(Exception):
    """规划/判断输出无效（解析失败或校验不通过）。

    调用方（运行器）捕获后必须让运行以 ``failed`` 收敛并给出明确错误，
    不得无限循环重试。
    """


@dataclass
class PlanStep:
    """计划中的一个可执行步骤。"""

    goal: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"goal": self.goal, "tool": self.tool, "args": self.args}


@dataclass
class Plan:
    """结构化执行计划。

    ``direct_answer`` 非空时表示目标无需工具，可直接给出最终回答
    （此时 ``steps`` 应为空）。
    """

    summary: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    direct_answer: str = ""

    @property
    def is_direct_answer(self) -> bool:
        return bool(self.direct_answer and not self.steps)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
            "direct_answer": self.direct_answer,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class Decision:
    """执行后的判断：answer（给出最终答案）/ replan（补充步骤）/ fail。"""

    action: str  # "answer" | "replan" | "fail"
    answer: str = ""
    steps: List[PlanStep] = field(default_factory=list)
    reason: str = ""


@dataclass
class StepResult:
    """一步执行后的观察结果（供后续决策使用，含完整工具输出）。"""

    seq: int
    goal: str
    tool: str
    args: Dict[str, Any]
    output: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def observation(self) -> str:
        if self.error:
            return f"[错误] {self.error[:OBSERVATION_MAX_CHARS]}"
        out = self.output or ""
        if len(out) > OBSERVATION_MAX_CHARS:
            out = out[:OBSERVATION_MAX_CHARS] + "…（已截断）"
        return out or "[空结果]"


@dataclass
class PlanContext:
    """规划/判断所需的全部上下文（会话历史与记忆由调用方组装）。"""

    goal: str
    tools: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, str]] = field(default_factory=list)
    memory_messages: List[Dict[str, str]] = field(default_factory=list)
    max_steps: int = 8


def truncate_text(value: Any, limit: int) -> str:
    """持久化/展示用的安全截断；空值返回空串。"""
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + "…（已截断）"


def _extract_json_object(text: str) -> Any:
    """从模型输出中提取 JSON 对象：整体解析优先，失败则取首尾花括号区间。"""
    if not text:
        raise PlanError("模型输出为空")
    s = text.strip()
    candidates = [s]
    # 去掉常见的 ```json ... ``` 代码块包裹
    if s.startswith("```"):
        body = s.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        candidates.insert(0, body.strip())
        first, last = s.find("{"), s.rfind("}")
        if 0 <= first < last:
            candidates.append(s[first : last + 1])
    else:
        first, last = s.find("{"), s.rfind("}")
        if 0 <= first < last and first != 0:
            candidates.append(s[first : last + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    raise PlanError("无法从模型输出解析出 JSON 对象")


def _validate_steps(
    raw_steps: Any,
    available_tools: set,
    max_steps: int,
) -> List[PlanStep]:
    if not isinstance(raw_steps, list):
        raise PlanError("计划 steps 必须是数组")
    steps: List[PlanStep] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            raise PlanError(f"计划步骤必须是对象：{item!r}")
        goal = str(item.get("goal") or "").strip()
        if not goal:
            raise PlanError("计划步骤缺少 goal（步骤目标）")
        tool = str(item.get("tool") or "").strip()
        if not tool:
            # 无需工具的问题应使用 direct_answer，而不是无工具步骤
            raise PlanError(f"计划步骤缺少 tool（无需工具时请用 direct_answer）：{goal}")
        if not available_tools or tool not in available_tools:
            raise PlanError(f"计划步骤引用了未知工具：{tool}")
        args = item.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise PlanError(f"计划步骤 {tool or goal!r} 的 args 必须是对象")
        steps.append(PlanStep(goal=goal, tool=tool, args=args))
    if len(steps) > max_steps:
        # 截断到上限，保证运行有界；被截断的步骤可由后续 replan 重新提出
        steps = steps[:max_steps]
    return steps


def parse_plan(raw: Any, available_tools: set, max_steps: int) -> Plan:
    """解析并校验规划器输出为 ``Plan``；无效时抛 ``PlanError``。"""
    if not isinstance(raw, dict):
        raise PlanError("计划必须是 JSON 对象")
    direct = str(raw.get("direct_answer") or "").strip()
    steps = _validate_steps(raw.get("steps") or [], available_tools, max_steps)
    summary = str(raw.get("summary") or "").strip()
    if not steps and not direct:
        raise PlanError("计划为空：既没有步骤也没有 direct_answer")
    if steps and direct:
        # 同时给出步骤与直接答案时以步骤为准（先执行工具再综合回答）
        direct = ""
    return Plan(summary=summary, steps=steps, direct_answer=direct)


def parse_decision(raw: Any, available_tools: set, max_steps: int) -> Decision:
    """解析并校验执行判断；无效时抛 ``PlanError``。"""
    if not isinstance(raw, dict):
        raise PlanError("判断结果必须是 JSON 对象")
    action = str(raw.get("action") or "").strip().lower()
    if action == "answer":
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            raise PlanError("判断为 answer 但缺少 answer 内容")
        return Decision(action="answer", answer=answer)
    if action == "fail":
        reason = str(raw.get("reason") or "").strip() or "Agent 判定无法完成任务"
        return Decision(action="fail", reason=reason)
    if action == "replan":
        steps = _validate_steps(raw.get("steps") or [], available_tools, max_steps)
        if not steps:
            raise PlanError("replan 必须给出至少一个步骤")
        return Decision(action="replan", steps=steps)
    raise PlanError(f"未知判断动作：{action!r}（应为 answer/replan/fail）")


def format_tools_description(tools: Dict[str, Any], limit_per_tool: int = 300) -> str:
    """把工具名 + 描述 + 参数 schema 渲染成给规划器的文本。"""
    lines = []
    for name, tool in tools.items():
        desc = str(getattr(tool, "description", "") or "").strip()
        if len(desc) > limit_per_tool:
            desc = desc[:limit_per_tool] + "…"
        schema = getattr(tool, "args_schema", None)
        args_desc = ""
        if schema is not None:
            try:
                shape = schema.schema()
                args_desc = "，参数：" + truncate_text(
                    json.dumps(shape, ensure_ascii=False), 200
                )
            except Exception:
                args_desc = ""
        lines.append(f"- {name}：{desc}{args_desc}" if desc else f"- {name}")
    return "\n".join(lines) if lines else "（无可用工具）"


def format_history(history: List[Dict[str, str]], limit: int = 2000) -> str:
    """渲染最近对话（超长截断）。"""
    if not history:
        return "（无历史对话）"
    lines = [f"{m.get('role', 'user')}: {truncate_text(m.get('content', ''), 500)}"
             for m in history[-8:]]
    text = "\n".join(lines)
    return truncate_text(text, limit)


def format_observations(results: List[StepResult], limit: int = 6000) -> str:
    """渲染已完成步骤的观察结果（每步截断，总量限长）。"""
    if not results:
        return "（尚未执行任何步骤）"
    lines = []
    for r in results:
        state = "成功" if r.ok else "失败"
        lines.append(f"步骤 {r.seq}（{state}）{r.tool or r.goal}：{r.observation()}")
    return truncate_text("\n".join(lines), limit)


__all__ = [
    "OBSERVATION_MAX_CHARS",
    "PlanError",
    "PlanStep",
    "Plan",
    "Decision",
    "StepResult",
    "PlanContext",
    "truncate_text",
    "parse_plan",
    "parse_decision",
    "format_tools_description",
    "format_history",
    "format_observations",
]