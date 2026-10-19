import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  attachSelfCheck,
  cancelAiReview,
  checkAiReviewStaleness,
  executeAiReview,
  retryAiReview,
} from "@/api/supervision";
import type {
  AiDraftBody,
  AiReviewRun,
  AiStaleness,
  ProjectDetail,
  Submission,
} from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  AI_STATUS_LABEL,
  MODEL_UNAVAILABLE_TEXT,
  TRIGGER_SOURCE_LABEL,
  describeUnknown,
  isModelUnavailable,
  queueNote,
  runActions,
} from "@/components/supervision/review/aiReview";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "未知";
}

/**
 * 把材料清单/安全提示折成可读文本。
 *
 * 参数刻意是 `unknown[]`：`unread_materials` 与 `partially_read_materials`
 * 是对象数组，而 `security_flags` 后端是 `List[str]`（纯文本提示），
 * `describeUnknown` 两种都能处理。
 */
function materialNames(items: unknown[]): string[] {
  return items.map((item) => describeUnknown(item)).filter((text) => text !== "");
}

/**
 * 一次 AI 预审运行：状态、时效、逐条摘要与动作。
 *
 * 创建只排队，执行是独立调用；execute 遇到 503 说明模型能力尚未接入，
 * 预审仍然成立（排队保留、导师照常审核），所以显示成说明而不是失败（§14.2）。
 */
