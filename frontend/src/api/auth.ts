import { getJson, postJson } from "@/api/http";
import type { AuthUser } from "@/stores/auth";

export interface LoginResponse {
  token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

export async function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  return postJson<LoginResponse>("/auth/login", { username, password });
}

export function fetchMe(): Promise<AuthUser> {
  return getJson<AuthUser>("/auth/me");
}

export function changePassword(
  oldPassword: string,
  password: string,
): Promise<AuthUser> {
  // 服务端改密会 bump auth_version，旧 token 立即失效 → 调用方必须重新登录
  return postJson<AuthUser>("/auth/change-password", {
    old_password: oldPassword,
    password,
  });
}

export async function logoutServer(): Promise<void> {
  try {
    await postJson("/auth/logout", {});
  } catch {
    // 无状态登出，服务端失败不影响本地清理
  }
}
