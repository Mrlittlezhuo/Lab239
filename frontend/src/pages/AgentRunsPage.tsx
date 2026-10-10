import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

import { listAgentRuns } from "@/api/agentRuns";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useState } from "react";

const STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function AgentRunsPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const conversationId = searchParams.get("conversation_id") ?? undefined;
  const [filter, setFilter] = useState(conversationId ?? "");

  const { data: runs = [], isFetching } = useQuery({
    queryKey: ["agent-runs", filter],
    queryFn: () => listAgentRuns(filter || undefined, 50),
    refetchInterval: (query) =>
      query.state.data?.some((r) => r.status === "running" || r.status === "pending")
        ? 3000
        : false,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-semibold">Agent 运行</h1>
        <Input
          className="ml-auto max-w-xs"
          placeholder="按会话 ID 过滤"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>

      <div className="space-y-2">
        {runs.map((run) => (
          <div
            key={run.run_id}
            className="flex cursor-pointer items-center gap-2 rounded-lg border p-3 text-sm hover:bg-accent/40"
            onClick={() => navigate(`/agent-runs/${run.run_id}`)}
          >
            <Badge
              variant={
                run.status === "completed"
                  ? "default"
                  : run.status === "failed"
                    ? "destructive"
                    : "secondary"
              }
            >
              {STATUS_LABEL[run.status] ?? run.status}
            </Badge>
            <span className="min-w-0 flex-1 truncate">{run.goal || run.run_id}</span>
            <span className="shrink-0 text-xs text-muted-foreground">
              步骤 {run.current_step}/{run.max_steps}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {run.create_time ?? ""}
            </span>
          </div>
        ))}
        {runs.length === 0 && (
          <div className="py-10 text-center text-sm text-muted-foreground">
            {isFetching ? "加载中…" : "暂无 Agent 运行记录"}
          </div>
        )}
      </div>
    </div>
  );
}
