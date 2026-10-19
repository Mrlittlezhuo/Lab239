import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { adoptAiSuggestion, listAiMentorDiff, listAiSuggestions } from "@/api/supervision";
import type { AiReviewRun, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  VERDICT_LABEL,
  canAdoptRow,
  mergeAiMentorRows,
  runActions,
} from "@/components/supervision/review/aiReview";
import {
  AC_CONCLUSION_LABEL,
  AI_RELATION_LABEL,
} from "@/components/supervision/review/reviewDraft";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/**
 * 「AI 建议」与「导师结论」的对照块（§15.2、P-22）。
 *
 * 这一块只说明 AI 建议了什么、导师最终选了什么、两者是否一致；
 * 它不是「审核结果」：导师结论仍然在「导师审核」页签独立填写与发布，
 * 采纳是逐条主动操作且必须写理由，系统不会自动复制或替导师转换。
 */
export function AiRelationCard({
  project,
  run,
}: {
  project: ProjectDetail;
  run: AiReviewRun;
}) {
  const queryClient = useQueryClient();
  const [adoptingAc, setAdoptingAc] = useState("");
  const [reason, setReason] = useState("");

  const canQuery = run.status === "completed" && Boolean(run.submission_id);

  const suggestions = useQuery({
    queryKey: ["supervision", "ai-review", "suggestions", project.project_id, run.code],
    queryFn: () => listAiSuggestions(project.project_id, run.code),
    enabled: canQuery,
  });
  const diff = useQuery({
    queryKey: ["supervision", "ai-review", "mentor-diff", project.project_id, run.code],
    queryFn: () => listAiMentorDiff(project.project_id, run.code),
    enabled: canQuery,
  });

  const adopt = useMutation({
    mutationFn: () =>
      adoptAiSuggestion(project.project_id, run.code, adoptingAc, reason),
    onSuccess: (saved) => {
      toast.success(`已采纳 ${saved.ac_code} 的建议：${saved.conclusion}`);
      setAdoptingAc("");
      setReason("");
      queryClient.invalidateQueries({ queryKey: ["supervision", "ai-review"] });
      queryClient.invalidateQueries({ queryKey: ["supervision", "review"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  if (!canQuery) {
    return (
      <AlertPanel
        title="还没有可对照的 AI 建议"
        description="只有完成且绑定正式提交的 AI 预审才会给出建议结论；学生自检不是导师审核依据，不能采纳为导师结论。"
      />
    );
  }
  if (suggestions.isLoading || diff.isLoading) {
    return <p className="text-sm text-muted-foreground">AI 建议加载中…</p>;
  }
  if (suggestions.isError) {
    return (
      <AlertPanel tone="error" title="AI 建议加载失败" description={errorText(suggestions.error)} />
    );
  }

  const rows = mergeAiMentorRows(suggestions.data ?? [], diff.data ?? []);
  const actions = runActions(run);

  return (
    <div className="space-y-3">
      <AlertPanel
        title="AI 建议与导师结论是两个独立域"
        description="下面是 AI 建议与导师已填结论的对照。采纳只会把对应的一条写进导师审核草稿，且必须写理由；其余结论仍要在「导师审核」页签逐条填写。"
      />

      {rows.length === 0 && (
        <AlertPanel title="这次运行没有给出建议结论" description="逐条结论缺失时不会凭空生成建议。" />
      )}

      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.ac_code} className="space-y-2 rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{row.ac_code}</span>
              <Badge variant="secondary">
                AI 判断：{VERDICT_LABEL[row.verdict] ?? row.verdict}
              </Badge>
              <Badge variant="outline">
                AI 建议：{row.suggested_label || row.suggested_conclusion}
              </Badge>
              <Badge variant={row.consistent === true ? "default" : "outline"}>
                导师：
                {row.mentor_conclusion === ""
                  ? "尚未填写"
                  : (AC_CONCLUSION_LABEL[row.mentor_conclusion as keyof typeof AC_CONCLUSION_LABEL] ??
                    row.mentor_conclusion)}
              </Badge>
              {row.ai_relation !== "none" && (
                <Badge variant="outline">
                  {AI_RELATION_LABEL[row.ai_relation] ?? row.ai_relation}
                </Badge>
              )}
            </div>

            {row.ai_reason && (
              <div className="whitespace-pre-wrap text-sm">AI 理由：{row.ai_reason}</div>
            )}
            {row.note && <div className="text-xs text-amber-600">{row.note}</div>}
            {row.consistent === false && (
              <div className="text-xs text-muted-foreground">
                导师结论与 AI 建议不同，已按「选择不同导师结论」记录，不影响导师选择。
              </div>
            )}

            {adoptingAc === row.ac_code ? (
              <div className="space-y-2">
                <Input
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="采纳理由（必填）：为什么按 AI 建议下结论"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => adopt.mutate()}
                    disabled={adopt.isPending || reason.trim() === ""}
                  >
                    {adopt.isPending ? "提交中…" : `确认采纳 ${row.ac_code}`}
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setAdoptingAc("")}>
                    取消
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  setAdoptingAc(row.ac_code);
                  setReason("");
                }}
                disabled={!canAdoptRow(row, actions)}
              >
                采纳此建议
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
