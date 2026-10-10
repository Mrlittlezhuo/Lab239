import { beforeEach, describe, expect, it } from "vitest";

import { clearAuth, getAuthToken, getAuthUser, useAuth } from "@/stores/auth";

const user = {
  id: "u1",
  username: "alice",
  display_name: "Alice",
  role: "user" as const,
  status: "active",
  must_change_password: false,
};

describe("auth store", () => {
  beforeEach(() => {
    clearAuth();
    localStorage.clear();
  });

  it("setSession 保存 token/user/expiresAt", () => {
    useAuth.getState().setSession("tok", user, 3600);
    expect(getAuthToken()).toBe("tok");
    expect(getAuthUser()?.username).toBe("alice");
    const expiresAt = useAuth.getState().expiresAt;
    expect(expiresAt).not.toBeNull();
    expect(expiresAt!).toBeGreaterThan(Date.now());
  });

  it("logout 清空全部会话字段", () => {
    useAuth.getState().setSession("tok", user, 60);
    clearAuth();
    expect(getAuthToken()).toBeNull();
    expect(getAuthUser()).toBeNull();
    expect(useAuth.getState().expiresAt).toBeNull();
  });

  it("setUser 单独更新用户（/auth/me 刷新）", () => {
    useAuth.getState().setSession("tok", user);
    useAuth.getState().setUser({ ...user, must_change_password: true });
    expect(getAuthUser()?.must_change_password).toBe(true);
    expect(getAuthToken()).toBe("tok");
  });

  it("persist 写入 localStorage", () => {
    useAuth.getState().setSession("tok", user);
    const raw = localStorage.getItem("chatchat.auth");
    expect(raw).toBeTruthy();
    expect(JSON.parse(raw!).state.token).toBe("tok");
  });
});
