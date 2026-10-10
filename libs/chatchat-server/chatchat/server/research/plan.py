"""研究计划的结构、校验与 LLM 生成（任务 017 阶段 C）。

研究计划 = 摘要 + 子问题（每个子问题带检索式）+ 选用的来源。计划必须
校验为结构化子问题与检索式，解析失败时抛 ``ResearchPlanError`` 可控
失败（任务 017 §5.3），绝不无限重试。

``ResearchPlanner`` 沿用任务 011 ``LLMPlanner`` 的做法：非流式
``get_ChatOpenAI``（``local_wrap=False`` 直连模型平台，避免自引用
死锁）、"只输出一个 JSON 对象"提示词、无效输出带纠正提示重试一次。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from chatchat.server.agent_run.schema import _extract_json_object

MAX_SUB_QUESTIONS = 6
MAX_QUERIES_PER_QUESTION = 4


class ResearchPlanError(Exception):
    """计划生成/解析/校验失败（调用方让运行以 failed 收敛）。"""


@dataclass
class SubQuestion:
    question: str
    queries: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"question": self.question, "queries": list(self.queries)}


@dataclass
class ResearchPlan:
    summary: str = ""
    sub_questions: List[SubQuestion] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def all_queries(self) -> List[str]:
        """全部检索式（去重、保序）。"""
        seen: set = set()
        out: List[str] = []
        for sq in self.sub_questions:
            for q in sq.queries:
                if q and q not in seen:
                    seen.add(q)
                    out.append(q)
        return out

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "sub_questions": [s.to_dict() for s in self.sub_questions],
            "sources": list(self.sources),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def parse_research_plan(raw: Any, available_sources: set) -> ResearchPlan:
    """解析并校验计划 JSON；无效时抛 ``ResearchPlanError``。

    - 子问题至少一个，每个子问题至少一条检索式；
    - 来源必须都在可用来源集合内（未知来源直接拒绝，避免静默漏检）；
    - 数量上限：子问题 ``MAX_SUB_QUESTIONS``、每子问题检索式
      ``MAX_QUERIES_PER_QUESTION``（超出截断，保证有界）。
    """
    if not isinstance(raw, dict):
        raise ResearchPlanError("计划必须是 JSON 对象")
    summary = str(raw.get("summary") or "").strip()

    raw_sq = raw.get("sub_questions")
    if not isinstance(raw_sq, list) or not raw_sq:
        raise ResearchPlanError("计划缺少 sub_questions（子问题列表）")
    sub_questions: List[SubQuestion] = []
    for item in raw_sq[:MAX_SUB_QUESTIONS]:
        if not isinstance(item, dict):
            raise ResearchPlanError(f"子问题必须是对象：{item!r}")
        question = str(item.get("question") or "").strip()
        if not question:
            raise ResearchPlanError(f"子问题缺少 question：{item!r}")
        raw_queries = item.get("queries")
        if not isinstance(raw_queries, list):
            raise ResearchPlanError(f"子问题 {question!r} 的 queries 必须是数组")
        queries = [str(q).strip() for q in raw_queries[:MAX_QUERIES_PER_QUESTION] if str(q).strip()]
        if not queries:
            raise ResearchPlanError(f"子问题 {question!r} 至少需要一条检索式")
        sub_questions.append(SubQuestion(question=question[:500], queries=queries))
    if not sub_questions:
        raise ResearchPlanError("计划为空：没有有效子问题")

    raw_sources = raw.get("sources")
    if raw_sources is None:
        sources = sorted(available_sources)
    elif not isinstance(raw_sources, list):
        raise ResearchPlanError("sources 必须是数组")
    else:
        sources = []
        for s in raw_sources:
            name = str(s).strip()
            if not name:
                continue
            if available_sources and name not in available_sources:
                raise ResearchPlanError(f"计划引用了不可用的来源：{name}")
            if name not in sources:
                sources.append(name)
    if not sources:
        raise ResearchPlanError("计划没有可用来源")
    return ResearchPlan(summary=summary, sub_questions=sub_questions, sources=sources)


PLAN_SYSTEM_PROMPT = """你是论文研究助手的研究计划器，负责把研究问题拆成可检索的子问题。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。"""

PLAN_USER_TEMPLATE = """可用论文来源：
{sources}

研究问题：
{question}

研究范围/约束：
{scope}

输出 JSON 格式（严格遵守）：
{{"summary": "一句话说明研究计划", "sub_questions": [{{"question": "子问题", "queries": ["英文检索式", "中文检索式"]}}], "sources": ["来源名"]}}

约束：
- sub_questions 至少 1 个、最多 {max_sq} 个；每个子问题至少 1 条、最多 {max_q} 条检索式；
- 检索式必须是可直接用于论文检索的短语（优先英文学术用语）；
- sources 只能从可用论文来源中选择。"""


class ResearchPlanner:
    """生产计划器：用现有非流式模型生成并校验研究计划。"""

    def __init__(self, llm: Any):
        self.llm = llm

    def _invoke(self, prompt: str) -> Any:
        resp = self.llm.invoke(prompt)
        return getattr(resp, "content", resp)

    def generate(
        self,
        research_question: str,
        scope: str,
        available_sources: set,
    ) -> ResearchPlan:
        from chatchat.server.agent_run.schema import PlanError

        prompt = PLAN_USER_TEMPLATE.format(
            sources="、".join(sorted(available_sources)) or "（无）",
            question=research_question,
            scope=scope or "（未指定）",
            max_sq=MAX_SUB_QUESTIONS,
            max_q=MAX_QUERIES_PER_QUESTION,
        )
        messages = [("system", PLAN_SYSTEM_PROMPT), ("user", prompt)]
        first = str(self._invoke(messages))
        try:
            raw = _extract_json_object(first)
            return parse_research_plan(raw, available_sources)
        except (PlanError, ResearchPlanError) as exc:
            # 与 LLMPlanner 一致：一次带纠正提示的重试，仍无效则可控失败
            retry_messages = messages + [
                ("assistant", first),
                ("user", f"上一次输出不符合要求（{exc}）。请只输出符合 JSON 格式要求的对象。"),
            ]
            raw = _extract_json_object(str(self._invoke(retry_messages)))
            return parse_research_plan(raw, available_sources)


def build_default_planner(model_name: Optional[str] = None) -> ResearchPlanner:
    """按现有模型配置构建计划器（测试中替换为假计划器）。

    与 ``agent_run.service._build_planner`` 同理：必须 ``local_wrap=False``
    直连模型平台，避免经本服务 /v1 的自引用死锁。
    """
    from chatchat.server.utils import get_ChatOpenAI, get_default_llm
    from chatchat.settings import Settings

    name = model_name or get_default_llm()
    llm = get_ChatOpenAI(
        model_name=name,
        streaming=False,
        local_wrap=False,
        max_tokens=Settings.model_settings.MAX_TOKENS,
    )
    return ResearchPlanner(llm)


__all__ = [
    "MAX_SUB_QUESTIONS",
    "MAX_QUERIES_PER_QUESTION",
    "ResearchPlanError",
    "SubQuestion",
    "ResearchPlan",
    "parse_research_plan",
    "ResearchPlanner",
    "build_default_planner",
]
