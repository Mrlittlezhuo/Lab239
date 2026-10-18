"""证据卡片的结构化提取与原文核对（任务 018 阶段 A）。

核心约束（任务 018 §5）：

- **原文必须可核对**：模型给出的 ``quote`` 必须在**提供给它的原文单元**
  中逐字出现（折叠空白后子串匹配）。匹配不上的一律拒绝入库，绝不把
  模型改写/臆造的文本标成"已核对原文"，也不给它编页码；
- **定位来自单元，不来自模型**：页码/章节/块号取自匹配到的单元，模型
  自报的 ``unit_index`` 只作为优先尝试的提示，不作为定位依据；
- **有界**：单次每篇每子问题最多 ``MAX_EVIDENCE_PER_CALL`` 条；解析失败
  带纠正提示重试一次，仍失败则可控失败（沿用 017 ``ResearchPlanner``
  的做法），绝不无限循环。

``EvidenceExtractor`` 用现有非流式模型；测试注入假 LLM，不联网不调模型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chatchat.server.agent_run.schema import _extract_json_object
from chatchat.server.db.repository.research_repository import EVIDENCE_TYPES
from chatchat.server.research.fulltext import PaperUnit, normalize_space
from chatchat.server.model_capacity import CONSUMER_PAPER_EXTRACT

MAX_EVIDENCE_PER_CALL = 8
MIN_QUOTE_CHARS = 12
#: 提供给模型的原文总量上限（按整条单元取舍，不截断单元）
MAX_PROMPT_UNITS_CHARS = 12000


class EvidenceError(Exception):
    """证据提取/解析失败（调用方记录失败，不影响其他论文）。"""


@dataclass
class EvidenceDraft:
    """一条通过原文核对的证据草稿（尚未落库）。"""

    evidence_type: str
    summary: str
    quote: str
    unit: PaperUnit
    level: str  # fulltext / abstract（跟随来源单元）
    verified: bool = True

    def locator(self) -> Dict[str, Any]:
        return {
            "unit_kind": self.unit.unit_kind,
            "page": self.unit.page,
            "section": self.unit.section,
            "block_index": self.unit.block_index,
            "source_kind": self.unit.source_kind,
        }


@dataclass
class EvidenceExtraction:
    """一次提取的结果：通过的草稿 + 被拒绝的原因（用户可见）。"""

    drafts: List[EvidenceDraft] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)


def find_quote(quote: str, units: Sequence[PaperUnit]) -> Optional[PaperUnit]:
    """在给定单元中逐字定位 ``quote``（折叠空白后子串匹配）。

    找不到返回 ``None``——调用方必须据此拒绝该条证据。
    """
    needle = normalize_space(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    for unit in units:
        if needle in normalize_space(unit.text):
            return unit
    return None


def parse_evidence_payload(
    raw: Any,
    units: Sequence[PaperUnit],
    preferred_index: Optional[int] = None,
) -> EvidenceExtraction:
    """解析模型 JSON 输出并逐条核对原文。

    返回通过的草稿与被拒绝条目的原因；不抛异常（单条无效不影响其余），
    但整体结构不合法时抛 ``EvidenceError``（调用方可带纠正提示重试）。
    """
    if not isinstance(raw, dict):
        raise EvidenceError("证据输出必须是 JSON 对象")
    items = raw.get("evidence")
    if items is None:
        return EvidenceExtraction()
    if not isinstance(items, list):
        raise EvidenceError("evidence 必须是数组")

    drafts: List[EvidenceDraft] = []
    rejected: List[str] = []
    seen: set = set()
    for pos, item in enumerate(items[:MAX_EVIDENCE_PER_CALL]):
        if not isinstance(item, dict):
            rejected.append(f"第 {pos + 1} 条不是对象，已丢弃")
            continue
        evidence_type = str(item.get("evidence_type") or "").strip()
        if evidence_type not in EVIDENCE_TYPES:
            rejected.append(
                f"第 {pos + 1} 条证据类型非法（{evidence_type or '空'}），已丢弃"
            )
            continue
        quote = str(item.get("quote") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not quote:
            rejected.append(f"第 {pos + 1} 条缺少原文 quote，已丢弃")
            continue
        # 模型自报的单元只作为优先尝试，定位仍以实际匹配到的单元为准
        ordered: List[PaperUnit] = list(units)
        idx = item.get("unit_index", preferred_index)
        if isinstance(idx, int) and 0 <= idx < len(ordered):
            ordered = [ordered[idx]] + [u for u in ordered if u is not ordered[idx]]
        unit = find_quote(quote, ordered)
        if unit is None:
            rejected.append(
                f"第 {pos + 1} 条原文未在给定的论文原文中出现，已丢弃："
                f"{quote[:80]}"
            )
            continue
        key = (evidence_type, quote)
        if key in seen:
            continue
        seen.add(key)
        drafts.append(
            EvidenceDraft(
                evidence_type=evidence_type,
                summary=summary[:2000],
                quote=normalize_space(quote),
                unit=unit,
                level="abstract" if unit.unit_kind == "abstract" else "fulltext",
                verified=True,
            )
        )
    return EvidenceExtraction(drafts=drafts, rejected=rejected)


EVIDENCE_SYSTEM_PROMPT = """你是论文研究助手，负责从给定的论文原文中提取可核对的证据。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。
quote 必须逐字复制给定原文单元中的连续文本，不得改写、翻译、拼接不同单元或补充原文没有的内容。
找不到支持原文时返回 {"evidence": []}，绝不编造原文或页码。"""

EVIDENCE_USER_TEMPLATE = """研究问题：
{question}

