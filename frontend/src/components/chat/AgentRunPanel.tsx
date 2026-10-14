import { Bot, CheckCircle2, Circle, Loader2, XCircle } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { UiAgentRun } from "@/hooks/useChatStream";

const STATUS_LABEL: Record<string, string> = {
  run_created: "已创建",
  plan_ready: "计划已生成",
  step_started: "执行中",
  step_finished: "执行中",
  replanned: "重新规划",
  run_completed: "已完成",
  run_failed: "失败",
  run_cancelled: "已取消",
};

/** 生成中的正文：模型还在写，先把已经出来的字显示出来。 */
function LiveText({ text }: { text: string }) {
  return (
    <div className="whitespace-pre-wrap rounded-md bg-muted/50 p-2 text-xs">
      {text}
      <span
        aria-hidden
        className="ml-0.5 inline-block h-3 w-[2px] animate-pulse bg-muted-foreground align-middle"
      />
    </div>
  );
}


export function AgentRunPanel({ agent }: { agent: UiAgentRun }) {
  const failed = agent.status === "run_failed";
  const done =
    agent.status === "run_completed" ||
    agent.status === "run_cancelled" ||
    failed;
  return (
    <Card className="my-2">
      <CardHeader className="py-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Bot className="h-4 w-4" />
          Agent 运行
          <span className={failed ? "text-destructive" : "text-muted-foreground"}>
            {STATUS_LABEL[agent.status] ?? agent.status}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 py-2 text-sm">
        {agent.plan?.summary ? (
          <div className="rounded-md bg-muted/50 p-2 text-xs">
            {String(agent.plan.summary)}
          </div>
        ) : !done && agent.livePlan ? (
          <LiveText text={agent.livePlan} />
        ) : null}
        {agent.steps.length > 0 && (
          <ol className="space-y-1">
            {agent.steps.map((step, i) => (
              <li key={i} className="flex items-center gap-2 text-xs">
                {step.status === "completed" ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
                ) : step.status === "failed" ? (
                  <XCircle className="h-3.5 w-3.5 text-destructive" />
                ) : !done ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Circle className="h-3.5 w-3.5" />
                )}
                <span className="font-medium">
                  {/* 服务端用 seq 标识步骤；不能退回数组下标，
                      否则一步被重复渲染时编号会跟着错位 */}
                  步骤 {String(step.seq ?? step.step ?? i + 1)}
                </span>
                <span className="text-muted-foreground">
                  {String(step.tool_name ?? "")}
                </span>
              </li>
            ))}
          </ol>
        )}
        {!done && agent.liveAnswer ? <LiveText text={agent.liveAnswer} /> : null}
        {agent.error && (
          <div className="text-xs text-destructive">{agent.error}</div>
        )}
      </CardContent>
    </Card>
  );
}
