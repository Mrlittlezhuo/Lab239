import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import {
  historyToUiMessages,
  useChatStream,
  type SendOptions,
} from "@/hooks/useChatStream";
import { messagesToMarkdown } from "@/lib/exportMarkdown";
import type { MessageRecord } from "@/api/conversations";

const record = (over: Partial<MessageRecord>): MessageRecord => ({
  id: "m1",
  chat_type: "agent_chat",
  query: "问题",
  response: "回答",
  metadata: {},
  create_time: null,
  feedback_score: -1,
  feedback_reason: "",
  ...over,
});

describe("historyToUiMessages", () => {
  it("一条记录拆成 user+assistant 两条，assistant 带服务端 id 与反馈", () => {
    const msgs = historyToUiMessages([record({ feedback_score: 100 })]);
    expect(msgs).toHaveLength(2);
    expect(msgs[0]).toMatchObject({ role: "user", content: "问题" });
    expect(msgs[1]).toMatchObject({
      role: "assistant",
      content: "回答",
      serverMessageId: "m1",
      feedbackScore: 100,
    });
  });

  it("metadata.reference_metadata 恢复为引用列表", () => {
    const msgs = historyToUiMessages([
      record({ metadata: { reference_metadata: [{ file_name: "a.pdf" }] } }),
    ]);
    expect(msgs[1].references).toEqual([{ file_name: "a.pdf" }]);
  });

  it("空 response 也保留 assistant 占位（失败轮次）", () => {
    const msgs = historyToUiMessages([record({ response: "" })]);
    expect(msgs[1].content).toBe("");
  });
});

describe("messagesToMarkdown", () => {
  it("包含用户与助手段落", () => {
    const msgs = historyToUiMessages([record({})]);
    const md = messagesToMarkdown(msgs, "标题");
    expect(md).toContain("# 标题");
    expect(md).toContain("## 用户");
    expect(md).toContain("问题");
    expect(md).toContain("回答");
  });
});

/**
 * 各工具模式实际发出的请求载荷（stub fetch，断言线上字段）。
 *
 * 出问题的现场：智能路由（auto）模式下前端既没有 MCP 开关，也不发
 * use_mcp，而服务端 auto_agent 分支只读全局 AUTO_ROUTE_USE_MCP，
 * 结果智能对话永远用不到 MCP 工具。
 */
function mockFetch() {
  const fn = vi.fn(async (_url: string, _init: RequestInit) =>
    new Response('data: {"choices":[{"delta":{"content":"好的"}}]}\n\n', {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    }),
  );
  vi.stubGlobal("fetch", fn);
  return fn as unknown as { mock: { calls: [string, RequestInit][] } };
}

function options(over: Partial<SendOptions> = {}): SendOptions {
  return {
    model: "glm4-chat",
    temperature: null,
    toolMode: "auto",
    selectedTools: [],
    useMcp: false,
    ...over,
  };
}

async function bodyFor(over: Partial<SendOptions> = {}) {
  const fetchMock = mockFetch();
  const { result } = renderHook(() =>
    useChatStream({ onConversationId: () => {} }),
  );
  await act(async () => {
    await result.current.send("打开浏览器看看官网今天的公告", options(over));
  });
  expect(fetchMock.mock.calls).toHaveLength(1);
  return JSON.parse(fetchMock.mock.calls[0][1].body as string) as Record<
    string,
    unknown
  >;
}

describe("send 请求载荷", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("智能路由开启 MCP 时带上 auto_agent 与 use_mcp", async () => {
    const body = await bodyFor({ toolMode: "auto", useMcp: true });
    expect(body.auto_agent).toBe(true);
    expect(body.use_mcp).toBe(true);
  });

  it("智能路由关闭 MCP 时显式发 false（服务端全局开关仍可强制开启）", async () => {
    const body = await bodyFor({ toolMode: "auto", useMcp: false });
    expect(body.auto_agent).toBe(true);
    expect(body.use_mcp).toBe(false);
  });

  it("直接回答模式不带 auto_agent 也不带 use_mcp", async () => {
    const body = await bodyFor({ toolMode: "plain", useMcp: true });
    expect("auto_agent" in body).toBe(false);
    expect("use_mcp" in body).toBe(false);
  });

  it("指定工具模式带勾选工具与 use_mcp", async () => {
    const body = await bodyFor({
      toolMode: "manual",
      selectedTools: ["calculate"],
      useMcp: true,
    });
    expect(body.tools).toEqual(["calculate"]);
    expect(body.use_mcp).toBe(true);
    expect("auto_agent" in body).toBe(false);
  });
});
