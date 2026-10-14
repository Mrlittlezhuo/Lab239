/**
 * 会话列表接口（离线，stub fetch）。
 *
 * 出问题的现场：知识库对话页的侧栏列出了智能聊天的会话 —— 侧栏组件的
 * chatType 只用在创建上，列表请求没带 chat_type，两个入口的历史混成一串。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createConversation,
  getConversation,
  listConversations,
  setConversationProject,
} from "@/api/conversations";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch() {
  const fn = vi.fn(async () => jsonResponse([]));
  vi.stubGlobal("fetch", fn);
  return fn as unknown as { mock: { calls: [string, RequestInit][] } };
}

describe("listConversations", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("按 chat_type 过滤（知识库对话只看 kb_chat）", async () => {
    const fetchMock = mockFetch();

    await listConversations("kb_chat");

    expect(fetchMock.mock.calls[0][0]).toBe("/conversations?chat_type=kb_chat");
  });

  it("不传 chat_type 时不带 query 参数", async () => {
    const fetchMock = mockFetch();

    await listConversations();

    expect(fetchMock.mock.calls[0][0]).toBe("/conversations");
  });
});

/**
 * 会话与项目空间的关联（第三期设计方案 §4.4）：项目范围记忆只在选定该
 * 项目的会话里召回，因此会话创建与切换都必须把 project_id 带给服务端。
 */
describe("conversation project", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("createConversation 带上 project_id", async () => {
    const fetchMock = mockFetch();

    await createConversation("", "agent_chat", false, "p1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/conversations");
    expect(JSON.parse(String(init.body))).toEqual({
      name: "",
      chat_type: "agent_chat",
      temporary: false,
      project_id: "p1",
    });
  });

  it("不选项目时 project_id 为 null，而不是空串", async () => {
    const fetchMock = mockFetch();

    await createConversation("会话", "kb_chat", true);

    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      name: "会话",
      chat_type: "kb_chat",
      temporary: true,
      project_id: null,
    });
  });

  it("setConversationProject 走 PATCH /conversations/{id}/project", async () => {
    const fetchMock = mockFetch();

    await setConversationProject("c1", "p2");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/conversations/c1/project");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ project_id: "p2" });
  });

  it("getConversation 读取单个会话（含 project_id）", async () => {
    const fetchMock = mockFetch();

    await getConversation("c1");

    expect(fetchMock.mock.calls[0][0]).toBe("/conversations/c1");
  });
});
