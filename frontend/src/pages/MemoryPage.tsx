import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  clearAllMemories,
  deleteMemory,
  getAutoMemory,
  listMemories,
  setAutoMemory,
  updateMemory,
  type Memory,
} from "@/api/memory";
import {
  MEMORY_SAVE_NOTICE,
  MEMORY_SENSITIVE_NOTICE,
  expiresLabel,
  groupMemories,
  kindLabel,
  statusLabel,
} from "@/components/memory/categories";
import { MemoryEditorDialog } from "@/components/memory/MemoryEditorDialog";
import { MemoryRevisionsDialog } from "@/components/memory/MemoryRevisionsDialog";
import { RecallSwitchesCard } from "@/components/memory/RecallSwitchesCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";

type ConfirmState =
  | { kind: "one"; memory: Memory }
  | { kind: "all" }
  | null;

export function MemoryPage() {
  const queryClient = useQueryClient();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [revisions, setRevisions] = useState<Memory | null>(null);

  const { data: memories = [] } = useQuery({
    queryKey: ["memories"],
    queryFn: listMemories,
  });
  const { data: auto } = useQuery({
    queryKey: ["memory-auto"],
    queryFn: getAutoMemory,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-auto"] });
    queryClient.invalidateQueries({ queryKey: ["memory-preferences"] });
  };

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateMemory(id, { enabled }),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteMemory(id),
    onSuccess: () => {
      toast.success("记忆已删除");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const clearMutation = useMutation({
    mutationFn: clearAllMemories,
    onSuccess: (res) => {
      toast.success(`已清空 ${res.deleted} 条记忆`);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "清空失败"),
  });

  const autoMutation = useMutation({
    mutationFn: setAutoMemory,
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "开关更新失败"),
  });

  function openCreate() {
    setEditing(null);
    setEditorOpen(true);
  }

  function openEdit(memory: Memory) {
    setEditing(memory);
    setEditorOpen(true);
  }

  const groups = groupMemories(memories);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">我的记忆</h1>
        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2 text-sm">
            自动提取
            <Switch
              checked={auto?.auto_memory ?? false}
              onCheckedChange={(v) => autoMutation.mutate(v === true)}
            />
          </label>
          <Button size="sm" onClick={openCreate}>
            <Plus className="h-4 w-4" />
            新增一条记忆
          </Button>
        </div>
      </div>

      <RecallSwitchesCard />

      <p className="text-xs text-muted-foreground">{MEMORY_SAVE_NOTICE}</p>
      <p className="text-xs text-destructive">{MEMORY_SENSITIVE_NOTICE}</p>

      <div className="space-y-5">
        {groups.map((group) => (
          <section key={group.key} className="space-y-2">
            <div>
              <h2 className="text-sm font-medium">{group.label}</h2>
              {group.hint && (
                <p className="text-xs text-muted-foreground">{group.hint}</p>
              )}
            </div>
            {group.items.map((m) => (
              <div key={m.id} className="rounded-lg border p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {!m.category && <Badge variant="secondary">{m.type}</Badge>}
                  {/* 第三期：类别 / 范围 / 状态 / 有效期，让每条记忆可解释 */}
                  <Badge variant="outline">{kindLabel(m.kind)}</Badge>
                  {m.scope === "project" && (
                    <Badge variant="outline">
                      项目内{m.project_name ? `：${m.project_name}` : ""}
                    </Badge>
                  )}
                  <Badge
                    variant={
                      m.status === "superseded" ||
                      m.status === "expired" ||
                      m.status === "disabled"
                        ? "destructive"
                        : "secondary"
                    }
                  >
                    {statusLabel(m.status)}
                  </Badge>
                  <span>{expiresLabel(m.expires_at)}</span>
                  <span>重要性 {m.importance}</span>
                  {m.source_label && <span>· 来源 {m.source_label}</span>}
                  <div className="ml-auto flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => openEdit(m)}
                    >
                      编辑
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setRevisions(m)}
                    >
                      历史
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        toggleMutation.mutate({ id: m.id, enabled: !m.enabled })
                      }
                    >
                      {m.enabled ? "停用" : "启用"}
                    </Button>
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-destructive"
                      onClick={() => setConfirm({ kind: "one", memory: m })}
                      title="删除"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <p
                  className={`text-sm ${
                    m.enabled ? "" : "text-muted-foreground line-through"
                  }`}
                >
                  {m.content}
                </p>
              </div>
            ))}
          </section>
        ))}

        {memories.length === 0 && (
          <div className="py-10 text-center text-muted-foreground">
            还没有记忆。新增后你的新智能聊天和知识库对话会参考这些内容。
          </div>
        )}
      </div>

      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" onClick={openCreate}>
          <Plus className="h-4 w-4" />
          新增一条记忆
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => setConfirm({ kind: "all" })}
          disabled={memories.length === 0}
        >
          清空全部个人记忆
        </Button>
      </div>

      <MemoryEditorDialog
        open={editorOpen}
        onOpenChange={setEditorOpen}
        memory={editing}
        onSaved={invalidate}
      />

      {/* 编辑与被替代前的内容都留了快照，因此历史始终可查（设计方案 §4.5） */}
      <MemoryRevisionsDialog
        open={revisions !== null}
        onOpenChange={(open) => !open && setRevisions(null)}
        memoryId={revisions?.id}
        memoryContent={revisions?.content}
      />

      {/* 删除/清空都需要二次确认（设计方案 §4.5：取消时一切保持不变） */}
      <Dialog
        open={confirm !== null}
        onOpenChange={(open) => !open && setConfirm(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirm?.kind === "all" ? "清空全部个人记忆？" : "删除这条记忆？"}
            </DialogTitle>
          </DialogHeader>
          {confirm?.kind === "all" ? (
            <p className="text-sm">
              将永久删除你的全部 <b>{memories.length}</b> 条记忆，包括自动提取的长期信息。
              删除后新会话不再使用任何个人偏好，不可恢复。
            </p>
          ) : (
            <p className="text-sm">
              将从此不再使用：<b>{confirm?.memory.content}</b>，不可恢复。
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setConfirm(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (confirm?.kind === "all") clearMutation.mutate();
                else if (confirm?.kind === "one") deleteMutation.mutate(confirm.memory.id);
                setConfirm(null);
              }}
            >
              确认删除
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
