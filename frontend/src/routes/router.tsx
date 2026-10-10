import { useEffect } from "react";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
  useNavigate,
} from "react-router-dom";

import { AppLayout } from "@/layouts/AppLayout";
import { AgentRunDetailPage } from "@/pages/AgentRunDetailPage";
import { AgentRunsPage } from "@/pages/AgentRunsPage";
import { ChangePasswordPage } from "@/pages/ChangePassword";
import { ChatPage } from "@/pages/ChatPage";
import { AdminUsersPage } from "@/pages/AdminUsersPage";
import { KnowledgeBasePage } from "@/pages/KnowledgeBasePage";
import { McpPage } from "@/pages/McpPage";
import { MemoryPage } from "@/pages/MemoryPage";
import { RagPage } from "@/pages/RagPage";
import { ResearchPage } from "@/pages/ResearchPage";
import { ResearchRunPage } from "@/pages/ResearchRunPage";
import { LoginPage } from "@/pages/Login";
import {
  AUTH_EVENT_EXPIRED,
  AUTH_EVENT_PASSWORD_REQUIRED,
} from "@/api/http";
import { RequireAdmin, RequireAuth, RequireToken } from "@/routes/guards";

/** 订阅 http 层广播的认证事件，统一导航（不依赖具体页面卸载）。 */
function AuthEventWatcher() {
  const navigate = useNavigate();
  useEffect(() => {
    const onExpired = () => navigate("/login", { replace: true });
    const onPassword = () => navigate("/change-password", { replace: true });
    window.addEventListener(AUTH_EVENT_EXPIRED, onExpired);
    window.addEventListener(AUTH_EVENT_PASSWORD_REQUIRED, onPassword);
    return () => {
      window.removeEventListener(AUTH_EVENT_EXPIRED, onExpired);
      window.removeEventListener(AUTH_EVENT_PASSWORD_REQUIRED, onPassword);
    };
  }, [navigate]);
  return null;
}

function Root() {
  return (
    <>
      <AuthEventWatcher />
      <AppLayout />
    </>
  );
}

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/change-password",
    element: (
      <RequireToken>
        <ChangePasswordPage />
      </RequireToken>
    ),
  },
  {
    element: <Root />,
    children: [
      { path: "/", element: <Navigate to="/chat" replace /> },
      { path: "/chat", element: <RequireAuth><ChatPage /></RequireAuth> },
      { path: "/chat/:conversationId", element: <RequireAuth><ChatPage /></RequireAuth> },
      { path: "/rag", element: <RequireAuth><RagPage /></RequireAuth> },
      { path: "/rag/:conversationId", element: <RequireAuth><RagPage /></RequireAuth> },
      { path: "/kb", element: <RequireAuth><KnowledgeBasePage /></RequireAuth> },
      { path: "/kb/:kbName", element: <RequireAuth><KnowledgeBasePage /></RequireAuth> },
      { path: "/mcp", element: <RequireAuth><McpPage /></RequireAuth> },
      { path: "/memory", element: <RequireAuth><MemoryPage /></RequireAuth> },
      { path: "/agent-runs", element: <RequireAuth><AgentRunsPage /></RequireAuth> },
      { path: "/agent-runs/:runId", element: <RequireAuth><AgentRunDetailPage /></RequireAuth> },
      { path: "/research", element: <RequireAuth><ResearchPage /></RequireAuth> },
      { path: "/research/:projectId", element: <RequireAuth><ResearchPage /></RequireAuth> },
      {
        path: "/research/:projectId/runs/:runId",
        element: <RequireAuth><ResearchRunPage /></RequireAuth>,
      },
      {
        path: "/admin/users",
        element: <RequireAdmin><AdminUsersPage /></RequireAdmin>,
      },
    ],
  },
]);

export function AppRouter() {
  return <RouterProvider router={router} />;
}
