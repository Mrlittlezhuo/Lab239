import type { ReportCitation } from "@/api/research";

import { locatorText } from "@/components/research/labels";

/** 正文里的引用标记：`[[e:<证据id>]]`，与后端 `report.py` 的 CITATION_RE 一致。 */
const MARKER = /\[\[e:([0-9a-zA-Z]{6,64})\]\]/g;

export interface ReportLegendItem {
  index: number;
  citation: ReportCitation;
  /** 供界面直接展示的一行说明（含定位与原文）。 */
  label: string;
}

export interface ReportView {
  /** 引用标记替换成 `[n]` 后的 Markdown，可安全交给 react-markdown。 */
  markdown: string;
  legend: ReportLegendItem[];
  /** 正文里指向不存在证据的标记（后端保存时已剥离，出现即说明数据异常）。 */
  orphan: string[];
}

/**
 * 把报告正文的引用标记换成编号，并生成可反查的引用图例。
 * 编号顺序取后端 citations 的顺序，因此与导出的 Markdown 脚注一致。
 */
export function prepareReport(content: string, citations: ReportCitation[]): ReportView {
  const order = new Map<string, number>();
  citations.forEach((c, i) => order.set(c.evidence_id, i + 1));

  const orphan: string[] = [];
  const markdown = (content ?? "").replace(MARKER, (_marker, id: string) => {
    const index = order.get(id);
    if (!index) {
      if (!orphan.includes(id)) orphan.push(id);
      return "";
    }
    return `[${index}]`;
  });

  const legend = citations.map((citation, i) => ({
    index: i + 1,
    citation,
    label:
      `${citation.paper_title} — ${locatorText({
        level: citation.level,
        quote_verified: citation.verified,
        locator: citation.locator,
      })}：${citation.quote}`,
  }));

  return { markdown, legend, orphan };
}
