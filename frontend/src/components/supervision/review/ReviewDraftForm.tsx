import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  publishReview,
  saveReviewDraft,
} from "@/api/supervision";
import type {
  AiSuggestion,
  ProjectDetail,
  Review,
  ReviewDecision,
} from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { AcConclusionMatrix } from "@/components/supervision/review/AcConclusionMatrix";
import type { AcRowWithAssessment } from "@/components/supervision/review/snapshot";
import {
  REVIEW_DECISION_LABEL,
  REVIEW_DECISION_VALUES,
  concludedCount,
  hasDraftContent,
  isFormDirty,
  reviewFormFromReview,
  reviewPayload,
  validatePublish,
  type ReviewFormState,
} from "@/components/supervision/review/reviewDraft";
import { Button } from "@/components/ui/button";
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

const DECISION_HINT: Record<string, string> = {
  approved: "任务转为已完成；所有必选验收标准都必须有结论。",
  revision_required: "任务保持进行中，学生随后提交新版本；必须写必须修改项。",
  rejected: "不接受本次成果，必须写原因，由导师决定取消任务还是重新下发。",
  paused: "暂缓审核，必须写原因，任务保持进行中。",
};

/**
 * 导师审核草稿表单：逐条结论矩阵 + 整体决定 + 反馈 + 必须修改项 + 建议 + 下一步。
 *
 * 草稿与发布分离（§5.4）：保存草稿可以反复做，只打开页面不创建草稿；
 * 发布后后端拒绝再次写入，只能追加意见。
 */
export function ReviewDraftForm({
  project,
  submissionRef,
  rows,
  review,
  suggestions,
  aiRunLabel,
  refresh,
}: {
  project: ProjectDetail;
  submissionRef: string;
  rows: AcRowWithAssessment[];
  review: Review | null;
  suggestions: AiSuggestion[];
  aiRunLabel: string;
  refresh: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ReviewFormState>(() =>
    reviewFormFromReview(review, rows),
  );
  const [decision, setDecision] = useState<ReviewDecision>(
    review?.overall_decision ?? "",
  );
  const [reason, setReason] = useState(review?.decision_reason ?? "");

  const problems = validatePublish(form, rows, decision, reason);
  const dirty = isFormDirty(form, review);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["supervision", "review"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "review-queue"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "submissions"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "tasks"] });
  };

  const saveDraft = useMutation({
    mutationFn: () =>
      saveReviewDraft(project.project_id, submissionRef, reviewPayload(form)),
    onSuccess: () => {
      toast.success("审核草稿已保存，尚未发布");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const publish = useMutation({
    mutationFn: () =>
      publishReview(project.project_id, submissionRef, {
        ...reviewPayload(form),
        decision,
        reason,
      }),
    onSuccess: (saved) => {
      toast.success(`审核已发布：${saved.code}（结论不可再修改，只能追加说明）`);
      invalidate();
      refresh();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  return (
    <div className="space-y-4">
      <AlertPanel
        title={review ? `草稿 ${review.code}（未发布）` : "尚未开始审核"}
        description={
          review
            ? `首次处理时间 ${review.first_touched_at?.slice(0, 16).replace("T", " ") ?? "未知"}。草稿只有导师自己可见，学生看不到未发布的结论。`
            : "只打开本页不会创建草稿；第一次保存逐条结论或意见才会记录「首次处理时间」。"
        }
      />

      <AcConclusionMatrix
        rows={rows}
        matrix={form.matrix}
        onChange={(matrix) => setForm({ ...form, matrix })}
        suggestions={suggestions}
        aiRunLabel={aiRunLabel}
      />

      <div className="space-y-3 rounded-md border p-3">
        <div className="text-sm font-medium">整体决定与意见</div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">总体结论</Label>
          <Select
            value={decision === "" ? "unset" : decision}
            onValueChange={(value) =>
              setDecision(value === "unset" ? "" : (value as Exclude<ReviewDecision, "">))
            }
          >
            <SelectTrigger className="w-[min(100%,320px)]">
              <SelectValue placeholder="尚未选择" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="unset">尚未选择</SelectItem>
              {REVIEW_DECISION_VALUES.map((value) => (
                <SelectItem key={value} value={value}>
                  {REVIEW_DECISION_LABEL[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {decision && (
            <p className="text-xs text-muted-foreground">{DECISION_HINT[decision]}</p>
          )}
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">
            决定原因{decision === "rejected" || decision === "paused" ? "（必填）" : ""}
          </Label>
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="为什么给出这个总体结论"
          />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">总体反馈</Label>
          <Textarea
            value={form.feedback}
            onChange={(event) => setForm({ ...form, feedback: event.target.value })}
            placeholder="给学生看的总体评价"
          />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">
            必须修改项（每行一条；「要求修改」时至少一条）
          </Label>
          <Textarea
            value={form.mustFix}
            onChange={(event) => setForm({ ...form, mustFix: event.target.value })}
            placeholder={"补上评测结果文件\n说明指标口径"}
          />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">建议改进项（每行一条）</Label>
          <Textarea
            value={form.suggestions}
            onChange={(event) => setForm({ ...form, suggestions: event.target.value })}
            placeholder="不阻塞通过的建议"
          />
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-muted-foreground">下一步安排</Label>
          <Textarea
            value={form.nextSteps}
            onChange={(event) => setForm({ ...form, nextSteps: event.target.value })}
            placeholder="下一版要交什么、什么时候再看"
          />
        </div>
      </div>

      {problems.length > 0 && (
        <AlertPanel
          tone="warning"
          title="发布前还缺这些"
          description={problems.join("\n")}
        />
      )}

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            onClick={() => saveDraft.mutate()}
            disabled={!hasDraftContent(form) || saveDraft.isPending}
          >
            {saveDraft.isPending ? "保存中…" : "保存草稿"}
          </Button>
          <Button onClick={() => publish.mutate()} disabled={publish.isPending}>
            {publish.isPending ? "发布中…" : "发布审核结论"}
          </Button>
          <span className="text-xs text-muted-foreground">
            已填 {concludedCount(form.matrix)} / 共 {rows.length} 条结论
            {dirty ? " · 有未保存的修改" : ""}
          </span>
        </div>
        <p className="text-xs text-muted-foreground">
          发布后结论不可修改，只能追加补充意见或发起修订；AI 预审状态与导师结论是两个独立域，
          发布不会改动 AI 预审，AI 也不会替导师填结论。
        </p>
      </div>
    </div>
  );
}
