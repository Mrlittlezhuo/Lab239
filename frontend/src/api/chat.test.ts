import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildVisionContent,
  chatCompletion,
  visionChatCompletion,
} from "@/api/chat";
import { clearAuth } from "@/stores/auth";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(impl: (url: string, init: RequestInit) => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

const completion = { choices: [{ message: { role: "assistant", content: "ok" } }] };

describe("vision chat contract", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAuth();
    localStorage.clear();
  });

  it("buildVisionContent 产出 OpenAI 多模态片段", () => {
    expect(buildVisionContent("这是什么", "data:image/png;base64,AAA")).toEqual([
      { type: "text", text: "这是什么" },
      { type: "image_url", image_url: { url: "data:image/png;base64,AAA" } },
    ]);
  });

  it("图片解析走 /v1/chat/completions 且 content 为多模态数组", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, completion));
    await visionChatCompletion({
      model: "qwen-vl-plus",
      content: buildVisionContent("描述图表", "data:image/png;base64,AAA"),
      temperature: 0.3,
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/chat/completions");
    const body = JSON.parse(String(init.body));
    expect(body.model).toBe("qwen-vl-plus");
    expect(body.stream).toBe(false);
    expect(body.temperature).toBe(0.3);
    expect(body.messages[0].role).toBe("user");
    expect(body.messages[0].content).toEqual([
      { type: "text", text: "描述图表" },
      { type: "image_url", image_url: { url: "data:image/png;base64,AAA" } },
    ]);
    // 服务端不解析 upload_image，也不该把视觉轮塞进会话
    expect(body.upload_image).toBeUndefined();
    expect(body.conversation_id).toBeUndefined();
  });

  it("未传 temperature 时不带该字段", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, completion));
    await visionChatCompletion({
      model: "m",
      content: buildVisionContent("x", "data:image/png;base64,BBB"),
    });
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body));
    expect("temperature" in body).toBe(false);
  });

  it("文本对话仍走 /chat/chat/completions，不带 upload_image", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, completion));
    await chatCompletion({
      messages: [{ role: "user", content: "hi" }],
      model: "glm-4",
      stream: false,
      conversation_id: "c1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/chat/chat/completions");
    const body = JSON.parse(String(init.body));
    expect(body.conversation_id).toBe("c1");
    expect(body.upload_image).toBeUndefined();
  });
});
