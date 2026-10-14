import { afterEach, describe, expect, it, vi } from "vitest";

import {
  editSessionContext,
  getSessionContext,
  ignoreMemory,
  listIgnoredMemoryIds,
  listPromotionCandidates,
  refreshSessionContext,
  resetSessionContext,
  unignoreMemory,
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

  // --- 第三期：候选提升与会话内「本次不使用」---

  it("listPromotionCandidates 只读，不写入任何记忆（P3-13）", async () => {
    const fetchMock = stubFetch({
      candidates: [
        {
          content: "已确认：第三期采用关键词召回",
          recommended_kind: "decision",
          recommended_scope: "personal",
          project_id: null,
          project_name: null,
          reason: "措辞是长期约定",
          already_saved: false,
        },
      ],
    });
    const res = await listPromotionCandidates("c1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/conversations/c1/promotions");
    expect(res.candidates[0].already_saved).toBe(false);
    // 候选接口本身没有任何写操作
    expect(fetchMock.mock.calls).toHaveLength(1);
  });

  it("ignoreMemory 只作用于本会话，记录保留（P3-04）", async () => {
    const fetchMock = stubFetch({ ignored: true });
    await ignoreMemory("c1", "m1");
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url).toContain("/conversations/c1/memory-ignores");
    expect(JSON.parse(String(body))).toEqual({ memory_id: "m1" });
  });

  it("unignoreMemory 按记忆 id 删除本会话的忽略记录", async () => {
    const fetchMock = stubFetch({ removed: true });
    await unignoreMemory("c1", "m1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("DELETE");
    expect(url).toContain("/conversations/c1/memory-ignores/m1");
  });

  it("listIgnoredMemoryIds 读取本会话已忽略的记忆 id", async () => {
    const fetchMock = stubFetch({ memory_ids: ["m1", "m2"] });
    const res = await listIgnoredMemoryIds("c1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/conversations/c1/memory-ignores");
    expect(res.memory_ids).toEqual(["m1", "m2"]);
  });
});
