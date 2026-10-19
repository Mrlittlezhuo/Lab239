import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";

import { createSubmission } from "@/api/supervision";
import type { Attachment, ProjectDetail, SelfAssessment, Submission, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  acsMissingEvidence,
  buildSelfAssessmentPayload,
  clientCompletenessHints,
  initialAcDrafts,
  SELF_ASSESSMENT_LABEL,
  SELF_ASSESSMENT_VALUES,
  type AcDraft,
} from "@/components/supervision/execution/logic";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

const TEXT_FIELDS: {
  key: "summary" | "experiment_notes" | "known_issues" | "unfinished_items";
  label: string;
  placeholder: string;
}[] = [
  { key: "summary", label: "提交摘要", placeholder: "这一版提交做了什么、结论是什么" },
  { key: "experiment_notes", label: "实验记录（配置/命令/环境/结果）", placeholder: "怎样复现这一版结果" },
  { key: "known_issues", label: "已知问题", placeholder: "已知的缺陷、局限或风险" },
  { key: "unfinished_items", label: "未完成事项", placeholder: "明确列出没做的部分，导师按此核对范围" },
];

/**
 * 正式提交表单（§5.2、§6.4）：摘要 + 逐条 AC 自评 + git 信息 + 材料勾选。
 * 完整性检查只提醒不阻止；提交成功后版本不可修改，只能撤回再生成新版本。
 * 父组件用 key={task.code} 挂载，切换任务时草稿自动重置。
 */
