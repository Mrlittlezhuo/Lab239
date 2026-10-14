/**
 * 会话上下文抽屉（第二期设计方案 §4.2，打桩上下文接口，全部离线）。
 *
 * 钉住四条：抽屉里能看到主题/已确认事项/待解决问题/摘要（P2-01 的界面侧）；
 * 编辑按"每行一条"提交为列表（P2-05）；重置必须二次确认且只调上下文接口
 * （P2-06）；临时对话不展示可编辑内容，而未建会话时可以在新建前打开临时开关
 * （P2-10 / §4.4）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SessionContextBadge } from "@/components/chat/SessionContextBadge";
import { useChatPrefs } from "@/stores/chatPrefs";

const api = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  editSessionContext: vi.fn(),
  refreshSessionContext: vi.fn(),
  resetSessionContext: vi.fn(),
}));

vi.mock("@/api/sessionContext", () => api);

const CONTEXT = {
  conversation_id: "c1",
  temporary: false,
  topic: "智能聊天记忆设计",
  goal: "确定三期功能边界",
  confirmed: ["第一期只做个人偏好记忆", "暂不做自动模型路由"],
  constraints: ["不重建公共向量索引"],
  open_questions: ["会话摘要由谁生成、何时更新"],
  summary: "已讨论第一期只做个人偏好，第二期新增会话上下文。",
  summarized_count: 4,
  update_time: null,
};

function renderBadge(conversationId?: string, conversationKey = "c1") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SessionContextBadge
        conversationId={conversationId}
        conversationKey={conversationKey}
      />
    </QueryClientProvider>,
  );
}

async function openDrawer() {
  fireEvent.click(screen.getByRole("button", { name: "上下文" }));
  await waitFor(() => expect(screen.getByText("本会话上下文")).toBeTruthy());
}

describe("SessionContextBadge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getSessionContext.mockResolvedValue(CONTEXT);
    api.editSessionContext.mockResolvedValue(CONTEXT);
    api.refreshSessionContext.mockResolvedValue(CONTEXT);
    api.resetSessionContext.mockResolvedValue({ reset: true });
    useChatPrefs.setState({ temporary: {} });
  });

  afterEach(() => {
    useChatPrefs.setState({ temporary: {} });
    localStorage.clear();
  });

  it("抽屉列出主题、目标、已确认事项、限制条件、待解决问题与摘要", async () => {
    renderBadge("c1");
    await waitFor(() =>
      expect(screen.getByText("本会话上下文已开启")).toBeTruthy(),
    );
    await openDrawer();

    expect(await screen.findByText("智能聊天记忆设计")).toBeTruthy();
    expect(screen.getByText("确定三期功能边界")).toBeTruthy();
    expect(screen.getByText("第一期只做个人偏好记忆")).toBeTruthy();
    expect(screen.getByText("暂不做自动模型路由")).toBeTruthy();
    expect(screen.getByText("不重建公共向量索引")).toBeTruthy();
    expect(screen.getByText("会话摘要由谁生成、何时更新")).toBeTruthy();
    expect(
      screen.getByText("已讨论第一期只做个人偏好，第二期新增会话上下文。"),
    ).toBeTruthy();
    // 摘要覆盖范围要说明清楚：最近的关键消息仍以原文参与回答
    expect(screen.getByText(/已覆盖 4 条较早消息/)).toBeTruthy();
  });

  it("编辑摘要：每行一条提交为列表（用户编辑不被自动更新覆盖）", async () => {
    renderBadge("c1");
    await openDrawer();

    fireEvent.click(await screen.findByRole("button", { name: "编辑摘要" }));
    const confirmed = await screen.findByLabelText("已确认事项（每行一条）");
    fireEvent.change(confirmed, {
      target: { value: "第一期只做个人偏好记忆\n第二期新增会话上下文" },
    });
    fireEvent.change(screen.getByLabelText("较早内容摘要"), {
      target: { value: "改过的摘要" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存编辑" }));

    await waitFor(() => expect(api.editSessionContext).toHaveBeenCalled());
    expect(api.editSessionContext.mock.calls[0][0]).toBe("c1");
    expect(api.editSessionContext.mock.calls[0][1]).toMatchObject({
      confirmed: ["第一期只做个人偏好记忆", "第二期新增会话上下文"],
      summary: "改过的摘要",
      // 未改动的组仍按原值整组提交，避免"漏字段"被服务端当成清空
      constraints: ["不重建公共向量索引"],
      open_questions: ["会话摘要由谁生成、何时更新"],
    });
  });

  it("重置本会话上下文需要二次确认，且只调上下文接口（P2-06）", async () => {
    renderBadge("c1");
    await openDrawer();

    fireEvent.click(
      await screen.findByRole("button", { name: "重置本会话上下文" }),
    );
    expect(
      await screen.findByText(/聊天历史与个人偏好不受影响/),
    ).toBeTruthy();
    expect(api.resetSessionContext).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认重置" }));
    await waitFor(() => expect(api.resetSessionContext).toHaveBeenCalledWith("c1"));
    expect(api.editSessionContext).not.toHaveBeenCalled();
  });

  it("更新摘要走 refresh 接口", async () => {
    renderBadge("c1");
    await openDrawer();
    fireEvent.click(await screen.findByRole("button", { name: "更新摘要" }));
    await waitFor(() =>
      expect(api.refreshSessionContext).toHaveBeenCalledWith("c1"),
    );
  });

  it("临时对话：徽章说明不保存上下文，抽屉里没有可编辑内容（P2-10）", async () => {
    api.getSessionContext.mockResolvedValue({
      ...CONTEXT,
      temporary: true,
      topic: "",
      goal: "",
      confirmed: [],
      constraints: [],
      open_questions: [],
      summary: "",
      summarized_count: 0,
    });
    renderBadge("c1");
    await waitFor(() =>
      expect(screen.getByText("临时对话 · 不保存上下文")).toBeTruthy(),
    );
    await openDrawer();
    expect(
      await screen.findByText(/临时对话不保存会话上下文/),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "编辑摘要" }),
    ).toBeNull();
    // 会话已创建后临时标记不能改（没有对应接口，只能新建会话时选）
    expect(screen.getByRole("switch", { name: "临时对话" })).toBeDisabled();
  });

  it("尚未建会话时可以提前打开临时对话开关（§4.4 新建时选择）", async () => {
    renderBadge(undefined, "new");
    await openDrawer();
    const toggle = await screen.findByRole("switch", { name: "临时对话" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(useChatPrefs.getState().temporary).toEqual({ new: true }),
    );
    // 还没建会话，因此没有任何上下文接口被调用
    expect(api.getSessionContext).not.toHaveBeenCalled();
  });
});
