import { describe, expect, it } from "vitest";

import { prepareReport } from "@/components/research/reportText";
import { locatorText } from "@/components/research/labels";
import type { ReportCitation } from "@/api/research";

function citation(evidenceId: string, over: Partial<ReportCitation> = {}): ReportCitation {
  return {
    marker: `[[e:${evidenceId}]]`,
    evidence_id: evidenceId,
    paper_id: `p-${evidenceId}`,
    paper_title: "论文 A",
    quote: "Exact match measures the answer string.",
    level: "fulltext",
    locator: { page: 3, section: "Evaluation", block_index: 2 },
    verified: true,
    ...over,
  };
}

describe("locatorText", () => {
  it("摘要级证据绝不显示页码或章节", () => {
    expect(
      locatorText({ level: "abstract", quote_verified: true, locator: { page: null, section: "abstract", block_index: 0 } }),
    ).toBe("摘要级证据");
  });

  it("全文证据给出定位，未核对的必须标注", () => {
    expect(
      locatorText({ level: "fulltext", quote_verified: true, locator: { page: 3, section: "Evaluation", block_index: 2 } }),
    ).toBe("第 3 页，Evaluation，文本块 2");
    expect(
      locatorText({ level: "fulltext", quote_verified: false, locator: { page: 7, block_index: 1 } }),
    ).toBe("第 7 页，文本块 1（原文未核对）");
  });
});

describe("prepareReport", () => {
  it("引用标记换成编号，顺序与后端 citations 一致", () => {
    const citations = [citation("a1a1a1"), citation("b2b2b2")];
    const view = prepareReport(
      `结论一 [[e:a1a1a1]]；结论二 [[e:b2b2b2]]；重复 [[e:a1a1a1]]`,
      citations,
    );
    expect(view.markdown).toBe("结论一 [1]；结论二 [2]；重复 [1]");
    expect(view.legend.map((l) => l.index)).toEqual([1, 2]);
    expect(view.legend[0].label).toContain("论文 A — 第 3 页，Evaluation，文本块 2");
    expect(view.legend[0].label).toContain("Exact match measures the answer string.");
    expect(view.orphan).toEqual([]);
  });

  it("指向不存在证据的标记被隐藏并如实上报", () => {
    const view = prepareReport("只有 [[e:ffffffffffff]] 和 [[e:a1a1a1]]", [citation("a1a1a1")]);
    expect(view.markdown).toBe("只有  和 [1]");
    expect(view.orphan).toEqual(["ffffffffffff"]);
  });

  it("摘要级/未核对的引用在图例里如实标注", () => {
    const view = prepareReport("结论 [[e:a1a1a1]]", [
      citation("a1a1a1", { level: "abstract", locator: {}, verified: false }),
    ]);
    expect(view.legend[0].label).toContain("摘要级证据（原文未核对）");
  });
});
