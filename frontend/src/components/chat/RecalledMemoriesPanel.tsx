import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, History } from "lucide-react";
import { toast } from "sonner";

import {
  deleteMemory,
  getMemory,
  listRecalledMemories,
  type Memory,
  type RecalledMemory,
} from "@/api/memory";
import { ignoreMemory } from "@/api/sessionContext";
import { MemoryEditorDialog } from "@/components/memory/MemoryEditorDialog";
import { expiresLabel, statusLabel } from "@/components/memory/categories";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/**
 * 回答下方的「本次参考了 N 条历史记忆」（第三期设计方案 §4.2、§7）。
 *
 * 列表直接来自服务端 `GET /memories/recall`：召回是确定性的，同样的
 * (用户, 问题, 会话, 项目) 得到同一批，因此这里看到的就是本轮真正注入的
 * 那一组，不会出现"界面显示 3 条、模型只用了 1 条"。
 *
 * 每条都能回答"它来自哪次历史记录、为什么被使用"，并就地
 * 本次不使用 / 编辑 / 删除：
 * - 本次不使用：只让本会话不再参考它，记录保留，其他会话不受影响（P3-04）；
 * - 编辑：改内容并保留修订历史；
 * - 删除：彻底移除（需二次确认）。
 */
export function RecalledMemoriesPanel({
  query,
  conversationId,
  projectId,
}: {
  /** 本轮用户的问题；召回与注入都以它为准 */
  query: string;
  conversationId?: string;
  projectId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<RecalledMemory | null>(null);
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["memory-recall", query, conversationId ?? null, projectId ?? null],
    queryFn: () =>
      listRecalledMemories({
        q: query,
        conversationId,
        projectId,
      }),
    enabled: !!query.trim(),
  });

  const used = data?.memories ?? [];

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["memory-recall"] });

  const ignoreMutation = useMutation({
    mutationFn: (memoryId: string) =>
      ignoreMemory(conversationId as string, memoryId),
    onSuccess: () => {
      invalidate();
      toast.success("本次不再参考这条记忆；记录仍保留，其他会话不受影响");
    },
    onError: (err) => toast.error(messageOf(err, "操作失败")),
  });

  const deleteMutation = useMutation({
    mutationFn: (memoryId: string) => deleteMemory(memoryId),
    onSuccess: () => {
      setConfirmDelete(null);
      invalidate();
      toast.success("该记忆已删除，其他会话也不会再参考");
    },
    onError: (err) => toast.error(messageOf(err, "删除失败")),
  });

  const editMutation = useMutation({
    mutationFn: async (memoryId: string) => getMemory(memoryId),
    onSuccess: (memory) => setEditing(memory),
    onError: (err) => toast.error(messageOf(err, "读取记忆失败")),
  });

  if (!query.trim() || used.length === 0) return null;

  return (
    <div className="mt-2 text-sm">
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        onClick={() => setOpen((v) => !v)}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5" />
        )}
        <History className="h-3.5 w-3.5" />
        本次参考了 {used.length} 条历史记忆
      </button>

      {open && (
        <ul className="mt-2 space-y-2 rounded-lg border p-3">
          {used.map((m) => (
            <li key={m.id} className="space-y-1">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <Badge variant="secondary">{m.kind_label}</Badge>
                <Badge variant="outline">{statusLabel(m.status)}</Badge>
                {m.scope === "project" && (
                  <Badge variant="outline">
                    项目内{m.project_name ? `：${m.project_name}` : ""}
                  </Badge>
                )}
                <span>{m.source_label || "来源未记录"}</span>
                <span>{expiresLabel(m.expires_at)}</span>
              </div>
              <p className="whitespace-pre-wrap">{m.content}</p>
              <p className="text-xs text-muted-foreground">
                为什么用它：{m.reason || "与当前问题的主题一致"}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!conversationId || ignoreMutation.isPending}
                  title={
                    conversationId
                      ? "只让本会话不再参考它，记录保留"
                      : "会话建立后才能设置"
                  }
                  onClick={() => ignoreMutation.mutate(m.id)}
                >
                  本次不使用
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={editMutation.isPending}
                  onClick={() => editMutation.mutate(m.id)}
                >
                  编辑
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  className="text-destructive"
                  onClick={() => setConfirmDelete(m)}
                >
                  删除
                </Button>
              </div>
              {confirmDelete?.id === m.id && (
                <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs">
                  <p>
                    删除后所有会话都不再参考这条记忆，且不可恢复。只想本次不用
                    请选「本次不使用」。
                  </p>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={deleteMutation.isPending}
                      onClick={() => deleteMutation.mutate(m.id)}
                    >
                      确认删除
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setConfirmDelete(null)}
                    >
                      取消
                    </Button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {open && (
        <p className="mt-2 text-xs text-muted-foreground">
          每次回答最多参考 {data?.max_count ?? 3} 条；历史记忆只作背景参考，
          本轮你的新要求优先，知识库问答的事实仍以检索到的文档为依据。
        </p>
      )}

      <MemoryEditorDialog
        open={editing !== null}
        onOpenChange={(next) => {
          if (!next) setEditing(null);
        }}
        memory={editing}
        onSaved={invalidate}
      />
    </div>
  );
}
