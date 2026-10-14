import { afterEach, describe, expect, it, vi } from "vitest";

import {
  editSessionContext,
  getSessionContext,
  refreshSessionContext,
  resetSessionContext,
} from "@/api/sessionContext";
import { clearAuth } from "@/stores/auth";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const CONTEXT = {
  conversation_id: "c1",
  temporary: false,
  topic: "智能聊天记忆设计",
  goal: "确定三期功能边界",
  confirmed: ["第一期只做个人偏好记忆"],
  constraints: [],
  open_questions: ["会话摘要由谁生成、何时更新"],
  summary: "讨论了分期边界。",
  summarized_count: 4,
  update_time: null,
};

function stubFetch(body: unknown = CONTEXT) {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
    jsonResponse(200, body),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function called(fetchMock: ReturnType<typeof stubFetch>, index = 0) {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return { url, method: init.method, body: init.body };
}

/**
 * 会话上下文接口的线上形状（第二期设计方案 §4.2）。
 *
 * 钉住三点：路径挂在会话下（因此天然按会话隔离）、编辑按整组替换列表、
 * 重置是独立动作（不会顺带动到个人偏好）。
 */
describe("sessionContext api", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAuth();
    localStorage.clear();
  });

  it("getSessionContext 读取会话上下文", async () => {
    const fetchMock = stubFetch();
    const res = await getSessionContext("c1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/conversations/c1/context");
    expect(res.topic).toBe("智能聊天记忆设计");
    expect(res.confirmed).toEqual(["第一期只做个人偏好记忆"]);
  });

  it("editSessionContext 按整组替换列表（PATCH）", async () => {
    const fetchMock = stubFetch();
    await editSessionContext("c1", {
      confirmed: ["第一期只做个人偏好记忆", "暂不做自动模型路由"],
      summary: "改过的摘要",
    });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("PATCH");
    expect(url).toContain("/conversations/c1/context");
    expect(JSON.parse(String(body))).toEqual({
      confirmed: ["第一期只做个人偏好记忆", "暂不做自动模型路由"],
      summary: "改过的摘要",
    });
  });

  it("refreshSessionContext 走 /context/refresh（手动更新摘要）", async () => {
    const fetchMock = stubFetch();
    await refreshSessionContext("c1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url).toContain("/conversations/c1/context/refresh");
  });

  it("resetSessionContext 是 DELETE /context，不碰记忆接口", async () => {
    const fetchMock = stubFetch({ reset: true, conversation_id: "c1" });
    await resetSessionContext("c1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("DELETE");
    expect(url.replace(/\/$/, "")).toMatch(/\/conversations\/c1\/context$/);
    expect(fetchMock.mock.calls).toHaveLength(1);
  });
});
