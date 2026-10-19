import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { addReviewNote } from "@/api/supervision";
import type { ProjectDetail, Review } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import type { AcRowWithAssessment } from "@/components/supervision/review/snapshot";
import {
  AC_CONCLUSION_LABEL,
  AI_RELATION_LABEL,
  REVIEW_DECISION_LABEL,
} from "@/components/supervision/review/reviewDraft";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const DECISION_TONE: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  approved: "default",
  revision_required: "secondary",
  rejected: "destructive",
  paused: "outline",
};

function list(title: string, items: string[]) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="text-xs text-muted-foreground">{title}</div>
      <ul className="list-disc pl-5 text-sm">
        {items.map((item, index) => (
          <li key={`${title}-${index}`} className="whitespace-pre-wrap">
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 已发布审核的只读视图 + 追加意见（§4.11）：
 * 后端拒绝再次写入结论，所以这里没有任何编辑结论的入口，只能追加说明或发起修订。
 */
export function PublishedReview({
  project,
  review,
  rows,
}: {
  project: ProjectDetail;
  review: Review;
  rows: AcRowWithAssessment[];
}) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [kind, setKind] = useState("clarification");

  const addNote = useMutation({
    mutationFn: () => addReviewNote(project.project_id, review.review_id, { note, kind }),
    onSuccess: () => {
      toast.success("已追加意见，原结论保持不变");
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["supervision", "review"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const statementByCode = new Map(rows.map((row) => [row.ac_code, row.statement]));
  const decisionLabel =
    REVIEW_DECISION_LABEL[review.overall_decision as keyof typeof REVIEW_DECISION_LABEL] ??
    review.overall_decision ??
    "未填";

  return (
    <div className="space-y-4">
      <div className="space-y-2 rounded-md border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{review.code}</span>
          <Badge variant={DECISION_TONE[review.overall_decision] ?? "outline"}>
            {decisionLabel}
          </Badge>
          <span className="text-xs text-muted-foreground">
            发布于 {review.published_at?.slice(0, 16).replace("T", " ") ?? "未知"} · 审核人{" "}
            {review.reviewer_id}
          </span>
        </div>
        {review.decision_reason && (
          <p className="whitespace-pre-wrap text-sm">决定原因：{review.decision_reason}</p>
        )}
        <AlertPanel
          tone="warning"
          title="已发布，结论不可修改"
          description="需要更正时只能在下面追加补充意见或发起修订，原结论与历史完整保留。"
        />
      </div>

      <div className="space-y-2">
        <div className="text-sm font-medium">逐条结论</div>
        {review.conclusions.length === 0 ? (
          <p className="text-sm text-muted-foreground">这份审核没有逐条结论。</p>
        ) : (
          <ul className="space-y-2">
            {review.conclusions.map((row) => (
              <li key={row.ac_code} className="space-y-1 rounded-md border p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{row.ac_code}</span>
                  <Badge variant="secondary">
                    {AC_CONCLUSION_LABEL[row.conclusion as keyof typeof AC_CONCLUSION_LABEL] ??
                      row.conclusion}
                  </Badge>
                  {row.ai_relation !== "none" && (
                    <Badge variant="outline">
                      {AI_RELATION_LABEL[row.ai_relation] ?? row.ai_relation}
                    </Badge>
                  )}
                </div>
                {statementByCode.get(row.ac_code) && (
                  <div className="whitespace-pre-wrap text-xs text-muted-foreground">
                    {statementByCode.get(row.ac_code)}
                  </div>
                )}
                <div className="whitespace-pre-wrap text-sm">{row.reason || "（未写理由）"}</div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="space-y-3 rounded-md border p-3">
        {review.feedback && (
          <div>
            <div className="text-xs text-muted-foreground">总体反馈</div>
            <p className="whitespace-pre-wrap text-sm">{review.feedback}</p>
          </div>
        )}
        {list("必须修改项", review.must_fix ?? [])}
        {list("建议改进项", review.suggestions ?? [])}
        {review.next_steps && (
          <div>
            <div className="text-xs text-muted-foreground">下一步安排</div>
            <p className="whitespace-pre-wrap text-sm">{review.next_steps}</p>
          </div>
        )}
      </div>

      <div className="space-y-2">
        <div className="text-sm font-medium">追加意见</div>
        {review.notes.length > 0 && (
          <ul className="space-y-2">
            {review.notes.map((item) => (
              <li key={item.note_id} className="rounded-md border p-3 text-sm">
                <div className="whitespace-pre-wrap">{item.note}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  {item.author_role === "mentor" ? "导师" : item.author_role} ·{" "}
                  {item.create_time?.slice(0, 16).replace("T", " ") ?? "时间未知"}
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="space-y-2 rounded-md border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Label className="text-xs text-muted-foreground">追加类型</Label>
            <Select value={kind} onValueChange={setKind}>
              <SelectTrigger className="h-9 w-[200px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="clarification">追加说明</SelectItem>
                <SelectItem value="amendment">发起修订</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="对已发布结论的补充说明或需要学生重新处理的内容"
          />
          <Button
            size="sm"
            onClick={() => addNote.mutate()}
            disabled={addNote.isPending || note.trim() === ""}
          >
            {addNote.isPending ? "提交中…" : "追加意见"}
          </Button>
        </div>
      </div>
    </div>
  );
}
