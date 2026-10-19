import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  confirmMilestone,
  createMilestone,
  listMilestones,
  listReviewQueue,
  listTasks,
} from "@/api/supervision";
import type { Milestone, ProjectDetail, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useState } from "react";

const MILESTONE_STATUS: Record<string, string> = {
  draft: "草稿",
  active: "进行中",
  completed: "已完成",
  confirmed: "导师已确认",
};

// 后端 confirm_milestone 只接受这三个取值（supervision_work_repository）。
const CONFIRMATION_LABEL: Record<string, string> = {
  confirmed: "确认完成",
  partially_confirmed: "部分确认",
  not_confirmed: "暂不确认",
};

/**
 * 概览：阶段目标 + 任务与提交的数量概览。
 * 状态标签一律用后端返回的 `label`（§5.6），前端不自己拼混合状态。
 */
export function OverviewPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const isMentor = project.my_role === "mentor";
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState({ title: "", description: "", success_criteria: "" });
  const [confirming, setConfirming] = useState<Milestone | null>(null);
  const [confirmation, setConfirmation] = useState("confirmed");
  const [note, setNote] = useState("");

  const milestones = useQuery({
    queryKey: ["supervision", "milestones", project.project_id],
    queryFn: () => listMilestones(project.project_id),
  });
  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });
  // 审核队列只有导师能读（学生调用后端返回 403），所以按角色开关。
  const reviewQueue = useQuery({
    queryKey: ["supervision", "review-queue", project.project_id],
    queryFn: () => listReviewQueue(project.project_id),
    enabled: isMentor,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["supervision", "milestones"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "tasks"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "review-queue"] });
    refresh();
  };

  const create = useMutation({
    mutationFn: () => createMilestone(project.project_id, draft),
    onSuccess: () => {
      toast.success("已创建阶段目标");
      setDraft({ title: "", description: "", success_criteria: "" });
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const confirm = useMutation({
    mutationFn: () =>
      confirmMilestone(project.project_id, confirming?.milestone_id ?? "", {
        confirmation,
        note,
      }),
    onSuccess: () => {
      toast.success(`阶段目标确认结果：${CONFIRMATION_LABEL[confirmation]}`);
      setConfirming(null);
      setNote("");
      setConfirmation("confirmed");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows: Task[] = tasks.data ?? [];
  const active = rows.filter((task) => task.status === "active");
  const completed = rows.filter((task) => task.status === "completed");

  return (
    <div className="space-y-4">
      <div className="grid gap-3 md:grid-cols-3">
        <Stat label="进行中的任务" value={active.length} />
        <Stat label="已完成的任务" value={completed.length} />
        <Stat
          label={isMentor ? "待处理的提交" : "阶段目标"}
          value={isMentor ? (reviewQueue.data?.length ?? 0) : (milestones.data?.length ?? 0)}
        />
      </div>

      {isMentor && (
        <form
          className="space-y-2 rounded-md border p-3"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <div className="text-sm font-medium">新增阶段目标</div>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="milestone-title">标题</Label>
              <Input
                id="milestone-title"
                value={draft.title}
                onChange={(event) => setDraft({ ...draft, title: event.target.value })}
                placeholder="例如：完成基线复现"
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="milestone-criteria">完成标准</Label>
              <Input
                id="milestone-criteria"
                value={draft.success_criteria}
                onChange={(event) =>
                  setDraft({ ...draft, success_criteria: event.target.value })
                }
                placeholder="怎样算完成"
              />
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="milestone-desc">说明</Label>
            <Textarea
              id="milestone-desc"
              rows={2}
              value={draft.description}
              onChange={(event) =>
                setDraft({ ...draft, description: event.target.value })
              }
            />
          </div>
          <div className="flex justify-end">
            <Button type="submit" disabled={create.isPending || !draft.title.trim()}>
              创建
            </Button>
          </div>
        </form>
      )}

      {milestones.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {milestones.isError && (
        <AlertPanel tone="error" title="阶段目标加载失败" description={errorText(milestones.error)} />
      )}
      {!milestones.isLoading && (milestones.data?.length ?? 0) === 0 && (
        <AlertPanel
          title="还没有阶段目标"
          description="导师可以先写一个阶段目标，再把它拆成具体任务。"
        />
      )}

      <ul className="space-y-2">
        {(milestones.data ?? []).map((milestone) => (
          <li key={milestone.milestone_id} className="rounded-md border p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">{milestone.title}</span>
              <Badge variant="secondary">
                {MILESTONE_STATUS[milestone.status] ?? milestone.status}
              </Badge>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">{milestone.code}</div>
            {milestone.description && (
              <p className="mt-2 whitespace-pre-wrap text-sm">{milestone.description}</p>
            )}
            {milestone.success_criteria && (
              <p className="mt-1 text-sm text-muted-foreground">
                完成标准：{milestone.success_criteria}
              </p>
            )}
            {milestone.mentor_confirmation && (
              <p className="mt-2 text-sm text-muted-foreground">
                导师确认结果：
                <Badge variant="secondary" className="ml-1">
                  {CONFIRMATION_LABEL[milestone.mentor_confirmation] ??
                    milestone.mentor_confirmation}
                </Badge>
              </p>
            )}
            {milestone.mentor_confirmation_note && (
              <p className="mt-2 text-sm text-muted-foreground">
                导师意见：{milestone.mentor_confirmation_note}
              </p>
            )}
            {isMentor && (
              <div className="mt-3 space-y-2 border-t pt-2">
                {confirming?.milestone_id === milestone.milestone_id ? (
                  <>
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(CONFIRMATION_LABEL).map(([value, label]) => (
                        <Button
                          key={value}
                          size="sm"
                          variant={confirmation === value ? "default" : "outline"}
                          onClick={() => setConfirmation(value)}
                        >
                          {label}
                        </Button>
                      ))}
                    </div>
                    <Textarea
                      rows={2}
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                      placeholder="确认说明（可留空，会写进审计并通知学生）"
                    />
                    <div className="flex gap-2">
                      <Button size="sm" onClick={() => confirm.mutate()} disabled={confirm.isPending}>
                        记录确认结果
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setConfirming(null)}>
                        取消
                      </Button>
                    </div>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setConfirming(milestone);
                      setConfirmation(
                        milestone.mentor_confirmation || "confirmed",
                      );
                      setNote(milestone.mentor_confirmation_note || "");
                    }}
                  >
                    记录阶段确认结果
                  </Button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border p-3">
      <div className="text-2xl font-semibold">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  );
}
