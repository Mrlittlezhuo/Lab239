import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  editReport,
  exportReportMarkdown,
  generateComparisons,
  ingestFulltext,
  listEvidence,
  listReports,
  reviewEvidence,
  runArtifacts,
  setDimensions,
  supplementRun,
} from "@/api/research";
import { clearAuth } from "@/stores/auth";

type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

function mockFetch(impl: FetchFn) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function call(fetchMock: ReturnType<typeof mockFetch>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index];
  return {
    url,
    init,
    body: init.body === undefined ? undefined : JSON.parse(String(init.body)),
  };
}

describe("research api 契约", () => {
  beforeEach(() => {
    clearAuth();
    localStorage.clear();
  });

  afterEach(() => vi.unstubAllGlobals());

  it("证据列表按审核状态与是否含已驳回过滤", async () => {
    const fetchMock = mockFetch(async () => json(200, { evidence: [], total: 0 }));
    await listEvidence("p1", { review_status: "pending", include_rejected: false });
    expect(call(fetchMock).url).toBe(
      "/research/projects/p1/evidence?review_status=pending&include_rejected=false",
    );
    expect(call(fetchMock).init.method).toBe("GET");
  });

  it("审核证据走 PATCH 且带上审核状态", async () => {
    const fetchMock = mockFetch(async () => json(200, { evidence_id: "e1" }));
    await reviewEvidence("e1", { review_status: "rejected", note: "与本文无关" });
    const c = call(fetchMock);
    expect(c.url).toBe("/research/evidence/e1");
    expect(c.init.method).toBe("PATCH");
    expect(c.body).toEqual({ review_status: "rejected", note: "与本文无关" });
  });

  it("取原文按论文定位，幂等可重复调用", async () => {
    const fetchMock = mockFetch(async () => json(200, { state: "fulltext" }));
    await ingestFulltext("p1", "paper9", { run_id: "r1" });
    const c = call(fetchMock);
    expect(c.url).toBe("/research/projects/p1/papers/paper9/fulltext");
    expect(c.init.method).toBe("POST");
    expect(c.body).toEqual({ run_id: "r1" });
  });

  it("维度设置为 PUT，空数组表示恢复默认", async () => {
    const fetchMock = mockFetch(async () => json(200, { dimensions: [] }));
    await setDimensions("p1", []);
    const c = call(fetchMock);
    expect(c.url).toBe("/research/projects/p1/dimensions");
    expect(c.init.method).toBe("PUT");
    expect(c.body).toEqual({ dimensions: [] });
  });

  it("比较生成默认同时检测冲突", async () => {
    const fetchMock = mockFetch(async () => json(200, { written: 0 }));
    await generateComparisons("p1");
    expect(call(fetchMock).body).toEqual({});
  });

  it("纳入论文后可基于同一运行继续生成证据与报告", async () => {
    const fetchMock = mockFetch(
      async () => json(200, { run_id: "r1", status: "awaiting_confirmation", stage: "evidence_review" }),
    );
    const run = await runArtifacts("r1");
    const c = call(fetchMock);
    expect(c.url).toBe("/research/runs/r1/artifacts");
    expect(c.init.method).toBe("POST");
    expect(c.init.body).toBeUndefined();
    expect(run.status).toBe("awaiting_confirmation");
  });

  it("补充检索基于运行创建新运行", async () => {
    const fetchMock = mockFetch(async () => json(200, { run_id: "r2" }));
    await supplementRun("r1");
    const c = call(fetchMock);
    expect(c.url).toBe("/research/runs/r1/supplement");
    expect(c.init.method).toBe("POST");
  });

  it("报告版本列表可要求返回全部版本", async () => {
    const fetchMock = mockFetch(async () => json(200, { reports: [], total: 0 }));
    await listReports("p1", true);
    expect(call(fetchMock).url).toBe("/research/projects/p1/reports?include_versions=true");
  });

  it("保存报告编辑走 PATCH（后端插入新版本）", async () => {
    const fetchMock = mockFetch(async () => json(200, { report_id: "rep2" }));
    await editReport("rep1", { content: "新正文" });
    const c = call(fetchMock);
    expect(c.url).toBe("/research/reports/rep1");
    expect(c.init.method).toBe("PATCH");
    expect(c.body).toEqual({ content: "新正文" });
  });

  it("导出 Markdown 返回原始文本而非 JSON", async () => {
    const fetchMock = mockFetch(
      async () =>
        new Response("# 报告\n\n正文 [^1]\n\n[^1]: 论文 A — 第 3 页", {
          status: 200,
          headers: { "content-type": "text/markdown; charset=utf-8" },
        }),
    );
    const text = await exportReportMarkdown("rep1");
    expect(call(fetchMock).url).toBe("/research/reports/rep1/export.md");
    expect(text).toContain("[^1]: 论文 A — 第 3 页");
  });
});
