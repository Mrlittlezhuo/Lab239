import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  confirmLatexProgressDraft,
  discardLatexProgressDraft,
} from "@/api/supervision";
import type {
  LatexDraftConfirmBody,
  LatexProgressDraft,
  ProjectDetail,
  Task,
} from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  DRAFT_STATUS_LABEL,
  canConfirmDraft,
  canDiscardDraft,
  countsLine,
  itemLine,
  labelOf,
} from "@/components/supervision/latex/latexModel";
import { Badge } from "@/components/ui/badge";
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
import { useAuth } from "@/stores/auth";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "未知";
}

const EMPTY_BODY: LatexDraftConfirmBody = {
  task_ref: "",
  milestone_ref: "",
  done: "",
  results: "",
  materials: "",
  problems: "",
  next_steps: "",
};

/** 模型摘要的四种状态各自给出不同说明（未接入不是失败，也不影响草稿本身）。 */
function modelSummaryNote(draft: LatexProgressDraft): { tone: "info" | "warning"; text: string } | null {
  switch (draft.model_summary_status) {
    case "generated":
      return {
        tone: "info",
        text: `模型摘要由 ${draft.model_name || "默认模型"} 生成，只是对静态差异的复述，不是新增结论。`,
      };
    case "unavailable":
      return {
        tone: "warning",
        text:
          "模型能力尚未接入，草稿里没有模型摘要。" +
          `${draft.model_summary_note ? ` 后端说明：${draft.model_summary_note}。` : ""}` +
          "静态差异条目本身完整，仍可照常确认。",
      };
    case "failed":
      return {
        tone: "warning",
        text:
          `模型摘要生成失败：${draft.model_summary_note || "未知原因"}。` +
          "差异条目本身不受影响；需要摘要可以重新生成一份草稿。",
      };
    default:
      return null;
  }
}

/**
 * 一份 LaTeX 进展草稿：静态差异条目 + 学生确认入口。
 *
 * 三条硬口径（后端同样这样裁决，这里只决定按钮与提示）：
 * 1. 草稿只追加不覆盖，丢弃也只改状态，快照与旧草稿都留着；
 * 2. 只有创建者本人且项目角色是学生才能确认，确认后生成一条正式进展；
 * 3. 差异来自源码结构比较，不能据此判定实验已执行——caveat 原样显示。
 */
