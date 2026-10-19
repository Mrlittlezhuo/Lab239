import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  cancelLiteratureSubtask,
  closeLiteratureTask,
  confirmLiteraturePlan,
  generateLiteraturePlan,
  listLiteratureSubtasks,
  retryLiteratureSubtask,
  runLiteratureAbstractAnalysis,
  runLiteratureSearch,
} from "@/api/supervision";
import type { LiteratureTask, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  MODEL_UNAVAILABLE_TEXT,
  PURPOSE_LABEL,
  SOURCE_UNAVAILABLE_TEXT,
  TASK_STATUS_LABEL,
  briefSupersededNote,
  canCancelSubtask,
  canRetrySubtask,
  canSearch,
  isCapabilityUnavailable,
  labelOf,
  planConfirmed,
  sourceFailureLines,
  subtaskLine,
  taskClosed,
  taskSummaryLine,
} from "@/components/supervision/literature/literatureModel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "未";
}

/** 503 分给来源还是模型：两者都不是失败，任务与已有结果都保留。 */
function unavailableText(kind: "search" | "model"): string {
  return kind === "search" ? SOURCE_UNAVAILABLE_TEXT : MODEL_UNAVAILABLE_TEXT;
}

/**
 * 一个检索任务的执行区：检索方案 → 学生确认 → 开始检索 → 子任务明细。
 *
 * 口径全部照后端：方案没确认不能搜索（409）；来源未接入是 503 说明而不是失败；
 * 检索不会自动深度提取任何候选；子任务按来源 × 关键词批次拆开，失败只重跑
 * 那一个；重新生成方案会清掉上一次的确认。
 */
