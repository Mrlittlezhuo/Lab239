import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  listAttachments,
  listSubmissions,
  withdrawSubmission,
} from "@/api/supervision";
import type { ProjectDetail, Submission, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  canWithdraw,
  parseCompletenessRows,
  SUBMISSION_STATUS_LABEL,
} from "@/components/supervision/execution/logic";
import { SubmissionDetail } from "@/components/supervision/execution/SubmissionDetail";
import { SubmissionForm } from "@/components/supervision/execution/SubmissionForm";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/stores/auth";

/**
 * 提交区：版本列表（按 display_code）、正式提交表单与撤回。
 * 提交版本不可修改；撤回必填原因，历史保留，重新提交生成新版本 S-xxx-vN。
 * 提交响应里的 completeness_check 只显示成提醒，明确说明提交已成功（§6.4）。
 */
export function SubmissionSection({
  project,
  task,
  isMentor,
  refresh,
}: {
  project: ProjectDetail;
  task: Task | null;
  isMentor: boolean;
  refresh: () => void;
}) {
  const queryClient = useQueryClient();
  const currentUserId = useAuth((state) => state.user?.id);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [withdrawing, setWithdrawing] = useState<Submission | null>(null);
  const [withdrawReason, setWithdrawReason] = useState("");
  const [lastSubmitted, setLastSubmitted] = useState<Submission | null>(null);

  const submissions = useQuery({
    queryKey: ["supervision", "submissions", project.project_id, task?.code ?? ""],
    queryFn: () => listSubmissions(project.project_id, task?.code ?? ""),
    enabled: Boolean(task),
  });
  const attachments = useQuery({
    queryKey: ["supervision", "attachments", project.project_id],
    queryFn: () => listAttachments(project.project_id),
  });

  const withdraw = useMutation({
    mutationFn: () =>
      withdrawSubmission(
        project.project_id,
        withdrawing?.display_code ?? "",
        withdrawReason,
      ),
    onSuccess: (submission) => {
      toast.success(`已撤回 ${submission.display_code}，历史保留`);
      setWithdrawing(null);
      setWithdrawReason("");
      queryClient.invalidateQueries({ queryKey: ["supervision", "submissions"] });
      refresh();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const activeAttachments = (attachments.data ?? []).filter(
    (item) => item.status === "active",
  );
  const rows = submissions.data ?? [];
  const canSubmit = !isMentor && task && task.status === "active";

  return (
    <section className="space-y-3 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">正式提交与版本</h2>
        <span className="text-xs text-muted-foreground">
          提交版本不可修改；撤回后历史保留，重新提交会生成新版本（如 S-001-v3）。
        </span>
      </div>

      {lastSubmitted && (
        <AlertPanel
          tone={parseCompletenessRows(lastSubmitted.completeness_check).some((c) => !c.passed) ? "warning" : "info"}
          title={`提交成功：${lastSubmitted.display_code}`}
          description={[
            ...parseCompletenessRows(lastSubmitted.completeness_check)
              .filter((check) => !check.passed)
              .map((check) => check.message),
            "这只是提醒，提交已成功；完整性检查不替代导师审核。",
          ].join("\n")}
        />
      )}

      {!isMentor && task && task.status !== "active" && (
        <AlertPanel
          title="该任务当前不能正式提交"
          description={`只有进行中的任务可以正式提交（当前状态：${task.label}）。`}
        />
      )}

      {canSubmit && task && (
        <SubmissionForm
          key={task.code}
          project={project}
          task={task}
          attachments={activeAttachments}
          onCreated={(submission) => {
            setLastSubmitted(submission);
            queryClient.invalidateQueries({ queryKey: ["supervision", "submissions"] });
            refresh();
          }}
        />
      )}

      {submissions.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {submissions.isError && (
        <AlertPanel tone="error" title="提交记录加载失败" description={errorText(submissions.error)} />
      )}
      {task && !submissions.isLoading && rows.length === 0 && (
        <AlertPanel
          title="还没有正式提交"
          description={
            isMentor
              ? "该任务还没有学生正式提交过。"
              : "填好摘要、逐条自评和材料后正式提交；平时的进展记录不算提交。"
          }
        />
      )}

      <ul className="space-y-2">
        {rows.map((submission) => (
          <li key={submission.submission_id} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{submission.display_code}</span>
              <Badge
                variant={
                  submission.status === "submitted"
                    ? "default"
                    : submission.status === "withdrawn"
                      ? "destructive"
                      : "secondary"
                }
              >
                {SUBMISSION_STATUS_LABEL[submission.status] ?? submission.status}
              </Badge>
              <span className="text-xs text-muted-foreground">
                {submission.create_time
                  ? submission.create_time.slice(0, 16).replace("T", " ")
                  : ""}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                {submission.summary}
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  setExpanded(expanded === submission.submission_id ? null : submission.submission_id)
                }
              >
                {expanded === submission.submission_id ? "收起" : "查看详情"}
              </Button>
              {canWithdraw(submission, currentUserId, project.my_role ?? "") && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setWithdrawing(submission);
                    setWithdrawReason("");
                  }}
                >
                  撤回
                </Button>
              )}
            </div>

            {withdrawing?.submission_id === submission.submission_id && (
              <div className="mt-2 space-y-2 rounded-md border border-amber-400/50 bg-amber-500/5 p-2">
                <p className="text-sm">
                  撤回 {submission.display_code}？撤回后历史保留，重新提交会生成新版本
                  S-xxx-vN；已发布正式审核的提交不能撤回。
                </p>
                <Input
                  value={withdrawReason}
                  placeholder="撤回原因（必填）"
                  onChange={(event) => setWithdrawReason(event.target.value)}
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => withdraw.mutate()}
                    disabled={withdraw.isPending || !withdrawReason.trim()}
                  >
                    确认撤回
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setWithdrawing(null)}>
                    取消
                  </Button>
                </div>
              </div>
            )}

            {expanded === submission.submission_id && (
              <SubmissionDetail project={project} submission={submission} />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
