import { useState } from "react";
import { Brain } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { INCOMPLETE_STATUSES } from "@/api/sse-frames";
import type {
  MemoryUsageEntry,
  MemoryUsageManifest,
} from "@/lib/sse/types";

/**
 * 单条回答下方的「本轮实际使用了什么」入口（第四期设计方案 §11.1 / §18.3.3）。
 *
 * 数据是服务端在**模型调用之前**生成的 `memory_usage_manifest`：流式期间经
 * SSE 元数据帧绑定到这条回答，刷新后从消息 metadata 恢复，两边同源。记忆后来
 * 被修改或删除都不会改变这里显示的内容——快照记的是当时真正注入的东西。
 */

// 「这条回答没说完」的状态集合现在来自 SSE 帧契约（@/api/sse-frames），
// 其权威来源是后端 usage_manifest.INCOMPLETE_STATUSES；这里原先是第三份手写副本。

const STAGE_LABELS: Record<string, string> = {
  routing: "意图路由",
  queued: "排队等待",
  context: "上下文准备",
  retrieval: "知识库检索",
  generation: "模型生成",
  streaming: "流式输出",
};

const STATUS_LABELS: Record<string, string> = {
  prepared: "上下文已准备",
  queued: "已排队等待模型",
  started: "模型正在生成",
  partial: "回答未完整",
  cancelled: "回答已停止",
  failed: "回答失败",
  completed: "本轮已完成",
};

/** 这条回答是不是没说完（停止 / 失败 / 中断）。 */
export function isIncompleteUsage(usage?: MemoryUsageManifest): boolean {
  if (!usage) return false;
  const status = usage.generation_status;
  return !!status && INCOMPLETE_STATUSES.includes(status);
}

/**
 * 标题文案必须跟随状态（§11.1）：只有模型真正开始生成过，才能说"本轮使用了"
 * 这些上下文；prepared / queued 阶段只能说"已准备"。
 */
export function usageHeadline(usage: MemoryUsageManifest): string {
  const status = usage.generation_status ?? "prepared";
  if (status === "completed") return "本轮实际使用的记忆与上下文";
  if (!usage.model_started_at) return "上下文已准备，但模型未开始生成";
  return "未完成的回答使用过这些上下文";
}

function summaryLine(usage: MemoryUsageManifest): string {
  if (usage.memory_disabled_for_turn) return "本轮未使用个人记忆";
  const prefs = usage.preferences?.length ?? 0;
  const recalled = usage.recalled_memories?.length ?? 0;
  const recent = usage.recent_message_ids?.length ?? 0;
  return `偏好 ${prefs} 条 · 召回记忆 ${recalled} 条 · 最近原文 ${recent} 轮`;
}

function summaryTypeLabel(type?: string): string {
  return type === "llm" ? "模型摘要" : "规则摘要";
}

function EntryList({
  title,
  entries,
  empty,
}: {
  title: string;
  entries: MemoryUsageEntry[];
  empty: string;
}) {
  return (
    <div>
      <p className="text-sm font-medium">{title}</p>
      {entries.length > 0 ? (
        <ul className="mt-1 space-y-1 rounded-md border p-3 text-sm">
          {entries.map((entry, i) => (
            <li key={entry.id ?? i}>
              <div>{entry.content}</div>
              {entry.reason && (
                <div className="text-xs text-muted-foreground">
                  使用原因：{entry.reason}
                </div>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 rounded-md border p-3 text-sm text-muted-foreground">
          {empty}
        </p>
      )}
    </div>
  );
}

export function MemorySnapshotBadge({ usage }: { usage: MemoryUsageManifest }) {
  const [open, setOpen] = useState(false);
  const status = usage.generation_status ?? "prepared";
  const prefs = usage.preferences ?? [];
  const recalled = usage.recalled_memories ?? [];
  const recent = usage.recent_message_ids ?? [];

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
      >
        <Brain className="h-3.5 w-3.5" />
        {summaryLine(usage)}
      </button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{usageHeadline(usage)}</DialogTitle>
          </DialogHeader>

          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant={status === "completed" ? "secondary" : "outline"}>
                {STATUS_LABELS[status] ?? status}
              </Badge>
              {usage.model_started_at && (
                <span className="text-muted-foreground">
                  模型开始于 {usage.model_started_at}
                </span>
              )}
              {usage.failure_stage && (
                <span className="text-destructive">
                  失败阶段：{STAGE_LABELS[usage.failure_stage] ?? usage.failure_stage}
                </span>
              )}
            </div>
            {usage.failure_reason && (
              <p className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
                {usage.failure_reason}
              </p>
            )}

            <EntryList
              title="个人偏好"
              entries={prefs}
              empty={
                usage.memory_disabled_for_turn
                  ? "本轮你选择了不使用个人记忆（没有删除任何记忆）。"
                  : "本轮没有注入个人偏好。"
              }
            />
            <EntryList
              title="召回的跨会话记忆"
              entries={recalled}
              empty="本轮没有召回跨会话记忆。"
            />

            <div>
              <p className="text-sm font-medium">本会话上下文</p>
              <ul className="mt-1 space-y-1 rounded-md border p-3 text-sm">
                <li>版本：v{usage.session_context_version ?? 0}</li>
                <li>
                  摘要：{summaryTypeLabel(usage.summary_type)}
                  {usage.summary_through_message_id
                    ? `，已覆盖到消息 ${usage.summary_through_message_id}`
                    : "，尚未覆盖任何消息"}
                </li>
                <li>
                  最近原文：{recent.length} 轮
                  {usage.history_boundary_message_id
                    ? `，从消息 ${usage.history_boundary_message_id} 开始`
                    : ""}
                </li>
              </ul>
            </div>

            {usage.degraded && (
              <p className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm">
                本轮历史有降级：较早的内容没能完整进入摘要或最近原文，回答可能缺少
                部分上下文。
              </p>
            )}

            <p className="text-xs text-muted-foreground">
              这是本轮生成时保存的快照，之后修改或删除记忆都不会改变它；上下文长度按
              字符估算（不是精确 token 数）。
            </p>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
