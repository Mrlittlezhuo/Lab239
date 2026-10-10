"""比较 / 冲突 / 覆盖 / 补充检索的服务层（任务 018 阶段 B）。

- 比较与冲突只以**已落库的证据卡片**为事实入口：生成前把证据集合喂给
  模型，生成后按 id 反查校验，无效引用被丢弃，因此任何一条比较或冲突
  都能追溯到真实原文；
- 覆盖检查是纯函数计算（``research.coverage``），同样的数据得到同样的
  结果，可重现；
- 补充检索只创建**新的可见运行**（复用任务 017 的创建/确认/检索流程），
  轮次有上限，绝不自动自循环。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.models.research_model import PaperRecordModel
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.comparison import (
    ComparisonGenerator,
    EvidenceRef,
    build_default_generator,
)
from chatchat.server.research.coverage import build_coverage
from chatchat.utils import build_logger

logger = build_logger()


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (json.JSONDecodeError, TypeError):
        return default


def latest_run_plan(session: Session, project_id: str, owner_id: str) -> Dict[str, Any]:
    """项目最近一次运行的计划（覆盖检查按它统计子问题）。"""
    runs = repo.list_runs(session, owner_id, project_id=project_id, limit=1)
    if not runs:
        return {}
    return _loads(runs[0].plan_json, {}) or {}


def collect_evidence_refs(
    session: Session,
    project_id: str,
    owner_id: str,
    *,
    paper_ids: Optional[Sequence[str]] = None,
    include_rejected: bool = False,
) -> List[EvidenceRef]:
    """把项目证据转成可引用集合（只含本项目/本用户的证据）。"""
    cards = repo.list_evidence(
        session,
        project_id,
        owner_id,
        include_rejected=include_rejected,
    )
    if paper_ids is not None:
        wanted = set(paper_ids)
        cards = [c for c in cards if c.paper_id in wanted]
    titles: Dict[str, str] = {}
    refs: List[EvidenceRef] = []
    for card in cards:
        if card.paper_id not in titles:
            paper = session.get(PaperRecordModel, card.paper_id)
            titles[card.paper_id] = paper.title if paper is not None else card.paper_id
        refs.append(
            EvidenceRef(
                evidence_id=card.id,
                paper_id=card.paper_id,
                paper_title=titles[card.paper_id],
                evidence_type=card.evidence_type,
                level=card.level,
                summary=card.summary,
                quote=card.quote,
                sub_question_id=card.sub_question_id,
                verified=bool(card.quote_verified),
            )
        )
    return refs


def included_paper_ids(session: Session, project_id: str, owner_id: str) -> List[str]:
    """本项目已纳入（screening=included）的论文。"""
    return [
        pp.paper_id
        for pp, _paper in repo.list_project_papers(
            session, project_id, owner_id, screening_status="included"
        )
    ]


def generate_project_comparisons(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    run_id: Optional[str] = None,
    generator: Any = None,
    model_name: str = "",
    dimension_ids: Optional[Sequence[str]] = None,
    detect_conflicts: bool = True,
) -> Dict[str, Any]:
    """按维度生成比较单元并检测冲突，全部落库（幂等）。

    只对**已纳入论文**的非驳回证据生成；模型输出中的无效引用被拒绝并
    如实返回原因。
    """
    project = repo.get_owned_project(session, project_id, owner_id)
    if project is None:
        raise repo.ResearchResourceNotFound(project_id)
    dimensions = repo.parse_dimensions_json(project.dimensions_json)
    if dimension_ids:
        wanted = set(dimension_ids)
        dimensions = [d for d in dimensions if d["id"] in wanted] or dimensions

    papers = included_paper_ids(session, project_id, owner_id)
    refs = collect_evidence_refs(session, project_id, owner_id, paper_ids=papers)
    if not refs:
        return {
            "written": 0,
            "skipped": 0,
            "conflicts": 0,
            "rejected": ["没有可用于比较的证据（请先纳入论文并提取证据）"],
        }

    generator = generator or build_default_generator(model_name or None)
    comparisons, rejected = generator.generate_comparisons(
        question=project.research_question, dimensions=dimensions, evidence=refs
    )
    stored = repo.replace_comparison_units(
        session,
        project_id=project_id,
        owner_id=owner_id,
        run_id=run_id,
        units=[
            {
                "paper_id": c.paper_id,
                "dimension_id": c.dimension_id,
                "content": c.content,
                "evidence_ids": c.evidence_ids,
            }
            for c in comparisons
        ],
    )
    rejected.extend(stored["rejected"])

    conflict_count = 0
    if detect_conflicts:
        try:
            conflicts, conflict_rejected = generator.detect_conflicts(
                question=project.research_question, evidence=refs
            )
        except Exception as exc:  # 冲突检测失败不影响已生成的比较
            logger.warning("冲突检测失败（忽略）", exc_info=True)
            conflicts, conflict_rejected = [], [f"冲突检测失败：{exc}"]
        rejected.extend(conflict_rejected)
        for draft in conflicts:
            try:
                _, action = repo.record_conflict(
                    session,
                    project_id=project_id,
                    owner_id=owner_id,
                    topic=draft.topic,
                    description=draft.description,
                    sub_question_id=draft.sub_question_id,
                    dimension_id=draft.dimension_id,
                    side_a=[{"evidence_id": e} for e in draft.side_a],
                    side_b=[{"evidence_id": e} for e in draft.side_b],
                    run_id=run_id,
                )
                if action == "created":
                    conflict_count += 1
            except ValueError as exc:
                rejected.append(str(exc))

    return {
        "written": stored["written"],
        "skipped": stored["skipped"],
        "conflicts": conflict_count,
        "rejected": rejected,
        "dimensions": dimensions,
        "evidence_used": len(refs),
    }


def project_coverage(session: Session, *, project_id: str, owner_id: str) -> Dict[str, Any]:
    """项目证据覆盖结果（纯计算，可重现）。"""
    project = repo.get_owned_project(session, project_id, owner_id)
    if project is None:
        raise repo.ResearchResourceNotFound(project_id)
    return build_coverage(
        plan=latest_run_plan(session, project_id, owner_id),
        dimensions=repo.parse_dimensions_json(project.dimensions_json),
        evidence=repo.list_evidence(session, project_id, owner_id),
        conflicts=repo.list_conflicts(session, project_id, owner_id),
        included_paper_ids=included_paper_ids(session, project_id, owner_id),
    )


def start_supplementary_run(
    session: Session, *, project_id: str, owner_id: str, parent_run_id: str
) -> Dict[str, Any]:
    """基于上一轮运行创建补充检索的新运行（需再次确认，轮次有界）。"""
    run = repo.create_supplementary_run(session, owner_id, project_id, parent_run_id)
    return {
        "run_id": run.id,
        "project_id": run.project_id,
        "parent_run_id": run.parent_run_id,
        "round_no": run.round_no,
        "max_rounds": repo.MAX_RESEARCH_ROUNDS,
        "status": run.status,
    }


__all__ = [
    "latest_run_plan",
    "collect_evidence_refs",
    "included_paper_ids",
    "generate_project_comparisons",
    "project_coverage",
    "start_supplementary_run",
]
