/**
 * 候选论文行的原文/摘要入口（全部离线，打桩研究接口）。
 *
 * 钉住两条要求：
 * - 「取原文」和「原文」必须是同一个按钮的两种状态 —— 没取到全文时只出现
 *   「取原文」，取到之后变成「原文」，点击在新标签页里显示 PDF；
 * - 摘要不再平铺在列表里，改由「摘要」按钮按需查看，并且要清洗 LaTeX 残留。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { PaperUnit, ResearchPaper } from "@/api/research";
import { PaperSourceActions } from "@/components/research/PaperSourceActions";

const api = vi.hoisted(() => ({
  ingestFulltext: vi.fn(),
  listUnits: vi.fn(),
  extractEvidence: vi.fn(),
}));

vi.mock("@/api/research", () => api);

function paper(overrides: Partial<ResearchPaper> = {}): ResearchPaper {
  return {
    project_paper_id: "pp1",
    paper_id: "p1",
    title: "记忆机制综述",
    authors: "张三; 李四",
    year: 2024,
    abstract: "targets \\text{unicode}(x2014)$a malicious agent",
    doi: "",
    arxiv_id: "2405.00001",
    url: "https://arxiv.org/pdf/2405.00001v2",
    source: "arxiv",
    sources: [],
    fulltext_status: "pdf",
    relevance: "",
    screening_status: "pending_review",
    screening_reason: "",
    first_run_id: "r1",
    fulltext_state: "abstract",
    unit_count: 1,
    fulltext_time: null,
    create_time: null,
    ...overrides,
  };
}

function unit(overrides: Partial<PaperUnit> = {}): PaperUnit {
  return {
    unit_id: "u1",
    paper_id: "p1",
    unit_kind: "block",
    page: 1,
    section: "",
    block_index: 0,
    text: "记忆机制让系统能够跨会话保存并检索历史信息。",
    char_count: 60,
    source_kind: "pdf",
    create_time: null,
    ...overrides,
  };
}

function renderActions(p: ResearchPaper) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PaperSourceActions projectId="proj1" paper={p} subQuestions={[]} />
    </QueryClientProvider>,
  );
}

describe("PaperSourceActions", () => {
  // 接口 mock 是模块级共享对象：不清调用记录的话，后一个用例会被前一个的点击污染
  beforeEach(() => vi.clearAllMocks());

  it("没取到全文时只有「取原文」，点击调用取原文接口", async () => {
    api.ingestFulltext.mockResolvedValue({ state: "fulltext", unit_count: 3, detail: "" });
    renderActions(paper({ fulltext_state: "abstract", unit_count: 1 }));

    expect(screen.queryByRole("button", { name: "原文" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "取原文" }));

    await waitFor(() =>
      expect(api.ingestFulltext).toHaveBeenCalledWith("proj1", "p1", {}),
    );
  });

  it("已取全文后按钮变成「原文」，点击在新标签页打开 PDF", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    renderActions(paper({ fulltext_state: "fulltext", unit_count: 53 }));

    expect(screen.queryByRole("button", { name: "取原文" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "原文" }));

    expect(open).toHaveBeenCalledWith(
      "https://arxiv.org/pdf/2405.00001v2",
      "_blank",
      expect.any(String),
    );
    expect(api.ingestFulltext).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it("原文只来自本地知识库时不开新标签页，退回原文单元视图", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    api.listUnits.mockResolvedValue([]);
    renderActions(
      paper({
        fulltext_state: "fulltext",
        unit_count: 12,
        url: "/knowledge_base/论文库/content/paper.pdf",
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "原文" }));

    expect(open).not.toHaveBeenCalled();
    // 对话框标题里的数量来自 listUnits，能等到它就说明走的是应用内单元视图
    expect(await screen.findByText(/原文单元（0）/)).toBeInTheDocument();
    open.mockRestore();
  });

  it("状态徽章本身就是原文单元入口，不再单独放一个按钮", async () => {
    api.listUnits.mockResolvedValue([]);
    renderActions(paper({ fulltext_state: "fulltext", unit_count: 53 }));

    expect(screen.queryByRole("button", { name: /^原文单元/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /已取全文 · 53 单元/ }));

    // 对话框标题里的数量来自 listUnits，能等到它就说明徽章真的把单元查询触发了
    expect(await screen.findByText(/原文单元（0）/)).toBeInTheDocument();
  });

  it("原文单元对话框：默认折叠、可搜索、断词只在显示时合并", async () => {
    api.listUnits.mockResolvedValue([
      unit({ text: "ISAC has been be- yond traditional separate communication." }),
      unit({
        unit_id: "u2",
        block_index: 1,
        page: 2,
        char_count: 140,
        text: "记忆机制让系统能够跨会话保存并检索历史信息。".repeat(6),
      }),
    ]);
    renderActions(paper({ fulltext_state: "fulltext", unit_count: 2 }));
    fireEvent.click(screen.getByRole("button", { name: /已取全文 · 2 单元/ }));

    // PDF 断词在显示时被合并（库里存的仍是 "be- yond"）
    expect(await screen.findByText(/has been beyond traditional/)).toBeInTheDocument();
    expect(screen.getByText(/2\/2 块 · 共 200 字/)).toBeInTheDocument();

    const toggles = screen.getAllByRole("button", { name: "展开全文" });
    expect(toggles).toHaveLength(2);
    fireEvent.click(toggles[0]);
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("在单元内搜索…"), {
      target: { value: "记忆" },
    });
    expect(await screen.findByText(/1\/2 块 · 共 200 字/)).toBeInTheDocument();
    expect(screen.queryByText(/has been beyond traditional/)).toBeNull();
  });

  it("「摘要」按钮打开对话框，并显示清洗后的摘要", async () => {
    renderActions(paper());

    fireEvent.click(screen.getByRole("button", { name: "摘要" }));

    expect(await screen.findByText(/targets — a malicious agent/)).toBeInTheDocument();
    expect(screen.queryByText(/\\text\{unicode\}/)).toBeNull();
  });
});
