import type { Attachment, SelfAssessment, Task } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import { SELF_ASSESSMENT_LABEL } from "@/components/supervision/review/reviewDraft";
import {
  draftHasContent,
  type SelfCheckDraft,
} from "@/components/supervision/review/aiReview";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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

const SELF_VALUES: SelfAssessment[] = [
  "done",
  "partially_done",
  "not_done",
  "not_applicable",
];

/**
 * 学生自检用的草稿表单。
 *
 * 里程碑1 的库里没有「草稿提交」实体：草稿由页面在点按钮这一刻随请求带去，
 * 后端据此生成不可变检查快照；运行期间继续编辑不会改动那份快照（§7.1）。
 * 同一份草稿也送去时效比对接口，用来判断旧结果是否「基于旧快照」（§11.2）。
 */
export function SelfCheckDraftForm({
  task,
  attachments,
  draft,
  onChange,
  onRun,
  running,
}: {
  task: Task;
  attachments: Attachment[];
  draft: SelfCheckDraft;
  onChange: (next: SelfCheckDraft) => void;
  onRun: () => void;
  running: boolean;
}) {
  const acs = task.acs ?? [];
  if (acs.length === 0) {
    return (
      <AlertPanel
        tone="warning"
        title="该任务没有结构化验收标准"
        description="没有验收标准时 AI 无法给出有效结论，后端会拒绝创建预审；请先请导师补齐标准。"
      />
    );
  }

  function patch(value: Partial<SelfCheckDraft>) {
    onChange({ ...draft, ...value });
  }

  function toggleCode(list: string[], code: string, on: boolean): string[] {
    const next = list.filter((item) => item !== code);
    return on ? [...next, code] : next;
  }

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="text-sm font-medium">当前草稿内容（自检按这份内容生成快照）</div>

      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">提交说明</Label>
        <Textarea
          value={draft.summary}
          onChange={(event) => patch({ summary: event.target.value })}
          placeholder="这一版做了什么"
        />
      </div>

      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">逐条自评与证据</Label>
        {acs.map((ac) => {
          const row = draft.selfAssessment[ac.ac_code] ?? {
            ac_code: ac.ac_code,
            self_assessment: "" as const,
            evidence: "",
            note: "",
          };
          return (
            <div key={ac.ac_code} className="space-y-1 rounded-md border border-dashed p-2">
              <div className="text-sm">
                {ac.ac_code} · {ac.statement}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={row.self_assessment === "" ? "unset" : row.self_assessment}
                  onValueChange={(value) =>
                    patch({
                      selfAssessment: {
                        ...draft.selfAssessment,
                        [ac.ac_code]: {
                          ...row,
                          self_assessment:
                            value === "unset" ? "" : (value as SelfAssessment),
                        },
                      },
                    })
                  }
                >
                  <SelectTrigger className="h-9 w-[200px]">
                    <SelectValue placeholder="尚未自评" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="unset">尚未自评</SelectItem>
                    {SELF_VALUES.map((value) => (
                      <SelectItem key={value} value={value}>
                        {SELF_ASSESSMENT_LABEL[value]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-xs text-muted-foreground">
                  {ac.required ? "必选" : "可选"}
                  {ac.evidence_requirement ? ` · 需要：${ac.evidence_requirement}` : ""}
                </span>
              </div>
              <Input
                value={row.evidence}
                onChange={(event) =>
                  patch({
                    selfAssessment: {
                      ...draft.selfAssessment,
                      [ac.ac_code]: { ...row, evidence: event.target.value },
                    },
                  })
                }
                placeholder="证据位置：文件名与行号、章节、结果字段等"
              />
              <Input
                value={row.note}
                onChange={(event) =>
                  patch({
                    selfAssessment: {
                      ...draft.selfAssessment,
                      [ac.ac_code]: { ...row, note: event.target.value },
                    },
                  })
                }
                placeholder="补充说明（可选）"
              />
            </div>
          );
        })}
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        <Input
          value={draft.git_repo}
          onChange={(event) => patch({ git_repo: event.target.value })}
          placeholder="Git 仓库"
        />
        <Input
          value={draft.git_branch}
          onChange={(event) => patch({ git_branch: event.target.value })}
          placeholder="分支"
        />
        <Input
          value={draft.git_commit}
          onChange={(event) => patch({ git_commit: event.target.value })}
          placeholder="commit"
        />
      </div>

      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">实验配置、命令与结果</Label>
        <Textarea
          value={draft.experiment_notes}
          onChange={(event) => patch({ experiment_notes: event.target.value })}
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">已知问题</Label>
        <Textarea
          value={draft.known_issues}
          onChange={(event) => patch({ known_issues: event.target.value })}
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-muted-foreground">未完成项</Label>
        <Textarea
          value={draft.unfinished_items}
          onChange={(event) => patch({ unfinished_items: event.target.value })}
        />
      </div>

      <div className="space-y-2">
        <Label className="text-xs text-muted-foreground">检查范围（勾选要纳入的材料）</Label>
        {attachments.length === 0 && (
          <p className="text-sm text-muted-foreground">项目里还没有可用附件。</p>
        )}
        <ul className="space-y-1">
          {attachments.map((item) => {
            const checked = draft.attachmentCodes.includes(item.code);
            return (
              <li key={item.attachment_id} className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={checked}
                    onCheckedChange={(value) =>
                      patch({
                        attachmentCodes: toggleCode(draft.attachmentCodes, item.code, value === true),
                        coreAttachmentCodes:
                          value === true
                            ? draft.coreAttachmentCodes
                            : toggleCode(draft.coreAttachmentCodes, item.code, false),
                      })
                    }
                  />
                  <span className="break-words">
                    {item.name} · {item.code} · v{item.version}
                  </span>
                </label>
                {checked && (
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <Checkbox
                      checked={draft.coreAttachmentCodes.includes(item.code)}
                      onCheckedChange={(value) =>
                        patch({
                          coreAttachmentCodes: toggleCode(
                            draft.coreAttachmentCodes,
                            item.code,
                            value === true,
                          ),
                        })
                      }
                    />
                    核心材料
                  </label>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={onRun} disabled={running}>
          {running ? "排队中…" : "AI 检查当前完成情况"}
        </Button>
        {!draftHasContent(draft) && (
          <span className="text-xs text-muted-foreground">
            草稿还是空的：没有说明和材料时，AI 只能给出「无法验证」。
          </span>
        )}
        <span className="text-xs text-muted-foreground">
          点击这一刻才生成检查快照；自检失败不影响保存草稿或正式提交。
        </span>
      </div>
    </div>
  );
}
