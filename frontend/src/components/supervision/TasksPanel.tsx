import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  cancelTask,
  listMilestones,
  listTasks,
  publishTask,
} from "@/api/supervision";
import type { ProjectDetail, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { TaskDetail } from "@/components/supervision/tasks/TaskDetail";
import { TaskForm } from "@/components/supervision/tasks/TaskForm";
import {
  TASK_FILTERS,
  dueLabel,
  filterTasks,
  priorityLabel,
} from "@/components/supervision/tasks/taskDraft";
import type { TaskFilterKey } from "@/components/supervision/tasks/taskDraft";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * 任务面板：导师在这里下发任务草稿、发布、修改和取消；学生只读查看同一批任务
 * （§7.1 师生共用界面）。按钮对学生隐藏只是体验，真正的权限由后端裁决。
 *
 * 列表上的状态标签一律用后端返回的 `label`（§5.6）——"待导师审核""退回修改"
 * 这些是提交/审核/进展三套状态折算出来的混合标签，前端绝不自己拼。
 */
export function TasksPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const isMentor = project.my_role === "mentor";
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<TaskFilterKey>("all");
  const [selectedCode, setSelectedCode] = useState("");
  /** null = 不在编辑；task=null 表示新建草稿 */
  const [editing, setEditing] = useState<{ task: Task | null } | null>(null);
  const [cancelling, setCancelling] = useState<Task | null>(null);
  const [cancelReason, setCancelReason] = useState("");

  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });
  const milestones = useQuery({
    queryKey: ["supervision", "milestones", project.project_id],
    queryFn: () => listMilestones(project.project_id),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["supervision", "tasks"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "task"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "submissions"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "review"] });
    refresh();
  };

  const publish = useMutation({
    mutationFn: (taskRef: string) => publishTask(project.project_id, taskRef),
    onSuccess: (task) => {
      toast.success(`${task.code} 已发布，学生已能看到`);
      setSelectedCode(task.code);
      setEditing(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const cancel = useMutation({
    mutationFn: () => cancelTask(project.project_id, cancelling?.code ?? "", cancelReason.trim()),
    onSuccess: (task) => {
      toast.success(`${task.code} 已取消，提交与审核记录仍保留`);
      setCancelling(null);
      setCancelReason("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = filterTasks(tasks.data ?? [], filter);
  const milestoneOf = (task: Task) =>
    (milestones.data ?? []).find((milestone) => milestone.milestone_id === task.milestone_id);
  const now = new Date();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1">
          {TASK_FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setFilter(item.key)}
              className={cn(
                "rounded-md px-3 py-1.5 text-sm",
                filter === item.key
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
        {isMentor && (
          <Button
            size="sm"
            className="ml-auto"
            onClick={() => setEditing({ task: null })}
            disabled={Boolean(editing)}
          >
            新建任务
          </Button>
        )}
      </div>

      {isMentor && editing && (
        <TaskForm
          project={project}
          task={editing.task}
          milestones={milestones.data ?? []}
          onSaved={(saved) => {
            setEditing(null);
            setSelectedCode(saved.code);
            invalidate();
          }}
          onClose={() => setEditing(null)}
        />
      )}

      {tasks.isLoading && <p className="text-sm text-muted-foreground">任务加载中…</p>}
      {tasks.isError && (
        <AlertPanel tone="error" title="任务列表读取失败" description={errorText(tasks.error)} />
      )}
      {!tasks.isLoading && (tasks.data?.length ?? 0) === 0 && (
        <AlertPanel
          title="还没有任务"
          description={
            isMentor
              ? "用「新建任务」把研究问题拆成可核验的任务，至少写一条验收标准再发布。"
              : "导师还没下发任务；下发后会在这里看到任务与验收标准。"
          }
        />
      )}
      {!tasks.isLoading && (tasks.data?.length ?? 0) > 0 && rows.length === 0 && (
        <AlertPanel title="该状态下没有任务" description="换一个状态筛选看看。" />
      )}

      <ul className="space-y-2">
        {rows.map((task) => {
          const milestone = milestoneOf(task);
          const terminal =
            task.status === "completed" ||
            task.status === "cancelled" ||
            task.status === "archived";
          return (
            <li key={task.task_id} className="rounded-md border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setSelectedCode(selectedCode === task.code ? "" : task.code)
                  }
                  className="min-w-0 flex-1 text-left"
                >
                  <span className="font-medium">{task.title}</span>
                  <span className="ml-2 text-xs text-muted-foreground">{task.code}</span>
                </button>
                <Badge variant="secondary">{task.label}</Badge>
                <Badge variant="outline">验收标准 v{task.version}</Badge>
                {selectedCode !== task.code && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setSelectedCode(task.code)}
                  >
                    详情
                  </Button>
                )}
                {isMentor && !terminal && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setEditing({ task })}
                    disabled={Boolean(editing)}
                  >
                    编辑
                  </Button>
                )}
                {isMentor && task.status === "draft" && (
                  <Button
                    size="sm"
                    onClick={() => publish.mutate(task.code)}
                    disabled={publish.isPending}
                  >
                    发布
                  </Button>
                )}
                {isMentor && !terminal && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setCancelling(task);
                      setCancelReason("");
                    }}
                  >
                    取消
                  </Button>
                )}
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                <span>优先级 {priorityLabel(task.priority)}</span>
                <span>截止 {dueLabel(task, now)}</span>
                <span>{task.acs.length} 条验收标准</span>
                {milestone && <span>阶段目标 {milestone.code} {milestone.title}</span>}
                {task.requires_code_review && <span>需代码审查</span>}
                {task.requires_runtime && <span>需运行证据</span>}
              </div>
            </li>
          );
        })}
      </ul>

      {selectedCode && (
        <TaskDetail
          project={project}
          taskCode={selectedCode}
          isMentor={isMentor}
          publishing={publish.isPending}
          onEdit={(task) => setEditing({ task })}
          onPublish={(task) => publish.mutate(task.code)}
          onCancel={(task) => {
            setCancelling(task);
            setCancelReason("");
          }}
        />
      )}

      {cancelling && (
        <div className="space-y-2 rounded-md border p-3">
          <div className="text-sm font-medium">
            取消 {cancelling.code}「{cancelling.title}」？
          </div>
          <AlertPanel
            tone="warning"
            title="取消不会删除已有提交与审核记录"
            description="任务进入终态后不能再修改；学生已提交的各版本、导师已发布的审核结论和审计记录都保留，只是不再要求继续执行。"
          />
          <Input
            value={cancelReason}
            onChange={(event) => setCancelReason(event.target.value)}
            placeholder="取消原因（必填，学生会收到通知）"
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="destructive"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending || !cancelReason.trim()}
            >
              确认取消任务
            </Button>
            <Button size="sm" variant="outline" onClick={() => setCancelling(null)}>
              返回
            </Button>
          </div>
        </div>
      )}

      {!isMentor && (
        <p className="text-xs text-muted-foreground">
          任务内容、验收标准和取消都由导师维护；对任务本身有疑问请在研究讨论里提出。
        </p>
      )}
    </div>
  );
}

export default TasksPanel;
