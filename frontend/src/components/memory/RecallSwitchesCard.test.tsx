/**
 * 「我的记忆」页的召回开关（第三期设计方案 §4.1，全部离线）。
 *
 * 钉住三条：默认只勾「个人偏好」与「已确认的历史决策」（项目内与一般历史
 * 结论默认关，P3-09/P3-12）；改动只发送被改的键（关闭只停止召回，不删数据）；
 * 界面上写清总开关不约束个人偏好（P3-11）。
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { RecallSwitchesCard } from "@/components/memory/RecallSwitchesCard";

const api = vi.hoisted(() => ({
  getRecallSwitches: vi.fn(),
  setRecallSwitches: vi.fn(),
}));

vi.mock("@/api/memory", () => api);

const DEFAULTS = {
  enabled: true,
  decisions: true,
  project: false,
  general: false,
  preferences: true,
};

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <RecallSwitchesCard />
    </QueryClientProvider>,
  );
}

function switchOf(name: string): HTMLElement {
  return screen.getByRole("switch", { name });
}

describe("RecallSwitchesCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getRecallSwitches.mockResolvedValue({ ...DEFAULTS });
    api.setRecallSwitches.mockResolvedValue({ ...DEFAULTS });
  });

  afterEach(() => localStorage.clear());

  it("默认只勾个人偏好与已确认决策，项目内与一般结论默认关", async () => {
    renderCard();
    await waitFor(() => expect(api.getRecallSwitches).toHaveBeenCalled());
    expect(await screen.findByRole("heading", { name: "跨会话相关记忆" })).toBeTruthy();
    expect(switchOf("跨会话相关记忆总开关")).toHaveAttribute("aria-checked", "true");
    expect(switchOf("使用个人偏好")).toHaveAttribute("aria-checked", "true");
    expect(switchOf("使用已确认的历史决策")).toHaveAttribute("aria-checked", "true");
    expect(switchOf("使用项目内记忆")).toHaveAttribute("aria-checked", "false");
    expect(switchOf("使用一般历史会话结论")).toHaveAttribute("aria-checked", "false");
  });

  it("打开「使用一般历史会话结论」只发送该键（P3-12）", async () => {
    renderCard();
    const toggle = await screen.findByRole("switch", {
      name: "使用一般历史会话结论",
    });
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(api.setRecallSwitches).toHaveBeenCalledWith({ general: true }),
    );
  });

  it("关闭总开关不会连带改动分项开关，也不删任何记忆（P3-11）", async () => {
    api.setRecallSwitches.mockResolvedValue({ ...DEFAULTS, enabled: false });
    renderCard();
    const master = await screen.findByRole("switch", {
      name: "跨会话相关记忆总开关",
    });
    fireEvent.click(master);
    await waitFor(() =>
      expect(api.setRecallSwitches).toHaveBeenCalledWith({ enabled: false }),
    );
    // 只发总开关这一个键：分项与个人偏好保持原值
    expect(api.setRecallSwitches.mock.calls).toHaveLength(1);
    expect(
      screen.getByText(/个人偏好仍按下面的“使用个人偏好”工作/),
    ).toBeTruthy();
  });
});
