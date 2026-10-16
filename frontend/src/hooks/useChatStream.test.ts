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

  it("metadata.memory_usage_manifest 恢复为本轮使用快照（第四期 §11.2）", () => {
    const msgs = historyToUiMessages([
      record({
        metadata: {
          generation_status: "cancelled",
          memory_usage_manifest: {
            generation_status: "cancelled",
            preferences: [{ id: "p1", content: "默认使用中文回答" }],
          },
        },
      }),
    ]);
    expect(msgs[1].memoryUsage).toMatchObject({
      generation_status: "cancelled",
      preferences: [{ id: "p1", content: "默认使用中文回答" }],
    });
  });

  it("没有快照的旧历史不会凭空造出一份使用记录", () => {
    const msgs = historyToUiMessages([record({ metadata: {} })]);
    expect(msgs[1].memoryUsage).toBeUndefined();
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
/**
 * 打桩 fetch：聊天流返回一个 token 帧，意图检测返回 `proposals`。
 *
 * 每次 send 现在有两个请求：聊天流 + `POST /memories/intents`（确认卡片检测）。
 * 断言聊天载荷时必须按 URL 取，不能再假设只有一个请求。
 */
function mockFetch({
  proposals = [],
  failDetect = false,
}: { proposals?: unknown[]; failDetect?: boolean } = {}) {
  const fn = vi.fn(async (url: string, _init: RequestInit) => {
    if (String(url).includes("/memories/intents")) {
      if (failDetect) throw new Error("检测接口不可用");
      return new Response(JSON.stringify({ proposals }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response('data: {"choices":[{"delta":{"content":"好的"}}]}\n\n', {
      status: 200,
      headers: { "content-type": "text/event-stream" },
    });
  });
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
  const chatCall = fetchMock.mock.calls.find(([url]) =>
    String(url).includes("/chat/chat/completions"),
  );
  expect(chatCall).toBeTruthy();
  return JSON.parse((chatCall as [string, RequestInit])[1].body as string) as Record<
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

  it("“本次不使用个人偏好”时带 use_memory=false", async () => {
    const body = await bodyFor({ useMemory: false });
    expect(body.use_memory).toBe(false);
  });

  it("默认使用个人记忆时不带 use_memory（请求形状与旧版一致）", async () => {
    const body = await bodyFor();
    expect("use_memory" in body).toBe(false);
  });

  it("临时对话时带 temporary=true", async () => {
    const body = await bodyFor({ temporary: true });
    expect(body.temporary).toBe(true);
  });

  it("默认不是临时对话时不带 temporary（请求形状与旧版一致）", async () => {
    const body = await bodyFor();
    expect("temporary" in body).toBe(false);
  });

  it("选定项目空间时带 project_id，服务端据此把新建会话挂到该项目（§4.4）", async () => {
    const body = await bodyFor({ projectId: "p1" });
    expect(body.project_id).toBe("p1");
  });

  it("没选项目时不带 project_id：普通聊天默认不进任何项目空间（P3-10）", async () => {
    const body = await bodyFor();
    expect("project_id" in body).toBe(false);
  });
});

/**
 * “记住 / 忘记”确认卡片（第二期设计方案 §4.3）。
 *
 * 关键契约：检测只是只读建议 —— 发送一轮消息不能产生任何写入请求，
 * 检测接口出错也不能打断正常回答。
 */
describe("确认卡片检测", () => {
  afterEach(() => vi.unstubAllGlobals());

  const proposal = {
    kind: "remember",
    content: "本项目不用自动模型路由",
    recommended_scope: "session",
    scope_reason: "内容限定了本次任务/当前项目，默认只记本会话",
    recommended_category: null,
    conversation_temporary: false,
    targets: [],
    note: "",
  };

  /** 让检测请求的 promise 链跑完（它在 send 之外并行解析）。 */
  async function flush() {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }

  it("检测到的建议挂在本轮助手消息上，且没有任何写入请求", async () => {
    const fetchMock = mockFetch({ proposals: [proposal] });
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onConversationId: () => {} }),
    );
    await act(async () => {
      await result.current.send("记住：本项目不用自动模型路由", options());
    });
    await flush();

    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[1].proposals).toEqual([proposal]);

    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(
      urls.filter((u) => u.includes("/memories/intents/apply")),
    ).toHaveLength(0);
    const detectCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/memories/intents"),
    ) as [string, RequestInit];
    expect(JSON.parse(detectCall[1].body as string)).toMatchObject({
      text: "记住：本项目不用自动模型路由",
      conversation_id: "c1",
    });
  });

  it("没有意图时不挂卡片", async () => {
    mockFetch();
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onConversationId: () => {} }),
    );
    await act(async () => {
      await result.current.send("今天天气怎么样", options());
    });
    await flush();
    expect(result.current.messages[1].proposals).toBeUndefined();
  });

  it("检测接口失败不影响回答", async () => {
    mockFetch({ failDetect: true });
    const { result } = renderHook(() =>
      useChatStream({ conversationId: "c1", onConversationId: () => {} }),
    );
    await act(async () => {
      await result.current.send("忘记我的回答偏好", options());
    });
    await flush();
    const assistant = result.current.messages[1];
    expect(assistant.content).toBe("好的");
    expect(assistant.error).toBeUndefined();
    expect(assistant.proposals).toBeUndefined();
  });
});
