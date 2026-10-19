/**
 * 研究讨论面板的渲染约定（全部离线打桩 supervision 接口）。
 *
 * 钉住最容易被改坏的三条后端语义：
 * 1. 发言先落库、回复后生成：模型未接入时接口 503，界面必须显示成
 *    「发言已保存，AI 尚未回复」+ 补回复入口，而不是「发送失败」；
 * 2. 已有回复的发言再补回复返回 409，后端文案原样显示；
 * 3. 提取研究定义遇到 503 时显示成说明，并给出手动填写入口。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { DialogueMessage, ProjectDetail, ResearchDialogue } from "@/api/supervision";
import { ApiError } from "@/lib/errors";
import { DialoguePanel } from "@/components/supervision/DialoguePanel";

const api = vi.hoisted(() => ({
  listDialogues: vi.fn(),
  getDialogueMessages: vi.fn(),
  getProjectContext: vi.fn(),
  createDialogue: vi.fn(),
  sendDialogueMessage: vi.fn(),
  regenerateDialogueReply: vi.fn(),
  extractBrief: vi.fn(),
}));
vi.mock("@/api/supervision", () => api);

function project(): ProjectDetail {
  return {
    project_id: "p1",
    code: "SP-001",
    name: "中文摘要质量研究",
    description: "",
    status: "active",
    my_role: "student",
    members: [{ user_id: "u1", role: "student", status: "active", joined_at: null, removed_at: null }],
  };
}

function dialogue(overrides: Partial<ResearchDialogue> = {}): ResearchDialogue {
  return {
    conversation_id: "c1",
    code: "CV-001",
    name: "主研究会话",
    chat_type: "agent_chat",
    supervision_project_id: "p1",
    message_count: 0,
    create_time: "2026-10-19T09:00:00",
    update_time: "2026-10-19T09:00:00",
    ...overrides,
  };
}

function pendingMessage(overrides: Partial<DialogueMessage> = {}): DialogueMessage {
  return {
    message_id: "m1",
    query: "我想把中文摘要的评估拆成两个子问题",
    response: "",
    metadata: { pending_reply: true, project_code: "SP-001" },
    create_time: "2026-10-19T09:05:00",
    ...overrides,
  };
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DialoguePanel project={project()} refresh={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  api.listDialogues.mockResolvedValue([dialogue()]);
  api.getProjectContext.mockResolvedValue({ project_id: "p1", context: "", has_project_context: false });
  api.getDialogueMessages.mockResolvedValue({ conversation_id: "c1", code: "CV-001", messages: [] });
  api.createDialogue.mockResolvedValue(dialogue());
  api.sendDialogueMessage.mockResolvedValue(pendingMessage());
  api.regenerateDialogueReply.mockResolvedValue(pendingMessage({ response: "可以拆成：…" }));
  api.extractBrief.mockResolvedValue({ display_code: "RB-001-v1" });
});

describe("进入项目时的默认会话", () => {
  it("会话为空时自动建立自己的会话，不用先点「新建讨论」", async () => {
    api.listDialogues.mockResolvedValueOnce([]);

    renderPanel();
    await waitFor(() =>
      expect(api.createDialogue).toHaveBeenCalledWith("p1", {
        title: "中文摘要质量研究 研究讨论",
      }),
    );

    const composer = (await screen.findByLabelText("接着讨论")) as HTMLTextAreaElement;
    await waitFor(() => expect(composer.disabled).toBe(false));
    // 会话名出现在侧栏与讨论记录标题两处
    expect((await screen.findAllByText("主研究会话")).length).toBeGreaterThanOrEqual(1);
  });
});

describe("发言与回复的两步语义", () => {
  it("模型未接入时 503：显示已保存待回复，不显示发送失败", async () => {
    let calls = 0;
    api.getDialogueMessages.mockImplementation(async () => {
      calls += 1;
      return {
        conversation_id: "c1",
        code: "CV-001",
        messages: calls > 1 ? [pendingMessage()] : [],
      };
    });
    api.sendDialogueMessage.mockRejectedValue(
      new ApiError(503, "模型能力尚未接入：这条发言已保存，AI 回复可在模型接入后补生成。"),
    );

    renderPanel();
    const composer = await screen.findByLabelText("接着讨论");
    fireEvent.change(composer, { target: { value: "我想把中文摘要的评估拆成两个子问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送并请求 AI 回复" }));

    expect(await screen.findByText("发言已保存，AI 尚未回复")).toBeInTheDocument();
    expect(screen.queryByText("发送失败")).toBeNull();
    expect(await screen.findByText("AI 尚未回复")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新生成回复" })).toBeInTheDocument();
  });

  it("已有回复再补生成：409 文案原样显示", async () => {
    api.getDialogueMessages.mockResolvedValue({
      conversation_id: "c1",
      code: "CV-001",
      messages: [pendingMessage()],
    });
    api.regenerateDialogueReply.mockRejectedValue(
      new ApiError(409, "这条消息已经有回复，不需要重新生成"),
    );

    renderPanel();
    const retry = await screen.findByRole("button", { name: "重新生成回复" });
    fireEvent.click(retry);

    expect(await screen.findByText("这条消息已经有回复，不需要重新生成")).toBeInTheDocument();
  });

  it("空正文在前端就挡住，不发请求", async () => {
    renderPanel();
    await screen.findByLabelText("接着讨论");
    expect(screen.getByRole("button", { name: "发送并请求 AI 回复" })).toBeDisabled();
    expect(api.sendDialogueMessage).not.toHaveBeenCalled();
  });
});

describe("从讨论提取研究定义", () => {
  it("模型未接入时把 503 显示成说明并给出手动入口", async () => {
    api.getDialogueMessages.mockResolvedValue({
      conversation_id: "c1",
      code: "CV-001",
      messages: [pendingMessage({ response: "可以拆成：…" })],
    });
    api.extractBrief.mockRejectedValue(
      new ApiError(503, "模型能力尚未接入：从聊天提取研究内容需要注入生成模型。"),
    );

    renderPanel();
    await waitFor(() => expect(api.getDialogueMessages).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "提取研究定义草稿" }));

    expect(await screen.findByText("模型能力尚未接入")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "手动填写研究定义" })).toBeInTheDocument();
  });
});
