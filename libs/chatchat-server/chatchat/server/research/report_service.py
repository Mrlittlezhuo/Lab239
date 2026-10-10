"""报告服务层（任务 018 阶段 C）。

- 生成：以本项目**已纳入论文 + 非驳回证据 + 比较 + 冲突 + 覆盖**为唯一
  事实来源，模型只填分析内容，必需小节由代码固定；
- 引用：正文里的 ``[[e:<id>]]`` 标记在保存前统一校验，指向不存在/他人
  证据的标记被剥离并记录原因，引用映射随版本落库，可反查论文与原文；
- 版本：每次生成或用户编辑都插入新版本（``report_key`` 相同、
  ``version`` 递增），旧版本保持可读，永不覆盖；
- 导出：Markdown 渲染成脚注；BibTeX 只由真实论文元数据生成。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.models.research_model import PaperRecordModel
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.comparison_service import (
    included_paper_ids,
    project_coverage,
)
from chatchat.server.research.report import (
    ReportDraft,
    ReportEvidence,
    ReportGenerator,
    assemble_body,
    bibtex_entries,
    build_default_generator,
    render_markdown,
    validate_report_body,
)
from chatchat.utils import build_logger

logger = build_logger()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def collect_report_evidence(
    session: Session, project_id: str, owner_id: str, paper_ids: Sequence[str]
) -> List[ReportEvidence]:
    """报告可引用的证据（本项目、非驳回、属于已纳入论文）。"""
    titles: Dict[str, str] = {}
    for pid in paper_ids:
        paper = session.get(PaperRecordModel, pid)
        titles[pid] = paper.title if paper is not None else pid
    wanted = set(paper_ids)
    refs: List[ReportEvidence] = []
    for card in repo.list_evidence(session, project_id, owner_id):
        if card.paper_id not in wanted:
            continue
        refs.append(
            ReportEvidence(
                evidence_id=card.id,
                paper_id=card.paper_id,
                paper_title=titles.get(card.paper_id, card.paper_id),
                evidence_type=card.evidence_type,
                level=card.level,
                summary=card.summary,
                quote=card.quote,
                locator=_loads(card.locator_json, {}) or {},
                verified=bool(card.quote_verified),
            )
        )
    return refs


def _comparison_rows(session: Session, project_id: str, owner_id: str) -> List[Dict[str, Any]]:
    project = repo.get_owned_project(session, project_id, owner_id)
    labels = {
        d["id"]: d.get("label") or d["id"]
        for d in repo.parse_dimensions_json(project.dimensions_json if project else "")
    }
    rows: List[Dict[str, Any]] = []
    for unit in repo.list_comparison_units(session, project_id, owner_id):
        paper = session.get(PaperRecordModel, unit.paper_id)
        rows.append(
            {
                "paper_id": unit.paper_id,
                "paper_title": paper.title if paper is not None else unit.paper_id,
                "dimension_id": unit.dimension_id,
                "dimension_label": labels.get(unit.dimension_id, unit.dimension_id),
                "content": unit.content,
                "evidence_ids": _loads(unit.evidence_ids_json, []),
                "level": unit.level,
            }
        )
    return rows


def _conflict_rows(session: Session, project_id: str, owner_id: str) -> List[Dict[str, Any]]:
    return [
        {
            "conflict_id": c.id,
            "topic": c.topic,
            "description": c.description,
            "dimension_id": c.dimension_id,
            "status": c.status,
            "side_a": _loads(c.side_a_json, []),
            "side_b": _loads(c.side_b_json, []),
        }
        for c in repo.list_conflicts(session, project_id, owner_id)
    ]


def _report_out(report) -> Dict[str, Any]:
    return {
        "report_id": report.id,
        "report_key": report.report_key or report.id,
        "project_id": report.project_id,
        "run_id": report.run_id,
        "title": report.title,
        "version": report.version,
        "status": report.status,
        "content": report.content,
        "citations": _loads(report.citations_json, []),
        "evidence_ids": _loads(report.evidence_ids_json, []),
        "unsupported": report.unsupported,
        "generated_by": report.generated_by,
        "error": report.error,
        "create_time": report.create_time.isoformat() if report.create_time else None,
        "update_time": report.update_time.isoformat() if report.update_time else None,
    }


def generate_project_report(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    run_id: Optional[str] = None,
    generator: Any = None,
    model_name: str = "",
) -> Dict[str, Any]:
    """生成报告新版本（引用全部可反查；无证据结论被标注为推测）。"""
    project = repo.get_owned_project(session, project_id, owner_id)
    if project is None:
        raise repo.ResearchResourceNotFound(project_id)

    papers_ids = included_paper_ids(session, project_id, owner_id)
    papers = [session.get(PaperRecordModel, pid) for pid in papers_ids]
    papers = [p for p in papers if p is not None]
    evidence = collect_report_evidence(session, project_id, owner_id, papers_ids)
    comparisons = _comparison_rows(session, project_id, owner_id)
    conflicts = _conflict_rows(session, project_id, owner_id)
    coverage = project_coverage(session, project_id=project_id, owner_id=owner_id)

    runs = repo.list_runs(session, owner_id, project_id=project_id, limit=1)
    run = None
    if run_id:
        run = repo.get_owned_run(session, run_id, owner_id)
        if run is None or run.project_id != project_id:
            raise repo.ResearchResourceNotFound(run_id)
    elif runs:
        run = runs[0]
    source_stats = _loads(run.sources_json, []) if run is not None else []

    counts: Dict[str, int] = {}
    for pp, _paper in repo.list_project_papers(session, project_id, owner_id):
        counts[pp.screening_status] = counts.get(pp.screening_status, 0) + 1

    generator = generator or build_default_generator(model_name or None)
    draft: ReportDraft = generator.generate(
        question=project.research_question,
        scope=project.scope,
        coverage=coverage,
        comparisons=comparisons,
        conflicts=conflicts,
        evidence=evidence,
    )
    body = assemble_body(
        project=project,
        draft=draft,
        papers=papers,
        comparisons=comparisons,
        conflicts=conflicts,
        coverage=coverage,
        source_stats=source_stats,
        screening_counts=counts,
    )
    cleaned, citations, warnings = validate_report_body(body, evidence)
    # 内容完全一致时不再出版本：证据闭环重放/重复点击只追加噪声版本没有意义，
    # 只有证据或结论真的变了才产生新版本（版本仍是只追加，不覆盖旧内容）。
    title = draft.title or f"{project.name} 研究报告"
    for existing in repo.latest_project_reports(session, project_id, owner_id):
        if (existing.content or "") == cleaned and (existing.title or "") == title:
            out = _report_out(existing)
            out["warnings"] = list(draft.rejected) + warnings
            out["unchanged"] = True
            return out
    report = repo.create_report_version(
        session,
        project_id=project_id,
        owner_id=owner_id,
        title=title,
        content=cleaned,
        citations=citations,
        evidence_ids=[e.evidence_id for e in evidence],
        run_id=run.id if run is not None else None,
        unsupported="\n".join(draft.unsupported),
        generated_by=getattr(generator, "model_name", "") or model_name or "",
        status="generated",
    )
    out = _report_out(report)
    out["warnings"] = list(draft.rejected) + warnings
    return out


def save_report_edit(
    session: Session,
    *,
    report_id: str,
    owner_id: str,
    content: str,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """保存用户编辑：插入新版本，旧版本不动；引用重新按项目证据校验。"""
    report = repo.get_owned_report(session, report_id, owner_id)
    if report is None:
        raise repo.ResearchResourceNotFound(report_id)
    evidence = collect_report_evidence(
        session,
        report.project_id,
        owner_id,
        included_paper_ids(session, report.project_id, owner_id),
    )
    cleaned, citations, warnings = validate_report_body(content or "", evidence)
    if not cleaned.strip():
        raise ValueError("报告正文不能为空")
    new_report = repo.create_report_version(
        session,
        project_id=report.project_id,
        owner_id=owner_id,
        report_key=report.report_key or report.id,
        title=(title if title is not None else report.title) or "",
        content=cleaned,
        citations=citations,
        evidence_ids=[e.evidence_id for e in evidence],
        run_id=report.run_id,
        unsupported=report.unsupported,
        generated_by=report.generated_by,
        status="edited",
    )
    out = _report_out(new_report)
    out["warnings"] = warnings
    return out


def render_report_markdown(session: Session, *, report_id: str, owner_id: str) -> str:
    """导出 Markdown：引用标记渲染成脚注 + 证据定义（含定位与原文）。"""
    report = repo.get_owned_report(session, report_id, owner_id)
    if report is None:
        raise repo.ResearchResourceNotFound(report_id)
    return render_markdown(
        title=report.title or "研究报告",
        body=report.content,
        citations=_loads(report.citations_json, []),
    )


def render_report_bibtex(session: Session, *, report_id: str, owner_id: str) -> str:
    """导出 BibTeX：只包含该报告实际引用到的论文，条目来自真实元数据。"""
    report = repo.get_owned_report(session, report_id, owner_id)
    if report is None:
        raise repo.ResearchResourceNotFound(report_id)
    citations = _loads(report.citations_json, [])
    paper_ids: List[str] = []
    for citation in citations:
        pid = citation.get("paper_id")
        if pid and pid not in paper_ids:
            paper_ids.append(pid)
    if not paper_ids:
        paper_ids = [
            card.paper_id
            for card in repo.list_evidence(session, report.project_id, owner_id)
            if card.id in set(_loads(report.evidence_ids_json, []))
        ]
    papers = [session.get(PaperRecordModel, pid) for pid in paper_ids]
    return bibtex_entries([p for p in papers if p is not None])


__all__ = [
    "collect_report_evidence",
    "generate_project_report",
    "save_report_edit",
    "render_report_markdown",
    "render_report_bibtex",
    "_report_out",
]