export function SubmissionForm({
  project,
  task,
  attachments,
  onCreated,
}: {
  project: ProjectDetail;
  task: Task;
  attachments: Attachment[];
  onCreated: (submission: Submission) => void;
}) {
  const [fields, setFields] = useState({
    summary: "",
    experiment_notes: "",
    known_issues: "",
    unfinished_items: "",
    git_repo: "",
    git_branch: "",
    git_commit: "",
  });
  const [drafts, setDrafts] = useState<Record<string, AcDraft>>(() => initialAcDrafts(task.acs));
  const [selectedCodes, setSelectedCodes] = useState<string[]>([]);

  const submit = useMutation({
    mutationFn: () =>
      createSubmission(project.project_id, task.code, {
        ...fields,
        self_assessment: buildSelfAssessmentPayload(drafts),
        attachment_codes: selectedCodes,
      }),
    onSuccess: (submission) => {
      toast.success(`已提交 ${submission.display_code}`);
      onCreated(submission);
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const selectedAttachments = attachments.filter((item) => selectedCodes.includes(item.code));
  const hints = clientCompletenessHints(task, drafts, selectedAttachments, {
    git_commit: fields.git_commit,
    experiment_notes: fields.experiment_notes,
    unfinished_items: fields.unfinished_items,
  }).filter((hint) => !hint.passed);
  const missingEvidence = acsMissingEvidence(task.acs, drafts);

  function updateDraft(acCode: string, patch: Partial<AcDraft>) {
    setDrafts((current) => ({
      ...current,
      [acCode]: { ...current[acCode], ...patch },
    }));
  }

  return (
    <form
      className="space-y-3 rounded-md border bg-muted/20 p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit.mutate();
      }}
    >
      <div className="text-sm font-medium">正式提交（生成新版本，提交后不可修改）</div>

      {TEXT_FIELDS.map((field) => (
        <div key={field.key} className="space-y-1">
          <Label htmlFor={`submission-${field.key}`}>{field.label}</Label>
          <Textarea
            id={`submission-${field.key}`}
            rows={2}
            value={fields[field.key]}
            placeholder={field.placeholder}
            onChange={(event) => setFields({ ...fields, [field.key]: event.target.value })}
          />
        </div>
      ))}

      <div className="grid gap-2 md:grid-cols-3">
        <div className="space-y-1">
          <Label htmlFor="submission-git-repo">Git 仓库</Label>
          <Input
            id="submission-git-repo"
            value={fields.git_repo}
            placeholder="仓库地址"
            onChange={(event) => setFields({ ...fields, git_repo: event.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="submission-git-branch">分支</Label>
          <Input
            id="submission-git-branch"
            value={fields.git_branch}
            placeholder="例如 feature/baseline"
            onChange={(event) => setFields({ ...fields, git_branch: event.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="submission-git-commit">Commit</Label>
          <Input
            id="submission-git-commit"
            value={fields.git_commit}
            placeholder="commit 哈希"
            onChange={(event) => setFields({ ...fields, git_commit: event.target.value })}
          />
        </div>
      </div>

      <div className="space-y-2 border-t pt-2">
        <div className="text-sm font-medium">逐条验收标准自评</div>
        {task.acs.length === 0 && (
          <AlertPanel
            title="该任务没有验收标准"
            description="没有验收标准的任务无法正式提交，请先请导师补充。"
          />
        )}
        {task.acs.map((ac) => {
          const draft = drafts[ac.ac_code];
          return (
            <div key={ac.ac_code} className="rounded-md border p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-sm">{ac.ac_code}</span>
                {ac.required && <Badge variant="outline">必填</Badge>}
                <span className="text-sm">{ac.statement}</span>
              </div>
              {ac.evidence_requirement && (
                <p className="mt-1 text-xs text-muted-foreground">
                  需要的证据：{ac.evidence_requirement}
                </p>
              )}
              <div className="mt-2 flex flex-wrap gap-1" role="radiogroup" aria-label={`${ac.ac_code} 自评`}>
                {SELF_ASSESSMENT_VALUES.map((value) => (
                  <button
                    key={value}
                    type="button"
                    role="radio"
                    aria-checked={draft.self_assessment === value}
                    onClick={() => updateDraft(ac.ac_code, { self_assessment: value as SelfAssessment })}
                    className={cn(
                      "rounded-md border px-2 py-1 text-xs",
                      draft.self_assessment === value
                        ? "border-primary bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                    )}
                  >
                    {SELF_ASSESSMENT_LABEL[value]}
                  </button>
                ))}
              </div>
              <div className="mt-2 grid gap-2 md:grid-cols-2">
                <Input
                  value={draft.evidence}
                  placeholder="证据（附件编号、commit、图表位置等）"
                  aria-label={`${ac.ac_code} 证据`}
                  onChange={(event) => updateDraft(ac.ac_code, { evidence: event.target.value })}
                />
                <Input
                  value={draft.note}
                  placeholder="补充说明（可留空）"
                  aria-label={`${ac.ac_code} 说明`}
                  onChange={(event) => updateDraft(ac.ac_code, { note: event.target.value })}
                />
              </div>
            </div>
          );
        })}
      </div>

      <div className="space-y-2 border-t pt-2">
        <div className="text-sm font-medium">作为材料的附件</div>
        {attachments.length === 0 && (
          <p className="text-xs text-muted-foreground">
            平台内还没有可用附件，可先到「附件」页签上传代码、日志或结果文件。
          </p>
        )}
        <ul className="space-y-1">
          {attachments.map((attachment) => (
            <li key={attachment.attachment_id} className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={selectedCodes.includes(attachment.code)}
                onCheckedChange={(checked) =>
                  setSelectedCodes((current) =>
                    checked === true
                      ? [...current, attachment.code]
                      : current.filter((code) => code !== attachment.code),
                  )
                }
                id={`attachment-${attachment.attachment_id}`}
              />
              <Label htmlFor={`attachment-${attachment.attachment_id}`} className="font-normal">
                {attachment.code} · {attachment.name} · v{attachment.version}
                {attachment.kind ? ` · ${attachment.kind}` : ""}
              </Label>
            </li>
          ))}
        </ul>
      </div>

      {(hints.length > 0 || missingEvidence.length > 0) && (
        <AlertPanel
          tone="warning"
          title="提交前提醒（只提醒，不影响提交）"
          description={[
            ...hints.map((hint) => hint.message),
            ...(missingEvidence.length > 0
              ? [`以下验收标准声称完成但没写证据：${missingEvidence.join("、")}`]
              : []),
          ].join("\n")}
        />
      )}

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">
          提交会绑定当前验收标准快照；完整性检查不阻止提交，也不替代导师审核。
        </span>
        <Button type="submit" disabled={submit.isPending || task.acs.length === 0}>
          正式提交
        </Button>
      </div>
    </form>
  );
}
