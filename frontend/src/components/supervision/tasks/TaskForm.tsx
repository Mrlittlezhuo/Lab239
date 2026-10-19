import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { createTask, updateTask } from "@/api/supervision";
import type { Milestone, ProjectDetail, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  CHECK_TYPE_OPTIONS,
  MATERIAL_KEY_HINT,
  PRIORITY_OPTIONS,
  addAcRow,
  addMaterialRow,
  acRowsChanged,
  buildTaskInput,
  draftFromTask,
  emptyTaskDraft,
  removeAcRow,
  removeMaterialRow,
  updateAcRow,
  updateMaterialRow,
  validateTaskDraft,
} from "@/components/supervision/tasks/taskDraft";
import type { TaskDraft } from "@/components/supervision/tasks/taskDraft";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

/**
 * 导师侧的任务草稿表单：新建与编辑共用。
 *
 * 表单只负责把 §7.2 的三问（为什么做 / 交付什么 / 怎么判定完成）和结构化
 * 验收标准填出来；能不能写、状态允不允许都由后端裁决，失败一律回到 toast。
 * 编辑已发布任务时，只有验收标准真的变化才会升版本（§7.2），所以提示也只在
 * 真的会升版本时出现。
 */
export function TaskForm({
  project,
  task,
  milestones,
  onSaved,
  onClose,
}: {
  project: ProjectDetail;
  /** null 表示新建草稿 */
  task: Task | null;
  milestones: Milestone[];
  onSaved: (saved: Task) => void;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<TaskDraft>(() =>
    task ? draftFromTask(task) : emptyTaskDraft(),
  );
  const [errors, setErrors] = useState<string[]>([]);

  const editingPublished = Boolean(task && task.status !== "draft");
  const willBumpVersion = Boolean(task) && acRowsChanged(task as Task, draft);

  const save = useMutation({
    mutationFn: (publish: boolean) => {
      const body = buildTaskInput(draft);
      if (task) return updateTask(project.project_id, task.code, body);
      return createTask(project.project_id, { ...body, publish });
    },
    onSuccess: (saved) => {
      toast.success(
        task
          ? `已保存 ${saved.code}（验收标准 v${saved.version}）`
          : saved.status === "active"
            ? `已发布 ${saved.code}，学生已能看到`
            : `已保存草稿 ${saved.code}`,
      );
      queryClient.invalidateQueries({ queryKey: ["supervision", "tasks"] });
      onSaved(saved);
    },
    onError: (error) => toast.error(errorText(error)),
  });

  function submit(publish: boolean) {
    const found = validateTaskDraft(draft);
    setErrors(found);
    if (found.length > 0) return;
    save.mutate(publish);
  }

  return (
    <form
      className="space-y-3 rounded-md border p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit(false);
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium">
          {task ? `编辑任务 ${task.code}` : "新建任务草稿"}
        </div>
        {task && (
          <Badge variant="outline">
            {task.status === "draft" ? "草稿未发布" : `已发布 · 验收标准 v${task.version}`}
          </Badge>
        )}
      </div>

      {errors.length > 0 && (
        <AlertPanel
          tone="error"
          title="还不能保存"
          description={errors.join("\n")}
        />
      )}

      {editingPublished && willBumpVersion && (
        <AlertPanel
          tone="warning"
          title={`改动验收标准会把版本从 v${task?.version} 升到 v${(task?.version ?? 0) + 1}`}
          description="学生看到的是新版本；已有提交仍绑定各自当时的验收标准快照，不会被这次改动追溯改写。"
        />
      )}
      {editingPublished && !willBumpVersion && (
        <AlertPanel
          title="只改文字不会升版本"
          description="验收标准内容不变时任务版本保持不变；要改验收标准请照实填，改完会提示升版本。"
        />
      )}

      <div className="grid gap-2 md:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="task-title">任务标题</Label>
          <Input
            id="task-title"
            value={draft.title}
            onChange={(event) => setDraft({ ...draft, title: event.target.value })}
            placeholder="例如：完成基线方法复现"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="task-due">截止时间</Label>
          <Input
            id="task-due"
            type="datetime-local"
            value={draft.due_at}
            onChange={(event) => setDraft({ ...draft, due_at: event.target.value })}
          />
        </div>
      </div>

      <div className="space-y-1">
        <Label htmlFor="task-purpose">任务目的（为什么要做）</Label>
        <Textarea
          id="task-purpose"
          rows={2}
          value={draft.purpose}
          onChange={(event) => setDraft({ ...draft, purpose: event.target.value })}
          placeholder="这项任务要解决研究里的哪个问题，做完对后续有什么帮助"
        />
      </div>

      <div className="space-y-1">
        <Label htmlFor="task-description">任务说明（需要交付什么）</Label>
        <Textarea
          id="task-description"
          rows={3}
          value={draft.description}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
          placeholder="做法、范围、约束，以及交付物的形态"
        />
      </div>

      <div className="grid gap-2 md:grid-cols-3">
        <div className="space-y-1">
          <Label>优先级</Label>
          <Select
            value={draft.priority}
            onValueChange={(value) => setDraft({ ...draft, priority: value })}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PRIORITY_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label>关联阶段目标</Label>
          <Select
            value={draft.milestone_id || "none"}
            onValueChange={(value) =>
              setDraft({ ...draft, milestone_id: value === "none" ? "" : value })
            }
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">不关联</SelectItem>
              {milestones.map((milestone) => (
                <SelectItem key={milestone.milestone_id} value={milestone.milestone_id}>
                  {milestone.code} {milestone.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-2 pt-1">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={draft.requires_code_review}
              onCheckedChange={(value) =>
                setDraft({ ...draft, requires_code_review: value === true })
              }
            />
            需要代码审查
          </label>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={draft.requires_runtime}
              onCheckedChange={(value) =>
                setDraft({ ...draft, requires_runtime: value === true })
              }
            />
            需要运行证据
          </label>
        </div>
      </div>

      <div className="space-y-2 border-t pt-2">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium">验收标准（至少一条）</div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setDraft(addAcRow(draft))}
          >
            新增一条
          </Button>
        </div>
        {draft.acs.map((row, index) => (
          <div key={row.key} className="space-y-2 rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{row.ac_code || `第 ${index + 1} 条`}</Badge>
              <Select
                value={row.check_type}
                onValueChange={(value) =>
                  setDraft(updateAcRow(draft, row.key, { check_type: value }))
                }
              >
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHECK_TYPE_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <Checkbox
                  checked={row.required}
                  onCheckedChange={(value) =>
                    setDraft(updateAcRow(draft, row.key, { required: value === true }))
                  }
                />
                必选
              </label>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="ml-auto"
                onClick={() => setDraft(removeAcRow(draft, row.key))}
              >
                删除
              </Button>
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">
                验收标准（写成可核验的条件）
              </Label>
              <Textarea
                rows={2}
                value={row.statement}
                onChange={(event) =>
                  setDraft(
                    updateAcRow(draft, row.key, { statement: event.target.value }),
                  )
                }
                placeholder="例如：固定随机种子下，中位定位误差小于 1 m"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">证据要求</Label>
              <Input
                value={row.evidence_requirement}
                onChange={(event) =>
                  setDraft(
                    updateAcRow(draft, row.key, {
                      evidence_requirement: event.target.value,
                    }),
                  )
                }
                placeholder="例如：实验配置、结果文件与图表"
              />
            </div>
          </div>
        ))}
      </div>

      <div className="space-y-2 border-t pt-2">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium">需要提交的材料</div>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setDraft(addMaterialRow(draft))}
          >
            新增一项
          </Button>
        </div>
        {draft.materials.length === 0 && (
          <p className="text-xs text-muted-foreground">
            没有额外材料要求；需要代码或运行证据时用上面的开关。
          </p>
        )}
        {draft.materials.map((row, index) => (
          <div key={row.rowId} className="flex flex-wrap items-center gap-2">
            <Input
              className="w-32"
              value={row.key}
              onChange={(event) =>
                setDraft(
                  updateMaterialRow(draft, row.rowId, { key: event.target.value }),
                )
              }
              placeholder={`材料标识 ${index + 1}`}
            />
            <Input
              className="min-w-48 flex-1"
              value={row.label}
              onChange={(event) =>
                setDraft(
                  updateMaterialRow(draft, row.rowId, { label: event.target.value }),
                )
              }
              placeholder="给学生看的名称，例如：结果图表"
            />
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <Checkbox
                checked={row.required}
                onCheckedChange={(value) =>
                  setDraft(
                    updateMaterialRow(draft, row.rowId, { required: value === true }),
                  )
                }
              />
              必交
            </label>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => setDraft(removeMaterialRow(draft, row.rowId))}
            >
              删除
            </Button>
          </div>
        ))}
        {draft.materials.length > 0 && (
          <p className="text-xs text-muted-foreground">{MATERIAL_KEY_HINT}</p>
        )}
      </div>

      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" onClick={onClose}>
          取消
        </Button>
        <Button type="submit" disabled={save.isPending}>
          {task ? "保存修改" : "保存草稿"}
        </Button>
        {!task && (
          <Button
            type="button"
            onClick={() => submit(true)}
            disabled={save.isPending}
          >
            保存并发布
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        保存并发布会立刻通知学生；草稿只有导师自己能看到。
      </p>
    </form>
  );
}
