import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { listTasks } from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { ProgressSection } from "@/components/supervision/execution/ProgressSection";
import { SubmissionSection } from "@/components/supervision/execution/SubmissionSection";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/**
 * 进展与提交面板（《学生研究与导师审核功能重新设计方案》里程碑1）：
 * 学生选一个任务，先写进展记录（不触发审核），准备好后正式提交
 * （摘要 + 逐条 AC 自评 + git 信息 + 材料勾选）；提交版本不可修改，
 * 只能撤回后重新提交生成新版本。
 *
 * 导师看到的是同一份数据的只读视图（按任务分组），不出现提交/撤回按钮。
 * 任务状态标签一律用后端现算的 `label`（§5.6）。
 */
export function ExecutionPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const isMentor = project.my_role === "mentor";
  const [taskRef, setTaskRef] = useState("");

  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });

  const rows = tasks.data ?? [];
  const selected =
    rows.find((task) => task.task_id === taskRef) ??
    rows.find((task) => task.status === "active") ??
    rows[0] ??
    null;

  return (
    <div className="space-y-4">
      {tasks.isLoading && <p className="text-sm text-muted-foreground">任务加载中…</p>}
      {tasks.isError && (
        <AlertPanel tone="error" title="任务加载失败" description={errorText(tasks.error)} />
      )}
      {!tasks.isLoading && rows.length === 0 && (
        <AlertPanel
          title="还没有任务"
          description="请导师先在「任务」页签创建并发布任务，这里才能记录进展和提交。"
        />
      )}

      {selected && (
        <>
          <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
            <span className="text-sm font-medium">当前任务</span>
            <Select value={selected.task_id} onValueChange={setTaskRef}>
              <SelectTrigger className="w-[min(100%,420px)]">
                <SelectValue placeholder="选择任务" />
              </SelectTrigger>
              <SelectContent>
                {rows.map((task) => (
                  <SelectItem key={task.task_id} value={task.task_id}>
                    {task.code} · {task.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Badge variant="secondary">{selected.label}</Badge>
            {selected.due_at && (
              <span className="text-xs text-muted-foreground">
                截止 {selected.due_at.slice(0, 10)}
              </span>
            )}
            <span className="text-xs text-muted-foreground">
              {selected.acs.length} 条验收标准
            </span>
          </div>

          {selected.purpose && (
            <p className="whitespace-pre-wrap text-sm text-muted-foreground">
              任务目的：{selected.purpose}
            </p>
          )}

          {isMentor && (
            <AlertPanel
              title="导师视角：按任务只读浏览"
              description="进展与提交只有学生本人能写入或撤回；逐条结论与整体决定请在「导师审核」页签完成。"
            />
          )}

          <ProgressSection project={project} task={selected} isMentor={isMentor} />
          <SubmissionSection
            project={project}
            task={selected}
            isMentor={isMentor}
            refresh={refresh}
          />
        </>
      )}
    </div>
  );
}
