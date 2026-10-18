"""论文比较与冲突检测（任务 018 阶段 B）。

铁律（任务 018 §5.2）：比较单元与冲突记录**必须关联具体证据卡片**，
不能只留下一段无法追溯的模型总结。因此：

- 模型必须引用给定证据的 id；引用不存在/不属于本项目的 id 一律丢弃；
- 比较单元的证据必须属于被比较的那篇论文；
- 冲突两侧都必须有证据，且两侧不得引用同一张卡片、必须涉及不同论文；
- 没有任何有效引用的结论直接拒绝，绝不落库。

模型可注入（测试用假 LLM），解析失败带纠正提示重试一次，沿用 017 的
有界重试风格。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from chatchat.server.agent_run.schema import _extract_json_object
from chatchat.server.model_capacity import CONSUMER_PAPER_COMPARE

MAX_COMPARISON_ITEMS = 40
MAX_CONFLICT_ITEMS = 10


class ComparisonError(Exception):
    """比较/冲突生成输出无法解析（调用方记录失败，不影响其他产物）。"""


@dataclass
class EvidenceRef:
    """可供模型引用的证据（已确定属于本项目/用户）。"""

    evidence_id: str
    paper_id: str
    paper_title: str
    evidence_type: str
    level: str
    summary: str
    quote: str
    sub_question_id: str = ""
    verified: bool = True

    def as_prompt_line(self, index: int) -> str:
        where = "摘要级" if self.level == "abstract" else "全文"
        mark = "" if self.verified else "（原文未核对）"
        return (
            f"[{index}] evidence_id={self.evidence_id} 论文={self.paper_title} "
            f"维度={self.evidence_type} 级别={where}{mark} "
            f"概括={self.summary[:120]} 原文={self.quote[:300]}"
        )


@dataclass
class ComparisonDraft:
    paper_id: str
    dimension_id: str
    content: str
    evidence_ids: List[str]


@dataclass
class ConflictDraft:
    topic: str
    description: str
    side_a: List[str]
    side_b: List[str]
    dimension_id: str = ""
    sub_question_id: str = ""


@dataclass
class ComparisonOutput:
    comparisons: List[ComparisonDraft] = field(default_factory=list)
    conflicts: List[ConflictDraft] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)


def _index(evidence: Sequence[EvidenceRef]) -> Dict[str, EvidenceRef]:
    return {e.evidence_id: e for e in evidence}


def parse_comparison_payload(
    raw: Any, evidence: Sequence[EvidenceRef]
) -> Tuple[List[ComparisonDraft], List[str]]:
    """解析比较结论；引用无效或跨论文的条目被拒绝。"""
    if not isinstance(raw, dict):
        raise ComparisonError("比较输出必须是 JSON 对象")
    items = raw.get("comparisons")
    if items is None:
        return [], []
    if not isinstance(items, list):
        raise ComparisonError("comparisons 必须是数组")
    index = _index(evidence)
    drafts: List[ComparisonDraft] = []
    rejected: List[str] = []
    for pos, item in enumerate(items[:MAX_COMPARISON_ITEMS]):
        if not isinstance(item, dict):
            rejected.append(f"第 {pos + 1} 条比较不是对象，已丢弃")
            continue
        paper_id = str(item.get("paper_id") or "").strip()
        dimension_id = str(item.get("dimension_id") or "").strip()
        content = str(item.get("content") or "").strip()
        refs = [str(e).strip() for e in (item.get("evidence_ids") or []) if str(e).strip()]
        if not paper_id or not dimension_id or not content:
            rejected.append(f"第 {pos + 1} 条比较缺少 paper_id/dimension_id/content，已丢弃")
            continue
        valid = [e for e in refs if e in index]
        # 比较单元的证据必须属于被比较的那篇论文
        valid = [e for e in valid if index[e].paper_id == paper_id]
        if valid and len(valid) != len(refs):
            rejected.append(
                f"第 {pos + 1} 条比较引用了无效证据 id，已按有效引用保留"
            )
        if not valid:
            rejected.append(
                f"第 {pos + 1} 条比较（论文 {paper_id[:8]}）没有属于该论文的真实证据，已丢弃"
            )
            continue
        drafts.append(
            ComparisonDraft(
                paper_id=paper_id,
                dimension_id=dimension_id,
                content=content[:4000],
                evidence_ids=valid,
            )
        )
    return drafts, rejected


def parse_conflict_payload(
    raw: Any, evidence: Sequence[EvidenceRef]
) -> Tuple[List[ConflictDraft], List[str]]:
    """解析冲突；两侧都必须有真实证据、不得重叠、必须涉及不同论文。"""
    if not isinstance(raw, dict):
        raise ComparisonError("冲突输出必须是 JSON 对象")
    items = raw.get("conflicts")
    if items is None:
        return [], []
    if not isinstance(items, list):
        raise ComparisonError("conflicts 必须是数组")
    index = _index(evidence)
    drafts: List[ConflictDraft] = []
    rejected: List[str] = []
    for pos, item in enumerate(items[:MAX_CONFLICT_ITEMS]):
        if not isinstance(item, dict):
            rejected.append(f"第 {pos + 1} 条冲突不是对象，已丢弃")
            continue
        a = [str(e).strip() for e in (item.get("side_a") or []) if str(e).strip() in index]
        b = [str(e).strip() for e in (item.get("side_b") or []) if str(e).strip() in index]
        topic = str(item.get("topic") or "").strip()
        if not a or not b:
            rejected.append(f"第 {pos + 1} 条冲突两侧都必须引用真实存在的证据，已丢弃")
            continue
        if set(a) & set(b):
            rejected.append(f"第 {pos + 1} 条冲突两侧引用了同一证据，已丢弃")
            continue
        if {index[e].paper_id for e in a} == {index[e].paper_id for e in b} and len(set(a) | set(b)) < 2:
            rejected.append(f"第 {pos + 1} 条冲突未涉及两条不同证据，已丢弃")
            continue
        drafts.append(
            ConflictDraft(
                topic=topic[:500],
                description=str(item.get("description") or "").strip()[:4000],
                side_a=a,
                side_b=b,
                dimension_id=str(item.get("dimension_id") or "").strip()[:64],
                sub_question_id=str(item.get("sub_question_id") or "").strip()[:64],
            )
        )
    return drafts, rejected


COMPARISON_SYSTEM_PROMPT = """你是论文研究助手，负责按比较维度归纳单篇论文的做法与结论。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。
每条结论都必须引用给定证据的 evidence_id；没有证据支持的结论一律不要输出。"""

COMPARISON_USER_TEMPLATE = """研究问题：
{question}

