"""证据覆盖、薄弱点与补充检索建议（任务 018 阶段 B）。

覆盖检查是**纯函数**：输入已持久化的证据/计划/维度/冲突，输出可重现的
结构化结果——不叫模型、不联网，因此同样的数据永远得到同样的结论，
UI 与报告都能直接引用。

判定口径（任务 018 §5.2）：

- 子问题没有任何证据 → ``missing``；
- 只有单篇论文、或全部是摘要级证据、或全部原文未核对 → ``thin``；
- 其余 → ``covered``；
- 关键维度在整个项目里没有任何证据 → 计入 ``missing_dimensions``；
- 冲突由 ``research_conflict`` 提供（两侧都引用具体证据），这里只做汇总。

补充检索只产出**建议**（复用任务 017 计划里的检索式），是否真的开新一轮
由服务层与用户确认决定，且轮次有上限。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

#: 覆盖状态
COVERED = "covered"
THIN = "thin"
MISSING = "missing"


def _plan_sub_questions(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(plan.get("sub_questions") or []):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        out.append(
            {
                # 计划里的子问题没有显式 id，用稳定序号生成（与证据落库时一致）
                "id": str(item.get("id") or f"sq{index + 1}"),
                "question": question,
                "queries": [str(q) for q in (item.get("queries") or []) if str(q)],
            }
        )
    return out


def _summarize(cards: Sequence[Any]) -> Dict[str, Any]:
    papers = {c.paper_id for c in cards}
    levels = {c.level for c in cards}
    return {
        "evidence_count": len(cards),
        "paper_count": len(papers),
        "paper_ids": sorted(papers),
        "abstract_only": bool(cards) and levels == {"abstract"},
        "unverified_only": bool(cards) and all(not c.quote_verified for c in cards),
        "verified_count": sum(1 for c in cards if c.quote_verified),
        "levels": sorted(levels),
    }


def _status(stats: Dict[str, Any]) -> str:
    if stats["evidence_count"] == 0:
        return MISSING
    if (
        stats["paper_count"] < 2
        or stats["abstract_only"]
        or stats["unverified_only"]
    ):
        return THIN
    return COVERED


def build_coverage(
    *,
    plan: Dict[str, Any],
    dimensions: Sequence[Dict[str, Any]],
    evidence: Sequence[Any],
    conflicts: Sequence[Any] = (),
    included_paper_ids: Sequence[str] = (),
) -> Dict[str, Any]:
    """计算证据覆盖结果（可重现）。

    ``evidence`` 是证据卡片 ORM 行（或同名字段的对象），``conflicts`` 是
    冲突记录；两者都必须已经落库，因此结果天然可反查。
    """
    usable = [c for c in evidence if getattr(c, "review_status", "pending") != "rejected"]
    sub_questions = _plan_sub_questions(plan or {})

    by_sub: Dict[str, List[Any]] = {}
    for card in usable:
        by_sub.setdefault(card.sub_question_id or "", []).append(card)

    sub_rows: List[Dict[str, Any]] = []
    for sq in sub_questions:
        cards = by_sub.get(sq["id"], [])
        stats = _summarize(cards)
        status = _status(stats)
        sub_rows.append({**sq, **stats, "status": status})

    # 计划之外仍有证据的子问题（例如手工补充）也要如实呈现
    known = {row["id"] for row in sub_rows}
    for sub_id, cards in sorted(by_sub.items()):
        if not sub_id or sub_id in known:
            continue
        stats = _summarize(cards)
        sub_rows.append(
            {
                "id": sub_id,
                "question": "",
                "queries": [],
                **stats,
                "status": _status(stats),
            }
        )

    dimension_rows: List[Dict[str, Any]] = []
    for dim in dimensions:
        cards = [c for c in usable if c.evidence_type == dim["id"]]
        stats = _summarize(cards)
        dimension_rows.append(
            {"id": dim["id"], "label": dim.get("label") or dim["id"], **stats}
        )
    missing_dimensions = [d for d in dimension_rows if d["evidence_count"] == 0]

    suggestions: List[Dict[str, Any]] = []
    for row in sub_rows:
        if row["status"] == COVERED:
            continue
        reasons = []
        if row["status"] == MISSING:
            reasons.append("该子问题没有任何证据")
        else:
            if row["paper_count"] < 2:
                reasons.append(f"仅有 {row['paper_count']} 篇论文的证据")
            if row["abstract_only"]:
                reasons.append("全部为摘要级证据（未取得全文）")
            if row["unverified_only"]:
                reasons.append("全部证据的原文未能核对")
        suggestions.append(
            {
                "sub_question_id": row["id"],
                "question": row["question"],
                "reason": "；".join(reasons),
                # 复用任务 017 计划里的检索式，不另起一套检索逻辑
                "queries": list(row["queries"]),
                "requires_confirmation": True,
            }
        )

    unresolved = [c for c in conflicts if getattr(c, "status", "open") == "open"]
    papers_without_evidence = sorted(
        {pid for pid in included_paper_ids} - {c.paper_id for c in usable}
    )

    return {
        "sub_questions": sub_rows,
        "dimensions": dimension_rows,
        "missing_dimensions": [
            {"id": d["id"], "label": d["label"]} for d in missing_dimensions
        ],
        "papers_without_evidence": papers_without_evidence,
        "conflicts": [
            {
                "conflict_id": getattr(c, "id", ""),
                "topic": getattr(c, "topic", ""),
                "status": getattr(c, "status", "open"),
                "side_a": getattr(c, "side_a_json", "[]"),
                "side_b": getattr(c, "side_b_json", "[]"),
            }
            for c in unresolved
        ],
        "suggestions": suggestions,
        "totals": {
            "evidence": len(usable),
            "sub_questions": len(sub_rows),
            "covered": sum(1 for r in sub_rows if r["status"] == COVERED),
            "thin": sum(1 for r in sub_rows if r["status"] == THIN),
            "missing": sum(1 for r in sub_rows if r["status"] == MISSING),
            "open_conflicts": len(unresolved),
        },
    }


__all__ = ["COVERED", "THIN", "MISSING", "build_coverage"]
