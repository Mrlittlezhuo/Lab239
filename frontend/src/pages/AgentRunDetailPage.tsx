import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Play, StopCircle } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import { cancelAgentRun, getAgentRun, resumeAgentRun } from "@/api/agentRuns";
import type { AgentRunStep } from "@/api/agentRuns";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const STEP_STATUS_CLS: Record<string, string> = {
  completed: "border-l-emerald-400",
  running: "border-l-blue-400",
  pending: "border-l-muted-foreground/30",
  failed: "border-l-destructive",
  cancelled: "border-l-muted-foreground/30",
};

export function AgentRunDetailPage() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [resuming, setResuming] = useState(false);

  const { data, error } = useQuery({
    queryKey: ["agent-run", runId],
    queryFn: () => getAgentRun(runId!),
    enabled: !!runId,
    refetchInterval: (query) => {
      const s = query.state.data?.run.status;
      return s === "running" || s === "pending" ? 3000 : false;
    },
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelAgentRun(runId!),
    onSuccess: () => {
      toast.success("已取消运行");
      queryClient.invalidateQueries({ queryKey: ["agent-run", runId] });
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "取消失败"),
  });

  const resumeMutation = useMutation({
    mutationFn: () => resumeAgentRun(runId!, false),
    onSuccess: () => {
      setResuming(false);
      toast.success("恢复完成（非流式聚合结果）");
      queryClient.invalidateQueries({ queryKey: ["agent-run", runId] });
    },
    onError: (err) => {
      setResuming(false);
      toast.error(err instanceof Error ? err.message : "恢复失败");
    },
  });

  if (error) {
    return (
      <div className="space-y-4">
        <Header title="Agent 运行" onBack={() => navigate("/agent-runs")} />
        <div className="rounded-lg border p-6 text-center text-sm text-destructive">
          {error instanceof Error ? error.message : "加载失败"}
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="space-y-4">
        <Header title="Agent 运行" onBack={() => navigate("/agent-runs")} />
        <div className="py-10 text-center text-muted-foreground">加载中…</div>
      </div>
    );
  }

  const { run, steps } = data;
  const canCancel = run.status === "pending" || run.status === "running";
  const canResume = run.status === "pending" || run.status === "running" || run.status === "failed";

  return (
    <div className="space-y-4">
      <Header title={`运行 ${run.run_id.slice(0, 8)}`} onBack={() => navigate("/agent-runs")} />

      <div className="flex flex-wrap items-center gap-2">
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
        <span className="text-xs text-muted-foreground">
          步骤 {run.current_step}/{run.max_steps}
        </span>
        {run.conversation_id && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 text-xs"
            onClick={() => navigate(`/chat/${run.conversation_id}`)}
          >
            打开会话
          </Button>
        )}
        <div className="ml-auto flex gap-2">
          {canResume && (
            <Button
              size="sm"
              variant="outline"
              disabled={resuming}
              onClick={() => {
                setResuming(true);
                resumeMutation.mutate();
              }}
            >
              <Play className="h-4 w-4" />
              {resuming ? "恢复中…" : "断点恢复（非流式）"}
            </Button>
          )}
          {canCancel && (
            <Button
              size="sm"
              variant="destructive"
              disabled={cancelMutation.isPending}
              onClick={() => cancelMutation.mutate()}
            >
              <StopCircle className="h-4 w-4" />
              取消
            </Button>
          )}
        </div>
      </div>

      <div className="rounded-lg border p-3 text-sm">
        <div className="font-medium">目标</div>
        <p className="mt-1 whitespace-pre-wrap">{run.goal || "-"}</p>
        {run.plan_text && (
          <>
            <div className="mt-3 font-medium">计划</div>
            <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{run.plan_text}</p>
          </>
        )}
        {run.error && (
          <div className="mt-3 rounded-md bg-destructive/5 p-2 text-destructive">{run.error}</div>
        )}
        {run.final_result && (
          <>
            <div className="mt-3 font-medium">最终结果</div>
            <p className="mt-1 whitespace-pre-wrap">{run.final_result}</p>
          </>
        )}
      </div>

      <div className="space-y-2">
        <h2 className="text-base font-semibold">步骤时间线（{steps.length}）</h2>
        {steps.map((step) => (
          <StepRow key={step.step_id} step={step} />
        ))}
        {steps.length === 0 && (
          <div className="py-6 text-center text-sm text-muted-foreground">暂无步骤</div>
        )}
      </div>
    </div>
  );
}

function Header({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="flex items-center gap-2">
      <Button variant="ghost" size="sm" onClick={onBack}>
        <ArrowLeft className="h-4 w-4" />
        返回列表
      </Button>
      <h1 className="text-lg font-semibold">{title}</h1>
    </div>
  );
}

function StepRow({ step }: { step: AgentRunStep }) {
  const [open, setOpen] = useState(false);
  const detail = step.output_summary || step.error_summary;
  return (
    <div className={cn("rounded-md border border-l-4 p-3 text-sm", STEP_STATUS_CLS[step.status] ?? "border-l-muted-foreground/30")}>
      <div className="flex items-center gap-2">
        <span className="text-xs text-muted-foreground">#{step.seq}</span>
        <Badge variant="secondary">{step.type}</Badge>
        <span className="min-w-0 flex-1 truncate">{step.goal || step.tool_name || ""}</span>
        <span className="text-xs text-muted-foreground">{step.status}</span>
      </div>
      {step.tool_name && (
        <div className="mt-1 text-xs text-muted-foreground">
          工具：<code>{step.tool_name}</code>
        </div>
      )}
      {step.tool_input != null && (
        <button
          type="button"
          className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "隐藏参数/输出" : "查看参数/输出"}
        </button>
      )}
      {open && (
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted/40 p-2 text-xs">
          {JSON.stringify({ tool_input: step.tool_input, output: step.output_summary, error: step.error_summary }, null, 2)}
        </pre>
      )}
      {!open && detail && (
        <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{detail}</p>
      )}
    </div>
  );
}
