/**
 * 知识库对话的请求形状（离线，stub fetch）。
 *
 * 出问题的现场：知识库对话页没选模型时发 `model: ""`，服务端签名
 * `model: str = Body(get_default_llm())` 的默认值不生效，
 * get_ChatOpenAI("") 返回 None，整轮崩在 `chat_prompt | None`
 * （界面显示 Expected a Runnable, callable or dict ... NoneType）。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { streamRagChat, type RagChatRequest } from "@/api/rag";

function sseResponse(): Response {
  return new Response('data: {"text":"hi"}\n\n', {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function mockFetch() {
  const fn = vi.fn(async () => sseResponse());
  vi.stubGlobal("fetch", fn);
  return fn as unknown as { mock: { calls: [string, RequestInit][] } };
}

function bodyOf(fetchMock: { mock: { calls: [string, RequestInit][] } }) {
  return JSON.parse(fetchMock.mock.calls[0][1].body as string) as Record<string, unknown>;
}

function req(overrides: Partial<RagChatRequest> = {}): RagChatRequest {
  return {
    messages: [{ role: "user", content: "反射面法向量怎么算" }],
    stream: true,
    ...overrides,
  };
}

describe("streamRagChat", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("model 为空串时整个不传，让服务端用默认模型", async () => {
    const fetchMock = mockFetch();

    await streamRagChat("local_kb", "samples", req({ model: "" }), () => {});

    const body = bodyOf(fetchMock);
    expect("model" in body).toBe(false);
    expect(body.stream).toBe(true);
  });

  it("model 只有空白字符时也不传", async () => {
    const fetchMock = mockFetch();

    await streamRagChat("local_kb", "samples", req({ model: "  " }), () => {});

    expect("model" in bodyOf(fetchMock)).toBe(false);
  });

  it("选了模型时原样带上", async () => {
    const fetchMock = mockFetch();

    await streamRagChat("local_kb", "samples", req({ model: "glm4-chat" }), () => {});

    expect(bodyOf(fetchMock).model).toBe("glm4-chat");
  });

  it("路径按 mode/param 拼，知识库名做 URL 编码", async () => {
    const fetchMock = mockFetch();

    await streamRagChat("local_kb", "无线通信", req(), () => {});

    expect(fetchMock.mock.calls[0][0]).toBe(
      `/knowledge_base/local_kb/${encodeURIComponent("无线通信")}/chat/completions`,
    );
  });
});
