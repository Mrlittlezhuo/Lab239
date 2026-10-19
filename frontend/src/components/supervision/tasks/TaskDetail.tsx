import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getReview,
  getTask,
  listProgress,
  listSubmissions,
} from "@/api/supervision";
import type {
  ProjectDetail,
  Review,
  Submission,
  Task,
  TaskAc,
} from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  AC_CONCLUSION_LABEL,
  REVIEW_DECISION_LABEL,
  REVIEW_STATUS_LABEL,
  SELF_ASSESSMENT_LABEL,
  SUBMISSION_STATUS_LABEL,
  checkTypeLabel,
  dueLabel,
  formatDateTime,
  priorityLabel,
} from "@/components/supervision/tasks/taskDraft";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface SelfAssessRow {
  ac_code?: string;
  self_assessment?: string;
  evidence?: string;
  note?: string;
}

interface AcSnapshotRow {
  ac_code?: string;
  statement?: string;
  required?: boolean;
  check_type?: string;
  version?: number;
}

interface CompletenessRow {
  key?: string;
  passed?: boolean;
  message?: string;
}

/**
 * 任务详情四象限（§6.4）：任务要解决什么 / 验收标准逐条 / 学生进展 /
 * 提交与审核情况。后两块在这里只读展示——进展、提交和审核分别由
 * 「进展与提交」「导师审核」面板负责写入，这里不产生任何写操作。
 *
 * 状态标签一律用后端 `label`（§5.6）；提交状态、审核状态和逐条结论是各自
 * 独立的枚举，这里分开显示，不折算成一个"审核结果"。
 */
