/**
 * 会话列表接口（离线，stub fetch）。
 *
 * 出问题的现场：知识库对话页的侧栏列出了智能聊天的会话 —— 侧栏组件的
 * chatType 只用在创建上，列表请求没带 chat_type，两个入口的历史混成一串。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { listConversations } from "@/api/conversations";

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
