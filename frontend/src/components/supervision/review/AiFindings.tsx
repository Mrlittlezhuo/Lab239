import type { AiReviewFinding } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import {
  EVIDENCE_DEPTH_LABEL,
  SUPPORT_RELATION_LABEL,
  VERDICT_LABEL,
  findingEvidenceLines,
  findingMissingMaterialLines,
  findingNotes,
  findingRiskLines,
} from "@/components/supervision/review/aiReview";
import { Badge } from "@/components/ui/badge";

const VERDICT_TONE: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  satisfied: "default",
  partial: "secondary",
  not_satisfied: "destructive",
  unverifiable: "outline",
};

function block(title: string, lines: string[]) {
  if (lines.length === 0) return null;
  return (
    <div>
      <div className="text-xs text-muted-foreground">{title}</div>
      <ul className="list-disc pl-5 text-sm">
        {lines.map((line, index) => (
          <li key={`${title}-${index}`} className="whitespace-pre-wrap break-words">
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * 逐条 AI 结论（§10.1）：判断状态、证据强度、支持关系、理由、已满足/缺失部分、
 * 证据定位、缺失材料、风险、建议导师检查的位置，以及注入与「静态材料不能证明已运行」提示。
 * 措辞按 §10.3 保持审慎，不给分数、不给通过概率。
 */
export function AiFindings({ findings }: { findings: AiReviewFinding[] }) {
  if (findings.length === 0) {
    return (
      <AlertPanel
        title="这次运行没有逐条结论"
        description="运行未完成或被取消时不会生成逐条结论；页面不会伪造结果。"
      />
    );
  }

  return (
    <ul className="space-y-3">
      {findings.map((finding) => (
        <li key={finding.finding_id} className="space-y-2 rounded-md border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{finding.ac_code}</span>
            <Badge variant={VERDICT_TONE[finding.verdict] ?? "outline"}>
              {VERDICT_LABEL[finding.verdict] ?? finding.verdict}
            </Badge>
            <Badge variant="outline">
              {EVIDENCE_DEPTH_LABEL[finding.evidence_depth] ?? finding.evidence_depth}
            </Badge>
            <Badge variant="outline">
              {SUPPORT_RELATION_LABEL[finding.support_relation] ?? finding.support_relation}
            </Badge>
            <Badge variant="secondary">{finding.required ? "必选" : "可选"}</Badge>
            <span className="text-xs text-muted-foreground">
              标准 v{finding.ac_version} · 批次 {finding.batch_index}
            </span>
          </div>

          <div className="whitespace-pre-wrap text-sm">{finding.ac_statement}</div>
          {finding.reason && (
            <div className="whitespace-pre-wrap text-sm">理由：{finding.reason}</div>
          )}
          {finding.satisfied_parts && (
            <div className="whitespace-pre-wrap text-sm">
              已满足部分：{finding.satisfied_parts}
            </div>
          )}
          {finding.missing_parts && (
            <div className="whitespace-pre-wrap text-sm">
              缺失部分：{finding.missing_parts}
            </div>
          )}

          {block("证据定位", findingEvidenceLines(finding))}
          {block("缺失材料", findingMissingMaterialLines(finding))}
          {block("风险或冲突", findingRiskLines(finding))}

          {finding.mentor_focus && (
            <div className="text-xs text-muted-foreground">
              建议导师重点看：{finding.mentor_focus}
            </div>
          )}

          {findingNotes(finding).length > 0 && (
            <AlertPanel tone="warning" title="需要注意" description={findingNotes(finding).join("\n")} />
          )}
        </li>
      ))}
    </ul>
  );
}
