import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface AuthUser {
  id: string;
  username: string;
  display_name?: string | null;
  role: "admin" | "user";
  status: string;
  must_change_password: boolean;
  create_time?: string;
  update_time?: string;
}

interface AuthSession {
  token: string | null;
  user: AuthUser | null;
  /** epoch ms；来自 login 响应 expires_in，仅用于到期提醒展示 */
  expiresAt: number | null;
}

interface AuthActions {
  setSession: (token: string, user: AuthUser, expiresInSec?: number) => void;
  setUser: (user: AuthUser) => void;
  logout: () => void;
}

export type AuthState = AuthSession & AuthActions;

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      expiresAt: null,
      setSession: (token, user, expiresInSec) =>
        set({
          token,
          user,
          expiresAt:
            typeof expiresInSec === "number"
              ? Date.now() + expiresInSec * 1000
              : null,
        }),
      setUser: (user) => set({ user }),
      logout: () => set({ token: null, user: null, expiresAt: null }),
    }),
    { name: "chatchat.auth" },
  ),
);

/** 非 React 环境访问器（fetch/SSE 层使用，避免 hook 约束）。 */
export const getAuthToken = (): string | null => useAuth.getState().token;
export const getAuthUser = (): AuthUser | null => useAuth.getState().user;
export const clearAuth = (): void => useAuth.getState().logout();