export function LiteratureTaskCard({
  project,
  task,
  onOpenResults,
}: {
  project: ProjectDetail;
  task: LiteratureTask;
  onOpenResults: () => void;
}) {
  const queryClient = useQueryClient();
  const [closeReason, setCloseReason] = useState("");
  const [closing, setClosing] = useState(false);
  const [notice, setNotice] = useState("");

  const isMentor = project.my_role === "mentor";

  const subtasks = useQuery({
    queryKey: ["supervision", "literature", "subtasks", project.project_id, task.task_id],
    queryFn: () => listLiteratureSubtasks(project.project_id, task.code, { limit: 50 }),
  });

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
  }

  const plan = useMutation({
    mutationFn: () => generateLiteraturePlan(project.project_id, task.code),
    onSuccess: () => {
      setNotice("");
      toast.success("检索方案已生成，请检查关键词与来源后确认");
      invalidate();
    },
    onError: (error) => {
      if (isCapabilityUnavailable(error)) {
        setNotice(`${MODEL_UNAVAILABLE_TEXT}\n后端说明：${errorText(error)}`);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const confirmPlan = useMutation({
    mutationFn: () => confirmLiteraturePlan(project.project_id, task.code),
    onSuccess: () => {
      toast.success("检索方案已确认，可以开始检索");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const search = useMutation({
    mutationFn: () => runLiteratureSearch(project.project_id, task.code),
    onSuccess: (saved) => {
      setNotice("");
      toast.success(`检索完成：入库 ${saved.stored_total} 篇候选`);
      invalidate();
    },
    onError: (error) => {
      if (isCapabilityUnavailable(error)) {
        // 不是失败：任务、方案与已入库候选都保留。
        setNotice(`${unavailableText("search")}\n后端说明：${errorText(error)}`);
        invalidate();
        return;
      }
      toast.error(errorText(error));
    },
  });

  const analysis = useMutation({
    mutationFn: () => runLiteratureAbstractAnalysis(project.project_id, task.code),
    onSuccess: () => {
      setNotice("");
      toast.success("摘要关联分析已完成（只对排序靠前的有限候选）");
      invalidate();
    },
    onError: (error) => {
      if (isCapabilityUnavailable(error)) {
        setNotice(`${MODEL_UNAVAILABLE_TEXT}\n后端说明：${errorText(error)}`);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const retry = useMutation({
    mutationFn: (subtaskRef: string) => retryLiteratureSubtask(project.project_id, task.code, subtaskRef),
    onSuccess: () => {
      toast.success("已重试该子任务；其它子任务状态不变");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const cancel = useMutation({
    mutationFn: (subtaskRef: string) => cancelLiteratureSubtask(project.project_id, task.code, subtaskRef),
    onSuccess: () => {
      toast.success("已取消该子任务");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const close = useMutation({
    mutationFn: () => closeLiteratureTask(project.project_id, task.code, closeReason.trim()),
    onSuccess: () => {
      setClosing(false);
      setCloseReason("");
      toast.success("任务已关闭；候选论文与结论都保留");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const searchGate = canSearch(task);
  const failures = sourceFailureLines(task);
  const superseded = briefSupersededNote(task);
  const planKeywords = (task.search_plan?.keyword_batches as unknown[] | undefined) ?? [];

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{task.code}</span>
        <Badge variant={task.status === "closed" ? "outline" : "secondary"}>
          {labelOf(TASK_STATUS_LABEL, task.status)}
        </Badge>
        <Badge variant="outline">{labelOf(PURPOSE_LABEL, task.purpose)}</Badge>
        <span className="text-xs text-muted-foreground">
          依据 {task.brief_code} v{task.brief_version} · 创建于 {when(task.create_time)} ·{" "}
          {task.created_by}（{task.created_role}）
        </span>
      </div>

      <div className="text-sm">{task.goal}</div>
      <div className="text-xs text-muted-foreground">
        关键词：{task.keywords.join("、") || "（未填，检索方案里补）"}
        {task.synonyms.length > 0 ? ` · 同义词：${task.synonyms.join("、")}` : ""}
        {task.exclude_terms.length > 0 ? ` · 排除词：${task.exclude_terms.join("、")}` : ""}
        {task.year_from || task.year_to ? ` · 年份 ${task.year_from ?? "?"}-${task.year_to ?? "?"}` : ""}
        {task.sources.length > 0 ? ` · 来源：${task.sources.join("、")}` : ""}
      </div>
      <div className="text-xs text-muted-foreground">{taskSummaryLine(task)}</div>

      {superseded && <AlertPanel tone="warning" title="依据的研究定义已更新" description={superseded} />}

      {task.plan_error && (
        <AlertPanel
          tone="warning"
          title="检索方案生成失败"
          description={`${task.plan_error}\n任务与已有候选不受影响，可重新生成方案。`}
        />
      )}

      {failures.length > 0 && (
        <AlertPanel
          tone="warning"
          title="有来源检索失败"
          description={`失败来源已列出，不隐藏检索过程：\n${failures.join("\n")}\n受影响来源的候选可能不全。`}
        />
      )}

      {notice && <AlertPanel tone="warning" title="这一步暂时做不了，但不是失败" description={notice} />}

      <div className="space-y-2 rounded-md border border-dashed p-2">
        <div className="text-sm font-medium">检索方案</div>
        {Object.keys(task.search_plan ?? {}).length === 0 ? (
          <p className="text-xs text-muted-foreground">
            还没有检索方案。方案是一次短模型调用生成的关键词批次与来源建议，
            必须由你确认后才允许检索；系统不会自己决定搜什么。
          </p>
        ) : (
          <div className="space-y-1 text-xs text-muted-foreground">
            <div>
              关键词批次 {Array.isArray(planKeywords) ? planKeywords.length : 0} 组 · 生成于{" "}
              {when(task.search_plan_generated_at)} · 模型 {task.search_plan_model || "未记录"} ·
              提示词 {task.search_plan_prompt_version}
            </div>
            {task.search_plan_note && <div>备注：{task.search_plan_note}</div>}
            <div>
              {planConfirmed(task)
                ? `已由 ${task.search_plan_confirmed_by} 确认于 ${when(task.search_plan_confirmed_at)}`
                : "尚未确认：确认后才能开始检索"}
            </div>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2">
              {JSON.stringify(task.search_plan, null, 2)}
            </pre>
          </div>
        )}
        {!isMentor && !task.status.startsWith("closed") && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={() => plan.mutate()} disabled={plan.isPending}>
              {plan.isPending ? "生成中…" : Object.keys(task.search_plan ?? {}).length ? "重新生成方案" : "生成检索方案"}
            </Button>
            {!planConfirmed(task) && (
              <Button
                size="sm"
                onClick={() => confirmPlan.mutate()}
                disabled={confirmPlan.isPending || Object.keys(task.search_plan ?? {}).length === 0}
              >
                确认检索方案
              </Button>
            )}
            <Button
              size="sm"
              onClick={() => search.mutate()}
              disabled={search.isPending || !searchGate.allowed}
            >
              {search.isPending ? "检索中…" : "开始检索"}
            </Button>
            {!searchGate.allowed && (
              <span className="text-xs text-muted-foreground">{searchGate.reason}</span>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => analysis.mutate()}
              disabled={analysis.isPending || task.stored_total === 0}
            >
              摘要关联分析（有上限）
            </Button>
          </div>
        )}
        {isMentor && (
          <p className="text-xs text-muted-foreground">
            导师可以查看任务、方案与候选；生成方案、确认方案与检索由学生发起。
          </p>
        )}
      </div>

      <div className="space-y-2">
        <div className="text-sm font-medium">子任务（{task.subtask_total} 个，失败 {task.subtask_failed_total} 个）</div>
        {subtasks.isError && (
          <AlertPanel tone="error" title="子任务加载失败" description={errorText(subtasks.error)} />
        )}
        {!subtasks.isLoading && !subtasks.isError && (subtasks.data ?? []).length === 0 && (
          <p className="text-xs text-muted-foreground">还没有子任务：确认检索方案后点「开始检索」。</p>
        )}
        <ul className="space-y-1">
          {(subtasks.data ?? []).map((subtask) => (
            <li key={subtask.subtask_id} className="flex flex-wrap items-center gap-2 text-sm">
              <span className="min-w-0 flex-1">{subtaskLine(subtask)}</span>
              {!isMentor && canRetrySubtask(subtask) && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => retry.mutate(subtask.code)}
                  disabled={retry.isPending}
                >
                  重试
                </Button>
              )}
              {!isMentor && canCancelSubtask(subtask) && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => cancel.mutate(subtask.code)}
                  disabled={cancel.isPending}
                >
                  取消
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={onOpenResults}>
          查看候选论文（{task.result_total}）
        </Button>
        {!isMentor && !taskClosed(task) && (
          <Button size="sm" variant="ghost" onClick={() => setClosing((value) => !value)}>
            关闭任务
          </Button>
        )}
      </div>

      {closing && (
        <div className="space-y-2 rounded-md border border-dashed p-2">
          <Input
            value={closeReason}
            onChange={(event) => setCloseReason(event.target.value)}
            placeholder="关闭原因（必填）"
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => close.mutate()}
              disabled={close.isPending || closeReason.trim() === ""}
            >
              确认关闭
            </Button>
            <Button size="sm" variant="outline" onClick={() => setClosing(false)}>
              算了
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
