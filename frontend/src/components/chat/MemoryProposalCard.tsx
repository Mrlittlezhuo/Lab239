import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  applyIntent,
  type MemoryCategory,
  type MemoryProposal,
  type MemoryScope,
} from "@/api/memory";
import { MEMORY_CATEGORIES, categoryLabel } from "@/components/memory/categories";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/**
 * “记住 / 忘记”确认卡片（第二期设计方案 §4.3）。
 *
 * 服务端只做了检测，没有写入：用户点「确认保存」才落库，点「不保存」什么都不
 * 发生（P2-09）。范围默认值来自服务端按措辞的推荐；含义不明确时不预选，
 * 必须用户自己选（P2-07）。“忘记”会列出将被停用的个人记忆，避免误删（P2-09）。
 */
export function MemoryProposalCard({
  proposal,
  conversationId,
  onApplied,
}: {
  proposal: MemoryProposal;
  conversationId?: string;
  onApplied?: () => void;
}) {
  const groupId = useId();
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<MemoryScope | null>(
    proposal.recommended_scope,
  );
  const [category, setCategory] = useState<string>(
    proposal.recommended_category ?? "",
  );
  const [status, setStatus] = useState<"pending" | "saved" | "dismissed">(
    "pending",
  );
  const [savedText, setSavedText] = useState("");

  const needsCategory = proposal.kind === "remember" && scope === "preference";
  const canConfirm =
    status === "pending" &&
    scope !== null &&
    !proposal.conversation_temporary &&
    (!needsCategory || category !== "") &&
    (scope !== "session" || !!conversationId);

  const applyMutation = useMutation({
    mutationFn: () =>
      applyIntent({
        kind: proposal.kind,
        content: proposal.content,
        scope: scope as MemoryScope,
        conversation_id: conversationId ?? null,
        ...(needsCategory ? { category: category as MemoryCategory } : {}),
        // 忘记 + 跨会话：只停用卡片上列出的那几条，避免扩大范围
        ...(proposal.kind === "forget" && scope === "preference"
          ? { target_ids: proposal.targets.map((t) => t.id) }
          : {}),
      }),
    onSuccess: (res) => {
      setStatus("saved");
      setSavedText(res.message);
      queryClient.invalidateQueries({
        queryKey: ["session-context", conversationId],
      });
      queryClient.invalidateQueries({ queryKey: ["memory-preferences"] });
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      onApplied?.();
    },
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : "保存失败"),
  });

  return (
    <div className="mt-3 rounded-md border bg-muted/40 p-3 text-sm">
      <div className="font-medium">
        {proposal.kind === "remember"
          ? "你希望保存以下内容："
          : "你希望忘记以下内容："}
      </div>
      <p className="mt-1 whitespace-pre-wrap">“{proposal.content}”</p>

      {proposal.kind === "forget" && scope === "preference" && (
        <div className="mt-2">
          {proposal.targets.length > 0 ? (
            <>
              <div className="text-xs text-muted-foreground">
                将停用以下个人记忆（不删除聊天历史，可随时重新启用）：
              </div>
              <ul className="mt-1 list-disc space-y-1 rounded-md border p-3 pl-6">
                {proposal.targets.map((t) => (
                  <li key={t.id}>
                    <span className="text-muted-foreground">
                      {categoryLabel(t.category)}：
                    </span>
                    {t.content}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <p className="mt-1 text-xs text-muted-foreground">{proposal.note}</p>
          )}
        </div>
      )}
      {proposal.kind === "forget" && scope === "session" && (
        <p className="mt-1 text-xs text-muted-foreground">
          只从本会话上下文里移除与该内容相关的条目，不影响其他会话与个人记忆。
        </p>
      )}

      <div className="mt-2">
        <div className="text-xs text-muted-foreground">保存范围：</div>
        <div className="mt-1 flex flex-wrap gap-3">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={groupId}
              value="session"
              aria-label="仅本会话"
              checked={scope === "session"}
              onChange={() => setScope("session")}
            />
            仅本会话
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name={groupId}
              value="preference"
              aria-label="作为跨会话个人偏好"
              checked={scope === "preference"}
              onChange={() => setScope("preference")}
            />
            作为跨会话个人偏好
          </label>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          {scope === "session"
            ? "只写入当前会话上下文，不会带入其他聊天窗口。"
            : scope === "preference"
              ? "写入个人记忆，你的新智能聊天与知识库对话都会使用。"
              : proposal.scope_reason}
        </p>
        {scope && proposal.scope_reason && (
          // 服务端按措辞给的推荐依据；用户改选后仍然显示，便于对照
          <p className="text-xs text-muted-foreground">
            推荐依据：{proposal.scope_reason}
          </p>
        )}
      </div>

      {needsCategory && (
        <div className="mt-2">
          <Label>偏好类别</Label>
          <Select value={category} onValueChange={setCategory}>
            <SelectTrigger className="mt-1 w-48">
              <SelectValue placeholder="请选择类别" />
            </SelectTrigger>
            <SelectContent>
              {MEMORY_CATEGORIES.map((c) => (
                <SelectItem key={c.value} value={c.value}>
                  {c.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="mt-1 text-xs text-muted-foreground">
            {MEMORY_CATEGORIES.find((c) => c.value === category)?.hint ??
              "跨会话保存需要选择类别；到“我的记忆”页还能继续修改或停用。"}
          </p>
        </div>
      )}

      {proposal.conversation_temporary && (
        <p className="mt-2 text-xs text-destructive">
          临时对话不保存记忆；请退出临时对话后再确认保存。
        </p>
      )}
      {!proposal.conversation_temporary && scope === "session" && !conversationId && (
        <p className="mt-2 text-xs text-muted-foreground">
          当前还没有会话；发送第一条消息建立会话后再保存到本会话。
        </p>
      )}

      {status === "pending" ? (
        <div className="mt-2 flex gap-2">
          <Button
            size="sm"
            disabled={!canConfirm || applyMutation.isPending}
            onClick={() => applyMutation.mutate()}
          >
            {applyMutation.isPending ? "保存中…" : "确认保存"}
          </Button>
          <Button size="sm" variant="outline" onClick={() => setStatus("dismissed")}>
            不保存
          </Button>
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">
          {status === "saved"
            ? savedText
            : "未保存：没有新增个人偏好，也没有加入本会话已确认事项。"}
        </p>
      )}
    </div>
  );
}