比较维度（dimension_id 必须取自其中）：
{dimensions}

可用证据（evidence_id 必须取自其中）：
{evidence}

输出 JSON 格式（严格遵守）：
{{"comparisons": [{{"paper_id": "论文ID", "dimension_id": "维度ID", "content": "该论文在该维度下的做法/结论", "evidence_ids": ["证据ID"]}}]}}

约束：
- 最多 {max_items} 条；每条必须至少引用一条证据，且引用的证据必须属于该 paper_id 对应的论文；
- 只依据给定证据，不使用你的先验知识；证据不足就不要编造结论。"""

CONFLICT_SYSTEM_PROMPT = """你是论文研究助手，负责找出不同论文结论之间的冲突。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。
冲突两侧都必须引用给定证据的 evidence_id；找不到对立证据时返回 {{"conflicts": []}}。"""

CONFLICT_USER_TEMPLATE = """研究问题：
{question}

可用证据（evidence_id 必须取自其中）：
{evidence}

输出 JSON 格式（严格遵守）：
{{"conflicts": [{{"topic": "冲突主题", "description": "冲突说明", "dimension_id": "维度ID", "sub_question_id": "子问题ID", "side_a": ["证据ID"], "side_b": ["证据ID"]}}]}}

约束：
- 最多 {max_items} 条；两侧都必须至少一条证据，且两侧不得引用同一证据；
- 冲突必须是不同论文之间的对立结论，不是同一篇论文内部的措辞差异。"""


class ComparisonGenerator:
    """生产比较/冲突生成器（一次维度归纳 + 一次冲突检测）。"""

    def __init__(self, llm: Any, model_name: str = ""):
        self.llm = llm
        self.model_name = model_name

    def _invoke(self, messages: Sequence[Tuple[str, str]]) -> str:
        resp = self.llm.invoke(messages)
        return str(getattr(resp, "content", resp))

    def _ask(self, system: str, user: str) -> Any:
        from chatchat.server.agent_run.schema import PlanError

        messages = [("system", system), ("user", user)]
        first = self._invoke(messages)
        try:
            return _extract_json_object(first)
        except PlanError as exc:
            retry = messages + [
                ("assistant", first),
                ("user", f"上一次输出不符合要求（{exc}）。请只输出符合 JSON 格式要求的对象。"),
            ]
            try:
                return _extract_json_object(self._invoke(retry))
            except PlanError as exc2:
                raise ComparisonError(f"输出无法解析：{exc2}") from exc2

    def generate_comparisons(
        self, *, question: str, dimensions: Sequence[Dict[str, Any]], evidence: Sequence[EvidenceRef]
    ) -> Tuple[List[ComparisonDraft], List[str]]:
        if not evidence:
            return [], ["没有可用证据，无法生成比较"]
        user = COMPARISON_USER_TEMPLATE.format(
            question=question,
            dimensions="\n".join(
                f"- {d['id']}：{d.get('label') or d['id']}" for d in dimensions
            ),
            evidence="\n".join(e.as_prompt_line(i) for i, e in enumerate(evidence)),
            max_items=MAX_COMPARISON_ITEMS,
        )
        raw = self._ask(COMPARISON_SYSTEM_PROMPT, user)
        return parse_comparison_payload(raw, evidence)

    def detect_conflicts(
        self, *, question: str, evidence: Sequence[EvidenceRef]
    ) -> Tuple[List[ConflictDraft], List[str]]:
        if len(evidence) < 2:
            return [], ["证据不足两篇，不做冲突检测"]
        user = CONFLICT_USER_TEMPLATE.format(
            question=question,
            evidence="\n".join(e.as_prompt_line(i) for i, e in enumerate(evidence)),
            max_items=MAX_CONFLICT_ITEMS,
        )
        raw = self._ask(CONFLICT_SYSTEM_PROMPT, user)
        return parse_conflict_payload(raw, evidence)


def build_default_generator(model_name: str | None = None) -> ComparisonGenerator:
    """按现有模型配置构建生成器（测试中替换为假生成器）。

    与 ``research.plan.build_default_planner`` 同理：必须
    ``local_wrap=False`` 直连模型平台，避免经本服务 /v1 的自引用死锁。
    """
    from chatchat.server.utils import get_ChatOpenAI, get_default_llm
    from chatchat.settings import Settings

    name = model_name or get_default_llm()
    llm = get_ChatOpenAI(
        consumer=CONSUMER_PAPER_COMPARE,
        model_name=name,
        streaming=False,
        local_wrap=False,
        max_tokens=Settings.model_settings.MAX_TOKENS,
    )
    return ComparisonGenerator(llm, model_name=name)


__all__ = [
    "MAX_COMPARISON_ITEMS",
    "MAX_CONFLICT_ITEMS",
    "ComparisonError",
    "EvidenceRef",
    "ComparisonDraft",
    "ConflictDraft",
    "ComparisonOutput",
    "parse_comparison_payload",
    "parse_conflict_payload",
    "ComparisonGenerator",
    "build_default_generator",
]
