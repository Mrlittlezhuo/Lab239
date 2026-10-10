"""证据服务层（任务 018 阶段 A）。

把"原文获取 → 原文单元落库 → 证据提取 → 原文核对 → 幂等落库"串成一条
可重试、可审计的链路，供 API 与研究 LangGraph 节点共用：

- **先落原文再谈证据**：没有原文单元就没有证据；取不到全文时降级为
  摘要级单元，产出的证据自动标记 ``level='abstract'``（不伪造页码）；
- **幂等**：原文按内容哈希跳过重写；证据按 ``(项目, 论文, 子问题, 类型,
  原文)`` 幂等键去重，重跑不产生重复卡片；
- **用户审核优先**：已被用户确认/编辑/驳回的卡片在重跑时**不被覆盖**，
  返回 ``kept_reviewed`` 供 UI 如实说明；
- **所有者隔离**：全部读写经 repository 的 id + owner 双条件。

模型与原文来源均可注入；测试注入假件，不联网、不调模型。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from chatchat.server.db.models.research_model import PaperRecordModel
from chatchat.server.db.repository import research_repository as repo
from chatchat.server.research.evidence import (
    EvidenceExtraction,
    build_default_extractor,
    find_quote,
)
from chatchat.server.research.fulltext import (
    FullTextProvider,
    PaperUnit,
    collect_units,
    default_providers,
)
from chatchat.utils import build_logger

logger = build_logger()


def _session_factory() -> Session:
    """持久化使用的会话工厂；测试中 monkeypatch 指向临时数据库。"""
    from chatchat.server.db.session import SessionLocal

    return SessionLocal()


def _to_paper_unit(model) -> PaperUnit:
    """DB 原文单元 → 提取层使用的单元（定位一致）。"""
    return PaperUnit(
        text=model.text,
        unit_kind=model.unit_kind,
        page=model.page,
        section=model.section,
        block_index=model.block_index,
        source_kind=model.source_kind,
    )


def get_paper_units(
    session: Session, project_id: str, owner_id: str, paper_id: str
) -> List[PaperUnit]:
    """读取本项目某论文已入库的原文单元（提取/重新核对共用）。"""
    return [_to_paper_unit(m) for m in repo.list_paper_units(session, project_id, owner_id, paper_id)]


def ingest_paper_fulltext(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    paper_id: str,
    run_id: Optional[str] = None,
    providers: Optional[Sequence[FullTextProvider]] = None,
    kb_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """获取并落库某论文在本项目内的原文单元（幂等）。

    返回 ``{state, created, changed, detail, unit_count}``；
    ``state`` 为 ``fulltext``/``abstract``/``none``，``detail`` 是降级或
    失败原因（用户可见，不含敏感信息）。
    """
    paper = session.get(PaperRecordModel, paper_id)
    if paper is None:
        raise repo.ResearchResourceNotFound(paper_id)
    # 论文必须已纳入本项目（原文是项目内私有产物）
    repo.get_project_paper(session, project_id, paper_id, owner_id)

    result = collect_units(
        paper,
        providers if providers is not None else default_providers(kb_names=kb_names),
    )
    stored = repo.replace_paper_units(
        session,
        project_id=project_id,
        owner_id=owner_id,
        paper_id=paper_id,
        run_id=run_id,
        units=[u.to_dict() for u in result.units],
    )
    return {
        "state": stored["state"],
        "created": stored["created"],
        "changed": stored["changed"],
        "fulltext_hash": stored["fulltext_hash"],
        "unit_count": len(repo.list_paper_units(session, project_id, owner_id, paper_id)),
        "detail": result.detail,
        "source_kind": result.source_kind,
    }


def extract_paper_evidence(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    paper_id: str,
    sub_question_id: str,
    question: str,
    sub_question: str,
    extractor: Any = None,
    model_name: str = "",
    run_id: Optional[str] = None,
    providers: Optional[Sequence[FullTextProvider]] = None,
) -> Dict[str, Any]:
    """按子问题提取一篇论文的证据并落库（原文核对 + 幂等 + 保护已审核）。

    - 没有原文单元时先尝试获取（``providers``）；仍没有则返回
      ``{"error": ...}``，不产生任何卡片；
    - 提取器输出的每条 quote 必须能在给定单元中逐字定位，否则该条被
      拒绝并计入 ``rejected``（用户可见），**不会**入库为已核对证据；
    - 已审核卡片不被重跑覆盖。
    """
    repo.get_project_paper(session, project_id, paper_id, owner_id)
    units = get_paper_units(session, project_id, owner_id, paper_id)
    ingest_detail = ""
    if not units:
        ingested = ingest_paper_fulltext(
            session,
            project_id=project_id,
            owner_id=owner_id,
            paper_id=paper_id,
            run_id=run_id,
            providers=providers,
        )
        ingest_detail = ingested.get("detail", "")
        units = get_paper_units(session, project_id, owner_id, paper_id)
    if not units:
        return {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "kept_reviewed": 0,
            "rejected": ["该论文没有可用的原文或摘要，无法提取证据"],
            "total": 0,
            "detail": ingest_detail,
        }

    paper = session.get(PaperRecordModel, paper_id)
    extractor = extractor or build_default_extractor(model_name or None)
    extraction: EvidenceExtraction = extractor.extract(
        question=question,
        sub_question=sub_question,
        paper=paper,
        units=units,
    )

    counts = {"created": 0, "updated": 0, "unchanged": 0, "kept_reviewed": 0}
    rejected: List[str] = list(extraction.rejected)
    extraction_model = getattr(extractor, "model_name", "") or model_name or ""
    for draft in extraction.drafts:
        try:
            _, action = repo.upsert_evidence(
                session,
                project_id=project_id,
                owner_id=owner_id,
                paper_id=paper_id,
                sub_question_id=sub_question_id,
                evidence_type=draft.evidence_type,
                quote=draft.quote,
                summary=draft.summary,
                unit=_unit_model_for(session, project_id, owner_id, paper_id, draft.unit),
                run_id=run_id,
                level=draft.level,
                source_kind=draft.unit.source_kind,
                extraction_model=extraction_model,
                extraction_mode="llm",
                quote_verified=draft.verified,
            )
        except ValueError as exc:
            rejected.append(str(exc))
            continue
        counts[action] = counts.get(action, 0) + 1
    return {
        **counts,
        "rejected": rejected,
        "total": sum(counts.values()),
        "detail": ingest_detail,
    }


def _unit_model_for(
    session: Session,
    project_id: str,
    owner_id: str,
    paper_id: str,
    unit: PaperUnit,
):
    """按 kind + 槽位找回落库后的原文单元 ORM 行。"""
    for model in repo.list_paper_units(session, project_id, owner_id, paper_id):
        if model.unit_kind == unit.unit_kind and model.block_index == unit.block_index:
            return model
    return None


def reverify_evidence(
    session: Session,
    *,
    project_id: str,
    owner_id: str,
    paper_id: Optional[str] = None,
) -> Dict[str, Any]:
    """按当前入库的原文单元重新核对证据原文，回写 ``quote_verified``。

    用于"原文重新解析后证据是否仍可核对"：证据保存了 quote 与定位快照，
    单元被替换后可能不再逐字匹配，此时如实标记为未核对。
    """
    if repo.get_owned_project(session, project_id, owner_id) is None:
        raise repo.ResearchResourceNotFound(project_id)
    cards = repo.list_evidence(session, project_id, owner_id, paper_id=paper_id)
    verified = unverified = 0
    for card in cards:
        units = get_paper_units(session, project_id, owner_id, card.paper_id)
        ok = find_quote(card.quote, units) is not None
        if bool(card.quote_verified) != ok:
            repo.set_evidence_verified(session, card.id, owner_id, ok)
        if ok:
            verified += 1
        else:
            unverified += 1
    return {"verified": verified, "unverified": unverified, "total": len(cards)}


__all__ = [
    "_session_factory",
    "get_paper_units",
    "ingest_paper_fulltext",
    "extract_paper_evidence",
    "reverify_evidence",
]
