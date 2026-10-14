/**
 * “记住 / 忘记”确认卡片（第二期设计方案 §4.3，打桩记忆接口，全部离线）。
 *
 * 钉住四条：范围默认值来自服务端推荐，含义不明确时不预选也不能保存（P2-07）；
 * 跨会话保存必须带类别（P2-08）；点「不保存」不产生任何写入请求（P2-09）；
 * “忘记”先列出将被停用的对象，确认时只停用列出的那几条（P2-09）；
 * 临时对话里的卡片不能确认保存（P2-11）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { MemoryProposal } from "@/api/memory";
import { MemoryProposalCard } from "@/components/chat/MemoryProposalCard";

const api = vi.hoisted(() => ({ applyIntent: vi.fn() }));
vi.mock("@/api/memory", () => api);

function proposal(over: Partial<MemoryProposal> = {}): MemoryProposal {
  return {
    kind: "remember",
    content: "本项目不用自动模型路由",
    recommended_scope: "session",
    scope_reason: "内容限定了本次任务/当前项目，默认只记本会话",
    recommended_category: null,
    conversation_temporary: false,
    targets: [],
    note: "",
    ...over,
  };
}

/** 注意：会话 id 必须显式传；传 undefined 表示"还没有会话"。 */
function renderCard(p: MemoryProposal, conversationId?: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryProposalCard proposal={p} conversationId={conversationId} />
    </QueryClientProvider>,
  );
}

function radio(name: string): HTMLInputElement {
  return screen.getByRole("radio", { name }) as HTMLInputElement;
}

describe("MemoryProposalCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.applyIntent.mockResolvedValue({
      scope: "session",
      kind: "remember",
      message: "已记入本会话上下文（不会带入其他会话）",
    });
  });

  afterEach(() => localStorage.clear());

  it("展示待保存内容，并按服务端推荐预选「仅本会话」（P2-07）", async () => {
    renderCard(proposal(), "c1");
    expect(screen.getByText("你希望保存以下内容：")).toBeTruthy();
    expect(screen.getByText("“本项目不用自动模型路由”")).toBeTruthy();
    expect(radio("仅本会话").checked).toBe(true);
    expect(radio("作为跨会话个人偏好").checked).toBe(false);
    // 服务端推荐依据也要看得见，用户才知道默认范围是怎么来的
    expect(
      screen.getByText(/内容限定了本次任务\/当前项目，默认只记本会话/),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() => expect(api.applyIntent).toHaveBeenCalled());
    expect(api.applyIntent.mock.calls[0][0]).toMatchObject({
      kind: "remember",
      content: "本项目不用自动模型路由",
      scope: "session",
      conversation_id: "c1",
    });
    expect(
      await screen.findByText("已记入本会话上下文（不会带入其他会话）"),
    ).toBeTruthy();
  });

  it("含义不明确时不预选，未选范围不能保存（P2-07）", async () => {
    renderCard(
      proposal({
        content: "不要用自动路由",
        recommended_scope: null,
        scope_reason: "未明确范围，请选择仅本会话还是跨会话个人偏好",
      }),
      "c1",
    );
    expect(radio("仅本会话").checked).toBe(false);
    expect(radio("作为跨会话个人偏好").checked).toBe(false);
    const confirm = screen.getByRole("button", { name: "确认保存" });
    expect(confirm).toBeDisabled();
    expect(
      screen.getByText("未明确范围，请选择仅本会话还是跨会话个人偏好"),
    ).toBeTruthy();

    fireEvent.click(radio("仅本会话"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "确认保存" })).toBeEnabled(),
    );
  });

  it("跨会话保存带上推荐类别（P2-08）", async () => {
    renderCard(
      proposal({
        content: "以后默认中文回答",
        recommended_scope: "preference",
        scope_reason: "内容表达的是长期习惯，默认作为跨会话个人偏好",
        recommended_category: "answer_style",
      }),
      "c1",
    );
    expect(radio("作为跨会话个人偏好").checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() => expect(api.applyIntent).toHaveBeenCalled());
    expect(api.applyIntent.mock.calls[0][0]).toMatchObject({
      scope: "preference",
      category: "answer_style",
    });
  });

  it("跨会话保存但推不出类别时不能保存", () => {
    renderCard(
      proposal({
        content: "以后按我给的顺序来",
        recommended_scope: "preference",
        recommended_category: null,
      }),
      "c1",
    );
    expect(screen.getByText("请选择类别")).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  });

  it("点「不保存」不产生任何写入请求（P2-09）", () => {
    renderCard(proposal(), "c1");
    fireEvent.click(screen.getByRole("button", { name: "不保存" }));
    expect(api.applyIntent).not.toHaveBeenCalled();
    expect(
      screen.getByText("未保存：没有新增个人偏好，也没有加入本会话已确认事项。"),
    ).toBeTruthy();
  });

  it("“忘记”列出将被停用的记忆，确认时只停用列出的那几条（P2-09）", async () => {
    renderCard(
      proposal({
        kind: "forget",
        content: "我偏好简短回答",
        recommended_scope: "preference",
        scope_reason: "内容表达的是长期习惯，默认作为跨会话个人偏好",
        targets: [
          { id: "m1", content: "回答偏好：简洁", category: "answer_style" },
        ],
        note: "将停用 1 条个人记忆（停用不删除历史聊天）。",
      }),
      "c1",
    );
    expect(screen.getByText("你希望忘记以下内容：")).toBeTruthy();
    expect(screen.getByText("回答偏好：简洁")).toBeTruthy();
    expect(screen.getByText(/不删除聊天历史/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    await waitFor(() => expect(api.applyIntent).toHaveBeenCalled());
    expect(api.applyIntent.mock.calls[0][0]).toMatchObject({
      kind: "forget",
      scope: "preference",
      target_ids: ["m1"],
    });
  });

  it("临时对话里的卡片不能确认保存，并说明原因（P2-11）", () => {
    renderCard(
      proposal({
        content: "默认中文",
        recommended_scope: "preference",
        recommended_category: "answer_style",
        conversation_temporary: true,
        note: "临时对话不保存记忆；如需保存，请退出临时对话后再确认。",
      }),
      "c1",
    );
    expect(
      screen.getByText("临时对话不保存记忆；请退出临时对话后再确认保存。"),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  });

  it("尚未建会话时不能保存到「仅本会话」", () => {
    renderCard(proposal());
    expect(
      screen.getByText(/发送第一条消息建立会话后再保存到本会话/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认保存" })).toBeDisabled();
  });
});
