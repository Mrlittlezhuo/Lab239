"""带稳定引用的版本化研究报告（任务 018 阶段 C）。

引用不是"参考文献列表"，而是可反查的关联：

- 正文里的引用标记是 ``[[e:<evidence_id>]]``，**内嵌真实证据卡片 id**，
  与显示顺序无关，因此重新渲染、导出或换版本都不会错位；
- 每条结论必须携带它依据的证据 id；没有证据的结论不会被写进"综合结论"，
  而是被放进"证据不足与推测"小节并显式标注，绝不伪装成论文事实；
- 引用了不存在或不属于本项目的证据 id 一律被剥离并记录原因；
- 参考文献与 BibTeX 只由真实论文元数据生成，模型不参与编造条目。

报告结构由代码固定（研究问题与范围 / 检索与筛选说明 / 方向概览 /
代表论文 / 维度比较 / 综合结论 / 冲突 / 证据不足 / 后续建议 / 参考文献），
模型只填分析内容，因此必需小节不会因为模型偷懒而缺失。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from chatchat.server.agent_run.schema import _extract_json_object

#: 正文引用标记：内嵌证据卡片 id（稳定，不依赖显示顺序）
CITATION_RE = re.compile(r"\[\[e:([0-9a-zA-Z]{6,64})\]\]")

MAX_CONCLUSIONS = 12
MAX_GAPS = 8
MAX_NEXT_STEPS = 8
MAX_OVERVIEW_CHARS = 2000


class ReportError(Exception):
    """报告生成输出无法解析（调用方如实回报失败）。"""


@dataclass
class ReportEvidence:
    """报告可引用的证据（已确定属于本项目/用户）。"""

    evidence_id: str
    paper_id: str
    paper_title: str
    evidence_type: str
    level: str
    summary: str
    quote: str
    locator: Dict[str, Any] = field(default_factory=dict)
    verified: bool = True

    def as_prompt_line(self, index: int) -> str:
        where = "摘要级" if self.level == "abstract" else "全文"
        mark = "" if self.verified else "（原文未核对）"
        page = self.locator.get("page")
        place = f"p.{page}" if page else where
        return (
            f"[{index}] evidence_id={self.evidence_id} 论文={self.paper_title} "
            f"定位={place}{mark} 概括={self.summary[:120]} 原文={self.quote[:300]}"
        )


@dataclass
class ReportDraft:
    """模型产出的报告分析内容（尚未渲染）。"""

    title: str = ""
    overview: str = ""
    conclusions: List[Tuple[str, List[str]]] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)


def _index(evidence: Sequence[ReportEvidence]) -> Dict[str, ReportEvidence]:
    return {e.evidence_id: e for e in evidence}


def parse_report_payload(raw: Any, evidence: Sequence[ReportEvidence]) -> ReportDraft:
    """解析模型输出并按证据 id 校验；无证据的结论归入"证据不足"。"""
    if not isinstance(raw, dict):
        raise ReportError("报告输出必须是 JSON 对象")
    index = _index(evidence)
    draft = ReportDraft(
        title=str(raw.get("title") or "").strip()[:500],
        overview=str(raw.get("overview") or "").strip()[:MAX_OVERVIEW_CHARS],
    )
    conclusions = raw.get("conclusions")
    if conclusions is None:
        conclusions = []
    if not isinstance(conclusions, list):
        raise ReportError("conclusions 必须是数组")
    for pos, item in enumerate(conclusions[:MAX_CONCLUSIONS]):
        if isinstance(item, str):
            text, refs = item.strip(), []
        elif isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            refs = [str(e).strip() for e in (item.get("evidence_ids") or []) if str(e).strip()]
        else:
            draft.rejected.append(f"第 {pos + 1} 条结论不是对象/字符串，已丢弃")
            continue
        if not text:
            continue
        valid = [e for e in refs if e in index]
        dropped = [e for e in refs if e not in index]
        if dropped:
            draft.rejected.append(
                f"第 {pos + 1} 条结论引用了不存在或不属于本项目的证据 "
                f"{[d[:8] for d in dropped]}，已剥离这些引用"
            )
        if not valid:
            draft.unsupported.append(f"{text}（推测：无证据支持）")
            continue
        draft.conclusions.append((text, valid))
    for item in (raw.get("gaps") or [])[:MAX_GAPS]:
        text = str(item or "").strip()
        if text:
            draft.unsupported.append(text)
    for item in (raw.get("next_steps") or [])[:MAX_NEXT_STEPS]:
        text = str(item or "").strip()
        if text:
            draft.next_steps.append(text)
    return draft


def markers_in(body: str) -> List[str]:
    """正文中出现的证据引用标记（按首次出现顺序去重）。"""
    seen: List[str] = []
    for match in CITATION_RE.finditer(body or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def validate_report_body(
    body: str, evidence: Sequence[ReportEvidence]
) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """校验已有正文（用户编辑后）里的引用标记。

    返回 ``(清理后的正文, 引用映射, 警告)``：无法解析的标记被剥离，
    绝不保留指向不存在证据的引用。
    """
    index = _index(evidence)
    warnings: List[str] = []
    citations: List[Dict[str, Any]] = []
    for marker_id in markers_in(body or ""):
        item = index.get(marker_id)
        if item is None:
            warnings.append(f"引用 [[e:{marker_id[:8]}…]] 指向不存在或不属于本项目的证据，已移除")
            continue
        citations.append(
            {
                "marker": f"[[e:{marker_id}]]",
                "evidence_id": item.evidence_id,
                "paper_id": item.paper_id,
                "paper_title": item.paper_title,
                "quote": item.quote[:400],
                "level": item.level,
                "locator": item.locator,
                "verified": item.verified,
            }
        )
    cleaned = CITATION_RE.sub(
        lambda m: m.group(0) if m.group(1) in index else "", body or ""
    )
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, citations, warnings


def render_markdown(
    *,
    title: str,
    body: str,
    citations: Sequence[Dict[str, Any]],
) -> str:
    """把引用标记渲染成 Markdown 脚注，并附证据定义（含定位与原文）。"""
    order = {c["evidence_id"]: i + 1 for i, c in enumerate(citations)}

    def _replace(match: "re.Match[str]") -> str:
        number = order.get(match.group(1))
        return f"[^{number}]" if number else ""

    rendered = CITATION_RE.sub(_replace, body or "")
    if citations:
        definitions = ["", "### 证据引用"]
        for i, citation in enumerate(citations, start=1):
            locator = citation.get("locator") or {}
            where = []
            if locator.get("page"):
                where.append(f"第 {locator['page']} 页")
            if locator.get("section"):
                where.append(str(locator["section"]))
            if locator.get("block_index") is not None:
                where.append(f"文本块 {locator['block_index']}")
            level = "摘要级证据" if citation.get("level") == "abstract" else "全文证据"
            verified = "" if citation.get("verified") else "（原文未核对）"
            # 摘要级证据不假装有页码/章节：只标注摘要级
            place = level if citation.get("level") == "abstract" else ("，".join(where) or level)
            definitions.append(
                f"[^{i}]: {citation['paper_title']} — {place}{verified}\n"
                f"    > {citation['quote']}"
            )
        rendered = rendered.rstrip() + "\n" + "\n".join(definitions) + "\n"
    return f"# {title}\n\n{rendered}".strip() + "\n"


_BIBKEY_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def bibtex_key(paper: Any, used: set) -> str:
    """由真实元数据生成稳定且唯一的 BibTeX key。"""
    first_author = str(paper.authors or "").split(";")[0].strip()
    surname = _BIBKEY_CLEAN_RE.sub("", (first_author.split()[-1] if first_author else "anon").lower())
    year = paper.year or "nd"
    base = f"{surname}{year}"
    key = base
    suffix = ord("a")
    while key in used:
        key = f"{base}{chr(suffix)}"
        suffix += 1
    used.add(key)
    return key


def _bib_escape(value: str) -> str:
    return (value or "").replace("{", r"\{").replace("}", r"\}")


def _bib_authors(value: str) -> str:
    """把存储的作者串（``;`` 或 ``and`` 分隔）规范成 BibTeX 的 ``and`` 形式。"""
    parts = [p.strip() for p in (value or "").replace(" and ", ";").split(";")]
    return " and ".join(p for p in parts if p)


def bibtex_entries(papers: Sequence[Any]) -> str:
    """由真实论文元数据生成 BibTeX（无元数据的论文不产出条目）。"""
    used: set = set()
    blocks: List[str] = []
    for paper in papers:
        key = bibtex_key(paper, used)
        fields = [
            ("title", _bib_escape(paper.title or "")),
            ("author", _bib_escape(_bib_authors(paper.authors or ""))),
        ]
        if paper.year:
            fields.append(("year", str(paper.year)))
        if paper.doi:
            fields.append(("doi", _bib_escape(paper.doi)))
        if paper.arxiv_id:
            fields.append(("eprint", _bib_escape(paper.arxiv_id)))
            fields.append(("archivePrefix", "arXiv"))
        if paper.url:
            fields.append(("url", _bib_escape(paper.url)))
        body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
        blocks.append(f"@misc{{{key},\n{body}\n}}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


REPORT_SYSTEM_PROMPT = """你是论文研究助手，负责把已核对的证据综合成研究报告的分析部分。
你必须只输出一个 JSON 对象，不要输出代码块标记、解释或任何多余文字。
每条结论都必须给出 evidence_ids（取自给定证据的 evidence_id）；找不到支持证据的判断请放进 gaps，不要写成事实。"""

REPORT_USER_TEMPLATE = """研究问题：
{question}

