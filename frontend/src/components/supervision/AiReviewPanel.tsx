import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  listAiFindings,
  listAiReviews,
  listAttachments,
  listSubmissions,
  listTasks,
  rerunMentorAiReview,
  runMentorAiReview,
  studentSelfCheck,
} from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { AiFindings } from "@/components/supervision/review/AiFindings";
import { AiRelationCard } from "@/components/supervision/review/AiRelationCard";
import { AiRunCard } from "@/components/supervision/review/AiRunCard";
import { AiSettingsCard } from "@/components/supervision/review/AiSettingsCard";
import { SelfCheckDraftForm } from "@/components/supervision/review/SelfCheckDraftForm";
import {
  buildDraftBody,
  emptySelfCheckDraft,
  isRunCurrentForSubmission,
  type SelfCheckDraft,
} from "@/components/supervision/review/aiReview";
import { submissionMaterials } from "@/components/supervision/review/snapshot";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/**
 * AI 预审面板（《AI预审功能详细设计方案》§6、§7、§11–§15）。
 *
 * 页面上 AI 状态与导师结论是两块独立区域：这里只排队、执行、看逐条 AI 结论、
 * 逐条采纳建议与看差异；导师总体结论仍在「导师审核」页签独立填写（§13.4、P-22）。
 */
