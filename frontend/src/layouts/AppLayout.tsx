import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  BookOpen,
  Brain,
  FileSearch,
  LogOut,
  MessagesSquare,
  Monitor,
  Moon,
  Plug,
  Bot,
  Search,
  Sun,
  UserCog,
} from "lucide-react";
import { toast } from "sonner";

import { logoutServer } from "@/api/auth";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { clearAuth, useAuth } from "@/stores/auth";
import { useTheme, useThemeEffect, type Theme } from "@/stores/theme";

const NAV_ITEMS = [
  { to: "/chat", label: "智能聊天", icon: MessagesSquare },
  { to: "/rag", label: "知识库对话", icon: BookOpen },
  { to: "/kb", label: "知识库管理", icon: FileSearch },
  { to: "/mcp", label: "MCP 服务", icon: Plug },
  { to: "/memory", label: "我的记忆", icon: Brain },
  { to: "/agent-runs", label: "Agent 运行", icon: Bot },
  { to: "/research", label: "论文研究", icon: Search },
  { to: "/admin/users", label: "用户管理", icon: UserCog, adminOnly: true },
];

const THEME_ORDER: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
  { value: "system", label: "跟随系统", icon: Monitor },
];

export function AppLayout() {
  const user = useAuth((s) => s.user);
  const theme = useTheme((s) => s.theme);
  const setTheme = useTheme((s) => s.setTheme);
  const navigate = useNavigate();
  useThemeEffect();

  async function handleLogout() {
    await logoutServer();
    clearAuth();
    toast.success("已退出登录");
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="flex w-56 shrink-0 flex-col border-r bg-muted/30">
        <div className="px-4 py-4 text-lg font-semibold">Lab239 工作台</div>
        <nav className="flex-1 space-y-1 px-2">
          {NAV_ITEMS.filter((item) => !item.adminOnly || user?.role === "admin").map(
            ({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium hover:bg-accent hover:text-accent-foreground",
                    isActive
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground",
                  )
                }
              >
                <Icon className="h-4 w-4" />
                {label}
              </NavLink>
            ),
          )}
        </nav>
        <div className="border-t p-3">
          <div className="mb-2 truncate text-sm">
            {user?.display_name || user?.username}
            {user?.role === "admin" && (
              <span className="ml-2 text-xs text-muted-foreground">管理员</span>
            )}
          </div>
          <div className="flex items-center gap-1">
            {THEME_ORDER.map(({ value, label, icon: Icon }) => (
              <Button
                key={value}
                variant={theme === value ? "secondary" : "ghost"}
                size="sm"
                title={label}
                onClick={() => setTheme(value)}
              >
                <Icon className="h-4 w-4" />
              </Button>
            ))}
          </div>
          <Button variant="outline" size="sm" className="mt-2 w-full" onClick={handleLogout}>
            <LogOut className="h-4 w-4" />
            退出登录
          </Button>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto p-6">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}
