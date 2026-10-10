/** 研究模块共用的中文标签与定位文案（证据/比较/报告都靠这些保持一致口径）。 */

export const EVIDENCE_TYPE_LABEL: Record<string, string> = {
  objective: "研究目标",
  method: "核心方法",
  dataset: "数据集",
  metric: "评测指标",
  result: "主要结果",
  conclusion: "结论",
  limitation: "局限",
};

export const REVIEW_LABEL: Record<string, string> = {
  pending: "待审核",
  confirmed: "已确认",
  edited: "已修改",
  rejected: "已驳回",
};

export const FULLTEXT_STATE_LABEL: Record<string, string> = {
  unknown: "未确认",
  none: "无原文",
  abstract: "仅摘要",
  fulltext: "已取全文",
};

export const COVERAGE_LABEL: Record<string, string> = {
  covered: "证据充分",
  thin: "证据偏薄",
  missing: "缺证据",
};

export const CONFLICT_LABEL: Record<string, string> = {
  open: "未裁决",
  resolved: "已裁决",
};

/** 证据定位文案：摘要级证据绝不显示页码，未核对的原文必须标注。 */
export function locatorText(card: {
  level: string;
  quote_verified: boolean;
  locator?: { page?: number | null; section?: string; block_index?: number | null };
}): string {
  const locator = card.locator ?? {};
  if (card.level === "abstract") {
    return card.quote_verified ? "摘要级证据" : "摘要级证据（原文未核对）";
  }
  const parts: string[] = [];
  if (locator.page) parts.push(`第 ${locator.page} 页`);
  if (locator.section) parts.push(locator.section);
  if (locator.block_index !== null && locator.block_index !== undefined) {
    parts.push(`文本块 ${locator.block_index}`);
  }
  const where = parts.length > 0 ? parts.join("，") : "全文证据";
  return card.quote_verified ? where : `${where}（原文未核对）`;
}