export function AiRunCard({
  project,
  run,
  selected,
  onSelect,
  draftBody,
  attachTargets,
}: {
  project: ProjectDetail;
  run: AiReviewRun;
  selected: boolean;
  onSelect: () => void;
  /** 学生侧传当前草稿用于指纹比对；导师侧不传（提交本身不可变）。 */
  draftBody?: AiDraftBody;
  /** 学生可附到的自己的提交列表。 */
  attachTargets?: Submission[];
}) {
  const queryClient = useQueryClient();
  const [cancelling, setCancelling] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [attachRef, setAttachRef] = useState("");
  const [notice, setNotice] = useState("");
  const [staleness, setStaleness] = useState<AiStaleness | null>(null);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["supervision", "ai-review"] });

  const done = (saved: AiReviewRun, message: string) => {
    toast.success(message);
    setStaleness(null);
    invalidate();
    return saved;
  };

  const execute = useMutation({
    mutationFn: () => executeAiReview(project.project_id, run.code),
    onSuccess: () => done(run, "预审已执行完成"),
    onError: (error) => {
      if (isModelUnavailable(error)) {
        // 不是失败：队列保留，导师审核不受影响。
        setNotice(`${MODEL_UNAVAILABLE_TEXT}\n后端说明：${errorText(error)}`);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const cancel = useMutation({
    mutationFn: () => cancelAiReview(project.project_id, run.code, cancelReason),
    onSuccess: () => {
      setCancelling(false);
      setCancelReason("");
      done(run, "已取消排队中的预审；正式提交与人工审核不受影响");
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const retry = useMutation({
    mutationFn: () => retryAiReview(project.project_id, run.code),
    onSuccess: (saved) => done(saved, `已生成新版本 ${saved.display_code}，旧结果保留为历史`),
    onError: (error) => toast.error(errorText(error)),
  });

  const attach = useMutation({
    mutationFn: () => attachSelfCheck(project.project_id, run.code, attachRef),
    onSuccess: () => done(run, "自检快照已附到该提交，导师现在能看到它"),
    onError: (error) => toast.error(errorText(error)),
  });

  const stalenessCheck = useMutation({
    mutationFn: () => checkAiReviewStaleness(project.project_id, run.code, draftBody),
    onSuccess: (result) => setStaleness(result),
    onError: (error) => toast.error(errorText(error)),
  });

  const actions = runActions(run, staleness);
  const queue = queueNote(run);

  return (
    <div
      className={cn(
        "space-y-3 rounded-md border p-3",
        selected ? "border-primary" : "border-dashed",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={onSelect} className="font-medium hover:underline">
          {run.display_code}
        </button>
        <Badge variant={run.status === "completed" ? "secondary" : "outline"}>
          {AI_STATUS_LABEL[run.status] ?? run.status}
        </Badge>
        {run.stale && <Badge variant="outline">{run.stale_label || "已过时"}</Badge>}
        <span className="text-xs text-muted-foreground">
          {TRIGGER_SOURCE_LABEL[run.trigger_source] ?? run.trigger_source} · 触发人{" "}
          {run.triggered_by}（{run.triggered_role}）· 排队于 {when(run.queued_at)}
        </span>
        {run.submission_display && (
          <span className="text-xs text-muted-foreground">
            绑定 {run.submission_display}
          </span>
        )}
      </div>

      {run.summary_state && (
        <div className="text-sm">
          整体摘要：{run.summary_state}
          {run.summary_note ? ` · ${run.summary_note}` : ""}
        </div>
      )}
      <div className="text-xs text-muted-foreground">
        逐条：{JSON.stringify(run.finding_counts ?? {})} · 批次{" "}
        {run.completed_batches}/{run.total_batches}
        {run.unchecked_acs.length > 0 ? ` · 未检查：${run.unchecked_acs.join("、")}` : ""}
        {run.partial_results ? " · 结果不完整" : ""}
      </div>

      {queue && <div className="text-xs text-muted-foreground">{queue}</div>}

      {run.failure_reason && (
        <AlertPanel
          tone="error"
          title={`预审失败：${run.failure_reason}`}
          description="提交版本与已上传材料都保留，仍可继续人工审核；需要时点「重新运行」生成新版本。"
        />
      )}
      {run.status === "cancelled" && (
        <AlertPanel
          title="已取消"
          description={`取消人 ${run.cancelled_by || "未知"} · ${when(run.cancelled_at)}${
            run.cancel_reason ? ` · 原因：${run.cancel_reason}` : ""
          }；正式提交与人工审核不受影响。`}
        />
      )}

      {materialNames(run.unread_materials).length > 0 && (
        <AlertPanel
          tone="warning"
          title="有材料未读取"
          description={`未纳入本次检查：${materialNames(run.unread_materials).join("、")}。受影响的标准不会判为证据充分。`}
        />
      )}
      {materialNames(run.partially_read_materials).length > 0 && (
        <AlertPanel
          tone="warning"
          title="有材料只读取了部分"
          description={`部分读取：${materialNames(run.partially_read_materials).join("、")}。`}
        />
      )}
      {materialNames(run.security_flags).length > 0 && (
        <AlertPanel
          tone="warning"
          title="材料安全提示"
          description={materialNames(run.security_flags).join("\n")}
        />
      )}

      {notice && (
        <AlertPanel tone="warning" title="预审已排队，尚未执行" description={notice} />
      )}

      {staleness && (
        <AlertPanel
          tone={staleness.stale ? "warning" : "info"}
          title={staleness.label}
          description={
            staleness.stale
              ? `${staleness.reasons.join("\n")}\n基于：${staleness.based_on}`
              : `基于：${staleness.based_on}`
          }
        />
      )}

      <div className="flex flex-wrap items-center gap-2">
        {actions.execute && (
          <Button size="sm" onClick={() => execute.mutate()} disabled={execute.isPending}>
            {execute.isPending ? "执行中…" : "执行"}
          </Button>
        )}
        {actions.retry && (
          <Button size="sm" variant="outline" onClick={() => retry.mutate()} disabled={retry.isPending}>
            {retry.isPending ? "排队中…" : "重新运行"}
          </Button>
        )}
        {actions.cancel && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setCancelling((value) => !value);
              setCancelReason("");
            }}
          >
            取消排队
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          onClick={() => stalenessCheck.mutate()}
          disabled={stalenessCheck.isPending}
        >
          {stalenessCheck.isPending ? "比对中…" : "检查是否过时"}
        </Button>
      </div>

      {cancelling && (
        <div className="space-y-2 rounded-md border p-2">
          <Input
            value={cancelReason}
            onChange={(event) => setCancelReason(event.target.value)}
            placeholder="取消原因（必填）"
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending || cancelReason.trim() === ""}
            >
              确认取消
            </Button>
            <Button size="sm" variant="outline" onClick={() => setCancelling(false)}>
              算了
            </Button>
          </div>
        </div>
      )}

      {run.trigger_source === "student_manual" && (
        <div className="space-y-2 rounded-md border border-dashed p-2">
          <div className="text-xs text-muted-foreground">
            自检结果默认只有学生可见；附到提交后导师才能看到这份快照。
            {run.attached_to_submission ? " 已附带。" : ""}
          </div>
          {attachTargets && attachTargets.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={attachRef === "" ? "none" : attachRef}
                onValueChange={(value) => setAttachRef(value === "none" ? "" : value)}
              >
                <SelectTrigger className="h-9 w-[240px]">
                  <SelectValue placeholder="选择要附到的提交" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">不附带</SelectItem>
                  {attachTargets.map((item) => (
                    <SelectItem key={item.submission_id} value={item.display_code}>
                      {item.display_code}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                size="sm"
                onClick={() => attach.mutate()}
                disabled={!actions.attach || attach.isPending || attachRef === ""}
              >
                附到该提交
              </Button>
              {!actions.attach && (
                <span className="text-xs text-muted-foreground">
                  自检未完成、已附带或已过时（{run.stale_label || "基于旧快照"}）时不能附带。
                </span>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
