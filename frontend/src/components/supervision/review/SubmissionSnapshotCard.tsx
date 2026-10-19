import type { Submission } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import {
  parseCompletenessRows,
  submissionMaterials,
} from "@/components/supervision/review/snapshot";
import {
  SUBMISSION_STATUS_LABEL,
} from "@/components/supervision/review/reviewDraft";
import { Badge } from "@/components/ui/badge";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "时间未知";
}

function field(label: string, value: string) {
  if (!value) return null;
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="whitespace-pre-wrap break-words text-sm">{value}</div>
    </div>
  );
}

/**
 * 提交快照只读块：导师审核始终绑定这份快照（§11.4），
 * 因此这里显示的是提交当时的说明、Git 声明与材料清单，而不是任务的当前内容。
 */
export function SubmissionSnapshotCard({ submission }: { submission: Submission }) {
  const materials = submissionMaterials(submission);
  const warnings = parseCompletenessRows(submission.completeness_check).filter(
    (item) => !item.passed,
  );

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{submission.display_code}</span>
        <Badge variant="secondary">
          {SUBMISSION_STATUS_LABEL[submission.status] ?? submission.status}
        </Badge>
        <span className="text-xs text-muted-foreground">
          提交于 {when(submission.create_time)} · 绑定验收标准快照{" "}
          {submission.ac_snapshot.length} 条
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {field("提交说明", submission.summary)}
        {field("未完成项", submission.unfinished_items)}
        {field("Git 仓库", submission.git_repo)}
        {field("Git 分支", submission.git_branch)}
        {field("Git commit", submission.git_commit)}
        {field("实验配置与结果", submission.experiment_notes)}
        {field("已知问题", submission.known_issues)}
      </div>

      <div>
        <div className="text-xs text-muted-foreground">关联材料（固定版本）</div>
        {materials.length === 0 ? (
          <p className="text-sm text-muted-foreground">该提交没有关联平台内材料。</p>
        ) : (
          <ul className="text-sm">
            {materials.map((item) => (
              <li key={item.attachment_code} className="break-words">
                {item.name || item.attachment_code} · {item.attachment_code} · v
                {item.version}
                {item.kind ? ` · ${item.kind}` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>

      {warnings.length > 0 && (
        <AlertPanel
          tone="warning"
          title="完整性提醒（不阻止提交，仅供核对）"
          description={warnings.map((item) => item.message).join("\n")}
        />
      )}
    </div>
  );
}
