import type { AcConclusion, AiSuggestion } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import type {
  AcSnapshotRow,
  SelfAssessmentRow,
} from "@/components/supervision/review/snapshot";
import {
  AC_CONCLUSION_LABEL,
  AC_CONCLUSION_VALUES,
  SELF_ASSESSMENT_LABEL,
  type ConclusionMatrix,
} from "@/components/supervision/review/reviewDraft";
import { VERDICT_LABEL } from "@/components/supervision/review/aiReview";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

/** Radix 的 SelectItem 不接受空串，用哨兵表示「还没判断」。 */
const UNSET = "unset";

export function AcConclusionMatrix({
  rows,
  matrix,
  onChange,
  suggestions,
  aiRunLabel,
}: {
  rows: (AcSnapshotRow & { assessment: SelfAssessmentRow | null })[];
  matrix: ConclusionMatrix;
  onChange: (next: ConclusionMatrix) => void;
  suggestions: AiSuggestion[];
  aiRunLabel: string;
}) {
  if (rows.length === 0) {
    return (
      <AlertPanel
        tone="warning"
        title="这份提交没有验收标准快照"
        description="导师结论必须绑定提交当时的验收标准（§11.4）。没有快照时不能逐条下结论，请先核对提交数据。"
      />
    );
  }

  const byCode = new Map(suggestions.map((item) => [item.ac_code, item]));

  function patch(acCode: string, patchValue: Partial<(typeof matrix)[string]>) {
    const current = matrix[acCode];
    if (!current) return;
    onChange({ ...matrix, [acCode]: { ...current, ...patchValue } });
  }

  return (
    <div className="space-y-3">
      <div className="text-sm text-muted-foreground">
        验收标准矩阵：逐条结论只针对本次提交快照里的验收标准。AI 建议列只读，
        {aiRunLabel ? `来自 ${aiRunLabel}` : "本次提交没有已完成的 AI 预审"}；
        采纳 AI 建议要在「AI 预审」页签逐条操作，这里不会自动复制 AI 结果。
      </div>

      {rows.map((row) => {
        const draft = matrix[row.ac_code];
        const suggestion = byCode.get(row.ac_code) ?? null;
        return (
          <div key={row.ac_code} className="space-y-3 rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{row.ac_code}</span>
              <Badge variant={row.required ? "default" : "outline"}>
                {row.required ? "必选" : "可选"}
              </Badge>
              {row.check_type && (
                <Badge variant="secondary">检查方式 {row.check_type}</Badge>
              )}
              <span className="text-xs text-muted-foreground">标准 v{row.version}</span>
            </div>

            <div className="whitespace-pre-wrap text-sm">{row.statement}</div>
            {row.evidence_requirement && (
              <div className="text-xs text-muted-foreground">
                需要的证据：{row.evidence_requirement}
              </div>
            )}

            <div className="grid gap-3 lg:grid-cols-3">
              <div className="space-y-1">
                <div className="text-xs text-muted-foreground">学生自评</div>
                {row.assessment === null ? (
                  <p className="text-sm text-muted-foreground">学生没有对这条自评。</p>
                ) : (
                  <>
                    <div className="text-sm">
                      {SELF_ASSESSMENT_LABEL[row.assessment.self_assessment] ??
                        row.assessment.self_assessment ??
                        "未填"}
                    </div>
                    {row.assessment.evidence && (
                      <div className="whitespace-pre-wrap text-xs text-muted-foreground">
                        证据：{row.assessment.evidence}
                      </div>
                    )}
                    {row.assessment.note && (
                      <div className="whitespace-pre-wrap text-xs text-muted-foreground">
                        说明：{row.assessment.note}
                      </div>
                    )}
                  </>
                )}
              </div>

              <div className="space-y-1">
                <div className="text-xs text-muted-foreground">AI 建议（只读）</div>
                {suggestion === null ? (
                  <p className="text-sm text-muted-foreground">AI 没有覆盖这条标准。</p>
                ) : (
                  <>
                    <div className="text-sm">
                      {suggestion.suggested_label}
                      <span className="ml-1 text-xs text-muted-foreground">
                        （AI 判断：{VERDICT_LABEL[suggestion.verdict] ?? suggestion.verdict}）
                      </span>
                    </div>
                    {suggestion.reason && (
                      <div className="whitespace-pre-wrap text-xs text-muted-foreground">
                        {suggestion.reason}
                      </div>
                    )}
                    {suggestion.note && (
                      <div className="text-xs text-amber-600">{suggestion.note}</div>
                    )}
                  </>
                )}
              </div>

              <div className="space-y-2">
                <Label className="text-xs text-muted-foreground">导师结论</Label>
                <Select
                  value={draft?.conclusion === "" ? UNSET : (draft?.conclusion ?? UNSET)}
                  onValueChange={(value) =>
                    patch(row.ac_code, {
                      conclusion: value === UNSET ? "" : (value as AcConclusion),
                    })
                  }
                >
                  <SelectTrigger className="h-9">
                    <SelectValue placeholder="尚未判断" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>尚未判断</SelectItem>
                    {AC_CONCLUSION_VALUES.map((value) => (
                      <SelectItem key={value} value={value}>
                        {AC_CONCLUSION_LABEL[value]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Textarea
                  value={draft?.reason ?? ""}
                  onChange={(event) => patch(row.ac_code, { reason: event.target.value })}
                  placeholder="结论理由（选了结论就必须填；标记「不适用」时必须填理由）"
                  className="min-h-[64px]"
                />
                {draft?.ai_relation && draft.ai_relation !== "none" && (
                  <div className="text-xs text-muted-foreground">
                    {draft.ai_relation === "adopted" ? "已采纳 AI 建议" : "与 AI 建议不同"}
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
