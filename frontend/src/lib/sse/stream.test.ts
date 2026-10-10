import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { streamSse } from "@/lib/sse/stream";
import { clearAuth, useAuth } from "@/stores/auth";

/** 一条最小 SSE 响应（够解析器走完 push/end）。 */
function sseResponse(frames = 'data: {"text":"hi"}\n\n'): Response {
  return new Response(frames, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function initOf(fetchMock: ReturnType<typeof vi.fn>, index = 0): RequestInit {
  return fetchMock.mock.calls[index][1] as RequestInit;
}

function headersOf(fetchMock: ReturnType<typeof vi.fn>, index = 0): Headers {
  return initOf(fetchMock, index).headers as Headers;
}

describe("streamSse 请求头", () => {
  beforeEach(() => {
    clearAuth();
    localStorage.clear();
  });

  afterEach(() => vi.unstubAllGlobals());

  it("带 body 时必须声明 application/json", async () => {
    // 浏览器对字符串 body 默认打 text/plain;charset=UTF-8，FastAPI 会因此
    // 不按 JSON 解析请求体，必填字段全部 422。这条是回归钉。
    const fetchMock = vi.fn(async () => sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamSse({
      path: "/chat/chat/completions",
      body: { messages: [{ role: "user", content: "hi" }] },
      onEvent: () => {},
    });

    const headers = headersOf(fetchMock);
    expect(headers.get("content-type")).toBe("application/json");
    expect(headers.get("accept")).toBe("text/event-stream");
    expect(initOf(fetchMock).method).toBe("POST");
  });

  it("无 body 的 GET 不声明 content-type", async () => {
    const fetchMock = vi.fn(async () => sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamSse({
      path: "/agent-runs/r1/resume",
      method: "GET",
      onEvent: () => {},
    });

    expect(headersOf(fetchMock).get("content-type")).toBeNull();
    expect(initOf(fetchMock).method).toBe("GET");
  });

  it("已登录带 Bearer，未登录不带", async () => {
    const fetchMock = vi.fn(async () => sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamSse({ path: "/chat/chat/completions", body: {}, onEvent: () => {} });
    expect(headersOf(fetchMock).get("authorization")).toBeNull();

    useAuth.setState({ token: "tok-123" });
    await streamSse({ path: "/chat/chat/completions", body: {}, onEvent: () => {} });
    expect(headersOf(fetchMock, 1).get("authorization")).toBe("Bearer tok-123");
  });

  it("请求体是 JSON 字符串", async () => {
    const fetchMock = vi.fn(async () => sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    await streamSse({
      path: "/knowledge_base/docx/chat/completions",
      body: { messages: [], stream: true },
      onEvent: () => {},
    });

    expect(initOf(fetchMock).body).toBe('{"messages":[],"stream":true}');
  });
});
