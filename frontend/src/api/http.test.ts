import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getJson, postJson, AUTH_EVENT_EXPIRED, AUTH_EVENT_PASSWORD_REQUIRED } from "@/api/http";
import { ApiError, unwrapEnvelope } from "@/lib/errors";
import { clearAuth, useAuth } from "@/stores/auth";

const user = {
  id: "u1",
  username: "alice",
  role: "user" as const,
  status: "active",
  must_change_password: false,
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

function mockFetch(impl: FetchFn) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("http request", () => {
  beforeEach(() => {
    clearAuth();
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("附带 Bearer 头且 credentials=omit", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, { ok: true }));
    useAuth.getState().setSession("tok123", user);

    await getJson("/auth/me");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/auth/me");
    expect(init.credentials).toBe("omit");
    const headers = init.headers as Headers;
    expect(headers.get("Authorization")).toBe("Bearer tok123");
  });

  it("无 token 时不带 Authorization", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, {}));
    await getJson("/server/models");
    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("Authorization")).toBeNull();
  });

  it("401 清空会话并广播 auth-expired", async () => {
    mockFetch(async () => jsonResponse(401, { detail: "令牌无效或已过期" }));
    useAuth.getState().setSession("tok", user);
    const events: string[] = [];
    const listener = () => events.push(AUTH_EVENT_EXPIRED);
    window.addEventListener(AUTH_EVENT_EXPIRED, listener);

    await expect(getJson("/auth/me")).rejects.toThrow(ApiError);
    expect(useAuth.getState().token).toBeNull();
    expect(events).toEqual([AUTH_EVENT_EXPIRED]);
    window.removeEventListener(AUTH_EVENT_EXPIRED, listener);
  });

  it("403 请先修改密码 → 广播 password-required", async () => {
    mockFetch(async () => jsonResponse(403, { detail: "请先修改密码" }));
    const events: string[] = [];
    const listener = () => events.push(AUTH_EVENT_PASSWORD_REQUIRED);
    window.addEventListener(AUTH_EVENT_PASSWORD_REQUIRED, listener);

    await expect(getJson("/chat/stream_chat_completion")).rejects.toThrow(
      "请先修改密码",
    );
    expect(events).toEqual([AUTH_EVENT_PASSWORD_REQUIRED]);
    window.removeEventListener(AUTH_EVENT_PASSWORD_REQUIRED, listener);
  });

  it("query 参数拼接并跳过 undefined", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, []));
    await getJson("/memories/auto", { query: { enabled: true, q: undefined } });
    expect(fetchMock.mock.calls[0][0]).toBe("/memories/auto?enabled=true");
  });

  it("postJson 序列化 body 并设置 Content-Type", async () => {
    const fetchMock = mockFetch(async () => jsonResponse(200, {}));
    await postJson("/auth/login", { username: "a", password: "b" });
    const init = fetchMock.mock.calls[0][1];
    expect(init.body).toBe(JSON.stringify({ username: "a", password: "b" }));
    expect((init.headers as Headers).get("Content-Type")).toBe("application/json");
  });

  it("非 2xx 抛 ApiError，detail 字符串作为 message", async () => {
    mockFetch(async () => jsonResponse(404, { detail: "会话不存在" }));
    await expect(getJson("/conversations/x")).rejects.toMatchObject({
      status: 404,
      message: "会话不存在",
    });
  });

  it("知识库信封 code!=200 抛 ApiError", () => {
    expect(() => unwrapEnvelope({ code: 404, msg: "知识库不存在" })).toThrow(ApiError);
    expect(unwrapEnvelope({ code: 200, data: [1, 2] })).toEqual([1, 2]);
  });
});