export function TaskDetail({
  project,
  taskCode,
  isMentor,
  onEdit,
  onPublish,
  onCancel,
  publishing,
}: {
  project: ProjectDetail;
  taskCode: string;
  isMentor: boolean;
  onEdit: (task: Task) => void;
  onPublish: (task: Task) => void;
  onCancel: (task: Task) => void;
  publishing: boolean;
}) {
  const task = useQuery({
    queryKey: ["supervision", "task", project.project_id, taskCode],
    queryFn: () => getTask(project.project_id, taskCode),
  });
  const progress = useQuery({
    queryKey: ["supervision", "progress", project.project_id, taskCode],
    queryFn: () => listProgress(project.project_id, taskCode),
  });
  const submissions = useQuery({
    queryKey: ["supervision", "submissions", project.project_id, taskCode],
    queryFn: () => listSubmissions(project.project_id, taskCode),
  });

  if (task.isLoading) {
    return <p className="text-sm text-muted-foreground">任务详情加载中…</p>;
  }
  if (task.isError || !task.data) {
    return (
      <AlertPanel
        tone="error"
        title="打不开该任务"
        description={task.error ? errorText(task.error) : "任务可能已被删除。"}
      />
    );
  }

  const current = task.data;
  const terminal =
    current.status === "completed" ||
    current.status === "cancelled" ||
    current.status === "archived";

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{current.title}</span>
        <Badge variant="secondary">{current.label}</Badge>
        <Badge variant="outline">{current.code}</Badge>
        <Badge variant="outline">验收标准 v{current.version}</Badge>
        <span className="text-xs text-muted-foreground">
          优先级 {priorityLabel(current.priority)} · 截止 {dueLabel(current, new Date())}
        </span>
        {isMentor && (
          <div className="ml-auto flex flex-wrap gap-2">
            {!terminal && (
              <Button size="sm" variant="outline" onClick={() => onEdit(current)}>
                编辑
              </Button>
            )}
            {current.status === "draft" && (
              <Button
                size="sm"
                onClick={() => onPublish(current)}
                disabled={publishing}
              >
                发布
              </Button>
            )}
            {!terminal && (
              <Button size="sm" variant="ghost" onClick={() => onCancel(current)}>
                取消任务
              </Button>
            )}
          </div>
        )}
      </div>

      {current.cancel_reason && (
        <AlertPanel tone="warning" title="取消原因" description={current.cancel_reason} />
      )}
      {current.version > 1 && (
        <AlertPanel
          title={`验收标准已改到 v${current.version}`}
          description="学生看到的是当前版本；此前每个提交仍绑定它当时的验收标准快照。"
        />
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <Quadrant title="任务要解决什么">
          <p className="whitespace-pre-wrap text-sm">{current.purpose || "（未填写目的）"}</p>
          {current.description && (
            <p className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground">
              {current.description}
            </p>
          )}
          <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
            <li>
              代码审查：{current.requires_code_review ? "需要" : "不需要"} · 运行证据：
              {current.requires_runtime ? "需要" : "不需要"}
            </li>
            {current.published_at && (
              <li>发布于 {formatDateTime(current.published_at)}</li>
            )}
          </ul>
          <div className="mt-2 text-xs font-medium">要求提交的材料</div>
          {(current.required_materials ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">没有额外材料要求。</p>
          ) : (
            <ul className="mt-1 space-y-1 text-xs text-muted-foreground">
              {(current.required_materials ?? []).map((material, index) => (
                <li key={material.key ?? String(index)}>
                  {material.label || material.key}
                  {material.required ? "（必交）" : "（可选）"}
                </li>
              ))}
            </ul>
          )}
        </Quadrant>

        <Quadrant title={`验收标准逐条（v${current.version}）`}>
          {current.acs.length === 0 ? (
            <AlertPanel title="还没有验收标准" description="发布前至少要有一条。" />
          ) : (
            <ul className="space-y-2">
              {current.acs.map((ac: TaskAc) => (
                <li key={ac.ac_code} className="rounded-md border p-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Badge variant="secondary">{ac.ac_code}</Badge>
                    <Badge variant="outline">{checkTypeLabel(ac.check_type)}</Badge>
                    <Badge variant={ac.required ? "default" : "outline"}>
                      {ac.required ? "必选" : "可选"}
                    </Badge>
                    <span className="text-muted-foreground">AC v{ac.version}</span>
                  </div>
                  <p className="mt-1 whitespace-pre-wrap text-sm">{ac.statement}</p>
                  {ac.evidence_requirement && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      证据要求：{ac.evidence_requirement}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Quadrant>

        <Quadrant title="学生进展">
          {progress.isLoading && (
            <p className="text-sm text-muted-foreground">加载中…</p>
          )}
          {progress.isError && (
            <AlertPanel tone="error" title="进展读取失败" description={errorText(progress.error)} />
          )}
          {!progress.isLoading && (progress.data?.length ?? 0) === 0 && (
            <AlertPanel
              title="还没有进展记录"
              description="学生在「进展与提交」里记录过程；进展不会被视为正式提交。"
            />
          )}
          <ul className="space-y-2">
            {(progress.data ?? []).map((note) => (
              <li key={note.progress_id} className="rounded-md border p-2">
                <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <span>{note.code}</span>
                  <span>{note.author_role === "mentor" ? "导师" : "学生"}</span>
                  <span>{formatDateTime(note.create_time)}</span>
                  {(note.links?.length ?? 0) > 0 && <span>{note.links.length} 条链接</span>}
                </div>
                <ProgressField label="本次完成" value={note.done} />
                <ProgressField label="结果" value={note.results} />
                <ProgressField label="用到的材料" value={note.materials} />
                <ProgressField label="遇到问题" value={note.problems} />
                <ProgressField label="下一步" value={note.next_steps} />
                {(note.notes?.length ?? 0) > 0 && (
                  <ul className="mt-2 space-y-1 border-t pt-1 text-xs text-muted-foreground">
                    {note.notes.map((item) => (
                      <li key={item.note_id}>
                        {item.author_role === "mentor" ? "导师补充" : "学生补充"}：{item.note}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </Quadrant>

        <Quadrant title="提交与审核情况">
          {submissions.isLoading && (
            <p className="text-sm text-muted-foreground">加载中…</p>
          )}
          {submissions.isError && (
            <AlertPanel
              tone="error"
              title="提交记录读取失败"
              description={errorText(submissions.error)}
            />
          )}
          {!submissions.isLoading && (submissions.data?.length ?? 0) === 0 && (
            <AlertPanel
              title="还没有正式提交"
              description="正式提交是学生提交的唯一审核输入，提交后版本只读。"
            />
          )}
          <ul className="space-y-2">
            {(submissions.data ?? []).map((submission) => (
              <SubmissionCard
                key={submission.submission_id}
                project={project}
                submission={submission}
              />
            ))}
          </ul>
        </Quadrant>
      </div>
    </div>
  );
}

function Quadrant({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2 rounded-md border bg-muted/20 p-3">
      <div className="text-sm font-medium">{title}</div>
      {children}
    </section>
  );
}

function ProgressField({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <p className="mt-1 whitespace-pre-wrap text-sm">
      <span className="text-xs text-muted-foreground">{label}：</span>
      {value}
    </p>
  );
}

/**
 * 一份提交加它的审核情况。审核记录只读展示；写审核是「导师审核」面板的事。
 * 没有审核记录时后端返回 null，不是 404。
 */
function SubmissionCard({
  project,
  submission,
}: {
  project: ProjectDetail;
  submission: Submission;
}) {
  const review = useQuery({
    queryKey: ["supervision", "review", project.project_id, submission.submission_id],
    queryFn: () => getReview(project.project_id, submission.submission_id),
  });

  const selfAssessment = (submission.self_assessment ?? []) as unknown as SelfAssessRow[];
  const snapshot = (submission.ac_snapshot ?? []) as unknown as AcSnapshotRow[];
  const checks = (submission.completeness_check ?? []) as unknown as CompletenessRow[];
  const warnings = checks.filter((item) => item.passed === false);

  return (
    <li className="rounded-md border p-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-sm">{submission.display_code}</span>
        <Badge variant="secondary">
          {SUBMISSION_STATUS_LABEL[submission.status] ?? submission.status}
        </Badge>
        <span className="text-muted-foreground">
          {formatDateTime(submission.create_time)} · 验收标准快照 {snapshot.length} 条
        </span>
      </div>
      {submission.summary && (
        <p className="mt-1 whitespace-pre-wrap text-sm">{submission.summary}</p>
      )}
      {(submission.git_repo || submission.git_branch || submission.git_commit) && (
        <p className="mt-1 text-xs text-muted-foreground">
          声明的代码：{submission.git_repo} {submission.git_branch} {submission.git_commit}
        </p>
      )}
      {selfAssessment.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
          {selfAssessment.map((item, index) => (
            <li key={`${item.ac_code ?? "row"}-${index}`}>
              {item.ac_code}：
              {SELF_ASSESSMENT_LABEL[item.self_assessment ?? ""] ?? item.self_assessment}
              {item.evidence ? ` · 证据：${item.evidence}` : ""}
            </li>
          ))}
        </ul>
      )}
      {warnings.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
          {warnings.map((item, index) => (
            <li key={`${item.key ?? "check"}-${index}`}>完整性提醒：{item.message}</li>
          ))}
        </ul>
      )}
      {submission.withdraw_reason && (
        <p className="mt-1 text-xs text-muted-foreground">
          撤回原因：{submission.withdraw_reason}
        </p>
      )}

      {review.isLoading && <p className="mt-2 text-xs text-muted-foreground">审核情况加载中…</p>}
      {review.isError && (
        <p className="mt-2 text-xs text-muted-foreground">审核情况暂不可读。</p>
      )}
      {!review.isLoading && !review.data && (
        <p className="mt-2 text-xs text-muted-foreground">该提交还没有审核记录。</p>
      )}
      {review.data && <ReviewSummary review={review.data} />}
    </li>
  );
}

function ReviewSummary({ review }: { review: Review }) {
  return (
    <div className="mt-2 space-y-1 border-t pt-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground">审核</span>
        <Badge variant="outline">
          {REVIEW_STATUS_LABEL[review.status] ?? review.status}
        </Badge>
        {review.overall_decision && (
          <Badge variant="secondary">
            {REVIEW_DECISION_LABEL[review.overall_decision] ?? review.overall_decision}
          </Badge>
        )}
        {review.published_at && (
          <span className="text-muted-foreground">
            发布于 {formatDateTime(review.published_at)}
          </span>
        )}
      </div>
      {review.decision_reason && <p>结论说明：{review.decision_reason}</p>}
      {review.feedback && <p className="whitespace-pre-wrap">意见：{review.feedback}</p>}
      {review.must_fix.length > 0 && (
        <p className="whitespace-pre-wrap">必须修改：{review.must_fix.join("；")}</p>
      )}
      {review.conclusions.length > 0 && (
        <ul className="space-y-1">
          {review.conclusions.map((item) => (
            <li key={item.ac_code}>
              {item.ac_code}：
              {AC_CONCLUSION_LABEL[item.conclusion] ?? item.conclusion}
              {item.reason ? ` · ${item.reason}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