export function AiReviewPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const isMentor = project.my_role === "mentor";
  const queryClient = useQueryClient();
  const [taskRef, setTaskRef] = useState("");
  const [submissionRef, setSubmissionRef] = useState("");
  const [selectedRunCode, setSelectedRunCode] = useState("");
  const [coreCodes, setCoreCodes] = useState<string[]>([]);
  const [draftsByTask, setDraftsByTask] = useState<Record<string, SelfCheckDraft>>({});

  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });
  const taskRows = tasks.data ?? [];
  const task =
    taskRows.find((item) => item.task_id === taskRef) ??
    taskRows.find((item) => item.status === "active") ??
    taskRows[0] ??
    null;

  const attachments = useQuery({
    queryKey: ["supervision", "attachments", project.project_id],
    queryFn: () => listAttachments(project.project_id),
  });
  const submissions = useQuery({
    queryKey: ["supervision", "submissions", project.project_id, task?.task_id ?? ""],
    queryFn: () => listSubmissions(project.project_id, task?.task_id ?? ""),
    enabled: task !== null,
  });
  const submissionRows = (submissions.data ?? []).filter(
    (item) => item.status !== "withdrawn",
  );
  const submission =
    submissionRows.find((item) => item.display_code === submissionRef) ??
    submissionRows[0] ??
    null;

  const runs = useQuery({
    queryKey: [
      "supervision",
      "ai-review",
      "runs",
      project.project_id,
      task?.task_id ?? "",
      isMentor ? (submission?.display_code ?? "") : "",
    ],
    queryFn: () =>
      listAiReviews(
        project.project_id,
        isMentor
          ? {
              task_ref: task?.task_id ?? "",
              submission_ref: submission?.display_code ?? "",
            }
          : { task_ref: task?.task_id ?? "" },
      ),
    enabled: task !== null,
  });
  const runRows = runs.data ?? [];
  const selectedRun =
    runRows.find((item) => item.code === selectedRunCode || item.run_id === selectedRunCode) ??
    runRows[0] ??
    null;

  const findings = useQuery({
    queryKey: ["supervision", "ai-review", "findings", project.project_id, selectedRun?.code ?? ""],
    queryFn: () => listAiFindings(project.project_id, selectedRun?.code ?? ""),
    enabled: selectedRun !== null && selectedRun.status === "completed",
  });

  const draftKey = task?.task_id ?? "";
  const draft = draftsByTask[draftKey] ?? emptySelfCheckDraft(task?.acs ?? []);
  const draftBody = buildDraftBody(draft);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["supervision", "ai-review"] });
  };

  const selfCheck = useMutation({
    mutationFn: () => studentSelfCheck(project.project_id, task?.task_id ?? "", draftBody),
    onSuccess: (saved) => {
      toast.success(`自检已进入队列：${saved.display_code}`);
      setSelectedRunCode(saved.code);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const mentorRun = useMutation({
    mutationFn: (force: boolean) =>
      force
        ? rerunMentorAiReview(project.project_id, submission?.display_code ?? "", coreCodes)
        : runMentorAiReview(project.project_id, submission?.display_code ?? "", coreCodes),
    onSuccess: (saved) => {
      toast.success(`AI 预审已进入队列：${saved.display_code}`);
      setSelectedRunCode(saved.code);
      invalidate();
      refresh();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  if (tasks.isLoading) return <p className="text-sm text-muted-foreground">任务加载中…</p>;
  if (tasks.isError) {
    return <AlertPanel tone="error" title="任务加载失败" description={errorText(tasks.error)} />;
  }
  if (task === null) {
    return (
      <AlertPanel
        title="还没有任务"
        description="AI 预审按任务的验收标准逐条检查，请先在「任务」页签创建并发布任务。"
      />
    );
  }

  const latestVersion = Math.max(
    0,
    ...submissionRows.map((item) => Number(item.version ?? 0)),
  );
  const runIsCurrent = selectedRun ? isRunCurrentForSubmission(selectedRun, latestVersion) : true;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
        <span className="text-sm font-medium">当前任务</span>
        <Select value={task.task_id} onValueChange={setTaskRef}>
          <SelectTrigger className="w-[min(100%,420px)]">
            <SelectValue placeholder="选择任务" />
          </SelectTrigger>
          <SelectContent>
            {taskRows.map((item) => (
              <SelectItem key={item.task_id} value={item.task_id}>
                {item.code} · {item.title}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          {task.acs.length} 条验收标准 · 任务状态：{task.label}
        </span>
      </div>

      <AlertPanel
        title="AI 预审与导师审核是两个独立域"
        description="AI 只找材料、对齐验收标准、标出证据与缺失项；证据是否可信、任务是否达到研究要求由导师判断。AI 排队、失败或取消都不会改动提交状态或审核状态，导师也不会被自动填入 AI 结果。"
      />

      {!isMentor && (
        <div className="space-y-3">
          <AlertPanel
            title="自检结果默认只有你自己可见"
            description="只有主动「附到某次提交」后导师才能看到那份快照；已过时（基于旧快照）的自检不能附带。"
          />
          <SelfCheckDraftForm
            key={task.task_id}
            task={task}
            attachments={(attachments.data ?? []).filter((item) => item.status === "active")}
            draft={draft}
            onChange={(next) => setDraftsByTask({ ...draftsByTask, [draftKey]: next })}
            onRun={() => selfCheck.mutate()}
            running={selfCheck.isPending}
          />
        </div>
      )}

      {isMentor && (
        <div className="space-y-3 rounded-md border p-3">
          <div className="text-sm font-medium">对某次提交发起 AI 预审</div>
          {submissionRows.length === 0 ? (
            <AlertPanel title="该任务还没有正式提交" description="学生提交后才能对提交运行预审。" />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={submission?.display_code ?? "none"}
                  onValueChange={setSubmissionRef}
                >
                  <SelectTrigger className="h-9 w-[min(100%,320px)]">
                    <SelectValue placeholder="选择提交" />
                  </SelectTrigger>
                  <SelectContent>
                    {submissionRows.map((item) => (
                      <SelectItem key={item.submission_id} value={item.display_code}>
                        {item.display_code} · v{item.version}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-xs text-muted-foreground">
                  已有同快照的完成结果时后端会直接复用；「重新预审」会生成新版本，旧结果保留。
                </span>
              </div>

              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">核心材料（优先纳入检查）</Label>
                {submission && submissionMaterials(submission).length > 0 ? (
                  <ul className="space-y-1">
                    {submissionMaterials(submission).map((item) => (
                      <li key={item.attachment_code} className="flex items-center gap-2 text-sm">
                        <Checkbox
                          checked={coreCodes.includes(item.attachment_code)}
                          onCheckedChange={(value) =>
                            setCoreCodes((codes) => {
                              const next = codes.filter((code) => code !== item.attachment_code);
                              return value === true
                                ? [...next, item.attachment_code]
                                : next;
                            })
                          }
                        />
                        <span className="break-words">
                          {item.name || item.attachment_code} · v{item.version}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-muted-foreground">该提交没有关联材料。</p>
                )}
              </div>

              <div className="flex flex-wrap gap-2">
                <Button
                  onClick={() => mentorRun.mutate(false)}
                  disabled={mentorRun.isPending || submission === null}
                >
                  {mentorRun.isPending ? "排队中…" : "运行 AI 预审"}
                </Button>
                <Button
                  variant="outline"
                  onClick={() => mentorRun.mutate(true)}
                  disabled={mentorRun.isPending || submission === null}
                >
                  重新预审
                </Button>
              </div>
            </>
          )}
        </div>
      )}

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">AI 预审运行（新→旧，旧版本保留为历史）</span>
          <Button size="sm" variant="ghost" onClick={() => runs.refetch()}>
            刷新排队状态
          </Button>
          <span className="text-xs text-muted-foreground">
            排队时可以离开本页，结果完成后仍会保存；这里不承诺精确等待时间。
          </span>
        </div>
        {runs.isError && (
          <AlertPanel tone="error" title="预审记录加载失败" description={errorText(runs.error)} />
        )}
        {!runs.isLoading && runRows.length === 0 && (
          <AlertPanel
            title="这个任务还没有 AI 预审记录"
            description={
              isMentor
                ? "选一次提交点「运行 AI 预审」；创建只排队，不会自动调用模型。"
                : "点「AI 检查当前完成情况」后，自检会进入队列。"
            }
          />
        )}
        {runRows.map((item) => (
          <AiRunCard
            key={item.run_id}
            project={project}
            run={item}
            selected={selectedRun?.run_id === item.run_id}
            onSelect={() => setSelectedRunCode(item.code)}
            draftBody={isMentor ? undefined : draftBody}
            attachTargets={isMentor ? undefined : submissionRows}
          />
        ))}
      </div>

      {selectedRun && (
        <div className="space-y-3">
          <div className="text-sm font-medium">
            {selectedRun.display_code} 的逐条 AI 结论
          </div>
          {!runIsCurrent && (
            <AlertPanel
              tone="warning"
              title="这是旧提交版本的结果"
              description={`本结果绑定 ${selectedRun.submission_display}，不是当前最新提交的当前预审（P-08）。请对新提交重新运行。`}
            />
          )}
          {selectedRun.status !== "completed" ? (
            <AlertPanel
              title={`当前状态：${selectedRun.status}`}
              description="只有已完成的运行才有逐条结论；排队、失败或取消时页面不会显示伪造结果。"
            />
          ) : findings.isError ? (
            <AlertPanel tone="error" title="逐条结论加载失败" description={errorText(findings.error)} />
          ) : findings.isLoading ? (
            <p className="text-sm text-muted-foreground">逐条结论加载中…</p>
          ) : (
            <AiFindings findings={findings.data ?? []} />
          )}
        </div>
      )}

      {isMentor && selectedRun && (
        <div className="space-y-3">
          <div className="text-sm font-medium">AI 建议与导师结论对照（独立于上面的 AI 状态）</div>
          <AiRelationCard project={project} run={selectedRun} />
        </div>
      )}

      <div className="space-y-3">
        <AiSettingsCard project={project} taskRef="" scopeLabel="项目默认" />
        <AiSettingsCard project={project} taskRef={task.task_id} scopeLabel={task.code} />
      </div>
    </div>
  );
}