当前子问题：
{sub_question}

论文：{title}（{year}）
原文可得级别：{level_note}

给定原文单元（编号从 0 开始，括号内为定位）：
{units}

输出 JSON 格式（严格遵守）：
{{"evidence": [{{"evidence_type": "objective|method|dataset|metric|result|conclusion|limitation", "summary": "一句话概括这条证据", "quote": "逐字复制的原文片段", "unit_index": 0}}]}}

约束：
- 最多 {max_items} 条；每条 quote 必须完整出现在同一个原文单元内（至少 {min_quote} 个字符）；
- unit_index 填该 quote 所在原文单元的编号；
- 只依据上面给定的原文，不使用你的先验知识；
- 若原文只有摘要级单元，证据同样只能摘自摘要（系统会标记为摘要级证据）。"""


class EvidenceExtractor:
    """生产提取器：用现有模型按子问题提取证据，并逐条核对原文。"""

    def __init__(self, llm: Any, model_name: str = ""):
        self.llm = llm
        self.model_name = model_name

    def _invoke(self, messages: Sequence[Tuple[str, str]]) -> str:
        resp = self.llm.invoke(messages)
        return str(getattr(resp, "content", resp))

    def _prompt(
        self,
        question: str,
        sub_question: str,
        paper: Any,
        units: Sequence[PaperUnit],
    ) -> str:
        lines: List[str] = []
        budget = MAX_PROMPT_UNITS_CHARS
        for i, unit in enumerate(units):
            where = []
            if unit.page is not None:
                where.append(f"p.{unit.page}")
            if unit.section:
                where.append(unit.section)
            where.append(f"block {unit.block_index}")
            line = f"[{i}]（{' / '.join(where)}）{unit.text}"
            # 按整条单元取舍：截断单元会让其中的 quote 无法核对，比少给一条更糟
            if len(line) > budget:
                break
            budget -= len(line)
            lines.append(line)
        level_note = (
            "全文（含页码/章节定位）"
            if any(u.unit_kind == "block" for u in units)
            else "仅摘要（无页码）"
        )
        return EVIDENCE_USER_TEMPLATE.format(
            question=question,
            sub_question=sub_question,
            title=getattr(paper, "title", "") or "（无标题）",
            year=getattr(paper, "year", None) or "年份未知",
            level_note=level_note,
            units="\n".join(lines),
            max_items=MAX_EVIDENCE_PER_CALL,
            min_quote=MIN_QUOTE_CHARS,
        )

    def extract(
        self,
        *,
        question: str,
        sub_question: str,
        paper: Any,
        units: Sequence[PaperUnit],
    ) -> EvidenceExtraction:
        """提取一篇论文对某个子问题的证据；无可用原文时返回空。"""
        if not units:
            return EvidenceExtraction(rejected=["该论文没有可用的原文单元"])
        from chatchat.server.agent_run.schema import PlanError

        prompt = self._prompt(question, sub_question, paper, units)
        messages = [("system", EVIDENCE_SYSTEM_PROMPT), ("user", prompt)]
        first = self._invoke(messages)
        try:
            raw = _extract_json_object(first)
        except PlanError as exc:
            retry = messages + [
                ("assistant", first),
                ("user", f"上一次输出不符合要求（{exc}）。请只输出符合 JSON 格式要求的对象。"),
            ]
            try:
                raw = _extract_json_object(self._invoke(retry))
            except PlanError as exc2:
                raise EvidenceError(f"证据输出无法解析：{exc2}") from exc2
        try:
            return parse_evidence_payload(raw, units)
        except EvidenceError:
            # 结构非法：带纠正提示重试一次，仍失败则可控失败
            retry = messages + [
                ("assistant", first),
                ("user", "上一次输出结构不符合要求。请只输出 "
                         '{"evidence": [...]} 形式的 JSON 对象。'),
            ]
            try:
                raw = _extract_json_object(self._invoke(retry))
            except PlanError as exc:
                raise EvidenceError(f"证据输出无法解析：{exc}") from exc
            return parse_evidence_payload(raw, units)


def build_default_extractor(model_name: Optional[str] = None) -> EvidenceExtractor:
    """按现有模型配置构建提取器（测试中替换为假提取器）。

    与 ``research.plan.build_default_planner`` 同理：必须
    ``local_wrap=False`` 直连模型平台，避免经本服务 /v1 的自引用死锁。
    """
    from chatchat.server.utils import get_ChatOpenAI, get_default_llm
    from chatchat.settings import Settings

    name = model_name or get_default_llm()
    llm = get_ChatOpenAI(
        consumer=CONSUMER_PAPER_EXTRACT,
        model_name=name,
        streaming=False,
        local_wrap=False,
        max_tokens=Settings.model_settings.MAX_TOKENS,
    )
    return EvidenceExtractor(llm, model_name=name)


__all__ = [
    "MAX_EVIDENCE_PER_CALL",
    "MIN_QUOTE_CHARS",
    "EvidenceError",
    "EvidenceDraft",
    "EvidenceExtraction",
    "find_quote",
    "parse_evidence_payload",
    "EvidenceExtractor",
    "build_default_extractor",
]
