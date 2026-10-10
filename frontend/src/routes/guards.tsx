import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "@/stores/auth";

/** 需要登录；未登录跳登录页并记住来源；强制改密用户先送去改密页。 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuth((s) => s.token);
  const user = useAuth((s) => s.user);
  const location = useLocation();
  if (!token) {
    const from = `${location.pathname}${location.search}`;
    return <Navigate to="/login" state={{ from }} replace />;
  }
  if (user?.must_change_password) {
    return <Navigate to="/change-password" replace />;
  }
  return <>{children}</>;
}

/** 改密页本身：有 token 即可进（不再检查 must_change_password，避免死循环）。 */
export function RequireToken({ children }: { children: ReactNode }) {
  const token = useAuth((s) => s.token);
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

/** 管理员页面。 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  const user = useAuth((s) => s.user);
  if (user?.role !== "admin") {
    return <Navigate to="/chat" replace />;
  }
  return <>{children}</>;
}
