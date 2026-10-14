import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearAllMemories,
  createMemory,
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
});
