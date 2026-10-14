/**
 * 回答下方的「本次参考了 N 条历史记忆」（第三期设计方案 §4.2、§7，全部离线）。
 *
 * 钉住四条：列表来自服务端召回预览（与真实注入同源，界面不会和注入不一致）；
 * 每条都能说明来源与使用原因；「本次不使用」只作用于本会话且原记录保留
 * （P3-04）；「删除」必须二次确认（P3-06 的界面侧）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { RecalledMemoriesPanel } from "@/components/chat/RecalledMemoriesPanel";

const memoryApi = vi.hoisted(() => ({
  listRecalledMemories: vi.fn(),
  getMemory: vi.fn(),
  deleteMemory: vi.fn(),
  createMemory: vi.fn(),
  updateMemory: vi.fn(),
  listMemoryRevisions: vi.fn(),
}));
const sessionApi = vi.hoisted(() => ({
  ignoreMemory: vi.fn(),
  unignoreMemory: vi.fn(),
  listIgnoredMemoryIds: vi.fn(),
  listPromotionCandidates: vi.fn(),
}));
const researchApi = vi.hoisted(() => ({ listProjects: vi.fn() }));

vi.mock("@/api/memory", () => memoryApi);
vi.mock("@/api/sessionContext", () => sessionApi);
vi.mock("@/api/research", () => researchApi);

const RECALLED = {
  switches: {
    enabled: true,
    decisions: true,
    project: false,
    general: false,
    preferences: true,
  },
  max_count: 3,
  conversation_id: "c1",
  project_id: null,
  memories: [
    {
      id: "m1",
      content: "已确认：第三期采用关键词召回，不做向量检索",
      kind: "decision",
      kind_label: "已确认决策",
      scope: "personal",
      project_id: null,
      project_name: null,
      confirmed: true,
      status: "confirmed",
      source_label: "会话「智能聊天记忆设计」，2026-10-12",
      reason: "命中关键词：第三期、召回",
      bucket: "decision",
      importance: 4,
      update_time: null,
      expires_at: null,
      source_conversation_id: "c0",
    },
  ],
};

function renderPanel(conversationId?: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecalledMemoriesPanel query="第三期要不要做向量检索" conversationId={conversationId} />
    </QueryClientProvider>,
  );
}

async function expand() {
  fireEvent.click(
    await screen.findByText("本次参考了 1 条历史记忆"),
  );
}

describe("RecalledMemoriesPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    memoryApi.listRecalledMemories.mockResolvedValue(RECALLED);
    sessionApi.ignoreMemory.mockResolvedValue({ ignored: true });
    memoryApi.deleteMemory.mockResolvedValue({ deleted: true });
    researchApi.listProjects.mockResolvedValue([]);
  });

  afterEach(() => localStorage.clear());

  it("没有召回结果时不显示这一行（无关问题不打扰）", async () => {
    memoryApi.listRecalledMemories.mockResolvedValue({ ...RECALLED, memories: [] });
    renderPanel("c1");
    await waitFor(() => expect(memoryApi.listRecalledMemories).toHaveBeenCalled());
    expect(screen.queryByText(/本次参考了/)).toBeNull();
  });

  it("展开后能看到内容、来源、状态与使用原因（§7）", async () => {
    renderPanel("c1");
    await expand();
    expect(
      await screen.findByText("已确认：第三期采用关键词召回，不做向量检索"),
    ).toBeTruthy();
    expect(screen.getByText("已确认决策")).toBeTruthy();
    expect(screen.getByText("已确认")).toBeTruthy();
    expect(screen.getByText(/会话「智能聊天记忆设计」/)).toBeTruthy();
    expect(screen.getByText(/命中关键词：第三期、召回/)).toBeTruthy();
    // 召回预览按 (问题, 会话) 取，与注入同源
    expect(memoryApi.listRecalledMemories).toHaveBeenCalledWith({
      q: "第三期要不要做向量检索",
      conversationId: "c1",
      projectId: undefined,
    });
  });

  it("本次不使用：只调本会话的忽略接口，原记录不删（P3-04）", async () => {
    renderPanel("c1");
    await expand();
    fireEvent.click(await screen.findByRole("button", { name: "本次不使用" }));
    await waitFor(() =>
      expect(sessionApi.ignoreMemory).toHaveBeenCalledWith("c1", "m1"),
    );
    expect(memoryApi.deleteMemory).not.toHaveBeenCalled();
    expect(memoryApi.updateMemory).not.toHaveBeenCalled();
  });

  it("会话还没建立时不能设置本次不使用", async () => {
    renderPanel(undefined);
    await expand();
    expect(screen.getByRole("button", { name: "本次不使用" })).toBeDisabled();
    expect(sessionApi.ignoreMemory).not.toHaveBeenCalled();
  });

  it("删除需要二次确认，取消时不发出删除请求", async () => {
    renderPanel("c1");
    await expand();
    fireEvent.click(await screen.findByRole("button", { name: "删除" }));
    expect(await screen.findByText(/删除后所有会话都不再参考/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(screen.queryByText(/删除后所有会话都不再参考/)).toBeNull(),
    );
    expect(memoryApi.deleteMemory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(memoryApi.deleteMemory).toHaveBeenCalledWith("m1"));
  });

  it("说明历史记忆只是背景参考，本轮新要求优先（§5）", async () => {
    renderPanel("c1");
    await expand();
    expect(
      await screen.findByText(/最多参考 3 条/),
    ).toBeTruthy();
  });
});
