import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyIntent,
  clearAllMemories,
  createMemory,
  detectIntents,
  getRecallSwitches,
  listMemoryRevisions,
  listRecalledMemories,
  listUsedPreferences,
  promoteMemory,
  setPreferencesEnabled,
  setRecallSwitches,
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

  // --- 第三期：跨会话相关记忆召回 ---

  it("getRecallSwitches 读取总开关与 4 个分项", async () => {
    const fetchMock = stubFetch({
      enabled: true,
      decisions: true,
      project: false,
      general: false,
      preferences: true,
    });
    const res = await getRecallSwitches();
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/memories/recall/switches");
    // 默认只勾个人偏好与已确认决策（设计方案 §4.1）
    expect(res).toEqual({
      enabled: true,
      decisions: true,
      project: false,
      general: false,
      preferences: true,
    });
  });

  it("setRecallSwitches 只发送被改动的键", async () => {
    const fetchMock = stubFetch({
      enabled: true,
      decisions: true,
      project: true,
      general: false,
      preferences: true,
    });
    await setRecallSwitches({ project: true });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("PATCH");
    expect(url).toContain("/memories/recall/switches");
    expect(JSON.parse(String(body))).toEqual({ project: true });
  });

  it("listRecalledMemories 带上问题、会话与项目（与注入同源）", async () => {
    const fetchMock = stubFetch({
      switches: {},
      max_count: 3,
      conversation_id: "c1",
      project_id: null,
      memories: [],
    });
    await listRecalledMemories({ q: "第三期做跨会话召回", conversationId: "c1" });
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/memories/recall?");
    expect(url).toContain("q=");
    expect(url).toContain("conversation_id=c1");
    // 没选项目时不带 project_id，由服务端按会话记录推导
    expect(url).not.toContain("project_id=");
  });

  it("promoteMemory 发送用户选定的范围/类别/有效期", async () => {
    const fetchMock = stubFetch({ memory: PREFERENCE, superseded_ids: ["m0"] });
    const res = await promoteMemory({
      content: "已确认：第三期采用关键词召回",
      kind: "decision",
      scope: "project",
      project_id: "p1",
      expires_at: "2026-12-31",
      conversation_id: "c1",
    });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("POST");
    expect(url).toContain("/memories/promote");
    expect(JSON.parse(String(body))).toEqual({
      content: "已确认：第三期采用关键词召回",
      kind: "decision",
      scope: "project",
      project_id: "p1",
      expires_at: "2026-12-31",
      conversation_id: "c1",
    });
    expect(res.superseded_ids).toEqual(["m0"]);
  });

  it("listMemoryRevisions 读取修订历史（新→旧）", async () => {
    const fetchMock = stubFetch([{ id: "r1", memory_id: "m1", content: "旧内容" }]);
    const res = await listMemoryRevisions("m1");
    const { url, method } = called(fetchMock);
    expect(method).toBe("GET");
    expect(url).toContain("/memories/m1/revisions");
    expect(res[0].content).toBe("旧内容");
  });

  it("updateMemory 可以改类别/范围/有效期，编辑保留旧内容", async () => {
    const fetchMock = stubFetch(PREFERENCE);
    await updateMemory("m1", {
      content: "改过的内容",
      kind: "decision",
      scope: "personal",
      project_id: "",
      expires_at: "",
    });
    const { url, method, body } = called(fetchMock);
    expect(method).toBe("PATCH");
    expect(url).toContain("/memories/m1");
    // 空串是"清除"约定，不能变成 undefined 被服务端当成"不改动"
    expect(JSON.parse(String(body))).toEqual({
      content: "改过的内容",
      kind: "decision",
      scope: "personal",
      project_id: "",
      expires_at: "",
    });
  });
});
