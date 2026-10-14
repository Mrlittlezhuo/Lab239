import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyIntent,
  clearAllMemories,
  createMemory,
  detectIntents,
  listUsedPreferences,
  setPreferencesEnabled,
  updateMemory,
} from "@/api/memory";
import { clearAuth } from "@/stores/auth";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** 记忆接口不走 {code,msg,data} 信封，直接返回模型/数组（服务端事实）。 */
const PREFERENCE = {
  id: "m1",
  memory_key: "manual:m1",
  type: "preference",
  category: "answer_style",
  content: "默认中文回答",
  source: "user",
  importance: 3,
  enabled: true,
  source_conversation_id: null,
  source_message_id: null,
  create_time: null,
  update_time: null,
};

function stubFetch(body: unknown = PREFERENCE) {
  const fetchMock = vi.fn(async (_url: string, _init: RequestInit) =>
    jsonResponse(200, body),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function called(fetchMock: ReturnType<typeof stubFetch>) {
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return { url, method: init.method, body: init.body };
}

describe("memory api", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAuth();
    localStorage.clear();
  });

  it("createMemory 带上 category（个人偏好类别）", async () => {
    const fetchMock = stubFetch();
    await createMemory({ content: "默认中文回答", category: "answer_style" });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url).toContain("/memories");
    expect(JSON.parse(String(body))).toMatchObject({
      content: "默认中文回答",
      category: "answer_style",
    });
  });

  it("updateMemory 传空串清除类别（undefined 会被服务端当作不改动）", async () => {
    const fetchMock = stubFetch({ ...PREFERENCE, category: null });
    await updateMemory("m1", { category: "" });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("PATCH");
    expect(url).toContain("/memories/m1");
    expect(JSON.parse(String(body))).toEqual({ category: "" });
  });

  it("listUsedPreferences 不带 q 时不加查询参数", async () => {
    const fetchMock = stubFetch({ enabled: true, max_count: 5, preferences: [PREFERENCE] });
    const res = await listUsedPreferences();
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/memories/preferences");
    expect(url).not.toContain("?");
    expect(res.preferences[0].category).toBe("answer_style");
  });

  it("listUsedPreferences 带 q 时作为查询参数（与真实注入排序一致）", async () => {
    const fetchMock = stubFetch({ enabled: true, max_count: 5, preferences: [] });
    await listUsedPreferences("Agent 怎么编排");
    const { url } = called(fetchMock);
    const parsed = new URL(url, "http://testserver");
    expect(parsed.pathname).toBe("/memories/preferences");
    expect(parsed.searchParams.get("q")).toBe("Agent 怎么编排");
  });

  it("setPreferencesEnabled 走 query 参数（服务端签名是裸标量 Body）", async () => {
    const fetchMock = stubFetch({ enabled: false });
    await setPreferencesEnabled(false);
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("PATCH");
    expect(url).toContain("/memories/preferences?enabled=false");
    expect(body ?? "").toBe("");
  });

  it("clearAllMemories 请求 DELETE /memories（不带 id）", async () => {
    const fetchMock = stubFetch({ deleted: 3 });
    const res = await clearAllMemories();
    const { url, method } = called(fetchMock);
    expect(method).toBe("DELETE");
    expect(url.replace(/\/$/, "")).toMatch(/\/memories$/);
    expect(res.deleted).toBe(3);
  });

  /**
   * 确认卡片（第二期 §4.3）：检测只读、写入是另一个请求。
   * 钉住"不保存就什么都不发生"这条契约的接口侧。
   */
  it("detectIntents 只调检测接口，不带写入", async () => {
    const fetchMock = stubFetch({ proposals: [] });
    await detectIntents("记住：本次任务不用自动路由", "c1");
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url.replace(/\/$/, "")).toMatch(/\/memories\/intents$/);
    expect(JSON.parse(String(body))).toEqual({
      text: "记住：本次任务不用自动路由",
      conversation_id: "c1",
    });
    expect(fetchMock.mock.calls).toHaveLength(1);
  });

  it("applyIntent 把用户选定的范围与类别发给 /memories/intents/apply", async () => {
    const fetchMock = stubFetch({ scope: "preference", kind: "remember", message: "已保存" });
    await applyIntent({
      kind: "remember",
      content: "以后默认中文回答",
      scope: "preference",
      conversation_id: "c1",
      category: "answer_style",
    });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url).toContain("/memories/intents/apply");
    expect(JSON.parse(String(body))).toMatchObject({
      kind: "remember",
      content: "以后默认中文回答",
      scope: "preference",
      category: "answer_style",
    });
  });
});