export function LatexDraftCard({
  project,
  draft,
  tasks,
  selected,
  onSelect,
}: {
  project: ProjectDetail;
  draft: LatexProgressDraft;
  tasks: Task[];
  selected: boolean;
  onSelect: () => void;
}) {
  const queryClient = useQueryClient();
  const userId = useAuth((state) => state.user?.id ?? "");
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState<LatexDraftConfirmBody>(EMPTY_BODY);
  const [discarding, setDiscarding] = useState(false);
  const [reason, setReason] = useState("");

  const confirmable = canConfirmDraft(draft, project, userId);
  const discardable = canDiscardDraft(draft, project, userId);
  const modelNote = modelSummaryNote(draft);

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "latex"] });
  }

  const confirm = useMutation({
    mutationFn: () => confirmLatexProgressDraft(project.project_id, draft.code, body),
    onSuccess: (result) => {
      setEditing(false);
      setBody(EMPTY_BODY);
      toast.success(`已生成正式进展 ${result.progress.code}，草稿保留为历史`);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const discard = useMutation({
    mutationFn: () => discardLatexProgressDraft(project.project_id, draft.code, reason.trim()),
    onSuccess: () => {
      setDiscarding(false);
      setReason("");
      toast.success("草稿已丢弃；快照与差异条目都保留，可重新生成新草稿");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  return (
    <div
      className={
        selected
          ? "space-y-3 rounded-md border border-primary p-3"
          : "space-y-3 rounded-md border border-dashed p-3"
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={onSelect} className="font-medium hover:underline">
          {draft.code}
        </button>
        <Badge variant={draft.status === "confirmed" ? "secondary" : "outline"}>
          {labelOf(DRAFT_STATUS_LABEL, draft.status)}
        </Badge>
        {draft.baseline_only && <Badge variant="outline">首次上传基线</Badge>}
        <span className="text-xs text-muted-foreground">
          {draft.attachment_code} v{draft.attachment_version}
          {draft.base_attachment_code ? ` ← 对比 ${draft.base_attachment_code} v${draft.base_attachment_version}` : "（无对比版本）"}
          · 生成于 {when(draft.create_time)} · 生成人 {draft.created_by}（{draft.created_role}）
        </span>
      </div>

      <div className="text-sm">{draft.summary || "（没有摘要）"}</div>
      <div className="text-xs text-muted-foreground">
        {countsLine(draft.counts) || "没有结构变化"} · 字数 {draft.word_count_old} →{" "}
        {draft.word_count_new}（{draft.word_count_delta >= 0 ? "+" : ""}
        {draft.word_count_delta}）· 条目 {draft.item_count}
      </div>

      <AlertPanel tone="warning" title="这条说明必须一起看" description={draft.caveat} />

      {draft.truncated && (
        <AlertPanel
          tone="warning"
          title="差异条目被截断"
          description={`还有 ${draft.dropped_items} 条没列出来（超出单次展示上限）。条目本身仍完整保存在草稿里，确认时会一并附加。`}
        />
      )}
      {modelNote && <AlertPanel tone={modelNote.tone} title="模型摘要" description={modelNote.text} />}

      {draft.items.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-sm">
          {draft.items.map((item, index) => (
            <li key={`${item.item_type}-${item.change}-${index}`}>{itemLine(item)}</li>
          ))}
        </ul>
      )}

      {draft.status === "confirmed" && (
        <AlertPanel
          title={`已确认，生成进展 ${draft.confirmed_progress_code}`}
          description={`确认人 ${draft.confirmed_by}（${draft.confirmed_role}）· ${when(draft.confirmed_at)}。草稿本身保留为历史，不会被改写。`}
        />
      )}
      {draft.status === "discarded" && (
        <AlertPanel
          title="草稿已丢弃"
          description={`丢弃人 ${draft.discarded_by}（${draft.discarded_role}）· ${when(draft.discarded_at)}${
            draft.discard_reason ? ` · 原因：${draft.discard_reason}` : ""
          }`}
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        {confirmable.allowed && (
          <Button size="sm" onClick={() => setEditing((value) => !value)} disabled={confirm.isPending}>
            {editing ? "收起" : "确认为正式进展"}
          </Button>
        )}
        {discardable.allowed && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setDiscarding((value) => !value);
              setReason("");
            }}
          >
            丢弃草稿
          </Button>
        )}
        {!confirmable.allowed && draft.status !== "confirmed" && draft.status !== "discarded" && (
          <span className="text-xs text-muted-foreground">{confirmable.reason}</span>
        )}
      </div>

      {editing && (
        <div className="space-y-2 rounded-md border border-dashed p-2">
          <p className="text-xs text-muted-foreground">
            草稿里的差异条目会自动附加到进展上，不需要重抄；下面写的是你自己的说法，
            确认后会生成一条正式进展（PR-NNN），导师在进展与审核里都能看到。
          </p>
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground">关联任务（可选）</Label>
            <Select
              value={body.task_ref === "" ? "none" : body.task_ref}
              onValueChange={(value) =>
                setBody((current) => ({ ...current, task_ref: value === "none" ? "" : value }))
              }
            >
              <SelectTrigger className="h-9 w-[280px]">
                <SelectValue placeholder="不关联具体任务" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">不关联具体任务</SelectItem>
                {tasks.map((task) => (
                  <SelectItem key={task.task_id} value={task.code}>
                    {task.code} {task.title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {(
            [
              ["done", "已完成"],
              ["results", "得到的结果"],
              ["materials", "用到的材料"],
              ["problems", "遇到的问题"],
              ["next_steps", "下一步"],
            ] as const
          ).map(([key, label]) => (
            <div key={key} className="space-y-1">
              <Label htmlFor={`latex-draft-${draft.code}-${key}`} className="text-xs text-muted-foreground">
                {label}
              </Label>
              <Textarea
                id={`latex-draft-${draft.code}-${key}`}
                rows={2}
                value={body[key] ?? ""}
                onChange={(event) =>
                  setBody((current) => ({ ...current, [key]: event.target.value }))
                }
              />
            </div>
          ))}
          <div className="flex gap-2">
            <Button size="sm" onClick={() => confirm.mutate()} disabled={confirm.isPending}>
              {confirm.isPending ? "确认中…" : "确认并生成进展"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
              取消
            </Button>
          </div>
        </div>
      )}

      {discarding && (
        <div className="space-y-2 rounded-md border border-dashed p-2">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="丢弃原因（可留空，会记进草稿）"
          />
          <div className="flex gap-2">
            <Button size="sm" onClick={() => discard.mutate()} disabled={discard.isPending}>
              {discard.isPending ? "丢弃中…" : "确认丢弃"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setDiscarding(false)}>
              算了
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
