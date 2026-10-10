import { apiUrl } from "@/lib/config";
import { ApiError } from "@/lib/errors";
import { clearAuth, getAuthToken } from "@/stores/auth";

/**
 * 全局认证事件：http 层发现 401/403 时广播，router 层订阅后导航。
 * 不在这里直接跳转，避免 fetch 层依赖 router。
 */
export const AUTH_EVENT_EXPIRED = "chatchat:auth-expired";
export const AUTH_EVENT_PASSWORD_REQUIRED = "chatchat:password-required";

function emitAuthEvent(name: string): void {
  window.dispatchEvent(new CustomEvent(name));
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
}

function withQuery(path: string, query?: RequestOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}${path.includes("?") ? "&" : "?"}${qs}` : path;
}

/**
 * 统一 fetch 封装：Bearer、credentials:"omit"（Tauri 预备）、JSON 编解码、
 * 401 登出广播、403“请先修改密码”广播。返回解析后的 JSON（或文本）。
 */
export async function request<T = unknown>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const { body, query, headers, ...init } = opts;
  const requestHeaders = new Headers(headers);
  const token = getAuthToken();
  if (token) requestHeaders.set("Authorization", `Bearer ${token}`);

  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body; // multipart 由浏览器自动设置 Content-Type
  } else if (body !== undefined) {
    requestHeaders.set("Content-Type", "application/json");
    payload = JSON.stringify(body);
  }

  const res = await fetch(apiUrl(withQuery(path, query)), {
    ...init,
    headers: requestHeaders,
    credentials: "omit",
    body: payload,
  });

  if (res.status === 401) {
    clearAuth();
    emitAuthEvent(AUTH_EVENT_EXPIRED);
    throw await ApiError.fromResponse(res);
  }

  const err = res.ok
    ? null
    : await ApiError.fromResponse(res);
  if (err?.isPasswordRequired()) {
    emitAuthEvent(AUTH_EVENT_PASSWORD_REQUIRED);
    throw err;
  }
  if (err) throw err;

  if (res.status === 204) return undefined as T;
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export const getJson = <T>(path: string, opts: RequestOptions = {}) =>
  request<T>(path, { ...opts, method: "GET" });

export const postJson = <T>(
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
) => request<T>(path, { ...opts, method: "POST", body });

export const putJson = <T>(
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
) => request<T>(path, { ...opts, method: "PUT", body });

export const patchJson = <T>(
  path: string,
  body?: unknown,
  opts: RequestOptions = {},
) => request<T>(path, { ...opts, method: "PATCH", body });

export const deleteJson = <T>(path: string, opts: RequestOptions = {}) =>
  request<T>(path, { ...opts, method: "DELETE" });
