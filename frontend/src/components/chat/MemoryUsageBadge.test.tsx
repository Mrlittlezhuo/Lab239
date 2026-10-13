/**
 * 聊天页"已使用个人偏好"入口（打桩记忆接口，全部离线）。
 *
 * 钉住第一期设计方案 §4.2/§4.5 的三点：
 * - 徽章与列表来自服务端 /memories/preferences，即本轮真正会注入的那一份；
 * - 总开关关闭时明确显示"个人记忆已关闭"，不再声称在使用偏好；
 * - "本次不使用个人偏好"按会话键写入偏好，不影响其他会话。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { MemoryUsageBadge } from "@/components/chat/MemoryUsageBadge";
import { useChatPrefs } from "@/stores/chatPrefs";

const api = vi.hoisted(() => ({
  listUsedPreferences: vi.fn(),
  setPreferencesEnabled: vi.fn(),
}));

vi.mock("@/api/memory", () => api);

function renderBadge(conversationKey = "conv1") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MemoryUsageBadge conversationKey={conversationKey} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("MemoryUsageBadge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatPrefs.setState({ memoryOff: {} });
    api.setPreferencesEnabled.mockResolvedValue({ enabled: true });
  });

  afterEach(() => {
    useChatPrefs.setState({ memoryOff: {} });
    localStorage.clear();
  });

  it("有可用偏好时显示“已使用个人偏好”，点开列出内容", async () => {
    api.listUsedPreferences.mockResolvedValue({
      enabled: true,
      max_count: 5,
      preferences: [
        {
          id: "m1",
          memory_key: "manual:m1",
          type: "preference",
          category: "answer_style",
          content: "默认中文回答",
          source: "user",
          importance: 3,
          enabled: true,
        },
      ],
    });
    renderBadge();
    await waitFor(() => expect(screen.getByText("已使用个人偏好")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    await waitFor(() => expect(screen.getByText("默认中文回答")).toBeTruthy());
    expect(screen.getByText(/回答偏好：/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "管理我的记忆" })).toBeTruthy();
  });

  it("总开关关闭时显示“个人记忆已关闭”", async () => {
    api.listUsedPreferences.mockResolvedValue({
      enabled: false,
      max_count: 5,
      preferences: [],
    });
    renderBadge();
    await waitFor(() => expect(screen.getByText("个人记忆已关闭")).toBeTruthy());
  });

  it("“本次不使用个人偏好”只作用于当前会话", async () => {
    api.listUsedPreferences.mockResolvedValue({
      enabled: true,
      max_count: 5,
      preferences: [
        {
          id: "m1",
          memory_key: "manual:m1",
          type: "preference",
          category: "answer_style",
          content: "默认中文回答",
          source: "user",
          importance: 3,
          enabled: true,
        },
      ],
    });
    renderBadge("conv1");
    await waitFor(() => expect(screen.getByText("已使用个人偏好")).toBeTruthy());

    fireEvent.click(screen.getByRole("button", { name: "记忆" }));
    const toggle = await screen.findByRole("switch", {
      name: "本次不使用个人偏好",
    });
    fireEvent.click(toggle);

    // 徽章文案随之变化（"本次不使用个人偏好"同时出现在徽章与开关标签处）
    await waitFor(() =>
      expect(screen.getAllByText("本次不使用个人偏好").length).toBeGreaterThanOrEqual(2),
    );
    expect(useChatPrefs.getState().memoryOff).toEqual({ conv1: true });
    // 服务端保存的偏好没有被删除
    expect(api.setPreferencesEnabled).not.toHaveBeenCalled();
  });
});