研究范围/约束：
{scope}

证据覆盖情况：
{coverage}

维度比较结论：
{comparisons}

已记录的冲突：
{conflicts}

可用证据（evidence_id 必须取自其中）：
{evidence}

输出 JSON 格式（严格遵守）：
{{"title": "报告标题", "overview": "研究方向概览", "conclusions": [{{"text": "结论", "evidence_ids": ["证据ID"]}}], "gaps": ["证据不足或尚不清楚的点"], "next_steps": ["后续建议"]}}

约束：
- conclusions 最多 {max_conclusions} 条，每条必须至少引用一条证据；
- 没有证据支持的判断只能写进 gaps；
- 只依据给定证据，不使用你的先验知识。"""


class ReportGenerator:
    """生产报告生成器：产出结构化分析内容（引用真实证据 id）。"""

    def __init__(self, llm: Any, model_name: str = ""):
        self.llm = llm
        self.model_name = model_name

    def _invoke(self, messages: Sequence[Tuple[str, str]]) -> str:
        resp = self.llm.invoke(messages)
        return str(getattr(resp, "content", resp))

    def generate(
        self,
        *,
        question: str,
        scope: str,
        coverage: Dict[str, Any],
        comparisons: Sequence[Dict[str, Any]],
        conflicts: Sequence[Dict[str, Any]],
        evidence: Sequence[ReportEvidence],
    ) -> ReportDraft:
        if not evidence:
            return ReportDraft(gaps=["没有可用证据，无法生成结论"])
        from chatchat.server.agent_run.schema import PlanError

        user = REPORT_USER_TEMPLATE.format(
            question=question,
            scope=scope or "（未指定）",
            coverage=json.dumps(coverage.get("totals", {}), ensure_ascii=False),
            comparisons="\n".join(
                f"- {c.get('paper_title', '')} / {c.get('dimension_id', '')}：{c.get('content', '')}"
                for c in comparisons
            )
            or "（暂无比较结论）",
            conflicts="\n".join(
                f"- {c.get('topic', '')}：{c.get('description', '')}" for c in conflicts
            )
            or "（未记录冲突）",
            evidence="\n".join(e.as_prompt_line(i) for i, e in enumerate(evidence)),
            max_conclusions=MAX_CONCLUSIONS,
        )
        messages = [("system", REPORT_SYSTEM_PROMPT), ("user", user)]
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
                raise ReportError(f"报告输出无法解析：{exc2}") from exc2
        return parse_report_payload(raw, evidence)


def build_default_generator(model_name: Optional[str] = None) -> ReportGenerator:
    """按现有模型配置构建报告生成器（测试中替换为假生成器）。

    与 ``research.plan.build_default_planner`` 同理：必须
    ``local_wrap=False`` 直连模型平台，避免经本服务 /v1 的自引用死锁。
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
    return ReportGenerator(llm, model_name=name)


