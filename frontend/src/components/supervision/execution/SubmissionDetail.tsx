import { useQuery } from "@tanstack/react-query";

import { getReview } from "@/api/supervision";
import type { ProjectDetail, Submission } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  AC_CONCLUSION_LABEL,
  joinAcWithAssessment,
  parseAcSnapshot,
  parseCompletenessRows,
  parseMaterialRows,
  parseSelfAssessmentRows,
  REVIEW_DECISION_LABEL,
  REVIEW_STATUS_LABEL,
  SELF_ASSESSMENT_LABEL,
} from "@/components/supervision/execution/logic";
import { Badge } from "@/components/ui/badge";

function Field({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <p className="whitespace-pre-wrap text-sm">
      <span className="text-muted-foreground">{label}：</span>
      {value}
    </p>
  );
}

/**
 * 提交详情（只读）：绑定提交当时的 AC 快照逐条展示，并显示学生当时的自评。
 * 导师结论由「导师审核」页签写入，这里只读展示审核状态与已发布内容。
 */
export function SubmissionDetail({
  project,
  submission,
}: {
  project: ProjectDetail;
  submission: Submission;
}) {
  const review = useQuery({
    queryKey: ["supervision", "review", project.project_id, submission.display_code],
    queryFn: () => getReview(project.project_id, submission.display_code),
  });

  const acRows = joinAcWithAssessment(
    parseAcSnapshot(submission.ac_snapshot),
    parseSelfAssessmentRows(submission.self_assessment),
  );
  const materials = parseMaterialRows(submission.materials);
  const checks = parseCompletenessRows(submission.completeness_check);
  const failedChecks = checks.filter((check) => !check.passed);

  return (
    <div className="space-y-3 border-t pt-3 text-sm">
      <Field label="摘要" value={submission.summary} />
      {(submission.git_repo || submission.git_branch || submission.git_commit) && (
        <p className="text-sm">
          <span className="text-muted-foreground">Git：</span>
          {[submission.git_repo, submission.git_branch, submission.git_commit]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
      <Field label="实验记录" value={submission.experiment_notes} />
      <Field label="已知问题" value={submission.known_issues} />
      <Field label="未完成事项" value={submission.unfinished_items} />

      <div>
        <div className="font-medium">验收标准快照与当时自评</div>
        <ul className="mt-1 space-y-2">
          {acRows.map((row) => (
            <li key={row.ac_code} className="rounded-md border p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{row.ac_code}</span>
                {row.required && <Badge variant="outline">必填</Badge>}
                <span>v{row.version}</span>
                {row.assessment?.self_assessment ? (
                  <Badge variant="secondary">
                    自评：
                    {SELF_ASSESSMENT_LABEL[
                      row.assessment.self_assessment as keyof typeof SELF_ASSESSMENT_LABEL
                    ] ?? row.assessment.self_assessment}
                  </Badge>
                ) : (
                  <Badge variant="outline">未自评</Badge>
                )}
              </div>
              <p className="mt-1 whitespace-pre-wrap">{row.statement}</p>
              {row.evidence_requirement && (
                <p className="mt-1 text-xs text-muted-foreground">
                  需要的证据：{row.evidence_requirement}
                </p>
              )}
              {row.assessment?.evidence && (
                <p className="mt-1 whitespace-pre-wrap">证据：{row.assessment.evidence}</p>
              )}
              {row.assessment?.note && (
                <p className="mt-1 text-muted-foreground">说明：{row.assessment.note}</p>
              )}
            </li>
          ))}
        </ul>
      </div>

      <div>
        <div className="font-medium">提交时关联的材料</div>
        {materials.length === 0 ? (
          <p className="mt-1 text-muted-foreground">没有勾选任何附件。</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {materials.map((row) => (
              <li key={row.attachment_code}>
                {row.attachment_code} · {row.name} · v{row.version}
                {row.kind ? ` · ${row.kind}` : ""}
              </li>
            ))}
          </ul>
        )}
      </div>

      {failedChecks.length > 0 && (
        <AlertPanel
          tone="warning"
          title="当时的完整性提醒（只是提醒，提交已成功）"
          description={failedChecks.map((check) => check.message).join("\n")}
        />
      )}

      {submission.status === "withdrawn" && (
        <AlertPanel
          title="该版本已撤回"
          description={`撤回原因：${submission.withdraw_reason || "未填写"}${
            submission.withdrawn_at ? ` · ${submission.withdrawn_at.slice(0, 16).replace("T", " ")}` : ""
          }。快照继续只读保留。`}
        />
      )}

      <div className="space-y-2 border-t pt-2">
        <div className="font-medium">导师审核</div>
        {review.isLoading && <p className="text-muted-foreground">审核状态加载中…</p>}
        {review.isError && (
          <AlertPanel tone="error" title="审核状态加载失败" description={errorText(review.error)} />
        )}
        {!review.isLoading && !review.isError && !review.data && (
          <AlertPanel title="导师尚未开始" description="该提交还没有任何审核记录。" />
        )}
        {review.data && (
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">
                审核状态：{REVIEW_STATUS_LABEL[review.data.status] ?? review.data.status}
              </Badge>
              {review.data.overall_decision && (
                <Badge>
                  结论：
                  {REVIEW_DECISION_LABEL[review.data.overall_decision] ??
                    review.data.overall_decision}
                </Badge>
              )}
              <span className="text-xs text-muted-foreground">{review.data.code}</span>
            </div>
            <Field label="结论原因" value={review.data.decision_reason} />
            {review.data.status !== "published" && (
              <p className="text-xs text-muted-foreground">
                审核尚未发布，以下内容只是导师的过程记录，不代表最终结论。
              </p>
            )}
            <Field label="反馈" value={review.data.feedback} />
            {review.data.must_fix.length > 0 && (
              <div>
                <span className="text-muted-foreground">必须修改：</span>
                <ul className="list-disc pl-5">
                  {review.data.must_fix.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {review.data.suggestions.length > 0 && (
              <div>
                <span className="text-muted-foreground">建议：</span>
                <ul className="list-disc pl-5">
                  {review.data.suggestions.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            <Field label="下一步" value={review.data.next_steps} />
            {review.data.conclusions.length > 0 && (
              <ul className="space-y-1">
                {review.data.conclusions.map((row) => (
                  <li key={row.ac_code} className="rounded-md border p-2">
                    <span className="font-medium">{row.ac_code}</span>
                    <span className="ml-2">
                      {AC_CONCLUSION_LABEL[row.conclusion] ?? row.conclusion}
                    </span>
                    {row.reason && <span className="ml-2 text-muted-foreground">{row.reason}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