def assemble_body(
    *,
    project: Any,
    draft: ReportDraft,
    papers: Sequence[Any],
    comparisons: Sequence[Dict[str, Any]],
    conflicts: Sequence[Dict[str, Any]],
    coverage: Dict[str, Any],
    source_stats: Sequence[Any] = (),
    screening_counts: Optional[Dict[str, int]] = None,
) -> str:
    """把结构化结果渲染成固定小节的 Markdown 正文（含引用标记）。"""
    lines: List[str] = []

    lines.append("## 研究问题与范围")
    lines.append(f"- 研究问题：{project.research_question}")
    lines.append(f"- 范围/约束：{project.scope or '（未指定）'}")
    lines.append("")

    lines.append("## 检索与筛选说明")
    if source_stats:
        for stat in source_stats:
            if isinstance(stat, dict):
                lines.append(
                    f"- 来源 {stat.get('source', '')}：命中 {stat.get('hits', 0)}，"
                    f"失败 {stat.get('failures', 0)}"
                )
    counts = screening_counts or {}
    lines.append(
        "- 筛选结果："
        f"纳入 {counts.get('included', 0)} 篇，"
        f"待审 {counts.get('pending_review', 0)} 篇，"
        f"排除 {counts.get('excluded', 0)} 篇"
    )
    lines.append("")

    lines.append("## 方向概览")
    lines.append(draft.overview or "（模型未给出概览）")
    lines.append("")

    lines.append("## 代表论文")
    if papers:
        for paper in papers:
            where = paper.url or paper.doi or paper.arxiv_id or "（无稳定链接）"
            lines.append(
                f"- **{paper.title}**（{paper.year or '年份未知'}）— {paper.authors or '作者未知'}，{where}"
            )
    else:
        lines.append("- （尚未纳入任何论文）")
    lines.append("")

    lines.append("## 维度比较")
    if comparisons:
        for item in comparisons:
            refs = "".join(f"[[e:{e}]]" for e in item.get("evidence_ids") or [])
            lines.append(
                f"- {item.get('paper_title', '')} · {item.get('dimension_label') or item.get('dimension_id', '')}："
                f"{item.get('content', '')}{refs}"
            )
    else:
        lines.append("- （尚未生成比较结论）")
    lines.append("")

    lines.append("## 综合结论")
    if draft.conclusions:
        for text, refs in draft.conclusions:
            lines.append(f"- {text}{''.join(f'[[e:{e}]]' for e in refs)}")
    else:
        lines.append("- （没有可支持的结论；见下方「证据不足与推测」）")
    lines.append("")

    lines.append("## 冲突")
    if conflicts:
        for item in conflicts:
            sides = []
            for side in (item.get("side_a") or []), (item.get("side_b") or []):
                sides.append(" ".join(f"[[e:{x.get('evidence_id')}]]" for x in side))
            lines.append(
                f"- {item.get('topic', '')}：{item.get('description', '')} "
                f"（甲方 {' '.join(sides)}；状态 {item.get('status', 'open')}）"
            )
    else:
        lines.append("- （未记录冲突）")
    lines.append("")

    lines.append("## 证据不足与推测")
    gaps = list(draft.unsupported)
    for row in coverage.get("sub_questions") or []:
        if row.get("status") in ("missing", "thin"):
            gaps.append(
                f"子问题「{row.get('question') or row.get('id')}」证据不足"
                f"（{row.get('evidence_count', 0)} 条证据，{row.get('paper_count', 0)} 篇论文）"
            )
    for dim in coverage.get("missing_dimensions") or []:
        gaps.append(f"维度「{dim.get('label') or dim.get('id')}」在整个项目里没有任何证据")
    if gaps:
        for gap in gaps:
            lines.append(f"- {gap}")
    else:
        lines.append("- （无）")
    lines.append("")

    lines.append("## 后续建议")
    if draft.next_steps:
        for step in draft.next_steps:
            lines.append(f"- {step}")
    for suggestion in coverage.get("suggestions") or []:
        queries = "、".join(suggestion.get("queries") or [])
        lines.append(
            f"- 补充检索「{suggestion.get('question') or suggestion.get('sub_question_id')}」："
            f"{suggestion.get('reason', '')}"
            + (f"（建议检索式：{queries}）" if queries else "")
        )
    if not draft.next_steps and not coverage.get("suggestions"):
        lines.append("- （暂无）")
    lines.append("")

    lines.append("## 参考文献")
    if papers:
        for paper in papers:
            parts = [paper.title or "（无标题）", paper.authors or "作者未知", str(paper.year or "年份未知")]
            for label, value in (
                ("DOI", paper.doi),
                ("arXiv", paper.arxiv_id),
                ("URL", paper.url),
            ):
                if value:
                    parts.append(f"{label}: {value}")
            lines.append("- " + "；".join(parts))
    else:
        lines.append("- （无）")
    return "\n".join(lines) + "\n"


__all__ = [
    "CITATION_RE",
    "MAX_CONCLUSIONS",
    "ReportError",
    "ReportEvidence",
    "ReportDraft",
    "parse_report_payload",
    "markers_in",
    "validate_report_body",
    "render_markdown",
    "bibtex_key",
    "bibtex_entries",
    "ReportGenerator",
    "build_default_generator",
    "assemble_body",
]
